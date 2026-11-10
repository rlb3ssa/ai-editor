"""Objetos (Fase 7): segmentar, rastrear, recortar e remover objetos do vídeo com IA local, sob demanda.

Tudo roda na máquina do usuário, em CPU:
  - motor: ONNX Runtime (MIT), dependência OPCIONAL (extra `ia`: `uv sync --extra ia` ou
    `pip install onnxruntime`). Sem ele, o resto do editor funciona e estas funções dão erro com a dica.
  - segmentação: SAM 2.1 (Meta, Apache-2.0) exportado para ONNX pelo `onnx-community` no Hugging Face (só o
    modelo de IMAGEM: codificador + decodificador de prompts). A propagação por memória do SAM 2 (memory
    attention/encoder) não está nesse export; o rastreamento é feito quadro a quadro: o prompt de cada quadro sai da
    máscara do quadro vizinho (caixa ampliada + ponto interno; o export não aceita a máscara anterior como
    entrada), escolhendo entre as 3 saídas a mais coerente com a anterior. Ver `segmentar`.
  - remoção: LaMa (Apache-2.0) em ONNX (`opencv/inpainting_lama`, entrada fixa 512x512), aplicado num recorte em
    volta da máscara; ou plano de fundo temporal (câmera parada), ou borrão (sem modelo).
  - modelos baixados no primeiro uso para ~/.local/share/ai-editor/modelos/<nome>/, de URLs com revisão (commit)
    fixa e conferidos por SHA-256 fixo neste arquivo; download em `.parcial` + renomeio atômico. Só arquivos de
    pesos (dados ONNX) — nada é executado.

Processamento sempre em fluxo (pipe do ffmpeg, quadro a quadro): o vídeo nunca é carregado inteiro na memória.
Só os quadros do trecho da máscara passam pelo Python (RGB); o resto do vídeo segue no ffmpeg pelo caminho yuv
nativo (`_cmd_versao`), com a mesma matriz de cor na ida e na volta.
Antes de abrir um modelo, confere a memória livre (`MemAvailable`) e espera se estiver curta; durante a inferência,
pausa se cair abaixo de 1,2 GB. Uma tarefa de IA por vez, também entre processos (`trava_ia`).

Máscaras: vídeo em tons de cinza sem perda (FFV1 em .mkv) em ~/.cache/ai-editor/mascaras/<hash do original>/
(fora de `.versoes/`, que o `desfazer` da última versão apaga), com um .json descrevendo o trecho (quadro inicial,
quantidade, fps, tamanho, arquivo de origem). Apagadas depois de 7 dias sem uso.

Sem Qt (usado pelo servidor MCP).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from fractions import Fraction
from pathlib import Path

from . import binarios, core
from .core import Documento, fmt_tempo

try:  # numpy é dependência principal do pacote, mas o módulo deve importar mesmo sem ele
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

DICA_IA = ("as funções de objetos precisam do ONNX Runtime (opcional). Habilite com `uv sync --extra ia` "
           "(no código-fonte) ou `pip install onnxruntime numpy` no mesmo Python do AI Editor")
# Empacotado (AppImage/PyInstaller) o ONNX Runtime vem DENTRO do pacote: se faltar, o pacote está quebrado e a dica
# de `uv sync`/`pip` não serve para nada.
DICA_IA_PACOTE = ("o ONNX Runtime deveria vir dentro do AppImage, mas não carregou ({erro}). Baixe o AppImage de "
                  "novo; se persistir, rode `ai-editor --diagnostico` e abra uma issue com a saída")


def _dica_ia(erro: BaseException | None = None) -> str:
    if getattr(sys, "frozen", False):
        return DICA_IA_PACOTE.format(erro=erro or "numpy ausente")
    return DICA_IA


class IAIndisponivel(RuntimeError):
    pass


class Cancelado(RuntimeError):
    pass


# ---------------------------------------------------------------- modelos (URLs e hashes fixos)
MODELOS_OBJ_DIR = binarios.MODELOS_DIR

_HF = "https://huggingface.co/{repo}/resolve/{rev}/{arq}"

# nome → repo, revisão (commit), licença, arquivos [(caminho no repo, bytes, sha256)]
MODELOS = {
    "sam2.1-tiny": {
        "repo": "onnx-community/sam2.1-hiera-tiny-ONNX",
        "revisao": "814a066640debee5a91e70aa401fb8e17e030503",
        "licenca": "Apache-2.0 (pesos do facebook/sam2.1-hiera-tiny; export ONNX do onnx-community)",
        "arquivos": [
            ("onnx/vision_encoder.onnx", 354238,
             "4f30aacd3aaefbca81a0b7fe4c1fc96345570ea0a6f80ced599493d1b3be2e8c"),
            ("onnx/vision_encoder.onnx_data", 134084864,
             "e83df9866a5afe68ea7f0f721f18f65137fc3acbf0da1c74e946d363e09c69cc"),
            ("onnx/prompt_encoder_mask_decoder.onnx", 213114,
             "874414704c5d686db7d206a35f6e15d26563d50c8c4468fccc6739bd7e491dcf"),
            ("onnx/prompt_encoder_mask_decoder.onnx_data", 20958208,
             "e9874d900dd4134ed60eab1e97910327c2419e0b2954485d8fd6e7f1a1470f47"),
        ],
    },
    "sam2.1-small": {
        "repo": "onnx-community/sam2.1-hiera-small-ONNX",
        "revisao": "a7df49d8de14b9d2e4504d1687b0d568f905fd8d",
        "licenca": "Apache-2.0 (pesos do facebook/sam2.1-hiera-small; export ONNX do onnx-community)",
        "arquivos": [
            ("onnx/vision_encoder.onnx", 467440,
             "aacf1f7137bb6fffcf6bf166abcfabe28f57a76059254f3fb611c4a64a208119"),
            ("onnx/vision_encoder.onnx_data", 162476288,
             "260fd1f0a34e72a3dc79a739e563b4facc0ba75504818b433a1f808e66637456"),
            ("onnx/prompt_encoder_mask_decoder.onnx", 213114,
             "079c59b261f723ff5c6a125e69b0170a957b21c58738c28d2b0394ecd0587d7f"),
            ("onnx/prompt_encoder_mask_decoder.onnx_data", 20958208,
             "f9e59a584ab8ced21fa812c211bc01084204db1c9e92a5ef4fb3a49972b4e864"),
        ],
    },
    "lama": {
        "repo": "opencv/inpainting_lama",
        "revisao": "aee6d22f0a13e5e35af1c9a1c3afd62841fc6f3f",
        "licenca": "Apache-2.0 (LaMa big-lama, advimman/lama; ONNX do opencv_zoo, derivado do Carve/LaMa-ONNX)",
        "arquivos": [
            ("inpainting_lama_2025jan.onnx", 92591623,
             "7df918ac3921d3daf0aae1d219776cf0dc4e4935f035af81841b40adcf74fdf2"),
        ],
    },
}
MODELO_SAM_PADRAO = "sam2.1-tiny"


def _mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB"


def _pasta_modelo(nome: str) -> Path:
    """Pasta do modelo: a embutida no pacote (AppImage, somente leitura) se tiver todos os arquivos; senão a do
    usuário (MODELOS_OBJ_DIR/<nome>, onde o download grava)."""
    m = MODELOS.get(nome)
    if m:
        for d in binarios.pastas_modelos():
            if d != MODELOS_OBJ_DIR and all((d / nome / Path(a).name).is_file() for a, _, _ in m["arquivos"]):
                return d / nome
    return MODELOS_OBJ_DIR / nome


def _arquivo_modelo(nome: str, arq: str) -> Path:
    return _pasta_modelo(nome) / Path(arq).name  # .onnx e .onnx_data lado a lado, com os nomes originais


def tamanho_modelo(nome: str) -> int:
    return sum(t for _, t, _ in MODELOS[nome]["arquivos"])


def _arquivo_integro(p: Path, tam: int, sha: str) -> bool:
    """Arquivo presente e íntegro. O SHA-256 é conferido UMA vez e fica num marcador `.<arquivo>.ok` (tamanho,
    mtime e hash, como na Fase 6); depois basta comparar tamanho/mtime com o marcador."""
    try:
        st = p.stat()
    except OSError:
        return False
    if st.st_size != tam:
        return False
    if binarios.modelo_embutido(p):  # do pacote: SHA-256 conferido no build, somente leitura
        return True
    ok = p.with_name(f".{p.name}.ok")
    assinatura = f"{st.st_size} {st.st_mtime_ns} {sha}"
    try:
        if ok.read_text().strip() == assinatura:
            return True
    except OSError:
        pass
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while bloco := f.read(1 << 22):
            h.update(bloco)
    if h.hexdigest() != sha:
        return False  # corrompido/alterado: baixar_modelo baixa de novo (o .parcial substitui atomicamente)
    with contextlib.suppress(OSError):
        ok.write_text(assinatura + "\n")
    return True


def modelo_pronto(nome: str) -> bool:
    return all(_arquivo_integro(_arquivo_modelo(nome, a), t, sha) for a, t, sha in MODELOS[nome]["arquivos"])


def _disco_cheio(e: BaseException) -> bool:
    return getattr(e, "errno", None) in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG) or (
        isinstance(getattr(e, "reason", None), OSError) and e.reason.errno in (errno.ENOSPC, errno.EDQUOT))


def _msg_disco_cheio(pasta: Path, precisa: int | None = None) -> str:
    try:
        import shutil
        livre = shutil.disk_usage(pasta if pasta.exists() else pasta.parent).free
    except OSError:
        livre = None
    return ("disco cheio em " + str(pasta) + (f" (livre: {_mb(livre)}" if livre is not None else " (")
            + (f", precisa de ~{_mb(precisa)})" if precisa else ")") + ". Libere espaço e tente de novo")


def _baixar(url: str, final: Path, sha256: str, tamanho: int, progresso=None, cancelar=None) -> None:
    """Baixa para `.<nome>.XXXX.parcial` na mesma pasta, confere tamanho e SHA-256 e só então renomeia
    (os.replace, atômico). Nunca recebe mais bytes que o tamanho esperado. Qualquer falha apaga o parcial."""
    final.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{final.name}.", suffix=".parcial", dir=final.parent)
    h = hashlib.sha256()
    lido = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ai-editor"})
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(req, timeout=60) as resp:
            while True:
                if cancelar and cancelar():
                    raise Cancelado("download cancelado")
                bloco = resp.read(min(1 << 20, tamanho - lido + 1))
                if not bloco:
                    break
                lido += len(bloco)
                if lido > tamanho:
                    raise RuntimeError(f"o servidor mandou mais que os {tamanho} bytes esperados de {url}; "
                                       f"download descartado")
                out.write(bloco)
                h.update(bloco)
                if progresso:
                    progresso(lido)
        if lido != tamanho:
            raise RuntimeError(f"download incompleto de {url}: {lido} de {tamanho} bytes")
        if h.hexdigest() != sha256:
            raise RuntimeError(f"SHA-256 não confere em {url} (esperado {sha256}, veio {h.hexdigest()}); "
                               f"arquivo descartado")
        os.chmod(tmp, 0o644)
        os.replace(tmp, final)
        st = final.stat()  # já conferido: grava o marcador para modelo_pronto não reler o arquivo
        with contextlib.suppress(OSError):
            final.with_name(f".{final.name}.ok").write_text(f"{st.st_size} {st.st_mtime_ns} {sha256}\n")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _limpar_parciais(pasta: Path, idade_s: float = 3600) -> None:
    if not pasta.is_dir():
        return
    agora = time.time()
    for p in pasta.glob(".*.parcial"):
        try:
            if agora - p.stat().st_mtime > idade_s:
                p.unlink()
        except OSError:
            pass


@contextlib.contextmanager
def _trava_arquivo(caminho: Path, cancelar=None, aviso=None, texto: str = ""):
    """Trava exclusiva entre processos (fcntl.flock), esperando a vez sem perder o cancelamento."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with open(caminho, "a") as f:
        avisado = False
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if cancelar and cancelar():
                    raise Cancelado("cancelada enquanto esperava a vez") from None
                if aviso and texto and not avisado:
                    aviso(texto)
                    avisado = True
                time.sleep(0.3)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def baixar_modelo(nome: str, progresso=None, cancelar=None) -> Path:
    """Garante o modelo em disco (baixa o que faltar, com SHA-256 conferido). progresso(fração 0..1).
    Downloads simultâneos do mesmo modelo (threads ou processos) fazem fila: o segundo só confere."""
    if nome not in MODELOS:
        raise ValueError(f"modelo desconhecido: {nome!r}; opções: {', '.join(MODELOS)}")
    m = MODELOS[nome]
    pasta = _pasta_modelo(nome)
    if modelo_pronto(nome):
        return pasta
    if not binarios.downloads_permitidos():
        prontos = [k for k in MODELOS if modelo_pronto(k)]
        raise IAIndisponivel(f"o modelo '{nome}' não vem neste AppImage (e o AppImage não baixa nada). "
                             f"Modelos embutidos: {', '.join(prontos) or 'nenhum'}")
    with _trava_arquivo(MODELOS_OBJ_DIR / f".{nome}.lock", cancelar):
        if modelo_pronto(nome):
            return pasta
        _limpar_parciais(pasta)
        total = tamanho_modelo(nome)
        feito = 0
        for arq, tam, sha in m["arquivos"]:
            final = _arquivo_modelo(nome, arq)
            if _arquivo_integro(final, tam, sha):
                feito += tam
                continue
            url = _HF.format(repo=m["repo"], rev=m["revisao"], arq=arq)
            base = feito
            cb = (lambda n, b=base: progresso((b + n) / total)) if progresso else None
            try:
                _baixar(url, final, sha, tam, cb, cancelar)
            except OSError as e:
                if _disco_cheio(e):
                    raise OSError(errno.ENOSPC, _msg_disco_cheio(pasta, total - feito)) from e
                raise IAIndisponivel(f"não consegui baixar o modelo '{nome}' ({_mb(total)}) de {url}: {e}. "
                                     f"Sem internet? Baixe manualmente para {final}") from e
            feito += tam
        (pasta / "ORIGEM.txt").write_text(
            f"{nome}\nrepositório: https://huggingface.co/{m['repo']}\nrevisão: {m['revisao']}\n"
            f"licença: {m['licenca']}\n" + "".join(f"{a}  {s}\n" for a, _, s in m["arquivos"]))
    if progresso:
        progresso(1.0)
    return pasta


# ---------------------------------------------------------------- ONNX Runtime e memória
_SESSOES: dict[str, object] = {}
_TRAVA_SESSOES = threading.Lock()
_USO = {"ultimo": 0.0, "vigia": None}
MEMORIA_MIN_GB = 2.5     # não abre modelo com menos que isto livre (MemAvailable)
MEMORIA_PAUSA_GB = 1.2   # durante a inferência: abaixo disto, pausa até voltar a MEMORIA_RETOMAR_GB
MEMORIA_RETOMAR_GB = 1.6
ESPERA_MEMORIA_S = 1200  # espera até 20 min pela memória, conferindo a cada 60 s
SESSOES_OCIOSAS_S = 300  # sessões em cache são fechadas depois de 5 min sem uso (ou antes, se a RAM cair)
THREADS = 4


def _ort():
    if np is None:
        raise IAIndisponivel(_dica_ia())
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise IAIndisponivel(_dica_ia(e)) from e
    return ort


def disponivel() -> dict:
    """Situação do ambiente de IA (para diagnóstico)."""
    try:
        ort = _ort()
        motor = f"onnxruntime {ort.__version__}"
    except IAIndisponivel as e:
        motor = None
        dica = str(e)
    r = {"onnxruntime": motor, "modelos": {n: {"baixado": modelo_pronto(n), "tamanho": _mb(tamanho_modelo(n)),
                                               "licenca": m["licenca"]} for n, m in MODELOS.items()},
         "pasta_modelos": str(MODELOS_OBJ_DIR), "memoria_livre_gb": round(memoria_livre_gb(), 2)}
    if motor is None:
        r["dica"] = dica
    return r


def memoria_livre_gb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for linha in f:
                if linha.startswith("MemAvailable:"):
                    return int(linha.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 99.0


def esperar_memoria(minimo_gb: float = MEMORIA_MIN_GB, espera_s: float = ESPERA_MEMORIA_S, cancelar=None,
                    aviso=None, intervalo_s: float = 60) -> float:
    """Espera (de `intervalo_s` em `intervalo_s`, até `espera_s`) haver `minimo_gb` livres; erro se não houver.
    Antes de esperar, fecha as sessões em cache (que podem ser a causa)."""
    fim = time.monotonic() + espera_s
    while True:
        livre = memoria_livre_gb()
        if livre >= minimo_gb:
            return livre
        if _SESSOES:
            liberar_modelos()
            continue
        if time.monotonic() >= fim:
            raise MemoryError(f"memória livre insuficiente para a IA: {livre:.1f} GB (precisa de {minimo_gb} GB); "
                              f"feche programas e tente de novo")
        if aviso:
            aviso(f"esperando memória livre ({livre:.1f} GB < {minimo_gb} GB)")
        for _ in range(int(intervalo_s)):
            if cancelar and cancelar():
                raise Cancelado("cancelado enquanto esperava memória")
            time.sleep(1)


def _pausa_memoria(cancelar=None, aviso=None) -> None:
    """Guarda DURANTE a inferência (chamada a cada poucos quadros): abaixo de MEMORIA_PAUSA_GB, pausa (de 5 em 5 s,
    até 20 min) até a memória voltar a MEMORIA_RETOMAR_GB. As sessões em uso não são fechadas aqui."""
    if memoria_livre_gb() >= MEMORIA_PAUSA_GB:
        return
    fim = time.monotonic() + ESPERA_MEMORIA_S
    while (livre := memoria_livre_gb()) < MEMORIA_RETOMAR_GB:
        if time.monotonic() >= fim:
            raise MemoryError(f"memória livre ficou abaixo de {MEMORIA_PAUSA_GB} GB por 20 min ({livre:.1f} GB): "
                              f"tarefa interrompida; feche programas e tente de novo")
        if aviso:
            aviso(f"pausado: pouca memória livre ({livre:.1f} GB); retomo com {MEMORIA_RETOMAR_GB} GB")
        for _ in range(5):
            if cancelar and cancelar():
                raise Cancelado("cancelado enquanto esperava memória")
            time.sleep(1)


def _vigiar_sessoes() -> None:
    """Thread de fundo: fecha as sessões ociosas há SESSOES_OCIOSAS_S, ou ociosas há 20 s se a RAM cair abaixo
    de MEMORIA_MIN_GB. Fechar é sempre seguro: quem está usando uma sessão guarda a própria referência."""
    while True:
        time.sleep(15)
        with _TRAVA_SESSOES:
            if not _SESSOES:
                _USO["vigia"] = None
                return
            ocioso = time.monotonic() - _USO["ultimo"]
            if ocioso > SESSOES_OCIOSAS_S or (ocioso > 20 and memoria_livre_gb() < MEMORIA_MIN_GB):
                _SESSOES.clear()


def _usar(sessao):
    _USO["ultimo"] = time.monotonic()
    return sessao


def _sessao_carregada(caminho: Path) -> bool:
    return str(caminho) in _SESSOES


def _sessao(caminho: Path):
    """Sessão ONNX Runtime em CPU (4 threads), em cache por alguns minutos (ver `_vigiar_sessoes`)."""
    chave = str(caminho)
    with _TRAVA_SESSOES:
        s = _SESSOES.get(chave)
        if s is None:
            ort = _ort()
            op = ort.SessionOptions()
            op.intra_op_num_threads = THREADS
            op.inter_op_num_threads = 1
            op.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            op.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            op.log_severity_level = 3
            s = ort.InferenceSession(str(caminho), op, providers=["CPUExecutionProvider"])
            _SESSOES[chave] = s
        _USO["ultimo"] = time.monotonic()
        if _USO["vigia"] is None:
            _USO["vigia"] = threading.Thread(target=_vigiar_sessoes, name="ia-sessoes", daemon=True)
            _USO["vigia"].start()
        return s


def liberar_modelos() -> None:
    """Fecha as sessões em cache (a memória volta quando ninguém mais as usa)."""
    with _TRAVA_SESSOES:
        _SESSOES.clear()


_TRAVA_IA = threading.RLock()  # uma tarefa pesada por vez neste processo (memória curta)
_IA_LOCAL = threading.local()  # .n (aninhamento da trava entre processos), .tarefa (Tarefa em execução)


@contextlib.contextmanager
def trava_ia(cancelar=None, aviso=None):
    """Uma tarefa de IA por vez: na thread (fila do processo) e entre processos (flock em
    ~/.cache/ai-editor/ia.lock — outro servidor MCP ou outra janela). Reentrante na mesma thread. A espera
    respeita o cancelamento (tarefa na fila cancela na hora)."""
    while not _TRAVA_IA.acquire(timeout=0.2):
        if cancelar and cancelar():
            raise Cancelado("cancelada na fila")
    try:
        if getattr(_IA_LOCAL, "n", 0):
            _IA_LOCAL.n += 1
            try:
                yield
            finally:
                _IA_LOCAL.n -= 1
            return
        with _trava_arquivo(core.CACHE_DIR / "ia.lock", cancelar, aviso,
                            "esperando outra tarefa de IA terminar (outro processo)"):
            _IA_LOCAL.n = 1
            try:
                yield
            finally:
                _IA_LOCAL.n = 0
    finally:
        _TRAVA_IA.release()


# ---------------------------------------------------------------- imagem em numpy (sem OpenCV)
def _caixa(a, r: int):
    """Média numa janela (2r+1)x(2r+1) nos eixos 0 e 1 (somas acumuladas: custo independe do raio)."""
    if r < 1:
        return a.astype(np.float32, copy=False)
    out = a.astype(np.float32, copy=False)
    for eixo in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[eixo] = (r + 1, r)
        c = np.cumsum(np.pad(out, pad, mode="edge"), axis=eixo, dtype=np.float32)
        n = out.shape[eixo]
        out = (np.take(c, range(2 * r + 1, 2 * r + 1 + n), axis=eixo)
               - np.take(c, range(0, n), axis=eixo)) / (2 * r + 1)
    return out


def _redim(a, h: int, w: int):
    """Redimensiona (bilinear, alinhado pelos centros dos pixels) um array HxW ou HxWxC."""
    H, W = a.shape[:2]
    if (H, W) == (h, w):
        return a.astype(np.float32, copy=False)

    def eixo(n_in, n_out):
        p = (np.arange(n_out, dtype=np.float32) + 0.5) * n_in / n_out - 0.5
        p = np.clip(p, 0, n_in - 1)
        i0 = np.floor(p).astype(np.int64)
        i1 = np.minimum(i0 + 1, n_in - 1)
        return i0, i1, (p - i0).astype(np.float32)
    y0, y1, wy = eixo(H, h)
    x0, x1, wx = eixo(W, w)
    a = a.astype(np.float32, copy=False)
    forma = (-1,) + (1,) * (a.ndim - 1)
    t = a[y0] * (1 - wy.reshape(forma)) + a[y1] * wy.reshape(forma)
    forma = (1, -1) + (1,) * (a.ndim - 2)
    return t[:, x0] * (1 - wx.reshape(forma)) + t[:, x1] * wx.reshape(forma)


def _reduzir(a, f: int):
    """Reduz por média em blocos f x f (corta a sobra das bordas)."""
    if f <= 1:
        return a.astype(np.float32, copy=False)
    H, W = a.shape[0] // f * f, a.shape[1] // f * f
    b = a[:H, :W].astype(np.float32, copy=False)
    return b.reshape(H // f, f, W // f, f, *a.shape[2:]).mean(axis=(1, 3))


def _borrar(img, raio: float):
    """Desfoque forte e barato: reduz, aplica duas médias em caixa (≈ gaussiano) e volta ao tamanho."""
    H, W = img.shape[:2]
    f = max(1, int(raio // 6))
    p = _reduzir(img, f)
    r = max(1, int(round(raio / f / 2)))
    p = _caixa(_caixa(p, r), r)
    return _redim(p, H, W)


def _dilatar(m, r: int):
    """Dilatação de uma máscara booleana por um quadrado de raio r."""
    if r < 1:
        return m
    return _caixa(m.astype(np.float32), r) > 1e-4


def _erodir(m, r: int):
    if r < 1:
        return m
    return _caixa(m.astype(np.float32), r) > 1 - 1e-4


def _suave(m, r: int):
    """Máscara booleana → alfa 0..1 com borda suavizada (raio r)."""
    a = m.astype(np.float32)
    return np.clip(_caixa(_caixa(a, r), r), 0, 1) if r >= 1 else a


def _preencher_rapido(img, buraco):
    """Preenche `buraco` sem modelo: convolução normalizada em várias escalas (as cores de fora escorrem para
    dentro). Bom para áreas pequenas/uniformes; em áreas grandes vira um borrão."""
    H, W = buraco.shape
    vis = (~buraco).astype(np.float32)
    out = img.astype(np.float32).copy()
    falta = buraco.copy()
    ys, xs = np.nonzero(buraco)
    if not len(ys):
        return out
    tam = max(ys.max() - ys.min(), xs.max() - xs.min()) + 1
    raio = 4
    while falta.any() and raio < 4 * max(H, W):
        num = _borrar(img.astype(np.float32) * vis[..., None], raio)
        den = _borrar(vis, raio)
        ok = falta & (den > 0.02)
        if raio >= tam / 2 or not falta.any():
            ok = falta & (den > 1e-6)
        out[ok] = num[ok] / den[ok][:, None]
        falta &= ~ok
        raio *= 2
    # suaviza o preenchimento por dentro (tira degraus entre escalas)
    liso = _borrar(out, max(2.0, tam / 8))
    out[buraco] = liso[buraco]
    return out


# ---------------------------------------------------------------- tarefas em segundo plano
class Tarefa:
    def __init__(self, nome: str, descricao: str):
        self.id = uuid.uuid4().hex[:8]
        self.nome = nome
        self.descricao = descricao
        self.estado = "na fila"  # na fila → rodando → concluida | erro | cancelada
        self.progresso = 0.0
        self.etapa = ""
        self.resultado = None
        self.erro: str | None = None
        self.inicio = time.time()
        self.fim: float | None = None
        self._cancelar = threading.Event()
        self._feito = threading.Event()
        self.thread: threading.Thread | None = None

    def cancelado(self) -> bool:
        return self._cancelar.is_set()

    def cancelar(self) -> None:
        self._cancelar.set()

    def esperar(self, timeout: float | None = None) -> bool:
        return self._feito.wait(timeout)

    def resumo(self) -> dict:
        r = {"id": self.id, "tarefa": self.nome, "descricao": self.descricao, "estado": self.estado,
             "progresso": f"{int(self.progresso * 100)}%", "etapa": self.etapa,
             "tempo_s": round((self.fim or time.time()) - self.inicio, 1)}
        if self.erro:
            r["erro"] = self.erro
        return r


TAREFAS: dict[str, Tarefa] = {}


def iniciar_tarefa(nome: str, descricao: str, fn, *a, ao_progresso=None, **k) -> Tarefa:
    """Roda fn(*a, progresso=..., cancelar=..., etapa=..., **k) numa thread. Tarefas fazem fila (uma por vez,
    também entre processos: `trava_ia`); cancelar uma tarefa na fila vale na hora. ao_progresso(fração, etapa) é chamado a cada atualização (ex.: barra de status da janela)."""
    t = Tarefa(nome, descricao)
    # guarda só as 20 tarefas mais recentes
    for velho in sorted(TAREFAS.values(), key=lambda x: x.inicio)[:-19]:
        if velho.estado not in ("na fila", "rodando"):
            TAREFAS.pop(velho.id, None)
    TAREFAS[t.id] = t

    def prog(f: float):
        t.progresso = max(0.0, min(1.0, f))
        if ao_progresso:
            try:
                ao_progresso(t.progresso, t.etapa)
            except Exception:  # noqa: BLE001
                pass

    def etapa(texto: str):
        t.etapa = texto
        prog(t.progresso)

    def corpo():
        _IA_LOCAL.tarefa = t
        try:
            with trava_ia(t.cancelado, etapa):
                if t.cancelado():
                    raise Cancelado("cancelada antes de começar")
                t.estado = "rodando"
                t.resultado = fn(*a, progresso=prog, cancelar=t.cancelado, etapa=etapa, **k)
            t.estado = "concluida"
            t.progresso = 1.0
        except Cancelado as e:
            t.estado, t.erro = "cancelada", str(e)
        except BaseException as e:  # noqa: BLE001
            t.estado, t.erro = "erro", f"{type(e).__name__}: {e}"
        finally:
            # as sessões ONNX ficam em cache por alguns minutos (_vigiar_sessoes fecha as ociosas)
            _IA_LOCAL.tarefa = None
            t.fim = time.time()
            t._feito.set()

    t.thread = threading.Thread(target=corpo, name=f"ia-{nome}-{t.id}", daemon=True)
    t.thread.start()
    return t


# ---------------------------------------------------------------- leitura/escrita de quadros (pipe do ffmpeg)
class _Leitor:
    """Quadros brutos (rgb24/gray) de um ffmpeg, um por vez."""

    def __init__(self, cmd: list[str], w: int, h: int, canais: int):
        self.w, self.h, self.c = w, h, canais
        self.tam = w * h * canais
        self.cmd = cmd
        self.err = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=self.err, stdin=subprocess.DEVNULL,
                                     preexec_fn=core._morrer_com_pai, bufsize=self.tam)

    def ler(self):
        buf = self.proc.stdout.read(self.tam)
        if not buf or len(buf) < self.tam:
            return None
        forma = (self.h, self.w, self.c) if self.c > 1 else (self.h, self.w)
        return np.frombuffer(buf, np.uint8).reshape(forma)

    def fechar(self, conferir: bool = False) -> None:
        if self.proc.poll() is None:
            if conferir:
                self.proc.stdout.read()
                self.proc.wait()
            else:
                self.proc.kill()
                self.proc.wait()
        if conferir and self.proc.returncode != 0:
            self.err.seek(0)
            raise core.ErroFFmpeg(f"comando falhou ({self.proc.returncode}): {shlex.join(self.cmd)}\n"
                                  f"{self.err.read().decode(errors='replace')[-3000:]}")
        self.err.close()


class _Escritor:
    """ffmpeg que recebe quadros brutos pelo stdin. ao_tempo(s): progresso do ffmpeg (`-progress pipe:1`, só se o
    comando o pedir)."""

    def __init__(self, cmd: list[str], ao_tempo=None):
        self.cmd = cmd
        self.err = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE if ao_tempo else subprocess.DEVNULL, stderr=self.err,
                                     preexec_fn=core._morrer_com_pai)
        if ao_tempo:
            def ler():
                for linha in self.proc.stdout:
                    if linha.startswith(b"out_time_us="):
                        with contextlib.suppress(ValueError):
                            ao_tempo(max(0, int(linha.split(b"=", 1)[1])) / 1e6)
            threading.Thread(target=ler, daemon=True).start()

    def escrever(self, quadro) -> None:
        try:
            self.proc.stdin.write(np.ascontiguousarray(quadro, dtype=np.uint8).tobytes())
        except BrokenPipeError:
            self.fechar()

    def fechar(self, cancelar=None) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        while True:
            try:
                self.proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancelar and cancelar():
                    self.abortar()
                    raise Cancelado("cancelado") from None
        if self.proc.returncode != 0:
            self.err.seek(0)
            texto = self.err.read().decode(errors="replace")
            if "No space left" in texto:
                raise OSError(errno.ENOSPC, _msg_disco_cheio(Path(self.cmd[-1]).parent))
            raise core.ErroFFmpeg(f"comando falhou ({self.proc.returncode}): {shlex.join(self.cmd)}\n{texto[-3000:]}")

    def abortar(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


# ---------------------------------------------------------------- cor (rgb ↔ yuv sem trocar a matriz)
_MATRIZES = {"bt709": "bt709", "smpte170m": "bt601", "bt470bg": "bt601", "bt2020nc": "bt2020",
             "bt2020c": "bt2020", "fcc": "fcc", "smpte240m": "smpte240m"}
_TAG_MATRIZ = {"bt709": "bt709", "bt601": "smpte170m", "bt2020": "bt2020nc", "fcc": "fcc", "smpte240m": "smpte240m"}


_FLAGS_LER = "lanczos+accurate_rnd+full_chroma_int"  # sem full_chroma_int a ida e volta escurecia ~1 nível
_FLAGS_GRAVAR = "area+accurate_rnd"  # medido: melhor PSNR de croma na volta para 4:2:0


def _cor(info: dict) -> tuple[str, str]:
    """(matriz para o filtro scale, faixa 'tv'|'pc') da origem. Sem tag, a que os players supõem: BT.709 em HD
    (altura ≥ 720), BT.601 abaixo. A MESMA matriz é usada para ler (yuv→rgb) e gravar (rgb→yuv): a ida e volta não
    muda as cores (antes a gravação usava o padrão do swscale, BT.601, num vídeo marcado BT.709)."""
    v = info.get("v") or {}
    mat = _MATRIZES.get(v.get("color_space") or "")
    if not mat:
        mat = "bt709" if min(info["w"], info["h"]) >= 720 else "bt601"
    faixa = "pc" if (v.get("color_range") == "pc" or str(v.get("pix_fmt", "")).startswith("yuvj")) else "tv"
    return mat, faixa


def _tags_cor_saida(info: dict) -> list[str]:
    """Tags de cor para um arquivo gerado do zero (recorte): as da origem, com a matriz usada na conversão."""
    mat, faixa = _cor(info)
    tags = core._tags_cor(info.get("v"))
    if "-colorspace" not in tags:
        tags += ["-colorspace", _TAG_MATRIZ[mat]]
    padrao = {"bt709": "bt709", "bt601": "smpte170m", "bt2020": "bt2020"}.get(mat)
    if padrao and "-color_primaries" not in tags:  # o MOV só grava o átomo colr com as três tags
        tags += ["-color_primaries", padrao]
    if padrao and "-color_trc" not in tags:
        tags += ["-color_trc", "bt2020-10" if padrao == "bt2020" else padrao]
    if "-color_range" not in tags:
        tags += ["-color_range", faixa]
    return tags


def _avisos_cor(info: dict) -> list[str]:
    v = info.get("v") or {}
    av = []
    if core._10bits(info.get("pix_fmt", "")):
        av.append("origem de 10 bits: o trecho processado passa por RGB de 8 bits (pode surgir 'banding' em "
                  "degradês); fora do trecho o vídeo continua em 10 bits")
    if v.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        av.append("vídeo HDR (PQ/HLG): o trecho é processado nos valores codificados, sem conversão de tom; as tags "
                  "HDR são mantidas")
    return av


def _cmd_ler_video(entrada: Path, info: dict, ss: float | None = None, quadros: int | None = None,
                   tamanho: tuple[int, int] | None = None, threads: int = 4) -> list[str]:
    """ffmpeg → rgb24 bruto no stdout, em fps constante (`info['taxa']`), no tamanho EXIBIDO (rotação e pixels não
    quadrados já aplicados) ou em `tamanho` (w, h), com a matriz de cor da origem (`_cor`).
    ss=None = desde o 1º quadro; quadros=None = até o fim (exatamente `_total_quadros`). Sempre com `-ss` quando o
    vídeo começa depois do contêiner: sem ele o modo cfr repete o 1º quadro para cobrir o atraso e desloca tudo."""
    i_ini = 0
    if ss is None:
        ss = _ss_do_quadro(info, 0)
    else:
        i_ini = _primeiro_quadro_desde(info, ss)
    if quadros is None:
        quadros = max(1, _total_quadros(info) - i_ini)
    mat, faixa = _cor(info)
    tw, th = tamanho or (info["w"], info["h"])
    flags = "bilinear" if tamanho else _FLAGS_LER
    vf = f"scale={tw}:{th}:flags={flags}:in_color_matrix={mat}:in_range={faixa},setsar=1,format=rgb24"
    cmd = core.FFMPEG + ["-threads", str(threads)] + info["dec"]
    if ss > 0:
        cmd += ["-ss", f"{ss:.6f}"]
    else:
        # sem -ss os tempos chegam ao cfr como (atraso + i/fps); com -ss, como (i - j)/fps + 1/4 de quadro. Põe a
        # leitura desde o início na MESMA fase, senão em VFR o cfr escolhe vizinhos diferentes num buraco
        vf = f"setpts=PTS+{0.25 / _fps(info) - info['atraso_video']:.6f}/TB," + vf
    cmd += ["-i", str(entrada), "-map", "0:V:0", "-an", "-sn", "-vf", vf,
            "-fps_mode", "cfr", "-r", info["taxa"], "-frames:v", str(int(quadros))]
    return cmd + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]


_PRE_S = 0.3  # a leitura de um quadro i começa ~0,3 s antes e descarta o excesso (ver _abrir_video)


def _abrir_video(entrada: Path, info: dict, i: int, quadros: int, tamanho: tuple[int, int] | None = None):
    """(_Leitor dos quadros [i, i+quadros), comando). Em vídeo VFR a busca precisa (`-ss`) DESCARTA o quadro de
    origem anterior ao ponto de busca, que ainda estaria na tela no quadro i (num buraco de 100 ms, o quadro i
    sairia como o seguinte); por isso começa até _PRE_S antes e descarta os quadros a mais: o quadro i sai igual ao
    da leitura sequencial do vídeo inteiro."""
    pre = min(i, int(np.ceil(_PRE_S * _fps(info))))
    j = i - pre
    cmd = _cmd_ler_video(entrada, info, _ss_do_quadro(info, j) if j > 0 else None, quadros + pre, tamanho)
    tw, th = tamanho or (info["w"], info["h"])
    leitor = _Leitor(cmd, tw, th, 3)
    for _ in range(pre):
        if leitor.ler() is None:
            break
    return leitor, cmd


def _cmd_ler_mascara(arq: Path, tamanho: tuple[int, int] | None = None) -> list[str]:
    """Máscara (cinza) → bruto. `tamanho` (w, h): reamostra (vizinho mais próximo) para um vídeo que perdeu 1 px na
    versão anterior (dimensão ímpar arredondada para par)."""
    vf = ["-vf", f"scale={tamanho[0]}:{tamanho[1]}:flags=neighbor"] if tamanho else []
    return core.FFMPEG + ["-threads", "2", "-i", str(arq), "-map", "0:v:0"] + vf + ["-f", "rawvideo",
                                                                                 "-pix_fmt", "gray", "-"]


# ---------------------------------------------------------------- tempo ↔ quadro
# Uma taxa só no módulo todo: a `taxa` (r_frame_rate) com que o leitor gera os quadros em CFR. Em vídeo VFR (celular:
# r=30, média 29,97) a média (avg_frame_rate) daria outro nº de quadros que o leitor e desalinharia os índices.
def _fps(info: dict) -> float:
    try:
        return float(Fraction(str(info["taxa"])))
    except (ValueError, ZeroDivisionError, KeyError):
        return float(info.get("fps") or 30.0)


def _quadro_em(info: dict, t: float) -> int:
    """Índice do quadro exibido no tempo t (desconta o atraso do vídeo no contêiner)."""
    return max(0, int(np.floor((t - info["atraso_video"]) * _fps(info) + 1e-3)))


def _primeiro_quadro_desde(info: dict, t: float) -> int:
    """Índice do primeiro quadro com tempo ≥ t (o que o `-ss t` preciso entrega primeiro)."""
    return max(0, int(np.ceil((t - info["atraso_video"]) * _fps(info) - 1e-3)))


def _duracao_video(info: dict) -> float:
    v = info.get("v") or {}
    try:
        dv = float(v.get("duration") or 0)
    except ValueError:
        dv = 0.0
    if dv <= 0:  # MKV/WebM: duração por faixa só na tag DURATION
        m = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", str((v.get("tags") or {}).get("DURATION", "")))
        if m:
            dv = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if dv <= 0:
        dv = info["dur"] - info["atraso_video"]
    return dv


def _total_quadros(info: dict) -> int:
    """Quadros do vídeo na grade CFR da `taxa`: duração da faixa de VÍDEO × taxa (a do contêiner inclui o áudio,
    que costuma passar um pouco do fim do vídeo). O leitor entrega exatamente isto (`-frames:v`)."""
    return max(1, int(round(_duracao_video(info) * _fps(info))))


def _tempo_do_quadro(info: dict, i: int) -> float:
    return info["atraso_video"] + i / _fps(info)


def _ss_do_quadro(info: dict, i: int) -> float:
    """-ss que faz o ffmpeg (busca precisa) entregar exatamente o quadro i primeiro."""
    return max(0.0, _tempo_do_quadro(info, i) - 0.25 / _fps(info))


# ---------------------------------------------------------------- SAM 2.1 (imagem) + rastreamento simples
_LADO = 1024  # entrada do codificador do SAM 2.1 (imagem redimensionada para 1024x1024, sem manter proporção)
_MEDIA = np.array([0.485, 0.456, 0.406], np.float32) if np is not None else None
_DESVIO = np.array([0.229, 0.224, 0.225], np.float32) if np is not None else None


class SAM:
    def __init__(self, nome: str = MODELO_SAM_PADRAO, progresso=None, cancelar=None):
        pasta = baixar_modelo(nome, progresso, cancelar)
        self.cod = _sessao(pasta / "vision_encoder.onnx")
        self.dec = _sessao(pasta / "prompt_encoder_mask_decoder.onnx")

    def codificar(self, img1024):
        x = (img1024.astype(np.float32) / 255.0 - _MEDIA) / _DESVIO
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
        return _usar(self.cod).run(None, {"pixel_values": x})

    def decodificar(self, emb, pontos, rotulos, caixa=None):
        """pontos/caixa em coordenadas 1024x1024. Devolve (logits [3,256,256], iou [3], placar do objeto)."""
        p = np.asarray(pontos, np.float32).reshape(1, 1, -1, 2)
        r = np.asarray(rotulos, np.int64).reshape(1, 1, -1)
        b = (np.asarray(caixa, np.float32).reshape(1, 1, 4) if caixa is not None
             else np.zeros((1, 0, 4), np.float32))
        iou, masks, obj = self.dec.run(None, {
            "input_points": p, "input_labels": r, "input_boxes": b,
            "image_embeddings.0": emb[0], "image_embeddings.1": emb[1], "image_embeddings.2": emb[2]})
        return masks[0, 0], iou[0, 0], float(obj.reshape(-1)[0])


def _caixa_mascara(m) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) inclusivos da máscara booleana, ou None se vazia."""
    ys = np.flatnonzero(m.any(axis=1))
    if not len(ys):
        return None
    xs = np.flatnonzero(m.any(axis=0))
    return int(xs[0]), int(ys[0]), int(xs[-1]), int(ys[-1])


def _ponto_interno(m) -> tuple[float, float]:
    """Um ponto bem dentro da máscara: o pixel que sobrevive à maior erosão (aprox.), perto do centro."""
    atual = m
    r = 1
    while True:
        e = _erodir(atual, r)
        if not e.any():
            break
        atual, r = e, r * 2
    ys, xs = np.nonzero(atual)
    cy, cx = ys.mean(), xs.mean()
    k = int(np.argmin((ys - cy) ** 2 + (xs - cx) ** 2))
    return float(xs[k]) + 0.5, float(ys[k]) + 0.5


def _iou(a, b) -> float:
    u = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / u) if u else 0.0


def _sao_fracoes(valores: list[float], nome: str) -> bool:
    """Frações 0–1 ou pixels? Todos em [0, 1] → frações; mas só 0 e 1 inteiros ([[1, 1]]) é ambíguo (pixel 1,1 ou
    o canto do quadro?) → erro pedindo para escrever de forma inequívoca."""
    if not all(0 <= v <= 1 for v in valores):
        return False
    if all(v in (0.0, 1.0) for v in valores) and any(v == 1.0 for v in valores):
        raise ValueError(f"{nome}: {valores} é ambíguo (pixel ou fração do quadro?). Use frações com decimais "
                         f"(ex.: 0.5 = meio; 0.999 = borda) ou pixels maiores que 1")
    return True


def _prompt_px(pontos, w: int, h: int, nome: str) -> list[tuple[float, float]]:
    """[[x, y], ...] em frações 0–1 do quadro (recomendado) ou pixels do vídeo exibido → (x, y) em pixels."""
    if not pontos:
        return []
    if isinstance(pontos[0], (int, float, str)):
        pontos = [pontos]
    out = []
    for p in pontos:
        try:
            x, y = (float(str(v).replace(",", ".")) for v in p)
        except (TypeError, ValueError):
            raise ValueError(f"{nome}: use [[x, y], ...] em pixels ou frações 0–1") from None
        out.append((x, y))
    if _sao_fracoes([v for p in out for v in p], nome):
        out = [(x * w, y * h) for x, y in out]
    for x, y in out:
        if not (0 <= x <= w and 0 <= y <= h):
            raise ValueError(f"{nome}: ponto ({x:g}, {y:g}) fora do quadro {w}x{h}")
    return out


def _caixa_px(caixa, w: int, h: int) -> tuple[float, float, float, float] | None:
    """[x, y, largura, altura] (pixels ou frações 0–1, como em borrar_area) → (x0, y0, x1, y1) em pixels."""
    if caixa is None:
        return None
    if isinstance(caixa, dict):
        caixa = [caixa.get("x", 0), caixa.get("y", 0), caixa.get("largura"), caixa.get("altura")]
    try:
        x, y, cw, ch = (float(str(v).replace(",", ".")) for v in caixa)
    except (TypeError, ValueError):
        raise ValueError("caixa: use [x, y, largura, altura] em pixels ou frações 0–1") from None
    if all(0 <= v <= 1 for v in (x, y, cw, ch)):  # caixa de 1 px não faz sentido: [0, 0, 1, 1] = quadro todo
        x, y, cw, ch = x * w, y * h, cw * w, ch * h
    if cw <= 0 or ch <= 0:
        raise ValueError("caixa: largura e altura precisam ser positivas")
    return max(0.0, x), max(0.0, y), min(float(w), x + cw), min(float(h), y + ch)


class _Rastreador:
    """Segmenta quadro a quadro: no quadro com prompt do usuário usa os pontos/caixa dele; nos outros, o prompt
    sai da máscara do quadro vizinho já segmentado (caixa ampliada + ponto interno), escolhendo entre as 3
    máscaras do SAM a que tem melhor (IoU previsto + IoU com a máscara anterior)."""

    def __init__(self, sam: SAM, w: int, h: int, prompts: dict):
        self.sam, self.w, self.h = sam, w, h
        self.prompts = prompts  # quadro → {"pos": [...], "neg": [...], "caixa": (x0,y0,x1,y1)|None} (pixels)
        self.sx, self.sy = _LADO / w, _LADO / h

    def passo(self, i: int, img1024, anterior):
        """anterior: máscara booleana 256x256 do quadro vizinho (ou None). Devolve (logits 256x256 float16,
        máscara 256x256 bool, dados)."""
        emb = self.sam.codificar(img1024)
        pts, rot, caixa = [], [], None
        u = self.prompts.get(i)
        if u:
            pts += [(x * self.sx, y * self.sy) for x, y in u["pos"]]
            rot += [1] * len(u["pos"])
            pts += [(x * self.sx, y * self.sy) for x, y in u["neg"]]
            rot += [0] * len(u["neg"])
            if u.get("caixa"):
                x0, y0, x1, y1 = u["caixa"]
                caixa = (x0 * self.sx, y0 * self.sy, x1 * self.sx, y1 * self.sy)
        if anterior is not None and anterior.any() and not (u and (u["pos"] or u.get("caixa"))):
            # prompt automático a partir do quadro vizinho (coordenadas 256 → 1024: ×4). Caixa pelos percentis 1–99
            # dos pixels (fiapos soltos não a esticam) e ampliada 12%; a máscara nova é cortada a essa caixa + 15%,
            # senão um pedaço de outra coisa encostada faz a caixa crescer quadro a quadro (efeito bola de neve).
            ys, xs = np.nonzero(anterior)
            x0, x1 = np.percentile(xs, [1, 99])
            y0, y1 = np.percentile(ys, [1, 99])
            mx, my = max(3, (x1 - x0) * 0.12), max(3, (y1 - y0) * 0.12)
            caixa = (max(0, x0 - mx) * 4, max(0, y0 - my) * 4, min(256, x1 + 1 + mx) * 4, min(256, y1 + 1 + my) * 4)
            px, py = _ponto_interno(anterior)
            pts.insert(0, (px * 4, py * 4))
            rot.insert(0, 1)
        if not pts and caixa is None:
            return None, np.zeros((256, 256), bool), {"perdido": True}
        if not pts:
            pts, rot = [(0.0, 0.0)], [-10]  # só caixa: um ponto de preenchimento (ignorado pelo modelo)
        logits, iou, obj = self.sam.decodificar(emb, pts, rot, caixa)
        if caixa is not None:  # nada muito fora da caixa do prompt
            bx0, by0, bx1, by1 = (v / 4 for v in caixa)
            fx, fy = (bx1 - bx0) * 0.15 + 2, (by1 - by0) * 0.15 + 2
            fora = np.ones((256, 256), bool)
            fora[max(0, int(by0 - fy)):int(np.ceil(by1 + fy)), max(0, int(bx0 - fx)):int(np.ceil(bx1 + fx))] = False
            logits = np.where(fora[None], np.minimum(logits, -1.0), logits)
        mascaras = logits > 0
        if anterior is not None and anterior.any():
            notas = [float(iou[k]) + _iou(mascaras[k], anterior) for k in range(3)]
        else:
            notas = [float(iou[k]) for k in range(3)]
        k = int(np.argmax(notas))
        m = mascaras[k]
        dados = {"iou_previsto": round(float(iou[k]), 3), "objeto": round(obj, 2)}
        if obj <= 0 or not m.any():
            dados["perdido"] = True
            return None, np.zeros((256, 256), bool), dados
        return logits[k].astype(np.float16), m, dados


def _mascara_cheia(logits, w: int, h: int):
    """logits 256x256 (espaço 1024x1024 esticado sobre o quadro) → máscara uint8 0/255 no tamanho do vídeo."""
    if logits is None:
        return np.zeros((h, w), np.uint8)
    return np.where(_redim(logits.astype(np.float32), h, w) > 0, 255, 0).astype(np.uint8)


def _sobrepor(img, mascara, cor=(255, 40, 160), opacidade: float = 0.45, contorno: int = 2):
    """Imagem com a máscara pintada por cima e contorno — para conferir a segmentação."""
    m = mascara > 127
    out = img.astype(np.float32).copy()
    c = np.array(cor, np.float32)
    out[m] = out[m] * (1 - opacidade) + c * opacidade
    if contorno:
        borda = _dilatar(m, contorno) & ~_erodir(m, contorno)
        out[borda] = c
    return np.clip(out, 0, 255).astype(np.uint8)


def _gravar_imagem(img, destino: Path, largura: int | None = 640) -> Path:
    h, w = img.shape[:2]
    vf = ["-vf", f"scale={largura}:-2"] if largura and w > largura else []
    cmd = core.FFMPEG + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "-"] + vf + \
        ["-frames:v", "1", "-q:v", "3", str(destino)]
    r = subprocess.run(cmd, input=np.ascontiguousarray(img).tobytes(), capture_output=True)
    if r.returncode != 0:
        raise core.ErroFFmpeg(r.stderr.decode(errors="replace")[-2000:])
    return destino


# ---------------------------------------------------------------- máscaras (cache por documento)
MASCARAS_DIR = core.CACHE_DIR / "mascaras"
MASCARAS_VALIDADE_S = 7 * 86400  # máscaras sem uso há 7 dias são apagadas


def pasta_mascaras(doc: Documento) -> Path:
    """Pasta das máscaras do documento: ~/.cache/ai-editor/mascaras/<hash do caminho do original>/.
    Fica FORA de `.versoes/` de propósito: `desfazer` da última versão apaga aquela pasta inteira, e a máscara
    tem que sobreviver para tentar outro método de remoção."""
    chave = hashlib.sha256(str(doc.original).encode()).hexdigest()[:16]
    return MASCARAS_DIR / chave


def _limpar_mascaras_velhas() -> None:
    agora = time.time()
    for f in core.CACHE_DIR.glob("ai-editor-mascara-*.f16"):  # temporário de segmentação interrompida
        try:
            if agora - f.stat().st_mtime > 3600:
                f.unlink()
        except OSError:
            pass
    if not MASCARAS_DIR.is_dir():
        return
    for pasta in MASCARAS_DIR.iterdir():
        try:
            for f in pasta.glob(".obj*.parcial.mkv"):  # processo morto no meio da segmentação
                if agora - f.stat().st_mtime > 3600:
                    f.unlink()
            arqs = list(pasta.iterdir())
            if all(agora - a.stat().st_mtime > MASCARAS_VALIDADE_S for a in arqs):
                for a in arqs:
                    a.unlink()
                pasta.rmdir()
        except OSError:
            pass


# ---- de onde veio a máscara (para não aplicar uma máscara velha num vídeo cuja linha do tempo mudou)
_LINHAS_TEMPO: dict[tuple, dict] = {}


def _identidade(arq: Path) -> dict:
    """Tamanho, mtime e hash parcial (tamanho + 1º e último MiB) do arquivo: barato até em vídeos grandes."""
    st = arq.stat()
    h = hashlib.sha256(str(st.st_size).encode())
    with open(arq, "rb") as f:
        h.update(f.read(1 << 20))
        if st.st_size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return {"tamanho": st.st_size, "mtime": st.st_mtime_ns, "hash": h.hexdigest()[:32]}


def _linha_do_tempo(arq: Path, info: dict) -> dict:
    """O que uma edição SÓ de quadros (cor, destacar, remover...) não muda: nº de quadros, atraso e duração do vídeo e
    os pacotes do áudio (copiado sem reencode nessas edições; MD5 dos pacotes da 1ª faixa)."""
    ident = _identidade(arq)
    chave = (str(arq), ident["tamanho"], ident["mtime"])
    if chave in _LINHAS_TEMPO:
        return _LINHAS_TEMPO[chave]
    md5 = None
    if info.get("auds"):
        r = subprocess.run(core.FFMPEG + ["-i", str(arq), "-map", "0:a:0", "-c", "copy", "-f", "md5", "-"],
                           capture_output=True, text=True)
        if r.returncode == 0 and "MD5=" in r.stdout:
            md5 = r.stdout.strip().split("MD5=", 1)[1][:32]
    lt = {"quadros": _total_quadros(info), "taxa": str(info["taxa"]), "atraso": round(info["atraso_video"], 4),
          "dur_video": round(_duracao_video(info), 4), "audio_md5": md5}
    _LINHAS_TEMPO[chave] = lt
    return lt


def _assinatura_fonte(entrada: Path, info: dict) -> dict:
    """Campos do .json da máscara que identificam o arquivo de onde ela saiu (ver `_conferir_mascara`)."""
    return {"fonte": str(entrada), "fonte_id": _identidade(entrada), "linha_tempo": _linha_do_tempo(entrada, info)}


def listar_mascaras(doc: Documento) -> list[dict]:
    p = pasta_mascaras(doc)
    out = []
    for j in sorted(p.glob("obj*.json"), key=lambda x: int(re.sub(r"\D", "", x.stem) or 0)) if p.is_dir() else []:
        try:
            d = json.loads(j.read_text())
        except (OSError, ValueError):
            continue
        if (p / d.get("arquivo", "")).is_file():
            out.append({k: d[k] for k in ("id", "inicio", "fim", "quadros", "tamanho", "fonte", "descricao",
                                           "quadros_perdidos", "criada") if k in d})
    return out


def ler_mascara(doc: Documento, mascara: str | None = None) -> dict:
    """Metadados da máscara `mascara` ('obj1'... ou só o número); padrão: a mais recente."""
    todas = listar_mascaras(doc)
    if not todas:
        raise ValueError("não há máscara neste vídeo: use `segmentar_objeto` primeiro")
    if mascara in (None, "", "ultima", "última"):
        mid = todas[-1]["id"]
    else:
        mid = str(mascara).strip().lower()
        mid = mid if mid.startswith("obj") else f"obj{int(mid)}"
    j = pasta_mascaras(doc) / f"{mid}.json"
    if not j.is_file():
        raise ValueError(f"máscara {mid!r} não existe; disponíveis: {', '.join(m['id'] for m in todas)}")
    d = json.loads(j.read_text())
    d["caminho"] = str(pasta_mascaras(doc) / d["arquivo"])
    for a in (j, Path(d["caminho"])):  # em uso: adia a limpeza automática
        try:
            os.utime(a)
        except OSError:
            pass
    return d


def _conferir_mascara(m: dict, info: dict, entrada: Path) -> list[str]:
    """A máscara vale para o vídeo atual? ERRO se o tamanho/taxa mudou ou se a linha do tempo mudou desde que ela foi
    calculada (corte, velocidade, `salvar` de uma versão cortada, `desfazer` + outra edição...). Aceita outro arquivo
    (outra versão, ou o original depois de `salvar`) só se as edições entre eles foram SÓ de quadros: mesmo nº de
    quadros, mesmo atraso e mesmos pacotes de áudio. Devolve avisos."""
    av_tam = []
    if (m["largura"] - m["largura"] % 2, m["altura"] - m["altura"] % 2) == (info["w"], info["h"]) != \
            (m["largura"], m["altura"]):
        av_tam = [f"máscara {m['id']} feita em {m['largura']}x{m['altura']}, ajustada para {info['w']}x{info['h']} "
                  f"(dimensão ímpar arredondada pela versão anterior)"]
    elif (m["largura"], m["altura"]) != (info["w"], info["h"]):
        raise ValueError(f"a máscara {m['id']} foi feita num vídeo {m['largura']}x{m['altura']} e o atual é "
                         f"{info['w']}x{info['h']}: segmente de novo")
    if str(m.get("taxa")) != str(info["taxa"]) and abs(float(m["fps"]) - _fps(info)) > 1e-3:
        raise ValueError(f"a máscara {m['id']} foi feita a {float(m['fps']):g} fps e o vídeo atual tem "
                         f"{_fps(info):g}: segmente de novo")
    if not m.get("fonte_id") or not m.get("linha_tempo"):
        raise ValueError(f"a máscara {m['id']} não registra de que arquivo veio (formato antigo): segmente de novo")
    if m["quadro_inicial"] + m["quadros"] > _total_quadros(info):
        raise ValueError(f"a máscara {m['id']} passa do fim do vídeo atual (a linha do tempo mudou?): "
                         f"segmente de novo")
    try:
        mesmo = _identidade(entrada)["hash"] == m["fonte_id"]["hash"]
    except OSError:
        mesmo = False
    if mesmo:
        return av_tam
    antes, agora = m["linha_tempo"], _linha_do_tempo(entrada, info)
    mudou = [k for k in ("quadros", "taxa") if str(antes.get(k)) != str(agora.get(k))]
    if abs(float(antes.get("atraso", 0)) - float(agora["atraso"])) > 0.5 / _fps(info):
        mudou.append("atraso")
    if antes.get("audio_md5") != agora.get("audio_md5"):
        mudou.append("áudio")
    if mudou:
        raise ValueError(f"a linha do tempo mudou desde que a máscara {m['id']} foi calculada em "
                         f"{Path(m['fonte']).name} ({', '.join(mudou)} diferente: corte, velocidade ou salvar de uma "
                         f"versão editada?): segmente de novo")
    if agora.get("audio_md5"):
        return av_tam + [f"máscara {m['id']} calculada em {Path(m['fonte']).name}, aplicada em {entrada.name}: só a imagem "
                f"mudou entre os dois (mesmos quadros e mesmo áudio)"]
    return av_tam + [f"máscara {m['id']} calculada em {Path(m['fonte']).name}, aplicada em {entrada.name}: mesmo nº de "
            f"quadros, mas sem áudio para confirmar que a linha do tempo é a mesma; confira com `destacar_objeto`"]


SEGMENTAR_MAX_SEM_TRECHO = 300  # quadros; acima disso, sem `inicio`/`fim`, pede confirmar=True
_SAM_S_POR_QUADRO = (1.5, 3.5)  # medido na máquina alvo (720p–1080p, 4 threads)


def segmentar(doc: Documento, tempo: float, pontos=None, pontos_negativos=None, caixa=None,
              inicio: float | None = None, fim: float | None = None, extras: list[dict] | None = None,
              modelo: str = MODELO_SAM_PADRAO, passo_quadros: int = 1, confirmar: bool = False, progresso=None,
              cancelar=None, etapa=None) -> dict:
    """Segmenta um objeto e o rastreia de `inicio` a `fim` (sem os dois: o vídeo todo, que acima de
    SEGMENTAR_MAX_SEM_TRECHO quadros exige confirmar=True — dá erro com a estimativa de tempo).

    Prompt no quadro exibido em `tempo` (que tem de estar dentro de [inicio, fim]): `pontos` (positivos, no objeto),
    `pontos_negativos` (fora dele) e/ou `caixa` [x, y, largura, altura]. Coordenadas: de preferência FRAÇÕES 0–1 do
    quadro (independem da miniatura); pixels do vídeo exibido também valem. `extras`: prompts em outros quadros
    [{tempo, pontos, pontos_negativos, caixa}] (para corrigir onde o rastreamento errou).
    A propagação vai para a frente e para trás a partir do primeiro quadro com prompt; nos quadros sem prompt, o
    prompt sai da máscara do quadro vizinho (caixa ampliada + ponto interno). `passo_quadros`=k roda o SAM a cada k
    quadros (os do meio repetem a máscara anterior; k vezes mais rápido, menos preciso).
    Grava a máscara (FFV1 cinza) + .json em ~/.cache/ai-editor/mascaras/<hash do original>/ e devolve resumo +
    quadros de conferência."""
    if np is None:
        raise IAIndisponivel(_dica_ia())
    _ort()
    with trava_ia(cancelar, etapa):
        return _segmentar(doc, tempo, pontos, pontos_negativos, caixa, inicio, fim, extras, modelo, passo_quadros,
                          confirmar, progresso, cancelar, etapa)


def _segmentar(doc, tempo, pontos, pontos_negativos, caixa, inicio, fim, extras, modelo, passo_quadros, confirmar,
               progresso, cancelar, etapa) -> dict:
    cancelar = cancelar or (lambda: False)
    etapa = etapa or (lambda _t: None)
    prog = progresso or (lambda _f: None)
    entrada = doc.atual()
    info = core._midia(entrada)
    w, h = info["w"], info["h"]
    total = _total_quadros(info)
    a = max(0.0, inicio or 0.0)
    i0 = min(total - 1, _primeiro_quadro_desde(info, a))
    i1 = total if fim is None else max(i0 + 1, min(total, _primeiro_quadro_desde(info, fim)))
    n = i1 - i0
    passo_quadros = max(1, int(passo_quadros or 1))
    if inicio is None and fim is None and n > SEGMENTAR_MAX_SEM_TRECHO and not confirmar:
        k = n / passo_quadros
        raise ValueError(f"sem `inicio`/`fim` o rastreamento cobre o vídeo todo: {n} quadros, ~"
                         f"{k * _SAM_S_POR_QUADRO[0] / 60:.0f}–{k * _SAM_S_POR_QUADRO[1] / 60:.0f} min em CPU. Indique o "
                         f"trecho onde o objeto aparece (inicio/fim) ou passe confirmar=True")

    prompts: dict[int, dict] = {}
    for p in [{"tempo": tempo, "pontos": pontos, "pontos_negativos": pontos_negativos, "caixa": caixa}] + \
            list(extras or []):
        pos = _prompt_px(p.get("pontos"), w, h, "pontos")
        neg = _prompt_px(p.get("pontos_negativos"), w, h, "pontos_negativos")
        cx = _caixa_px(p.get("caixa"), w, h)
        if not pos and not cx and not neg:
            continue
        if not pos and not cx and not prompts:
            raise ValueError("o primeiro prompt precisa de pelo menos um ponto positivo ou uma caixa")
        t = float(p.get("tempo") if p.get("tempo") is not None else tempo)
        q = _quadro_em(info, t)
        if not i0 <= q < i1:
            raise ValueError(f"o tempo do prompt ({fmt_tempo(t)}) está fora do trecho a rastrear "
                             f"({fmt_tempo(_tempo_do_quadro(info, i0))} → {fmt_tempo(_tempo_do_quadro(info, i1))}): "
                             f"ajuste `tempo` ou `inicio`/`fim`")
        d = prompts.setdefault(q, {"pos": [], "neg": [], "caixa": None})
        d["pos"] += pos
        d["neg"] += neg
        d["caixa"] = cx or d["caixa"]
    if not prompts:
        raise ValueError("indique o objeto: `pontos` (no objeto) e/ou `caixa` [x, y, largura, altura]")
    ip = min(prompts)

    etapa(f"modelo {modelo}")
    sam_pronto = modelo_pronto(modelo)
    if not (sam_pronto and _sessao_carregada(_pasta_modelo(modelo) / "vision_encoder.onnx")):
        esperar_memoria(cancelar=cancelar, aviso=etapa)
    sam = SAM(modelo, (lambda f: prog(0.05 * f)) if not sam_pronto else None, cancelar)
    prog(0.05)

    _limpar_mascaras_velhas()
    pasta = pasta_mascaras(doc)
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "original.txt").write_text(str(doc.original))
    existentes = [int(re.sub(r"\D", "", j.stem) or 0) for j in pasta.glob("obj*.json")]
    mid = f"obj{max(existentes, default=0) + 1}"
    final = pasta / f"{mid}.mkv"
    tmp = pasta / f".{mid}.parcial.mkv"
    pw = min(640, w)
    ph = max(2, int(round(pw * h / w / 2)) * 2)
    alvo_prev = sorted({i0, ip, (i0 + i1 - 1) // 2, i1 - 1})
    previas: dict[int, object] = {}
    areas = np.zeros(n, np.float32)
    caixas: list = [None] * n
    perdidos: list[int] = []
    rast = _Rastreador(sam, w, h, prompts)
    feitos = [0]
    t_ini = time.monotonic()

    def contar():
        feitos[0] += 1
        prog(0.05 + 0.93 * feitos[0] / n)
        if feitos[0] % 5 == 0:
            _pausa_memoria(cancelar, etapa)
        if feitos[0] % 5 == 0 or feitos[0] == n:
            seg = (time.monotonic() - t_ini) / feitos[0]
            etapa(f"segmentando quadro {feitos[0]}/{n} ({seg:.1f} s/quadro, faltam ~{seg * (n - feitos[0]):.0f} s)")

    def registrar(i, logits, m256, img1024, dados):
        k = i - i0
        if dados.get("perdido"):
            perdidos.append(i)
        if m256.any():
            x0, y0, x1, y1 = _caixa_mascara(m256)
            caixas[k] = [round(x0 * w / 256), round(y0 * h / 256), round((x1 + 1) * w / 256),
                         round((y1 + 1) * h / 256)]
            areas[k] = m256.mean()
        if i in alvo_prev:
            img = _redim(img1024, ph, pw)
            mp = _mascara_cheia(logits, pw, ph)
            previas[i] = _sobrepor(img, mp)

    # ---- para trás (do primeiro prompt até o início do trecho), em blocos de até 30 quadros
    n_tras = ip - i0
    tras = None
    tras_arq = None
    ultimo_logits = None
    semente = None  # resultado do quadro do prompt (calculado na ida para trás, reaproveitado na ida para a frente)
    try:
        if n_tras:
            tras_arq = tempfile.NamedTemporaryFile(prefix="ai-editor-mascara-", suffix=".f16", delete=False,
                                                   dir=core.CACHE_DIR if core.CACHE_DIR.is_dir() else None)
            tras_arq.close()
            tras = np.memmap(tras_arq.name, dtype=np.float16, mode="w+", shape=(n_tras, 256, 256))
            anterior = None
            fim_bloco = ip + 1  # inclui o quadro do prompt (ponto de partida; não é gravado deste lado)
            while fim_bloco > i0:
                ini_bloco = max(i0, fim_bloco - 30)
                leitor, _ = _abrir_video(entrada, info, ini_bloco, fim_bloco - ini_bloco, (_LADO, _LADO))
                bloco = []
                try:
                    while (q := leitor.ler()) is not None:
                        bloco.append(q)
                finally:
                    leitor.fechar()
                for j in range(len(bloco) - 1, -1, -1):
                    i = ini_bloco + j
                    if cancelar():
                        raise Cancelado("segmentação cancelada")
                    if i == ip:
                        if anterior is None:
                            semente = rast.passo(i, bloco[j], None)
                            anterior = semente[1]
                        continue
                    if (ip - i) % passo_quadros and ultimo_logits is not None:
                        logits, m256, dados = ultimo_logits, anterior, {}
                    else:
                        logits, m256, dados = rast.passo(i, bloco[j], anterior)
                    tras[i - i0] = logits if logits is not None else np.float16(-1)
                    registrar(i, logits, m256, bloco[j], dados)
                    if m256.any():
                        anterior = m256
                    ultimo_logits = logits
                    contar()
                fim_bloco = ini_bloco
                del bloco

        # ---- grava a máscara: primeiro os quadros de trás (em ordem), depois segue para a frente
        esc = _Escritor(core.FFMPEG + ["-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-r", info["taxa"],
                                       "-i", "-", "-c:v", "ffv1", "-level", "3", "-g", "1", "-threads", "2",
                                       str(tmp)])
        try:
            for k in range(n_tras):
                esc.escrever(_mascara_cheia(np.asarray(tras[k]), w, h))
            leitor, _ = _abrir_video(entrada, info, ip, i1 - ip, (_LADO, _LADO))
            anterior, ultimo_logits = None, None
            try:
                i = ip
                while i < i1 and (q := leitor.ler()) is not None:
                    if cancelar():
                        raise Cancelado("segmentação cancelada")
                    if i == ip and semente is not None:
                        logits, m256, dados = semente
                    elif (i - ip) % passo_quadros and ultimo_logits is not None and i not in prompts:
                        logits, m256, dados = ultimo_logits, anterior, {}
                    else:
                        logits, m256, dados = rast.passo(i, q, anterior)
                    registrar(i, logits, m256, q, dados)
                    esc.escrever(_mascara_cheia(logits, w, h))
                    if m256.any():
                        anterior = m256
                    ultimo_logits = logits
                    contar()
                    i += 1
                lidos = i - ip
            finally:
                leitor.fechar()
            for _ in range(i1 - ip - lidos):  # o vídeo acabou antes do previsto: completa com máscara vazia
                esc.escrever(np.zeros((h, w), np.uint8))
            esc.fechar()
        except BaseException:
            esc.abortar()
            raise
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if tras is not None:
            del tras
        if tras_arq is not None:
            Path(tras_arq.name).unlink(missing_ok=True)

    os.replace(tmp, final)
    desc_prompt = []
    for q, d in sorted(prompts.items()):
        partes = []
        if d["pos"]:
            partes.append(f"{len(d['pos'])} ponto(s)")
        if d["neg"]:
            partes.append(f"{len(d['neg'])} negativo(s)")
        if d["caixa"]:
            partes.append("caixa")
        desc_prompt.append(f"{fmt_tempo(_tempo_do_quadro(info, q))}: {' + '.join(partes)}")
    meta = {
        "id": mid, "arquivo": final.name, **_assinatura_fonte(entrada, info), "modelo": modelo,
        "largura": w, "altura": h, "fps": _fps(info), "taxa": info["taxa"],
        "quadro_inicial": i0, "quadros": n,
        "inicio": fmt_tempo(_tempo_do_quadro(info, i0)), "fim": fmt_tempo(_tempo_do_quadro(info, i1)),
        "inicio_s": _tempo_do_quadro(info, i0), "fim_s": _tempo_do_quadro(info, i1),
        "prompts": {str(q): {"pos": d["pos"], "neg": d["neg"], "caixa": d["caixa"]} for q, d in prompts.items()},
        "passo_quadros": passo_quadros, "caixas": caixas,
        "areas": [round(float(x), 4) for x in areas],
        "quadros_perdidos": len(perdidos),
        "descricao": f"{mid}: {'; '.join(desc_prompt)} ({n} quadros)",
        "criada": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "segundos": round(time.monotonic() - t_ini, 1),
    }
    (pasta / f"{mid}.json").write_text(json.dumps(meta, ensure_ascii=False))
    # quadros de conferência (contorno sobre o vídeo)
    imgs = []
    pv = core.RUNTIME_DIR / "objetos"
    pv.mkdir(parents=True, exist_ok=True)
    for i in sorted(previas):
        dest = pv / f"{pasta.name[:8]}-{mid}-q{i}.jpg"
        _gravar_imagem(previas[i], dest, None)
        imgs.append({"tempo": fmt_tempo(_tempo_do_quadro(info, i)), "imagem": str(dest)})
    prog(1.0)
    vazios = int((areas == 0).sum())
    r = {"mascara": mid, "trecho": f"{meta['inicio']} → {meta['fim']}", "quadros": n,
         "quadros_sem_objeto": vazios, "area_media": f"{100 * float(areas[areas > 0].mean() if vazios < n else 0):.1f}%",
         "tempo_s": meta["segundos"], "por_quadro_s": round(meta["segundos"] / max(1, feitos[0]), 2),
         "arquivo_mascara": str(final), "previas": imgs}
    if vazios:
        perd = [fmt_tempo(_tempo_do_quadro(info, i0 + k)) for k in np.flatnonzero(areas == 0)[:8]]
        r["aviso"] = (f"objeto não encontrado em {vazios} quadro(s) (ex.: {', '.join(perd)}); se não saiu de cena, "
                      f"use `ajustar_mascara` com pontos nesses tempos")
    return r


# ---------------------------------------------------------------- aplicar a máscara (versões e exportação)
_FORMATO_OVERLAY = {  # pix_fmt da origem → (format do overlay, pix_fmt com alfa da camada processada)
    "yuv420p": ("yuv420", "yuva420p"), "yuvj420p": ("yuv420", "yuva420p"),
    "yuv422p": ("yuv422", "yuva422p"), "yuvj422p": ("yuv422", "yuva422p"),
    "yuv444p": ("yuv444", "yuva444p"), "yuvj444p": ("yuv444", "yuva444p"),
    "yuv420p10le": ("yuv420p10", "yuva420p10le"), "yuv422p10le": ("yuv422p10", "yuva422p10le"),
    "yuv444p10le": ("yuv444p10", "yuva444p10le"),
}


def _cmd_versao(info: dict, entrada: Path, saida: Path, i0: int, n: int) -> list[str]:
    """ffmpeg da versão: o vídeo INTEIRO vem da entrada no caminho yuv nativo (sem passar por RGB; 10 bits e matriz
    preservados) e só o trecho [i0, i0+n) é trocado pelos quadros processados (rgb24 pelo stdin), convertidos com a
    MESMA matriz/faixa da origem e postos por cima com `overlay` no tempo certo: `settb=AVTB,setpts` põe o quadro k
    em t(i0+k) − 1/4 de quadro (o `-itsoffset` não serve: o ffmpeg o desconta como início da entrada; e na base de
    tempo 1/fps o 1/4 de quadro some no arredondamento). Quadros em RGBA: o escritor recebe n+1 quadros, o último
    TRANSPARENTE, senão o overlay solta o último quadro do trecho ao ver o fim da entrada (e `enable=` não serve: o
    `t` dele segue o quadro mais novo da 2ª entrada). Antes do 1º quadro e depois do fim (`eof_action=pass`) a
    origem passa intacta. `-fps_mode passthrough` mantém os tempos da origem (start_time do vídeo, VFR).
    Áudio/legendas copiados."""
    fps = _fps(info)
    mat, faixa = _cor(info)
    t0 = _tempo_do_quadro(info, i0)
    fmt_ov, pix_camada = _FORMATO_OVERLAY.get(info.get("pix_fmt", ""), ("auto", "yuva444p"))
    if fmt_ov == "auto" and core._10bits(info.get("pix_fmt", "")):
        fmt_ov, pix_camada = "yuv420p10", "yuva420p10le"
    base = f"[1:V:0]{info['pre']}[base]" if info.get("pre") else "[1:V:0]null[base]"
    grafo = [base,
             f"[0:v]settb=AVTB,setpts=PTS-STARTPTS+{max(0.0, t0 - 0.25 / fps):.6f}/TB,"
             f"scale=out_color_matrix={mat}:out_range={faixa}:flags={_FLAGS_GRAVAR},format={pix_camada}[proc]",
             f"[base][proc]overlay=eof_action=pass:repeatlast=0:format={fmt_ov},setsar=1"
             + (f",{core._PAR_FILTRO}" if info["w"] % 2 or info["h"] % 2 else "") + "[vo]"]
    return (core.FFMPEG + ["-progress", "pipe:1", "-nostats",
                           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{info['w']}x{info['h']}",
                           "-r", info["taxa"], "-i", "-"]
            + info["dec"] + ["-i", str(entrada), "-filter_complex", ";".join(grafo), "-map", "[vo]"]
            + core._mapas_copia(info, entrada, saida, "1") + ["-fps_mode", "passthrough"]
            + core._args_video_edicao(saida.suffix, info) + core._tags_faixa(info["v"], "s:v:0")
            + ["-map_metadata", "1"] + core._faststart(saida) + [str(saida)])


# ---- versões gravadas por tarefa: trava do documento + reserva do vNNN
_ORFAO_S = 600  # vNNN sem registro no histórico e parado há 10 min = sobra de processo morto


def _pid_vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def _gravando_versao(doc: Documento, descricao: str):
    """Trava do documento enquanto a tarefa grava uma versão: `.versoes/.trava` criado com O_EXCL (pid, thread,
    tarefa). `Documento.proxima_saida/registrar/desfazer/salvar/descartar` (core) recusam com
    "tarefa X está gravando uma versão" enquanto ela existir e o dono estiver vivo; trava de processo/thread morto
    é ignorada e substituída."""
    doc.pasta.mkdir(exist_ok=True)
    trava = doc.pasta / ".trava"
    t = getattr(_IA_LOCAL, "tarefa", None)
    dados = {"pid": os.getpid(), "thread": threading.get_ident(), "tarefa": t.id if t else "direta",
             "descricao": descricao, "desde": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for _ in range(3):
        try:
            fd = os.open(trava, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            outra = core.Documento._ler_trava(trava)
            if outra is None:  # dono morto (ou arquivo ilegível): substitui
                trava.unlink(missing_ok=True)
                continue
            raise RuntimeError(f"tarefa {outra.get('tarefa')} está gravando uma versão ({outra.get('descricao')}); "
                               f"espere terminar (`tarefas`) ou cancele") from None
        with os.fdopen(fd, "w") as f:
            json.dump(dados, f)
        break
    else:
        raise RuntimeError(f"não consegui travar {doc.pasta} para gravar a versão")
    try:
        yield
    finally:
        trava.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            doc.pasta.rmdir()  # só se ficou vazia (nenhuma versão)


def _limpar_versoes_orfas(doc: Documento) -> None:
    """vNNN.* na pasta de versões que não está no histórico e não muda há 10 min: sobra de processo morto."""
    if not doc.pasta.is_dir():
        return
    registrados = {v["arquivo"] for v in doc._ler()["versoes"]}
    agora = time.time()
    for f in doc.pasta.iterdir():
        if re.fullmatch(r"v\d{3,}\.\w+", f.name) and f.name not in registrados:
            with contextlib.suppress(OSError):
                if agora - f.stat().st_mtime > _ORFAO_S:
                    f.unlink()


def _reservar_versao(doc: Documento) -> Path:
    """Próximo vNNN livre, criado com O_EXCL (nenhuma outra edição pega o mesmo nome)."""
    ext = doc.atual().suffix.lower()
    ext = ext if ext in core._CONTEINERES_EDICAO else ".mp4"
    doc.pasta.mkdir(exist_ok=True)
    n = len(doc._ler()["versoes"]) + 1
    while True:
        p = doc.pasta / f"v{n:03d}{ext}"
        if not any(doc.pasta.glob(f"v{n:03d}.*")):
            try:
                os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
                return p
            except FileExistsError:
                pass
        n += 1


def _versao_com_mascara(doc: Documento, m: dict, fn, descricao: str, progresso=None, cancelar=None,
                        etapa=None, info: dict | None = None, avisos: list[str] | None = None,
                        detalhes: dict | None = None, vazio_tambem: bool = False) -> dict:
    """Nova versão: SÓ os quadros do trecho da máscara passam pelo Python — fn(quadro, mascara, k) (quadro uint8
    HxWx3, máscara uint8 HxW, k = índice no trecho) — e o resto do vídeo segue no ffmpeg pelo caminho yuv nativo
    (`_cmd_versao`). Durante a gravação o documento fica travado (`_gravando_versao`); se a versão atual mudar
    mesmo assim, o resultado é descartado."""
    cancelar = cancelar or (lambda: False)
    entrada = doc.atual()
    info = info or core._midia(entrada)
    avisos = list(avisos or []) + _conferir_mascara(m, info, entrada) + _avisos_cor(info)
    w, h = info["w"], info["h"]
    i0, n = m["quadro_inicial"], m["quadros"]
    dur = max(0.001, info["dur"])
    t_ini = time.monotonic()
    with _gravando_versao(doc, descricao):
        _limpar_versoes_orfas(doc)
        if doc.atual() != entrada:
            raise RuntimeError("a versão atual mudou antes de começar; tente de novo")
        saida = _reservar_versao(doc)
        cmd_esc = _cmd_versao(info, entrada, saida, i0, n)
        leitor, cmd_ler = _abrir_video(entrada, info, i0, n)
        lm = _Leitor(_cmd_ler_mascara(Path(m["caminho"]), (w, h)), w, h, 1)
        esc = _Escritor(cmd_esc, (lambda s: progresso(min(0.99, s / dur))) if progresso else None)
        try:
            ultimo = None
            rgba = np.full((h, w, 4), 255, np.uint8)
            for k in range(n):
                if cancelar():
                    raise Cancelado("cancelado")
                q = leitor.ler()
                if q is None:  # o vídeo acabou antes (VFR/arredondamento): repete o último quadro
                    if ultimo is None:
                        break
                    q = ultimo
                ultimo = q
                mk = lm.ler()
                if mk is not None and (vazio_tambem or mk.any()):
                    q = fn(q, mk, k)
                rgba[..., :3] = q
                esc.escrever(rgba)
                if etapa and k % 10 == 0:
                    etapa(f"quadro {k + 1}/{n} do trecho")
            esc.escrever(np.zeros((h, w, 4), np.uint8))  # quadro extra transparente: ver _cmd_versao
            leitor.fechar()
            lm.fechar()
            if etapa:
                etapa("gravando o resto do vídeo (sem reprocessar)")
            esc.fechar(cancelar)
            if not saida.exists() or saida.stat().st_size == 0:
                raise core.ErroFFmpeg(f"o ffmpeg terminou mas não gerou {saida.name}")
            if doc.atual() != entrada:
                raise RuntimeError("a versão atual mudou enquanto a tarefa rodava (desfazer/outra edição); "
                                   "resultado descartado — refaça")
            v = doc.registrar(saida, descricao, [cmd_ler, _cmd_ler_mascara(Path(m["caminho"]), (w, h)), cmd_esc])
        except BaseException:
            esc.abortar()
            leitor.fechar()
            lm.fechar()
            saida.unlink(missing_ok=True)
            raise
    v = dict(v)
    det = {"mascara": m["id"], "trecho": f"{m['inicio']} → {m['fim']}", "segundos": round(time.monotonic() - t_ini, 1)}
    det.update(detalhes or {})
    v["detalhes"] = det
    if avisos:
        v["avisos"] = avisos
    if progresso:
        progresso(1.0)
    return v


_CORES = {"preto": (0, 0, 0), "black": (0, 0, 0), "branco": (255, 255, 255), "white": (255, 255, 255),
          "verde": (0, 177, 64), "green": (0, 177, 64), "chroma": (0, 177, 64), "azul": (0, 71, 187),
          "blue": (0, 71, 187), "cinza": (128, 128, 128), "gray": (128, 128, 128), "grey": (128, 128, 128),
          "vermelho": (230, 30, 40), "red": (230, 30, 40), "amarelo": (255, 210, 0), "yellow": (255, 210, 0),
          "rosa": (255, 40, 160), "magenta": (255, 0, 255), "ciano": (0, 220, 255), "cyan": (0, 220, 255),
          "laranja": (255, 140, 0), "orange": (255, 140, 0)}


def parse_cor(cor) -> tuple[int, int, int]:
    """'#rrggbb', 'rrggbb', 'r,g,b' ou nome (preto, branco, verde [chroma key], azul, cinza, vermelho...)."""
    if isinstance(cor, (list, tuple)) and len(cor) == 3:
        return tuple(int(max(0, min(255, float(c)))) for c in cor)
    s = str(cor).strip().lower()
    if s in _CORES:
        return _CORES[s]
    hx = s.lstrip("#").removeprefix("0x")
    if re.fullmatch(r"[0-9a-f]{6}", hx):
        return int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    p = re.split(r"[,\s]+", s)
    if len(p) == 3 and all(re.fullmatch(r"\d+", x) for x in p):
        return tuple(min(255, int(x)) for x in p)
    raise ValueError(f"cor inválida: {cor!r} (use '#rrggbb', 'r,g,b' ou {', '.join(sorted(_CORES))})")


def _margem_padrao(w: int, h: int) -> int:
    return max(3, int(round(min(w, h) * 0.01)))


# ---- destacar (pré-visualização)
def destacar(doc: Documento, mascara: str | None = None, cor="rosa", opacidade: float = 0.4,
             contorno: int = 3, progresso=None, cancelar=None, etapa=None) -> dict:
    """Versão de conferência: objeto pintado por cima (cor/opacidade) com contorno. Depois, `desfazer`."""
    m = ler_mascara(doc, mascara)
    c = parse_cor(cor)
    op = max(0.0, min(1.0, float(opacidade)))
    return _versao_com_mascara(doc, m, lambda q, mk, k: _sobrepor(q, mk, c, op, max(0, int(contorno))),
                               f"destacar objeto {m['id']} ({m['inicio']}→{m['fim']})", progresso, cancelar, etapa)


# ---- fundo: desfocar / trocar
def fundo(doc: Documento, mascara: str | None = None, modo: str = "desfocar", intensidade: float = 20,
          cor="verde", imagem: str | None = None, suavizar: int = 2, progresso=None, cancelar=None,
          etapa=None) -> dict:
    """Mantém o objeto e muda o resto: modo 'desfocar' (intensidade = raio em px num vídeo 720p), 'cor'
    (fundo liso: 'verde' = chroma key), 'imagem' (arquivo; ajustado por recorte para cobrir o quadro) ou 'cinza'
    (fundo em preto e branco). Só no trecho da máscara."""
    m = ler_mascara(doc, mascara)
    w, h = m["largura"], m["altura"]
    modo = (modo or "desfocar").lower()
    r_suave = max(0, int(suavizar))
    if modo in ("desfocar", "borrar", "desfoque", "blur"):
        raio = max(1.0, float(intensidade) * min(w, h) / 720)

        def bg(q):
            return _borrar(q, raio)
        desc = f"fundo desfocado ({intensidade:g})"
    elif modo in ("cor", "liso", "chroma"):
        c = np.array(parse_cor(cor), np.float32)

        def bg(q):
            return c
        desc = f"fundo trocado pela cor {cor}"
    elif modo in ("cinza", "pb", "preto_e_branco"):
        def bg(q):
            y = q.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
            return y[..., None]
        desc = "fundo em preto e branco"
    elif modo == "imagem":
        if not imagem:
            raise ValueError("modo 'imagem' precisa de `imagem` (caminho do arquivo)")
        p = Path(imagem).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"imagem não encontrada: {p}")
        r = subprocess.run(core.FFMPEG + ["-i", str(p), "-frames:v", "1", "-vf",
                                          f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                                          f"format=rgb24", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True)
        if r.returncode != 0 or len(r.stdout) < w * h * 3:
            raise core.ErroFFmpeg(f"não consegui ler a imagem {p}: {r.stderr.decode(errors='replace')[-500:]}")
        fundo_img = np.frombuffer(r.stdout[: w * h * 3], np.uint8).reshape(h, w, 3).astype(np.float32)

        def bg(q):
            return fundo_img
        desc = f"fundo trocado pela imagem {p.name}"
    else:
        raise ValueError("modo: 'desfocar', 'cor', 'imagem' ou 'cinza'")

    def fn(q, mk, k):
        a = _suave(mk > 127, r_suave)[..., None]
        return np.clip(q * a + bg(q) * (1 - a), 0, 255).astype(np.uint8)
    return _versao_com_mascara(doc, m, fn, f"{desc}, objeto {m['id']} ({m['inicio']}→{m['fim']})",
                               progresso, cancelar, etapa, vazio_tambem=True)


# ---- recorte com transparência (exportação: arquivo novo)
_FORMATOS_RECORTE = {
    "webm": (".webm", "rgba", ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-crf", "30", "-b:v", "0",
                               "-row-mt", "1", "-deadline", "good", "-cpu-used", "4", "-auto-alt-ref", "0"],
             ["-c:a", "libopus", "-b:a", "160k"]),
    "mov": (".mov", "rgba", ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
                             "-vendor", "apl0"], ["-c:a", "pcm_s16le"]),
    "mascara": (".mp4", "gray", ["-c:v", "libx264", "-crf", "12", "-preset", "veryfast", "-pix_fmt", "yuv420p"],
                ["-c:a", "aac", "-b:a", "192k"]),
}
_PIX_RECORTE = {"webm": "yuva420p", "mov": "yuva444p10le", "prores": "yuva444p10le", "mascara": "yuv420p"}
_FORMATOS_RECORTE["prores"] = _FORMATOS_RECORTE["mov"]


def recortar(doc: Documento, mascara: str | None = None, formato: str = "webm", inverter: bool = False,
             audio: bool = True, destino: str | None = None, suavizar: int = 1, progresso=None, cancelar=None,
             etapa=None) -> dict:
    """Exporta (arquivo NOVO, não versão) só o trecho da máscara: 'webm' (VP9 com alfa), 'mov' (ProRes 4444 com
    alfa) ou 'mascara' (vídeo P&B da máscara, para outros editores). inverter=True → o objeto fica
    transparente e o resto visível. Nunca sobrescreve (mesmo esquema de `core.exportar`)."""
    cancelar = cancelar or (lambda: False)
    formato = (formato or "webm").lower()
    if formato not in _FORMATOS_RECORTE:
        raise ValueError(f"formato: {', '.join(_FORMATOS_RECORTE)}")
    ext, modo_pix, args_v, args_a = _FORMATOS_RECORTE[formato]
    m = ler_mascara(doc, mascara)
    entrada = doc.atual()
    info = core._midia(entrada)
    avisos = _conferir_mascara(m, info, entrada)
    w, h = info["w"], info["h"]
    i0, n = m["quadro_inicial"], m["quadros"]
    t_audio = _tempo_do_quadro(info, i0)  # áudio começa no tempo EXATO do 1º quadro (o -ss do vídeo é 1/4 antes)
    dur = n / _fps(info)
    mat, faixa = _cor(info)
    sufixo = {"webm": "recorte", "mov": "recorte", "prores": "recorte", "mascara": "mascara"}[formato]
    if inverter:
        sufixo = "mascara_invertida" if formato == "mascara" else "sem_objeto"
    final = core._reservar(core._caminho(doc.original, f"{sufixo}.{m['id']}", ext, destino, criar=True))
    tmp = core._temporario(final)
    cmd_esc = core.FFMPEG + ["-f", "rawvideo", "-pix_fmt", "rgba" if modo_pix == "rgba" else "gray",
                             "-s", f"{w}x{h}", "-r", info["taxa"], "-i", "-"]
    tem_audio = audio and bool(info["auds"])
    if tem_audio:
        cmd_esc += ["-ss", f"{t_audio:.6f}", "-t", f"{dur:.6f}", "-i", str(entrada), "-map", "1:a:0"] + args_a
    # rgb → yuv com a matriz/faixa da origem (e tags iguais); máscara: cinza puro, sempre faixa limitada BT.709
    if modo_pix == "rgba":
        vf = f"scale=out_color_matrix={mat}:out_range={faixa},format={_PIX_RECORTE[formato]}"
        tags = _tags_cor_saida(info)
    else:
        vf = f"scale=out_color_matrix=bt709:out_range=tv,format={_PIX_RECORTE[formato]}"
        tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    vf = ("scale=trunc(iw/2)*2:trunc(ih/2)*2," if w % 2 or h % 2 else "") + vf + ",setsar=1"
    cmd_esc += ["-map", "0:v", "-vf", vf] + args_v + tags
    cmd_esc += core._faststart(final) + ["-f", {".webm": "webm", ".mov": "mov", ".mp4": "mp4"}[ext], str(tmp)]
    leitor = _abrir_video(entrada, info, i0, n)[0] if modo_pix == "rgba" else None
    lm = _Leitor(_cmd_ler_mascara(Path(m["caminho"]), (w, h)), w, h, 1)
    esc = _Escritor(cmd_esc)
    try:
        for k in range(n):
            if cancelar():
                raise Cancelado("cancelado")
            mk = lm.ler()
            if mk is None:
                break
            a = mk if not inverter else 255 - mk
            if suavizar and suavizar > 0:
                a = np.clip(_suave(a > 127, int(suavizar)) * 255, 0, 255).astype(np.uint8)
            if leitor is None:
                esc.escrever(a)
            else:
                q = leitor.ler()
                if q is None:
                    break
                esc.escrever(np.dstack([q, a]))
            if progresso and k % 5 == 0:
                progresso(k / n)
        if leitor:
            leitor.fechar()
        lm.fechar()
        esc.fechar(cancelar)
        core._publicar(tmp, final)
    except BaseException:
        esc.abortar()
        if leitor:
            leitor.fechar()
        lm.fechar()
        tmp.unlink(missing_ok=True)
        core._soltar_reserva(final)
        raise
    if progresso:
        progresso(1.0)
    r = {"arquivo": str(final), "mascara": m["id"], "trecho": f"{m['inicio']} → {m['fim']}",
         "formato": formato, "objeto": "transparente" if inverter else "visível (fundo transparente)"}
    if formato == "mascara":
        r["objeto"] = "branco sobre preto"
    try:
        r["info"] = core.info_arquivo(final)
    except Exception:  # noqa: BLE001
        pass
    if w % 2 or h % 2:
        avisos = avisos + [f"dimensão ímpar ({w}x{h}): a saída foi arredondada para pares"]
    if avisos:
        r["avisos"] = avisos
    return r


# ---- remover o objeto (reconstruir o fundo)
_LAMA_LADO = 512
_LAMA_S_POR_QUADRO = 5.5  # medido: 4,4–5 s/quadro na máquina alvo (4 threads, carregada) + ~10–30 s de carga do modelo


def _lama_sessao(progresso=None, cancelar=None):
    """Sessão do LaMa (~10–30 s para carregar; fica em cache por alguns minutos)."""
    pasta = baixar_modelo("lama", progresso, cancelar)
    arq = pasta / "inpainting_lama_2025jan.onnx"
    if not _sessao_carregada(arq):
        esperar_memoria(cancelar=cancelar)
    return _sessao(arq)


def _janela_lama(caixa, W: int, H: int) -> tuple[int, int, int, int]:
    """Região (x0, y0, x1, y1) em volta do buraco para o LaMa: quadrada quando cabe, com contexto ≈ 0,8× o
    tamanho do buraco de cada lado (o modelo trabalha em 512x512)."""
    x0, y0, x1, y1 = caixa
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    lado = int(max(bw, bh) * 1.8 + 48)
    lado = max(lado, min(256, W, H))
    if lado <= min(W, H):
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        a = min(max(0, cx - lado // 2), W - lado)
        b = min(max(0, cy - lado // 2), H - lado)
        return a, b, a + lado, b + lado
    mx, my = int(bw * 0.4 + 24), int(bh * 0.4 + 24)
    return max(0, x0 - mx), max(0, y0 - my), min(W, x1 + 1 + mx), min(H, y1 + 1 + my)


def _lama_preencher(sessao, img, buraco):
    """Reconstrói `buraco` (bool HxW) de `img` (HxWx3) com o LaMa num recorte em volta; devolve float32."""
    out = img.astype(np.float32).copy()
    cx = _caixa_mascara(buraco)
    if cx is None:
        return out
    H, W = buraco.shape
    a, b, c, d = _janela_lama(cx, W, H)
    rec = out[b:d, a:c]
    mb = _dilatar(buraco[b:d, a:c], 3)
    ch, cw = rec.shape[:2]
    x = np.ascontiguousarray((_redim(rec, _LAMA_LADO, _LAMA_LADO) / 255.0).transpose(2, 0, 1)[None], np.float32)
    mk = (_redim(mb.astype(np.float32), _LAMA_LADO, _LAMA_LADO) > 0.01).astype(np.float32)[None, None]
    y = _usar(sessao).run(None, {"image": x, "mask": mk})[0][0].transpose(1, 2, 0)
    y = _redim(np.clip(y, 0, 255), ch, cw)
    alfa = _suave(mb, 2)[..., None]
    alfa = np.maximum(alfa, mb[..., None].astype(np.float32))
    out[b:d, a:c] = rec * (1 - alfa) + y * alfa
    return out


def _preencher_rapido_local(img, buraco):
    """_preencher_rapido só numa região em volta do buraco (bem mais rápido em quadros grandes)."""
    cx = _caixa_mascara(buraco)
    out = img.astype(np.float32).copy()
    if cx is None:
        return out
    H, W = buraco.shape
    x0, y0, x1, y1 = cx
    mx, my = (x1 - x0) + 16, (y1 - y0) + 16
    a, b, c, d = max(0, x0 - mx), max(0, y0 - my), min(W, x1 + 1 + mx), min(H, y1 + 1 + my)
    out[b:d, a:c] = _preencher_rapido(out[b:d, a:c], buraco[b:d, a:c])
    return out


def _plano_de_fundo(entrada: Path, info: dict, m: dict, margem: int, cancelar, progresso, etapa):
    """1ª passada (só o trecho): para cada pixel, o PRIMEIRO valor em que o fundo aparece (o objeto, dilatado,
    fica de fora) e quanto a câmera se mexe (mediana da diferença absoluta para o 1º quadro, nas áreas
    visíveis). Na 2ª passada o buraco recebe o valor visto mais recentemente (ver `remover`): o fundo vem do
    quadro mais próximo no tempo, o que evita "fantasmas" de coisas que passaram pelo fundo (uma média os teria)."""
    w, h = info["w"], info["h"]
    i0, n = m["quadro_inicial"], m["quadros"]
    plano = np.zeros((h, w, 3), np.uint8)
    visto = np.zeros((h, w), bool)
    uniao = np.zeros((h, w), bool)
    leitor, _ = _abrir_video(entrada, info, i0, n)
    lm = _Leitor(_cmd_ler_mascara(Path(m["caminho"]), (w, h)), w, h, 1)
    ref = ref_vis = None
    movimentos = []
    try:
        for k in range(n):
            if cancelar():
                raise Cancelado("cancelado")
            q, mk = leitor.ler(), lm.ler()
            if q is None or mk is None:
                break
            buraco = _dilatar(mk > 127, margem)
            uniao |= buraco
            vis = ~buraco
            novo = vis & ~visto
            plano[novo] = q[novo]
            visto |= novo
            if k % 5 == 0:
                peq = _reduzir(q, 8)
                pv = _reduzir(vis.astype(np.float32), 8) > 0.99
                if ref is None:
                    ref, ref_vis = peq, pv
                else:
                    ambos = pv & ref_vis
                    if ambos.sum() > 50:
                        movimentos.append(float(np.median(np.abs(peq - ref).mean(axis=2)[ambos])))
            if progresso and k % 5 == 0:
                progresso(k / n)
            if etapa and k % 15 == 0:
                etapa(f"plano de fundo: quadro {k + 1}/{n}")
    finally:
        leitor.fechar()
        lm.fechar()
    nunca = uniao & ~visto
    return plano.astype(np.float32), nunca, uniao, (max(movimentos) if movimentos else 0.0)


def remover(doc: Documento, mascara: str | None = None, metodo: str = "auto", margem: int | None = None,
            progresso=None, cancelar=None, etapa=None) -> dict:
    """Remove o objeto da máscara e reconstrói o que estava atrás (nova versão).
    metodo:
      - 'fundo' (câmera parada): o buraco recebe o fundo visto no quadro mais próximo no tempo (o objeto se
        mexendo revela o que está atrás); o que nunca aparece é preenchido UMA vez pelo LaMa e o mesmo remendo
        vale para todos os quadros (estável, sem tremulação, rápido);
      - 'lama' (câmera em movimento): LaMa em cada quadro, num recorte em volta do objeto (~5,5 s/quadro em CPU,
        mais ~10–30 s para carregar o modelo; pode tremular um pouco entre quadros);
      - 'rapido': sem modelo, as cores em volta escorrem para dentro (bom para objetos pequenos ou fundo liso);
      - 'auto': mede o movimento da câmera; parada → 'fundo'; em movimento → 'lama' se couber em ~10 min,
        senão 'rapido'.
    margem: pixels a mais em volta da máscara (padrão 1% do lado menor), cobre borda e borrão de movimento.
    Sem ONNX Runtime: 'auto' vira 'rapido' (com aviso) e 'fundo' preenche o que nunca aparece sem o LaMa."""
    with trava_ia(cancelar, etapa):
        return _remover(doc, mascara, metodo, margem, progresso, cancelar, etapa)


def _remover(doc, mascara, metodo, margem, progresso, cancelar, etapa) -> dict:
    cancelar = cancelar or (lambda: False)
    etapa = etapa or (lambda _t: None)
    prog = progresso or (lambda _f: None)
    metodo = (metodo or "auto").lower()
    if metodo not in ("auto", "fundo", "lama", "rapido", "rápido"):
        raise ValueError("metodo: 'auto', 'fundo', 'lama' ou 'rapido'")
    metodo = "rapido" if metodo == "rápido" else metodo
    avisos: list[str] = []
    try:
        _ort()
        tem_ia = True
    except IAIndisponivel:
        tem_ia = False
        if metodo == "lama":
            raise
        if metodo == "auto":
            metodo = "rapido"
            avisos.append("sem ONNX Runtime (IA opcional): usei o preenchimento rápido, sem modelo. Para 'fundo'/'lama' "
                          "com LaMa: " + _dica_ia())
    m = ler_mascara(doc, mascara)
    entrada = doc.atual()
    info = core._midia(entrada)
    w, h = info["w"], info["h"]
    margem = _margem_padrao(w, h) if margem is None else max(0, int(margem))
    detalhes: dict = {"margem_px": margem}
    n = m["quadros"]
    sessao = None
    if metodo in ("auto", "fundo"):
        etapa("analisando o fundo")
        plano, nunca, uniao, mov = _plano_de_fundo(entrada, info, m, margem, cancelar,
                                                   lambda f: prog(0.3 * f), etapa)
        detalhes["movimento_camera"] = round(mov, 2)
        if metodo == "auto":
            if mov <= 6.0:
                metodo = "fundo"
            elif n * _LAMA_S_POR_QUADRO <= 600:
                metodo = "lama"
            else:
                metodo = "rapido"
                avisos.append(f"câmera em movimento e trecho longo ({n} quadros; LaMa levaria "
                              f"~{n * _LAMA_S_POR_QUADRO / 60:.0f} min): usei o preenchimento rápido. Para mais "
                              f"qualidade, segmente trechos menores e use metodo='lama'")
        elif mov > 6.0:
            avisos.append(f"a câmera parece se mexer (diferença {mov:.1f}): o plano de fundo pode ficar "
                          f"desalinhado; prefira metodo='lama'")
        detalhes["metodo"] = metodo
        if metodo == "fundo":
            frac = float(nunca.sum() / max(1, uniao.sum()))
            detalhes["area_nunca_vista"] = f"{100 * frac:.1f}%"
            if nunca.any() and tem_ia:
                etapa("LaMa no plano de fundo (área que nunca aparece)")
                sessao = _lama_sessao(None, cancelar)
                plano = _lama_preencher(sessao, plano, nunca)
            elif nunca.any():
                avisos.append("sem ONNX Runtime: a área que nunca aparece foi preenchida sem o LaMa (borrão)")
                plano = _preencher_rapido_local(plano, nunca)
            prog(0.35)

            recente = plano  # fundo visto mais recentemente (começa com o primeiro visto + remendo do LaMa)

            def fn(q, mk, k):
                buraco = _dilatar(mk > 127, margem)
                vis = ~_dilatar(buraco, 2)
                recente[vis] = q[vis]
                a = np.maximum(_suave(buraco, 2), buraco)[..., None]
                return np.clip(q * (1 - a) + recente * a, 0, 255).astype(np.uint8)
            return _versao_com_mascara(doc, m, fn, f"remover objeto {m['id']} ({m['inicio']}→{m['fim']}, "
                                       f"plano de fundo)", lambda f: prog(0.35 + 0.65 * f), cancelar, etapa,
                                       info, avisos, detalhes)
    detalhes["metodo"] = metodo
    if metodo == "lama":
        sessao = _lama_sessao(None, cancelar)
        detalhes["estimativa"] = f"~{n * _LAMA_S_POR_QUADRO:.0f} s"
        t0 = time.monotonic()
        feitos = [0]

        def fn(q, mk, k):
            r = _lama_preencher(sessao, q, _dilatar(mk > 127, margem))
            feitos[0] += 1
            if feitos[0] % 3 == 0:
                _pausa_memoria(cancelar, etapa)
                s = (time.monotonic() - t0) / feitos[0]
                etapa(f"LaMa: quadro {k + 1}/{n} ({s:.1f} s/quadro)")
            return np.clip(r, 0, 255).astype(np.uint8)
        desc = "LaMa por quadro"
    else:
        def fn(q, mk, k):
            return np.clip(_preencher_rapido_local(q, _dilatar(mk > 127, margem)), 0, 255).astype(np.uint8)
        desc = "preenchimento rápido"
    base = 0.3 if "movimento_camera" in detalhes else 0.0
    return _versao_com_mascara(doc, m, fn, f"remover objeto {m['id']} ({m['inicio']}→{m['fim']}, {desc})",
                               lambda f: prog(base + (1 - base) * f), cancelar, etapa, info, avisos, detalhes)


def ajustar(doc: Documento, mascara: str | None = None, tempo: float | None = None, pontos=None,
            pontos_negativos=None, caixa=None, progresso=None, cancelar=None, etapa=None) -> dict:
    """Refaz a segmentação da máscara com os prompts originais + os novos (pontos positivos/negativos ou caixa no
    quadro exibido em `tempo`). Gera uma máscara NOVA (a antiga continua disponível)."""
    m = ler_mascara(doc, mascara)
    if tempo is None:
        raise ValueError("indique o `tempo` do quadro onde corrigir")
    if not (pontos or pontos_negativos or caixa):
        raise ValueError("indique `pontos` (o que faz parte do objeto), `pontos_negativos` (o que não faz) e/ou "
                         "`caixa`")
    info = core._midia(doc.atual())
    _conferir_mascara(m, info, doc.atual())
    q = _quadro_em(info, float(tempo))
    if not m["quadro_inicial"] <= q < m["quadro_inicial"] + m["quadros"]:
        raise ValueError(f"o tempo {fmt_tempo(float(tempo))} está fora da máscara {m['id']} ({m['inicio']} → "
                         f"{m['fim']}): para outro trecho, use `segmentar_objeto`")
    extras = []
    for q, p in sorted(m["prompts"].items(), key=lambda x: int(x[0])):
        t = _tempo_do_quadro(info, int(q)) + 0.5 / _fps(info)
        cx = p.get("caixa")
        extras.append({"tempo": t, "pontos": [list(v) for v in p.get("pos") or []] or None,
                       "pontos_negativos": [list(v) for v in p.get("neg") or []] or None,
                       "caixa": [cx[0], cx[1], cx[2] - cx[0], cx[3] - cx[1]] if cx else None})
    # coordenadas antigas já estão em pixels; se as novas vierem em frações, converte antes de juntar
    w, h = info["w"], info["h"]
    novo = {"tempo": tempo, "pontos": [list(v) for v in _prompt_px(pontos, w, h, "pontos")] or None,
            "pontos_negativos": [list(v) for v in _prompt_px(pontos_negativos, w, h, "pontos_negativos")] or None,
            "caixa": None}
    if caixa is not None:
        x0, y0, x1, y1 = _caixa_px(caixa, w, h)
        novo["caixa"] = [x0, y0, x1 - x0, y1 - y0]
    primeiro = extras[0]
    r = segmentar(doc, primeiro["tempo"], primeiro["pontos"], primeiro["pontos_negativos"], primeiro["caixa"],
                  inicio=m["inicio_s"],
                  fim=m["fim_s"] if m["quadro_inicial"] + m["quadros"] < _total_quadros(info) else None,
                  extras=extras[1:] + [novo], modelo=m.get("modelo", MODELO_SAM_PADRAO),
                  passo_quadros=m.get("passo_quadros", 1), progresso=progresso, cancelar=cancelar, etapa=etapa)
    r["ajuste_de"] = m["id"]
    return r
