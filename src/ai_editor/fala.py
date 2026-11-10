"""Fala (Fase 6): transcrição local com whisper.cpp, legendas (.srt/.vtt), gravar/embutir legenda e
edição pelo texto (achar onde uma frase é dita).

Tudo roda na máquina do usuário (nada vai para a nuvem):
  - motor: `whisper-cli` do whisper.cpp (MIT), localizado por `binarios` (sob demanda em
    ~/.local/share/ai-editor/bin). Se faltar, `python -m ai_editor.fala --instalar-whisper` compila o release
    fixado (WHISPER_VERSAO) a partir do código-fonte, conferindo o SHA-256 do tarball (precisa de g++ e make;
    cmake do sistema ou, se não houver, o pacote pip `cmake` via `uvx`).
  - modelos: ggml multilíngues do repositório oficial ggerganov/whisper.cpp no Hugging Face (MIT), baixados no
    primeiro uso para ~/.local/share/ai-editor/modelos/whisper/ com SHA-256 conferido, arquivo .parcial e
    renomeio atômico.
  - cache das transcrições: ~/.cache/ai-editor/transcricoes/<hash do arquivo + parâmetros>.json.

Sem Qt (usado pelo servidor MCP). Edições que mudam o vídeo (gravar/embutir legenda) usam o mecanismo de versões
de `core` (`_editar_quadros`, `_rodar_versao`): cada uma cria uma versão vNNN do documento.
"""
from __future__ import annotations

import contextlib
import difflib
import functools
import errno
import fcntl
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
import urllib.request
import wave
from pathlib import Path

from . import binarios, core
from .core import Documento, fmt_tempo

# ---------------------------------------------------------------- versões fixadas
WHISPER_VERSAO = "v1.9.5"
WHISPER_TARBALL = f"https://github.com/ggml-org/whisper.cpp/archive/refs/tags/{WHISPER_VERSAO}.tar.gz"
WHISPER_TARBALL_SHA256 = "ff1a9053feb509ff9d7729703355541ae9690073a6b1c40eb692c962e0dc1720"

# revisão (commit) fixada do repositório de modelos: a URL não muda mesmo se o repositório for atualizado
HF_REVISAO = "5359861c739e955e79d9a303bcbc70fb988958b1"
HF_BASE = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{HF_REVISAO}/"

# nome → (arquivo, tamanho em bytes, sha256, preset de alinhamento DTW). Todos multilíngues (português precisa).
MODELOS = {
    "tiny": ("ggml-tiny.bin", 77_691_713,
             "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21", "tiny"),
    "base": ("ggml-base.bin", 147_951_465,
             "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe", "base"),
    "small": ("ggml-small.bin", 487_601_967,
              "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b", "small"),
    # quantizados: menores e um pouco mais rápidos, qualidade quase igual
    "base-q5_1": ("ggml-base-q5_1.bin", 59_707_625,
                  "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898", "base"),
    "small-q5_1": ("ggml-small-q5_1.bin", 190_085_487,
                   "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb", "small"),
}
MODELO_PADRAO = "base"

MODELOS_WHISPER_DIR = binarios.MODELOS_DIR / "whisper"
CACHE_TRANSCRICOES = core.CACHE_DIR / "transcricoes"
_VERSAO_CACHE = 2  # muda se o formato/cálculo do resultado mudar (2: áudio atrasado em relação ao contêiner)
_CMAKE_PIP = "cmake==4.4.4"  # versão do pacote pip `cmake` (via uvx) com que o build foi testado

# idiomas que o Whisper conhece (lista de whisper.cpp/whisper.h, g_lang)
_IDIOMAS_WHISPER = set(
    "en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms cs ro da hu ta no th ur hr bg lt la "
    "mi ml cy sk te fa lv bn sr az sl kn et mk br eu is hy ne mn bs kk sq sw gl mr pa si km sn yo so af oc ka be "
    "tg sd gu am yi lo uz fo ht ps tk nn mt sa lb my bo tl mg as tt haw ln ha ba jw su yue".split())

# idioma (como o usuário/Claude escreve) → código do whisper
_IDIOMAS = {
    "auto": "auto", "automatico": "auto", "automático": "auto", "detectar": "auto", "": "auto",
    "pt": "pt", "pt-br": "pt", "pt_br": "pt", "pt-pt": "pt", "por": "pt", "portugues": "pt", "português": "pt",
    "en": "en", "eng": "en", "ingles": "en", "inglês": "en", "english": "en",
    "es": "es", "spa": "es", "espanhol": "es", "español": "es",
    "fr": "fr", "fra": "fr", "frances": "fr", "francês": "fr",
    "de": "de", "deu": "de", "alemao": "de", "alemão": "de",
    "it": "it", "ita": "it", "italiano": "it",
    "ja": "ja", "jpn": "ja", "japones": "ja", "japonês": "ja",
}
# código do whisper → ISO 639-2 (tag de idioma das faixas de legenda)
_ISO639_2 = {"pt": "por", "en": "eng", "es": "spa", "fr": "fra", "de": "deu", "it": "ita", "ja": "jpn",
             "zh": "zho", "ru": "rus", "ko": "kor", "nl": "nld", "ar": "ara", "hi": "hin", "pl": "pol",
             "tr": "tur", "uk": "ukr", "sv": "swe", "ca": "cat", "gl": "glg", "la": "lat"}


class FalaIndisponivel(RuntimeError):
    """Falta o motor (whisper-cli) ou o modelo; a mensagem diz como obter."""


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.0f} MB"


def norm_idioma(idioma: str | None) -> str:
    s = str(idioma or "auto").strip().lower()
    if s in _IDIOMAS:
        return _IDIOMAS[s]
    base = re.split(r"[-_]", s)[0]  # 'en-us' → 'en'
    if base in _IDIOMAS_WHISPER:
        return base
    raise ValueError(f"idioma desconhecido para o Whisper: {idioma!r}. Use 'auto' ou um código como 'pt', 'en', "
                     f"'es', 'fr', 'de', 'it', 'ja', 'zh' (lista: {' '.join(sorted(_IDIOMAS_WHISPER))})")


def norm_modelo(modelo: str | None) -> str:
    m = str(modelo or MODELO_PADRAO).strip().lower()
    if m not in MODELOS:
        raise ValueError(f"modelo desconhecido: {modelo!r}. Opções: " + ", ".join(
            f"{k} ({_mb(v[1])})" for k, v in MODELOS.items()))
    return m


# ---------------------------------------------------------------- downloads
def _baixar(url: str, final: Path, sha256: str, tamanho: int | None = None, progresso=None,
            rotulo: str = "") -> Path:
    """Baixa `url` para `final` conferindo o SHA-256. Grava em `.<nome>.XXXX.parcial` na mesma pasta e só
    renomeia (os.replace, atômico) depois de conferido. Downloads simultâneos não se atrapalham."""
    final.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{final.name}.", suffix=".parcial", dir=final.parent)
    h = hashlib.sha256()
    lido = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ai-editor"})
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0) or tamanho or 0
            ultimo = 0.0
            while True:
                bloco = resp.read(1 << 20)
                if not bloco:
                    break
                out.write(bloco)
                h.update(bloco)
                lido += len(bloco)
                if progresso and total and time.monotonic() - ultimo > 0.5:
                    ultimo = time.monotonic()
                    progresso(min(1.0, lido / total))
        if tamanho and lido != tamanho:
            raise RuntimeError(f"download incompleto de {rotulo or url}: {lido} de {tamanho} bytes")
        if h.hexdigest() != sha256:
            raise RuntimeError(f"SHA-256 não confere em {rotulo or url} (esperado {sha256}, veio {h.hexdigest()}); "
                               f"arquivo descartado")
        os.chmod(tmp, 0o644)
        os.replace(tmp, final)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if progresso:
        progresso(1.0)
    return final


def _disco_cheio(e: BaseException) -> bool:
    return getattr(e, "errno", None) in (errno.ENOSPC, errno.EDQUOT) or (
        isinstance(getattr(e, "reason", None), OSError) and e.reason.errno in (errno.ENOSPC, errno.EDQUOT))


def _msg_disco_cheio(pasta: Path, precisa: int | None = None) -> str:
    try:
        livre = shutil.disk_usage(pasta if pasta.exists() else pasta.parent).free
    except OSError:
        livre = None
    return ("disco cheio em " + str(pasta) + (f" (livre: {_mb(livre)}" if livre is not None else " (")
            + (f", precisa de ~{_mb(precisa)})" if precisa else ")") + ". Libere espaço e tente de novo")


@contextlib.contextmanager
def _trava(nome: str):
    """Trava exclusiva entre processos (fcntl.flock) em ~/.cache/ai-editor/<nome>.lock; espera a vez."""
    core.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(core.CACHE_DIR / f"{nome}.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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


def caminho_modelo(modelo: str = MODELO_PADRAO) -> Path:
    return MODELOS_WHISPER_DIR / MODELOS[norm_modelo(modelo)][0]


def modelo_pronto(modelo: str = MODELO_PADRAO) -> bool:
    """Modelo presente e íntegro. O SHA-256 é conferido UMA vez (~0,3 s para o base) e fica registrado num
    marcador `.<arquivo>.ok` com tamanho, mtime e hash; depois basta comparar tamanho/mtime com o marcador."""
    nome = norm_modelo(modelo)
    arq, tam, sha, _ = MODELOS[nome]
    p = caminho_modelo(nome)
    try:
        st = p.stat()
    except OSError:
        return False
    if st.st_size != tam:
        return False
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


def baixar_modelo(modelo: str = MODELO_PADRAO, progresso=None) -> tuple[Path, bool]:
    """(caminho do modelo, True se baixou agora). Baixa só se ainda não existir (SHA-256 conferido)."""
    nome = norm_modelo(modelo)
    arq, tam, sha, _ = MODELOS[nome]
    final = MODELOS_WHISPER_DIR / arq
    if modelo_pronto(nome):
        return final, False
    _limpar_parciais(MODELOS_WHISPER_DIR)
    try:
        _baixar(HF_BASE + arq, final, sha, tam, progresso, f"modelo {nome}")
    except OSError as e:
        if _disco_cheio(e):
            raise FalaIndisponivel(f"não consegui baixar o modelo '{nome}': "
                                   + _msg_disco_cheio(MODELOS_WHISPER_DIR, tam)) from e
        raise FalaIndisponivel(f"não consegui baixar o modelo '{nome}' ({_mb(tam)}) de {HF_BASE + arq}: {e}. "
                               f"Sem internet? Baixe manualmente para {final}") from e
    return final, True


def whisper_cli() -> str:
    """Caminho do whisper-cli ou FalaIndisponivel explicando como obter."""
    p = binarios.localizar("whisper-cli")
    if p:
        return p
    raise FalaIndisponivel(
        "o motor de transcrição (whisper-cli, do whisper.cpp) não está instalado. Para compilar localmente o "
        f"whisper.cpp {WHISPER_VERSAO} (~1–2 min, ~3 MB; precisa de g++ e make, que o Ubuntu tem com "
        "`sudo apt install build-essential`), rode no terminal:\n"
        f"  {shlex.quote(sys.executable)} -m ai_editor.fala --instalar-whisper\n"
        f"O binário vai para {binarios.BIN_SOB_DEMANDA}/whisper-cli. Alternativa: apontar AI_EDITOR_WHISPER_CLI "
        "para um whisper-cli já existente.")


# ---------------------------------------------------------------- compilar o whisper.cpp
def _preexec():
    """preexec_fn que faz o filho morrer junto com quem o criou (PR_SET_PDEATHSIG), como no core."""
    return core._morrer_com_pai if core._prctl is not None else None


def _rodar_ff(cmd: list[str], texto: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Como core.rodar, mas o ffmpeg morre junto se o processo/thread do MCP morrer (sem órfãos)."""
    r = subprocess.run(cmd, capture_output=True, text=texto, timeout=timeout, preexec_fn=_preexec())
    if r.returncode != 0:
        err = r.stderr if texto else r.stderr.decode(errors="replace")
        if "No space left on device" in err:
            raise OSError(errno.ENOSPC, _msg_disco_cheio(Path(cmd[-1]).parent))
        raise core.ErroFFmpeg(f"comando falhou ({r.returncode}): {shlex.join(cmd)}\n{err[-4000:]}")
    return r


@functools.lru_cache(maxsize=8)
def _versao_motor_de(caminho: str, mtime_ns: int) -> str:
    try:
        r = subprocess.run([caminho, "--version"], capture_output=True, text=True, timeout=15)
        m = re.search(r"version:\s*([\w.\-]+)", r.stdout + r.stderr)
        return m.group(1) if m else "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


def _versao_motor(exe: str | None = None) -> str:
    """Versão REAL do whisper-cli em uso (ex. '1.9.5-dev'), não a fixada no código: entra na chave do cache
    (um whisper-cli trocado por AI_EDITOR_WHISPER_CLI ou recompilado não reaproveita transcrições antigas)."""
    exe = exe or binarios.localizar("whisper-cli")
    if not exe:
        return "?"
    try:
        st = os.stat(exe)
    except OSError:
        return "?"
    return _versao_motor_de(str(Path(exe).resolve()), st.st_mtime_ns)


_versao_motor.cache_clear = _versao_motor_de.cache_clear


def _cmake() -> list[str]:
    c = shutil.which("cmake")
    if c:
        return [c]
    uvx = shutil.which("uvx") or (str(Path.home() / ".local/bin/uvx") if (Path.home() / ".local/bin/uvx").exists()
                                  else None)
    if uvx:
        return [uvx, "--from", _CMAKE_PIP, "cmake"]
    raise FalaIndisponivel("para compilar o whisper.cpp falta o cmake: `sudo apt install cmake` "
                           "(ou instale o uv, que baixa o cmake do PyPI sem sudo)")


def compilar_whisper(progresso=None, jobs: int | None = None, log=print) -> Path:
    """Baixa o código-fonte do whisper.cpp WHISPER_VERSAO (SHA-256 conferido), compila só o whisper-cli
    (estático, sem OpenMP/SDL/curl; otimizado para este processador) e instala em BIN_SOB_DEMANDA.
    Sem sudo. Pasta de trabalho própria (mkdtemp) em ~/.cache/ai-editor-build/ (apagada no fim, mesmo com erro).
    Duas instalações ao mesmo tempo: a segunda espera a primeira (trava) e reaproveita o resultado."""
    for f in ("g++", "make"):
        if not shutil.which(f):
            raise FalaIndisponivel(f"para compilar o whisper.cpp falta o {f}: `sudo apt install build-essential`")
    cmake = _cmake()
    raiz = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ai-editor-build"
    raiz.mkdir(parents=True, exist_ok=True)
    destino = binarios.BIN_SOB_DEMANDA / "whisper-cli"
    marcador = binarios.BIN_SOB_DEMANDA / "whisper-cli.versao"
    t_pedido = time.time()
    with _trava("compilar-whisper"):
        if (destino.is_file() and marcador.is_file() and marcador.stat().st_mtime >= t_pedido
                and f"whisper.cpp {WHISPER_VERSAO}\n" in marcador.read_text()):
            log(f"já instalado (por outro processo): {destino}")
            binarios.localizar_com_origem.cache_clear()
            return destino
        trab = Path(tempfile.mkdtemp(prefix="whisper.cpp-", dir=raiz))
        tmp = None
        try:
            tar = trab / f"whisper.cpp-{WHISPER_VERSAO}.tar.gz"
            log(f"baixando {WHISPER_TARBALL}")
            try:
                _baixar(WHISPER_TARBALL, tar, WHISPER_TARBALL_SHA256, None, None, "código-fonte do whisper.cpp")
            except OSError as e:
                if _disco_cheio(e):
                    raise FalaIndisponivel("não consegui baixar o whisper.cpp: " + _msg_disco_cheio(raiz)) from e
                raise
            with tarfile.open(tar) as t:
                t.extractall(trab / "fonte", filter="data")
            fonte = next((trab / "fonte").iterdir())
            build = trab / "build"
            log("configurando (cmake)")
            r = subprocess.run(cmake + ["-S", str(fonte), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
                                        "-DBUILD_SHARED_LIBS=OFF", "-DGGML_OPENMP=OFF", "-DWHISPER_BUILD_TESTS=OFF",
                                        "-DWHISPER_BUILD_SERVER=OFF", "-DWHISPER_SDL2=OFF", "-DWHISPER_CURL=OFF"],
                               capture_output=True, text=True, preexec_fn=_preexec())
            if r.returncode != 0:
                raise RuntimeError("falha ao configurar o whisper.cpp (cmake):\n" + (r.stdout + r.stderr)[-3000:])
            j = jobs or max(1, min(4, os.cpu_count() or 1))  # 4 jobs: pico de RAM ~1 GB
            log(f"compilando com {j} processos (1–2 min)")
            r = subprocess.run(cmake + ["--build", str(build), "--config", "Release", "--target", "whisper-cli",
                                        "-j", str(j)], capture_output=True, text=True, preexec_fn=_preexec())
            if r.returncode != 0:
                saida = r.stdout + r.stderr
                if "No space left on device" in saida:
                    raise FalaIndisponivel("falha ao compilar o whisper.cpp: " + _msg_disco_cheio(raiz))
                raise RuntimeError("falha ao compilar o whisper.cpp:\n" + saida[-3000:])
            exe = build / "bin" / "whisper-cli"
            binarios.BIN_SOB_DEMANDA.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".whisper-cli.", suffix=".parcial", dir=binarios.BIN_SOB_DEMANDA)
            os.close(fd)
            shutil.copy2(exe, tmp)
            os.chmod(tmp, 0o755)
            os.replace(tmp, destino)
            tmp = None
            marcador.write_text(f"whisper.cpp {WHISPER_VERSAO}\nfonte: {WHISPER_TARBALL}\n"
                                f"sha256: {WHISPER_TARBALL_SHA256}\n")
        except OSError as e:
            if _disco_cheio(e):
                raise FalaIndisponivel("falha ao compilar o whisper.cpp: " + _msg_disco_cheio(raiz)) from e
            raise
        finally:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
            shutil.rmtree(trab, ignore_errors=True)
    binarios.localizar_com_origem.cache_clear()
    _versao_motor.cache_clear()
    log(f"instalado: {destino}")
    return destino


def status() -> dict:
    """O que está pronto para transcrever (motor, modelos baixados)."""
    return {"whisper_cli": binarios.localizar("whisper-cli"),
            "modelos": {k: {"baixado": modelo_pronto(k), "tamanho": _mb(v[1])} for k, v in MODELOS.items()},
            "pasta_modelos": str(MODELOS_WHISPER_DIR)}


# ---------------------------------------------------------------- transcrição
_HASH_COMPLETO_ATE = 256 << 20  # arquivos até 256 MB: hash do conteúdo todo (~0,3 s)


def _impressao_arquivo(p: Path) -> str:
    """Identidade do conteúdo para o cache. Até 256 MB: SHA-256 do arquivo inteiro (vale mesmo se o arquivo for
    copiado/renomeado). Maior: tamanho + 3 blocos de 4 MB (início, meio, fim) + mtime_ns + caminho — uma edição
    que mantém o tamanho e não toca nos blocos amostrados muda o mtime."""
    st = p.stat()
    tam = st.st_size
    h = hashlib.sha256(str(tam).encode())
    with open(p, "rb") as f:
        if tam <= _HASH_COMPLETO_ATE:
            while bloco := f.read(1 << 22):
                h.update(bloco)
            return h.hexdigest()
        bloco = 4 << 20
        for pos in sorted({0, max(0, tam // 2 - bloco // 2), max(0, tam - bloco)}):
            f.seek(pos)
            h.update(f.read(bloco))
    h.update(f"|{st.st_mtime_ns}|{p.resolve()}".encode())
    return h.hexdigest()


def _duracao_wav(wav: Path) -> float:
    """Duração pelo cabeçalho do WAV (o ffmpeg grava um bloco LIST antes dos dados: tamanho − 44 erra)."""
    try:
        with wave.open(str(wav), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 16000)
    except (wave.Error, EOFError, OSError):
        return max(0.0, (wav.stat().st_size - 44) / 32000)


def extrair_audio(arquivo: Path, destino: Path, faixa: int = 1, inicio: float | None = None,
                  fim: float | None = None) -> float:
    """WAV 16 kHz mono PCM (o que o whisper espera) da faixa `faixa` (1 = A1). Devolve a duração.
    O instante 0 do WAV é o instante `inicio` (ou 0) do ARQUIVO: se a faixa começa depois do contêiner (áudio
    atrasado, start_time > 0), o começo é preenchido com silêncio (`aresample=async=1:first_pts=0`); sem isso
    os tempos sairiam adiantados pelo atraso da faixa."""
    probe = core.ffprobe(arquivo)
    auds = core.faixas_audio(probe)
    if not auds:
        raise ValueError(f"o arquivo não tem áudio: {arquivo.name}")
    if not 1 <= int(faixa) <= len(auds):
        raise ValueError(f"faixa A{faixa} não existe (o arquivo tem {len(auds)} faixa(s) de áudio)")
    inicio = max(0.0, float(inicio)) if inicio else None
    cmd = list(core.FFMPEG)
    if inicio:
        cmd += ["-ss", f"{inicio:.3f}"]
    if fim is not None:
        cmd += ["-t", f"{max(0.01, fim - (inicio or 0)):.3f}"]
    cmd += ["-i", str(arquivo), "-map", f"0:a:{int(faixa) - 1}", "-vn", "-sn", "-dn",
            "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destino)]
    _rodar_ff(cmd)
    return _duracao_wav(destino)


def _rodar_whisper(cmd: list[str], progresso=None) -> str:
    """Roda o whisper-cli lendo o progresso ('progress = N%') do stderr; devolve o stderr (para erros)."""
    linhas: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace",
                            preexec_fn=_preexec())
    try:
        for linha in proc.stderr:
            linhas.append(linha)
            if len(linhas) > 400:
                del linhas[:200]
            m = re.search(r"progress\s*=\s*(\d+)%", linha)
            if m and progresso:
                progresso(int(m.group(1)) / 100)
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    err = "".join(linhas)
    if proc.returncode != 0:
        raise RuntimeError(f"whisper-cli falhou ({proc.returncode}): {shlex.join(cmd)}\n{err[-3000:]}")
    return err


_ATRASO_DTW = 0.1  # o DTW marca o token ~0,1 s depois do início real (medido com fala sintética: 0,05–0,17 s)
_ESPECIAL = re.compile(r"^\[_[A-Z]+_?\d*\]$|^\[_TT_\d+\]$|^<\|.*\|>$")


def _palavras(tokens: list[dict], desloc: float) -> list[dict]:
    """Junta os tokens em palavras (token que começa com espaço abre palavra nova).
    Tempo: o do alinhamento DTW (t_dtw, unidade de 10 ms) quando existe — muito mais preciso que o tempo dos
    tokens, que o whisper costuma "esticar" para janelas redondas; senão o do token. Fim da palavra = início da
    seguinte; a última do segmento ganha uma duração estimada (o ajuste por silêncio refina depois)."""
    out: list[dict] = []
    pont_fim = None
    for t in tokens:
        txt = t.get("text", "")
        if not txt or txt.startswith("[_") or _ESPECIAL.match(txt.strip()):
            continue
        off = t.get("offsets") or {}
        a = off.get("from", -1) / 1000
        b = off.get("to", -1) / 1000
        dtw = t.get("t_dtw", -1)
        com_dtw = dtw is not None and dtw >= 0
        if com_dtw:
            a = b = max(0.0, dtw / 100 - _ATRASO_DTW)
        if a < 0:
            continue
        if not re.search(r"\w", txt):  # pontuação: gruda na palavra anterior
            if out:
                out[-1]["texto"] += txt.strip()
                pont_fim = a if com_dtw else None
            continue
        pont_fim = None
        if txt.startswith(" ") or not out:
            out.append({"inicio": a, "fim": b, "texto": txt.strip(), "p": [t.get("p", 1.0)], "dtw": com_dtw})
        else:
            w = out[-1]
            w["texto"] += txt
            w["fim"] = max(w["fim"], b)
            w["p"].append(t.get("p", 1.0))
    _espalhar_colados(out)
    for i, w in enumerate(out):
        if i + 1 < len(out):
            prox = out[i + 1]["inicio"]
            if w["dtw"] or w["fim"] > prox or w["fim"] <= w["inicio"]:
                w["fim"] = max(w["inicio"] + 0.05, prox)
        else:
            est = w["inicio"] + min(1.0, 0.1 + 0.07 * len(w["texto"]))
            if w["dtw"]:
                w["fim"] = min(est, pont_fim) if pont_fim and pont_fim > w["inicio"] + 0.1 else est
            elif w["fim"] <= w["inicio"]:
                w["fim"] = est
    return [{"inicio": round(w["inicio"] + desloc, 3), "fim": round(w["fim"] + desloc, 3),
             "texto": w["texto"], "p": round(sum(w["p"]) / len(w["p"]), 3)} for w in out]


# frases que o Whisper costuma "ouvir" em música, ruído ou silêncio (vêm das legendas usadas no treino)
_ALUCINACOES_TXT = ["thank you", "thanks for watching", "thank you for watching", "you", "bye",
                    "subtitles by the amara org community", "legendas pela comunidade amara org",
                    "obrigado", "obrigada", "obrigado por assistir", "inscreva se no canal", "tchau",
                    "legenda adriana zanotto", "e ai", "gracias", "musica", "aplausos"]


def _alucinacao(seg: dict) -> bool:
    """Segmento que é só uma frase típica de alucinação e com confiança média baixa (< 0,75)."""
    if normalizar(seg["texto"]) not in {normalizar(t) for t in _ALUCINACOES_TXT}:
        return False
    ps = [w.get("p", 1.0) for w in seg.get("palavras") or []]
    return bool(ps) and sum(ps) / len(ps) < 0.75


def _espalhar_colados(ws: list[dict]) -> None:
    """O DTW às vezes dá o MESMO tempo a várias palavras seguidas (acontece perto de uma palavra inventada ou
    corrigida). Redistribui as coladas + a palavra antes delas, entre o início dessa palavra e o início da
    seguinte ao grupo, proporcionalmente ao número de letras."""
    i = 1
    while i < len(ws):
        if ws[i]["dtw"] and abs(ws[i]["inicio"] - ws[i - 1]["inicio"]) < 0.03:
            j = i
            while j + 1 < len(ws) and abs(ws[j + 1]["inicio"] - ws[i]["inicio"]) < 0.03:
                j += 1
            k0 = i - 2 if i >= 2 else i - 1  # o grupo colado é ws[i-1..j]; inclui a palavra antes dele
            ini = ws[k0]["inicio"]
            fim = ws[j + 1]["inicio"] if j + 1 < len(ws) else ws[i]["inicio"] + 0.08 * sum(
                len(w["texto"]) + 1 for w in ws[i:j + 1])
            grupo = ws[k0:j + 1]
            tot = sum(len(w["texto"]) + 1 for w in grupo)
            t = ini
            for w in grupo:
                w["inicio"] = t
                t += (fim - ini) * (len(w["texto"]) + 1) / tot
            i = j + 1
        else:
            i += 1


def _silencios(wav: Path) -> list[tuple[float, float]]:
    """Pausas do WAV (silencedetect), com limiar relativo ao volume médio (−18 dB, entre −50 e −30 dBFS)."""
    r = subprocess.run([core.FFMPEG[0], "-hide_banner", "-nostdin", "-i", str(wav), "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True, preexec_fn=_preexec())
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
    limiar = max(-50.0, min(-30.0, float(m.group(1)) - 18)) if m else -40.0
    r = subprocess.run([core.FFMPEG[0], "-hide_banner", "-nostdin", "-i", str(wav), "-af",
                        f"silencedetect=n={limiar:.1f}dB:d=0.25", "-f", "null", "-"], capture_output=True, text=True,
                       preexec_fn=_preexec())
    ini = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", r.stderr)]
    fim = [float(x) for x in re.findall(r"silence_end:\s*(-?[\d.]+)", r.stderr)]
    dur = _duracao_wav(wav)
    return [(max(0.0, a), fim[i] if i < len(fim) else dur) for i, a in enumerate(ini)]


def _ajustar_silencios(segs: list[dict], silencios: list[tuple[float, float]], desloc: float) -> None:
    """Encosta as bordas das palavras nas pausas medidas: a primeira palavra depois de uma pausa começa onde a
    pausa termina e a última antes dela termina onde a pausa começa (o DTW erra ~0,1–0,4 s nessas bordas).
    Depois recalcula início/fim dos segmentos pelas palavras."""
    ws = [w for s in segs for w in s.get("palavras", [])]
    if ws and silencios:
        for a, b in silencios:
            a, b = a + desloc, b + desloc
            # primeira palavra que começa depois do início da pausa
            j = next((k for k, w in enumerate(ws) if w["inicio"] >= a - 0.05), None)
            if j is not None and ws[j]["inicio"] - b < 0.45:
                ws[j]["inicio"] = round(b, 3)
                if ws[j]["fim"] <= ws[j]["inicio"]:
                    ws[j]["fim"] = round(ws[j]["inicio"] + 0.1, 3)
            i = (j - 1) if j is not None else len(ws) - 1
            if i >= 0 and ws[i]["inicio"] < a and a - 0.6 <= ws[i]["fim"] <= b + 0.5:
                ws[i]["fim"] = round(max(ws[i]["inicio"] + 0.05, a + 0.02), 3)
        for k in range(len(ws) - 1):  # nada de sobreposição
            if ws[k]["fim"] > ws[k + 1]["inicio"]:
                ws[k]["fim"] = max(ws[k]["inicio"] + 0.02, ws[k + 1]["inicio"])
    for s in segs:
        if s.get("palavras"):
            s["inicio"] = s["palavras"][0]["inicio"]
            s["fim"] = s["palavras"][-1]["fim"]


_PREFIXO_TMP = "ai-editor-fala-"


def _limpar_orfaos_tmp(idade_s: float = 3600) -> None:
    """Apaga pastas temporárias de transcrições (ai-editor-fala-<pid>-*) com mais de 1 h cujo processo já
    morreu (MCP morto no meio de uma transcrição não passa pelo `finally` do TemporaryDirectory)."""
    agora = time.time()
    for d in Path(tempfile.gettempdir()).glob(f"{_PREFIXO_TMP}*"):
        try:
            if not d.is_dir() or agora - d.stat().st_mtime < idade_s:
                continue
            m = re.match(rf"{_PREFIXO_TMP}(\d+)-", d.name)
            if m:
                try:
                    os.kill(int(m.group(1)), 0)
                    continue  # processo vivo: não mexe
                except ProcessLookupError:
                    pass
                except PermissionError:
                    continue
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def transcrever(arquivo: Path | str, idioma: str = "auto", modelo: str = MODELO_PADRAO, faixa: int = 1,
                inicio: float | None = None, fim: float | None = None, palavras: bool = True,
                threads: int | None = None, usar_cache: bool = True, progresso=None) -> dict:
    """Transcreve a fala de `arquivo` (vídeo ou áudio) localmente com o whisper.cpp.

    idioma: 'auto' (detecta) ou código ('pt', 'en'...); modelo: 'tiny' | 'base' (padrão) | 'small' |
    'base-q5_1' | 'small-q5_1' (baixado no primeiro uso). faixa: faixa de áudio (1 = A1).
    inicio/fim (segundos): só esse trecho; os tempos devolvidos são SEMPRE do arquivo inteiro.
    palavras=True: inclui cada palavra com tempo (alinhamento DTW do whisper.cpp; precisão típica ±0,1–0,3 s).

    Devolve {segmentos: [{inicio, fim, texto, palavras: [{inicio, fim, texto, p}]}], texto, idioma,
    modelo, duracao_audio, segundos_processamento, fator_tempo_real, cache, modelo_baixado (se baixou agora)}.
    O resultado fica em cache (~/.cache/ai-editor/transcricoes) pela identidade do conteúdo + parâmetros.
    """
    arquivo = Path(arquivo).expanduser().resolve()
    if not arquivo.is_file():
        raise FileNotFoundError(f"arquivo não encontrado: {arquivo}")
    lang = norm_idioma(idioma)
    nome = norm_modelo(modelo)
    if inicio is not None:
        inicio = max(0.0, float(inicio))
    if inicio is not None and fim is not None and fim - inicio <= 0.05:
        raise ValueError(f"intervalo vazio: {fmt_tempo(inicio)} → {fmt_tempo(fim)}")
    exe = whisper_cli()  # erro claro antes de baixar o modelo
    impressao = _impressao_arquivo(arquivo)

    def arq_cache(idioma_chave: str) -> Path:
        params = {"v": _VERSAO_CACHE, "whisper": _versao_motor(exe), "idioma": idioma_chave, "modelo": nome,
                  "faixa": int(faixa), "inicio": round(inicio, 3) if inicio else None,
                  "fim": round(fim, 3) if fim is not None else None}
        chave = hashlib.sha256((impressao + json.dumps(params, sort_keys=True)).encode()).hexdigest()
        return CACHE_TRANSCRICOES / f"{chave[:40]}.json"

    cache = arq_cache(lang)
    if usar_cache and cache.is_file():
        try:
            r = json.loads(cache.read_text())
            r.update(arquivo=str(arquivo), cache=True)
            if not palavras:
                r["segmentos"] = [{k: v for k, v in s.items() if k != "palavras"} for s in r["segmentos"]]
            if progresso:
                progresso(1.0)
            return r
        except (OSError, ValueError):
            pass

    baixado = None
    if not modelo_pronto(nome):
        caminho, _ = baixar_modelo(nome, (lambda f: progresso(0.3 * f)) if progresso else None)
        baixado = {"modelo": nome, "tamanho": _mb(MODELOS[nome][1]), "caminho": str(caminho)}
        base = 0.3
    else:
        base = 0.0
    caminho = caminho_modelo(nome)
    t0 = time.monotonic()
    _limpar_orfaos_tmp()
    with tempfile.TemporaryDirectory(prefix=f"{_PREFIXO_TMP}{os.getpid()}-") as tmp:
        wav = Path(tmp) / "audio.wav"
        dur = extrair_audio(arquivo, wav, faixa, inicio, fim)
        if progresso:
            progresso(base + 0.03)
        if dur < 0.1:
            raise ValueError("trecho de áudio vazio")
        n = threads or max(1, min(4, os.cpu_count() or 1))
        saida = Path(tmp) / "saida"
        cmd = [exe, "-m", str(caminho), "-f", str(wav), "-l", lang, "-t", str(n), "-ojf", "-of", str(saida),
               "-pp", "-sns",
               # alinhamento DTW: tempo das palavras bem melhor (o whisper sozinho arredonda os segmentos para
               # janelas de ~4 s); o DTW não funciona com flash attention (-nfa), que viria ligada por padrão
               "--dtw", MODELOS[nome][3], "-nfa"]
        # um whisper-cli por vez na máquina (~0,4–1 GB de RAM e 4 threads cada): os outros esperam a vez
        with _trava("whisper"):
            err = _rodar_whisper(cmd, (lambda f: progresso(base + 0.03 + (0.97 - base) * f)) if progresso else None)
        try:
            bruto = (saida.with_suffix(".json")).read_bytes()
        except FileNotFoundError:
            raise RuntimeError("o whisper-cli terminou sem gerar o resultado (JSON). Fim da saída dele:\n"
                               + "\n".join(err.strip().splitlines()[-15:])) from None
        silencios = _silencios(wav)
    seg_proc = time.monotonic() - t0
    try:
        dados = json.loads(bruto.decode("utf-8"))
    except UnicodeDecodeError:
        dados = json.loads(bruto.decode("utf-8", errors="replace"))
    desloc = float(inicio or 0)
    segs = []
    for s in dados.get("transcription", []):
        texto = (s.get("text") or "").strip()
        if not texto:
            continue
        off = s.get("offsets") or {}
        seg = {"inicio": round(off.get("from", 0) / 1000 + desloc, 3), "fim": round(off.get("to", 0) / 1000 + desloc, 3),
               "texto": texto}
        ps = _palavras(s.get("tokens") or [], desloc)
        if ps:
            seg["palavras"] = ps
        segs.append(seg)
    descartados = [s for s in segs if _alucinacao(s)]
    segs = [s for s in segs if not _alucinacao(s)]
    _ajustar_silencios(segs, silencios, desloc)
    idioma_det = (dados.get("result") or {}).get("language") or (lang if lang != "auto" else None)
    r = {"arquivo": str(arquivo), "idioma": idioma_det, "modelo": nome, "faixa": int(faixa),
         "inicio": inicio, "fim": fim, "duracao_audio": round(dur, 3),
         "segundos_processamento": round(seg_proc, 2),
         "fator_tempo_real": round(seg_proc / dur, 3) if dur else None,
         "texto": " ".join(s["texto"] for s in segs), "segmentos": segs, "whisper": _versao_motor(exe)}
    if descartados:
        r["descartados"] = [{"inicio": (s.get("palavras") or [s])[0]["inicio"], "texto": s["texto"],
                             "motivo": "frase típica de alucinação do Whisper com baixa confiança"} for s in descartados]
    # com idioma='auto', guarda também sob a chave do idioma detectado: gerar_legenda/buscar_fala com
    # idioma='pt' (ou o contrário, 'auto' de novo) reaproveitam esta transcrição
    alvos = [cache]
    if lang == "auto" and idioma_det and idioma_det in _IDIOMAS_WHISPER:
        alvos.append(arq_cache(idioma_det))
    try:
        CACHE_TRANSCRICOES.mkdir(parents=True, exist_ok=True)
        for c in alvos:
            tmpc = c.with_name(f".{c.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmpc.write_text(json.dumps(r, ensure_ascii=False))
            os.replace(tmpc, c)
    except OSError:
        pass
    r["cache"] = False
    if baixado:
        r["modelo_baixado"] = baixado
    if not palavras:
        r["segmentos"] = [{k: v for k, v in s.items() if k != "palavras"} for s in segs]
    return r


# ---------------------------------------------------------------- legendas (.srt/.vtt)
def _palavras_de(segmentos: list[dict]) -> list[dict]:
    """Palavras com tempo; segmento sem palavras é dividido proporcionalmente ao número de caracteres."""
    out = []
    for s in segmentos:
        if s.get("palavras"):
            out += [{**w, "seg": id(s)} for w in s["palavras"]]
            continue
        ws = s["texto"].split()
        tot = sum(len(w) + 1 for w in ws) or 1
        t = s["inicio"]
        d = max(0.0, s["fim"] - s["inicio"])
        for w in ws:
            dw = d * (len(w) + 1) / tot
            out.append({"inicio": t, "fim": t + dw, "texto": w, "seg": id(s)})
            t += dw
    return out


_PALAVRAS_LIGACAO = set("a o as os e de da do das dos em no na nos nas por pelo pela para pra com um uma que se "
                        "ao aos the of to and in on at for with".split())


def _quebrar_linhas(texto: str, max_chars: int, max_linhas: int) -> list[str]:
    """Quebra equilibrada: 1 linha se couber; senão o ponto de quebra que deixa as linhas mais parecidas."""
    if len(texto) <= max_chars or max_linhas <= 1:
        return [texto]
    ws = texto.split()
    if max_linhas == 2:
        melhor = None
        for i in range(1, len(ws)):
            a, b = " ".join(ws[:i]), " ".join(ws[i:])
            custo = max(len(a), len(b))
            if max(len(a), len(b)) > max_chars:
                custo += 100  # só se não houver jeito
            if re.search(r"[.;:!?…]$", ws[i - 1]):
                custo -= 8   # fim de frase: melhor lugar para quebrar
            elif re.search(r",$", ws[i - 1]):
                custo -= 4
            elif normalizar(ws[i - 1]) in _PALAVRAS_LIGACAO:
                custo += 6   # não deixa "de", "por", "o"... pendurado no fim da linha
            if melhor is None or custo < melhor[0]:
                melhor = (custo, [a, b])
        return melhor[1] if melhor else [texto]
    linhas, atual = [], ""
    for w in ws:
        if atual and len(atual) + 1 + len(w) > max_chars:
            linhas.append(atual)
            atual = w
        else:
            atual = f"{atual} {w}".strip()
    return linhas + ([atual] if atual else [])


def montar_legendas(segmentos: list[dict], max_caracteres: int = 42, max_linhas: int = 2,
                    duracao_min: float = 1.0, duracao_max: float = 6.0, pausa_quebra: float = 0.6,
                    fim_audio: float | None = None) -> list[dict]:
    """Agrupa as palavras em legendas legíveis: até `max_linhas` linhas de até `max_caracteres`, duração entre
    `duracao_min` e `duracao_max` s. Quebra preferencialmente no fim de frase, em pausas (≥ pausa_quebra s)
    e entre segmentos. Devolve [{inicio, fim, linhas: [str], texto}]."""
    ws = _palavras_de(segmentos)
    limite = max_caracteres * max_linhas

    def txt(g):
        return " ".join(w["texto"] for w in g)

    # 1) frases: quebra em fim de frase e em pausas
    frases: list[list[dict]] = []
    for i, w in enumerate(ws):
        if frases and frases[-1]:
            ult = frases[-1][-1]
            if re.search(r"[.!?…]$", ult["texto"]) or w["inicio"] - ult["fim"] >= pausa_quebra:
                frases.append([])
        elif not frases:
            frases.append([])
        frases[-1].append(w)
    # 2) frases curtas seguidas (sem pausa) dividem a mesma legenda se couberem
    grupos: list[list[dict]] = []
    for f in frases:
        if grupos:
            g = grupos[-1]
            if (len(txt(g)) < max_caracteres * 0.5 and len(txt(g + f)) <= limite
                    and f[-1]["fim"] - g[0]["inicio"] <= duracao_max and f[0]["inicio"] - g[-1]["fim"] < pausa_quebra):
                g.extend(f)
                continue
        grupos.append(list(f))

    # 3) grupo grande demais: divide em partes equilibradas, preferindo vírgulas e pausas, nunca deixando
    #    palavra de ligação ("de", "por"...) no fim de uma legenda
    def dividir(g: list[dict]) -> list[list[dict]]:
        if len(g) <= 1 or (len(txt(g)) <= limite and g[-1]["fim"] - g[0]["inicio"] <= duracao_max):
            return [g]
        total = len(txt(g))
        melhor = None
        for i in range(1, len(g)):
            esq = len(txt(g[:i]))
            custo = abs(esq - (total - esq - 1)) / max(1, total)
            ant = g[i - 1]["texto"]
            if re.search(r"[,;:]$", ant):
                custo -= 0.25
            if normalizar(ant) in _PALAVRAS_LIGACAO:
                custo += 0.3
            custo -= min(0.3, max(0.0, g[i]["inicio"] - ant_fim) if (ant_fim := g[i - 1]["fim"]) else 0.0)
            if melhor is None or custo < melhor[0]:
                melhor = (custo, i)
        i = melhor[1]
        return dividir(g[:i]) + dividir(g[i:])

    cues = [{"inicio": p[0]["inicio"], "fim": p[-1]["fim"], "texto": txt(p)}
            for g in grupos for p in dividir(g) if p]
    # durações mínimas sem invadir a próxima; máxima respeitada
    for i, c in enumerate(cues):
        prox = cues[i + 1]["inicio"] if i + 1 < len(cues) else None
        if c["fim"] - c["inicio"] < duracao_min:
            c["fim"] = c["inicio"] + duracao_min
        if c["fim"] - c["inicio"] > duracao_max:
            c["fim"] = c["inicio"] + duracao_max
        if prox is not None and c["fim"] > prox - 0.04:
            # termina 40 ms antes da próxima; se isso a deixar com menos de 0,2 s, encosta na próxima (nunca
            # passa do início dela: legendas sobrepostas aparecem empilhadas)
            c["fim"] = (prox - 0.04 if prox - 0.04 >= c["inicio"] + 0.2
                        else max(c["inicio"], min(prox, c["inicio"] + 0.2)))
        if fim_audio is not None and c["fim"] > fim_audio:
            c["fim"] = max(c["inicio"] + 0.2, fim_audio)
        c["inicio"], c["fim"] = round(c["inicio"], 3), round(c["fim"], 3)
        c["linhas"] = _quebrar_linhas(c["texto"], max_caracteres, max_linhas)
    return cues


def _ts(t: float, sep: str) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _linha_srt(ln: str) -> str:
    # '-->' no texto seria lido como linha de tempo; linha vazia terminaria a legenda
    return ln.replace("-->", "->").strip() or "\u00a0"


def texto_srt(cues: list[dict]) -> str:
    return "\n".join(f"{i}\n{_ts(c['inicio'], ',')} --> {_ts(c['fim'], ',')}\n"
                     + "\n".join(_linha_srt(ln) for ln in c["linhas"]) + "\n" for i, c in enumerate(cues, 1))


def texto_vtt(cues: list[dict]) -> str:
    # WebVTT: '&' e '<' são marcação (entidades/tags) e precisam de escape; '>' também ('-->' vira '--&gt;')
    corpo = "\n".join(f"{_ts(c['inicio'], '.')} --> {_ts(c['fim'], '.')}\n"
                      + "\n".join(html.escape(_linha_srt(ln), quote=False) for ln in c["linhas"]) + "\n"
                      for c in cues)
    return "WEBVTT\n\n" + corpo


_TEMPO_LEG = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")


def ler_legenda(arq: Path) -> list[dict]:
    """Lê .srt/.vtt → [{inicio, fim, linhas}] com o TEXTO das legendas (sem marcação): tags HTML simples
    (<i>, <b>, <u>) viram marcadores internos, outras tags (<font>, <c.x>, <v Nome>) e entidades do VTT são
    resolvidas. Um `{\\anN}` no começo (convenção de .srt para posição) é mantido como posição."""
    texto = arq.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    vtt = arq.suffix.lower() == ".vtt"
    cues = []
    for bloco in re.split(r"\n\s*\n", texto):
        linhas = bloco.split("\n")
        k = next((i for i, ln in enumerate(linhas) if "-->" in ln), None)
        if k is None:
            continue  # cabeçalho WEBVTT, NOTE, STYLE, REGION, lixo
        tempos = _TEMPO_LEG.findall(linhas[k])
        if len(tempos) < 2:
            continue
        ini, fim = (int(h or 0) * 3600 + int(m) * 60 + int(s_) + int(ms.ljust(3, "0")) / 1000
                    for h, m, s_, ms in tempos[:2])
        corpo = [ln for ln in linhas[k + 1:]]
        while corpo and not corpo[-1].strip():
            corpo.pop()
        if not corpo:
            continue
        pos = None
        m = re.match(r"\s*\{\\an([1-9])\}", corpo[0])
        if m:
            pos = int(m.group(1))
            corpo[0] = corpo[0][m.end():]
        saida = []
        for ln in corpo:
            ln = re.sub(r"<(/?)([ibu])(?:\.[^>\s]*)?>", lambda t: f"\x00{t.group(1)}{t.group(2)}\x00", ln, flags=re.I)
            ln = re.sub(r"<[^>]*>", "", ln)  # <font ...>, <c.amarelo>, <v Ana>, <00:00:01.000>...
            if vtt or "&" in ln:
                ln = html.unescape(ln)
            saida.append(ln)
        cues.append({"inicio": ini, "fim": fim, "linhas": saida, "pos": pos})
    return cues


def _esc_ass(ln: str) -> str:
    """Texto literal numa linha Dialogue do ASS: '\\' seguido de N/n/h seria quebra/espaço e '{...}' seria
    bloco de comando (\\pos, \\fs...). Barra ganha um 'word joiner' invisível depois; chaves viram \\{ \\}."""
    ln = ln.replace("\\", "\\\u2060").replace("{", "\\{").replace("}", "\\}")
    ln = re.sub(r"\x00(/?)([ibu])\x00", lambda t: "{\\" + t.group(2).lower() + ("0" if t.group(1) else "1") + "}", ln)
    return ln.replace("\x00", "")


def _tempo_ass(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s_, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s_:02d}.{cs:02d}"


_FORMATO_ESTILO = ("Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
                   "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                   "Alignment, MarginL, MarginR, MarginV, Encoding")


def texto_ass(cues: list[dict], largura: int, altura: int) -> str:
    """Script ASS completo (PlayRes = tamanho do vídeo, estilo Default a ser ajustado) com o texto escapado."""
    ev = []
    for c in cues:
        pos = "{\\an%d}" % c["pos"] if c.get("pos") else ""
        ev.append(f"Dialogue: 0,{_tempo_ass(c['inicio'])},{_tempo_ass(c['fim'])},Default,,0,0,0,,"
                  + pos + "\\N".join(_esc_ass(ln) for ln in c["linhas"]))
    return ("[Script Info]\nScriptType: v4.00+\n"
            f"PlayResX: {largura}\nPlayResY: {altura}\nScaledBorderAndShadow: yes\nWrapStyle: 0\n"
            "YCbCr Matrix: None\n\n[V4+ Styles]\n"
            f"Format: {_FORMATO_ESTILO}\n"
            "Style: Default,DejaVu Sans,16,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,"
            "2,10,10,10,1\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            + "\n".join(ev) + "\n")


def _criar_exclusivo(p: Path, conteudo: str) -> Path:
    """Grava num arquivo NOVO: se `p` existir, tenta nome.2.ext, nome.3.ext... (O_EXCL: nunca sobrescreve)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    i = 1
    while True:
        c = p if i == 1 else p.with_name(f"{p.stem}.{i}{p.suffix}")
        try:
            fd = os.open(c, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            i += 1
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conteudo)
        return c


def gerar_legenda(arquivo: Path | str, formato: str = "srt", destino: str | Path | None = None,
                  idioma: str = "auto", modelo: str = MODELO_PADRAO, faixa: int = 1,
                  max_caracteres: int = 42, max_linhas: int = 2, duracao_min: float = 1.0,
                  duracao_max: float = 6.0, transcricao: dict | None = None, progresso=None) -> dict:
    """Transcreve (ou usa `transcricao`) e grava legenda(s) em ARQUIVO NOVO. formato: 'srt' | 'vtt' | 'ambos'.
    Nome padrão: ao lado do ORIGINAL do documento, `video.<idioma>.srt` (players como o mpv carregam sozinhos);
    se existir, `video.pt.2.srt`. destino: pasta ou caminho de arquivo (.srt/.vtt)."""
    fmt = str(formato or "srt").lower().lstrip(".")
    if fmt not in ("srt", "vtt", "ambos"):
        raise ValueError("formato: 'srt', 'vtt' ou 'ambos'")
    arquivo = Path(arquivo).expanduser().resolve()
    tr = transcricao or transcrever(arquivo, idioma=idioma, modelo=modelo, faixa=faixa, progresso=progresso)
    cues = montar_legendas(tr["segmentos"], max_caracteres, max_linhas, duracao_min, duracao_max,
                           fim_audio=(tr.get("inicio") or 0) + tr["duracao_audio"])
    if not cues:
        raise ValueError("nenhuma fala reconhecida: nada para legendar")
    original = Documento(arquivo).original
    lang = tr.get("idioma")
    stem = f"{original.stem}.{lang}" if lang else original.stem
    exts = ["srt", "vtt"] if fmt == "ambos" else [fmt]
    caminhos = []
    for ext in exts:
        if destino:
            d = Path(str(destino)).expanduser()
            if d.is_dir() or str(destino).endswith("/") or not d.suffix:
                alvo = d / f"{stem}.{ext}"
            else:
                alvo = d.with_suffix("." + ext) if len(exts) > 1 or d.suffix.lower() != "." + ext else d
        else:
            alvo = original.parent / f"{stem}.{ext}"
        conteudo = texto_srt(cues) if ext == "srt" else texto_vtt(cues)
        caminhos.append(str(_criar_exclusivo(alvo.resolve(), conteudo)))
    r = {"arquivos": caminhos, "legendas": len(cues), "idioma": lang, "modelo": tr["modelo"],
         "duracao_audio": tr["duracao_audio"], "previa": [
             f"[{fmt_tempo(c['inicio'])} → {fmt_tempo(c['fim'])}] " + " / ".join(c["linhas"]) for c in cues[:8]]}
    if tr.get("modelo_baixado"):
        r["modelo_baixado"] = tr["modelo_baixado"]
    if Path(tr["arquivo"]) != original:
        r["aviso"] = ("a legenda foi feita da versão atual (edições não salvas); se você não salvar, os tempos "
                      "podem não bater com o original")
    return r


# ---------------------------------------------------------------- estilo (libass)
_CORES = {
    "white": "FFFFFF", "branco": "FFFFFF", "branca": "FFFFFF", "black": "000000", "preto": "000000",
    "preta": "000000", "yellow": "FFFF00", "amarelo": "FFFF00", "amarela": "FFFF00", "red": "FF0000",
    "vermelho": "FF0000", "vermelha": "FF0000", "green": "00FF00", "verde": "00C800", "blue": "0000FF",
    "azul": "1E64FF", "cyan": "00FFFF", "ciano": "00FFFF", "magenta": "FF00FF", "rosa": "FF66CC",
    "pink": "FF66CC", "orange": "FF8C00", "laranja": "FF8C00", "gray": "808080", "grey": "808080",
    "cinza": "808080",
}


def cor_ass(cor: str) -> str:
    """'white', 'amarelo', '#RRGGBB' ou 'cor@opacidade' (0–1) → &HAABBGGRR do ASS (AA: 00 = opaco)."""
    s = str(cor).strip()
    op = 1.0
    if "@" in s:
        s, _, o = s.partition("@")
        op = max(0.0, min(1.0, float(o)))
    s = s.strip().lower()
    hexa = _CORES.get(s) or (s.lstrip("#").upper() if re.fullmatch(r"#?[0-9a-fA-F]{6}", s) else None)
    if not hexa:
        raise ValueError(f"cor desconhecida: {cor!r}. Use nome (branco, amarelo, preto...) ou #RRGGBB")
    rr, gg, bb = hexa[0:2], hexa[2:4], hexa[4:6]
    return f"&H{round((1 - op) * 255):02X}{bb}{gg}{rr}"


# posição → Alignment do ASS (teclado numérico do formato V4+: 2 = embaixo no centro, 8 = em cima, 5 = meio).
# Não uso force_style para isso: a libass 0.17.1 (Ubuntu 24.04) lê o Alignment do force_style no formato antigo do
# SSA (8 vira "meio à esquerda"), e versões novas no formato V4+. Escrevendo o estilo no próprio .ass o resultado
# é igual em qualquer versão.
_ALINHAMENTO = {"base": 2, "baixo": 2, "rodape": 2, "rodapé": 2, "inferior": 2,
                "topo": 8, "cima": 8, "superior": 8, "centro": 5, "meio": 5}


def campos_estilo(altura_ref: int, altura_video: int, tamanho=None, cor=None, contorno=None, cor_contorno=None,
                  posicao=None, margem=None, caixa=None, cor_caixa=None, fonte=None, negrito=None,
                  completo: bool = True, largura_video: int | None = None) -> dict:
    """Campos do estilo ASS. Medidas em pixels do vídeo (ou fração da altura) convertidas para a escala do
    script (PlayResY = altura_ref). completo=True preenche os padrões (legenda .srt/.vtt); False só o que foi
    pedido (para não estragar o estilo de um .ass do usuário)."""
    k = altura_ref / max(1, altura_video)
    c: dict[str, str] = {}

    def px(v, padrao_frac):
        if v is None:
            return altura_video * padrao_frac
        v = float(v)
        return v * altura_video if v < 1 else v

    if tamanho is not None or completo:
        # padrão: 5,5% do MENOR lado (num vídeo vertical, 5,5% da altura seria letra enorme)
        tam = px(tamanho, 0.055 * min(1.0, largura_video / altura_video) if largura_video else 0.055)
        c["Fontsize"] = f"{tam * k:.1f}"
    else:
        tam = None
    if fonte and re.search(r"[,\r\n]", str(fonte)):
        raise ValueError(f"fonte inválida: {fonte!r} (sem vírgula nem quebra de linha)")
    if fonte or completo:
        c["Fontname"] = str(fonte).strip() if fonte else "DejaVu Sans"
    if cor is not None or completo:
        c["PrimaryColour"] = c["SecondaryColour"] = cor_ass(cor or "white")
    if negrito is not None or completo:
        c["Bold"] = "-1" if negrito else "0"
    if posicao is not None or completo:
        pos = str(posicao or "base").strip().lower()
        if pos not in _ALINHAMENTO:
            raise ValueError(f"posicao: {', '.join(sorted(_ALINHAMENTO))}")
        c["Alignment"] = str(_ALINHAMENTO[pos])
    if margem is not None or completo:
        c["MarginV"] = str(round(px(margem, 0.05) * k))
        c["MarginL"] = c["MarginR"] = str(round(altura_video * 0.04 * k))
    if caixa:
        tb = tam if tam is not None else altura_video * 0.055
        c.update({"BorderStyle": "3", "OutlineColour": cor_ass(cor_caixa or "black@0.6"),
                  "BackColour": cor_ass(cor_caixa or "black@0.6"), "Outline": f"{max(1.0, tb * 0.18) * k:.1f}",
                  "Shadow": "0"})
    elif completo or contorno is not None or cor_contorno is not None:
        tb = tam if tam is not None else altura_video * 0.055
        borda = max(1.0, tb / 16) if contorno is None else float(contorno)
        c.update({"BorderStyle": "1", "Outline": f"{borda * k:.2f}", "Shadow": "0"})
        if cor_contorno is not None or completo:
            c["OutlineColour"] = c["BackColour"] = cor_ass(cor_contorno or "black")
    return c


_CAMPOS_COR = ("PrimaryColour", "SecondaryColour", "OutlineColour", "BackColour")
# HDR: o branco da legenda deve ficar em ~203 nits (ITU-R BT.2408), não no pico do formato (10 000 nits no PQ).
# Fração do sinal para 203 nits: PQ ≈ 0,58; HLG = 0,75. As cores do estilo são escaladas por esse fator.
_ESCALA_HDR = {"smpte2084": 0.58, "arib-std-b67": 0.75}


def _escalar_cor(valor: str, k: float) -> str:
    m = re.fullmatch(r"\s*&H([0-9A-Fa-f]{1,8})&?\s*", valor)
    if not m:
        return valor
    v = int(m.group(1), 16)
    a, b, g, r = (v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255
    b, g, r = (round(x * k) for x in (b, g, r))
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def preparar_ass(origem: Path, destino: Path, largura: int, altura: int, estilo: dict, completo: bool,
                 escala_cor: float = 1.0) -> Path:
    """Gera o .ass que vai ser queimado. .srt/.vtt: lido por `ler_legenda` e escrito como ASS com o texto
    escapado (chaves e barras do texto não viram comandos), PlayRes = tamanho do vídeo (medidas em pixels reais)
    e o estilo Default reescrito. .ass do usuário: copiado, com os campos pedidos aplicados a todos os estilos
    (na escala do PlayResY dele). escala_cor < 1 (vídeo HDR): escurece as cores dos estilos."""
    if origem.suffix.lower() in (".ass", ".ssa"):
        texto = origem.read_text(encoding="utf-8-sig", errors="replace")
    else:
        cues = ler_legenda(origem)
        if not cues:
            raise ValueError(f"nenhuma legenda reconhecida em {origem.name}")
        texto = texto_ass(cues, largura, altura)
    m = re.search(r"(?m)^PlayResY:\s*(\d+)", texto)
    ref = int(m.group(1)) if m else 288
    campos = campos_estilo(ref, altura, completo=completo, largura_video=largura, **estilo)
    fmt: list[str] = []
    linhas = []
    for ln in texto.splitlines():
        if re.match(r"(?i)^format:", ln) and not fmt and "Fontname" in ln:
            fmt = [f.strip() for f in ln.split(":", 1)[1].split(",")]
        elif re.match(r"(?i)^style:", ln) and fmt:
            vals = [v.strip() for v in ln.split(":", 1)[1].split(",", len(fmt) - 1)]
            for nome, val in campos.items():
                if nome in fmt:
                    vals[fmt.index(nome)] = val
            if escala_cor != 1.0:
                for nome in _CAMPOS_COR:
                    if nome in fmt and fmt.index(nome) < len(vals):
                        vals[fmt.index(nome)] = _escalar_cor(vals[fmt.index(nome)], escala_cor)
            ln = "Style: " + ",".join(vals)
        linhas.append(ln)
    destino.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return destino


def _legenda_do_documento(doc: Documento, legenda, idioma, modelo, faixa, max_caracteres, max_linhas,
                          progresso) -> tuple[Path, dict | None]:
    """Arquivo de legenda a usar: o dado pelo usuário, ou um .srt gerado agora da versão atual (guardado na
    pasta de versões, para o comando registrado continuar válido)."""
    if legenda:
        p = Path(str(legenda)).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"legenda não encontrada: {p}")
        if p.suffix.lower() not in (".srt", ".vtt", ".ass", ".ssa"):
            raise ValueError("legenda: arquivo .srt, .vtt, .ass ou .ssa")
        return p, None
    tr = transcrever(doc.atual(), idioma=idioma, modelo=modelo, faixa=faixa,
                     progresso=(lambda f: progresso(0.5 * f)) if progresso else None)
    cues = montar_legendas(tr["segmentos"], max_caracteres, max_linhas,
                           fim_audio=(tr.get("inicio") or 0) + tr["duracao_audio"])
    if not cues:
        raise ValueError("nenhuma fala reconhecida: nada para legendar")
    doc.pasta.mkdir(exist_ok=True)
    n = len(doc._ler()["versoes"]) + 1
    p = _criar_exclusivo(doc.pasta / f"legenda_v{n:03d}.srt", texto_srt(cues))
    return p, tr


def gravar_legenda(doc: Documento, legenda: str | Path | None = None, idioma: str = "auto",
                   modelo: str = MODELO_PADRAO, faixa: int = 1, tamanho=None, cor: str | None = None,
                   contorno: float | None = None, cor_contorno: str | None = None, posicao: str | None = None,
                   margem: float | None = None, caixa: bool | None = None, cor_caixa: str | None = None,
                   fonte: str | None = None, negrito: bool | None = None, max_caracteres: int = 42,
                   max_linhas: int = 2, progresso=None) -> dict:
    """QUEIMA a legenda na imagem (filtro `subtitles`/libass; nova versão; áudio copiado).
    legenda: .srt/.vtt/.ass existente; sem ela, transcreve a versão atual e usa o resultado.
    Estilo (num .srt/.vtt, o que não for dado usa o padrão; num .ass só o que for dado muda o estilo dele):
    tamanho (px ou fração da altura; padrão 5,5% do menor lado), cor (branco), contorno (px; padrão tamanho/16), cor_contorno
    (preto), posicao (base/topo/centro), margem (px ou fração; padrão 5%), caixa (fundo; cor_caixa
    'black@0.6'), fonte (DejaVu Sans), negrito.
    O estilo é escrito num .ass na pasta de versões (legenda_vNNN.estilo.ass) com PlayRes = tamanho do vídeo."""
    if not core._tem_opcao_filtro("subtitles", "filename"):
        raise FalaIndisponivel("este ffmpeg não tem o filtro `subtitles` (libass); veja `ai-editor --diagnostico`")
    arq, tr = _legenda_do_documento(doc, legenda, idioma, modelo, faixa, max_caracteres, max_linhas, progresso)
    eh_ass = arq.suffix.lower() in (".ass", ".ssa")
    estilo = {"tamanho": tamanho, "cor": cor, "contorno": contorno, "cor_contorno": cor_contorno,
              "posicao": posicao, "margem": margem, "caixa": caixa, "cor_caixa": cor_caixa, "fonte": fonte,
              "negrito": negrito}
    doc.pasta.mkdir(exist_ok=True)
    n = len(doc._ler()["versoes"]) + 1

    def montar(info):
        trc = str((info.get("v") or {}).get("color_transfer") or "")
        ass = preparar_ass(arq, doc.pasta / f"legenda_v{n:03d}.estilo.ass", info["w"], info["h"], estilo,
                           completo=not eh_ass, escala_cor=_ESCALA_HDR.get(trc, 1.0))
        g = f"[0:V:0]subtitles=filename={core.esc_filtro(str(ass))}[vo]"
        return [g], f"legenda gravada no vídeo ({arq.name})"

    prog = (lambda f: progresso(0.5 + 0.5 * f)) if (progresso and tr) else progresso
    v = core._editar_quadros(doc, montar, None, prog)
    v = {**v, "legenda": str(arq)}
    if tr:
        v["detalhes"] = {"transcricao": {"idioma": tr.get("idioma"), "modelo": tr["modelo"],
                                         "segmentos": len(tr["segmentos"]), "cache": tr.get("cache")}}
        if tr.get("modelo_baixado"):
            v["detalhes"]["modelo_baixado"] = tr["modelo_baixado"]
    return v


def _idioma_do_nome(stem: str) -> str | None:
    """Idioma no fim do nome de uma legenda: 'video.pt' / 'video.pt-BR' / 'video.pt.2' / 'video.eng' → código."""
    partes = stem.lower().split(".")[1:]
    while partes and partes[-1].isdigit():  # video.pt.2 (cópia numerada por _criar_exclusivo)
        partes.pop()
    if not partes or not re.fullmatch(r"[a-z]{2,3}(?:[-_][a-z0-9]{2,4})?", partes[-1]):
        return None
    try:
        lang = norm_idioma(partes[-1])
    except ValueError:
        return None
    return None if lang == "auto" else lang


def embutir_legenda(doc: Documento, legenda: str | Path | None = None, idioma: str = "auto",
                    modelo: str = MODELO_PADRAO, faixa: int = 1, titulo: str | None = None,
                    padrao: bool = True, max_caracteres: int = 42, max_linhas: int = 2,
                    progresso=None) -> dict:
    """Acrescenta a legenda como FAIXA de legenda (liga/desliga no player), sem re-encodar vídeo nem áudio
    (nova versão). MP4/MOV/M4V → mov_text; MKV → SRT (ou ASS, se a legenda for .ass); WebM → WebVTT.
    As legendas que já existiam no arquivo são mantidas."""
    entrada = doc.atual()
    ext = entrada.suffix.lower()
    arq, tr = _legenda_do_documento(doc, legenda, idioma, modelo, faixa, max_caracteres, max_linhas, progresso)
    eh_ass = arq.suffix.lower() in (".ass", ".ssa")
    if ext in (".mp4", ".mov", ".m4v"):
        codec = "mov_text"
    elif ext == ".mkv":
        codec = "ass" if eh_ass else "srt"
    elif ext == ".webm":
        codec = "webvtt"
    else:
        raise ValueError(f"o contêiner {ext} não guarda faixa de legenda de texto; exporte para .mkv ou .mp4 "
                         f"ou use `gravar_legenda` (queima na imagem)")
    probe = core.ffprobe(entrada)
    n_leg = sum(1 for s in probe["streams"] if s.get("codec_type") == "subtitle")
    lang = (tr or {}).get("idioma") or (norm_idioma(idioma) if norm_idioma(idioma) != "auto" else None)
    if not lang:  # legenda do usuário: tenta o sufixo do nome (video.pt.srt, video.pt-BR.srt, video.pt.2.srt)
        lang = _idioma_do_nome(arq.stem)
    iso = _ISO639_2.get(lang or "", "und")
    saida = doc.proxima_saida(ext)
    cmd = core.FFMPEG + ["-i", str(entrada), "-i", str(arq), "-map", "0:V?", "-map", "0:a?", "-map", "0:s?"]
    # capa (attached_pic: o 0:V acima a exclui) e fluxos de dados que o contêiner aceita
    for st in probe["streams"]:
        if st.get("codec_type") == "video" and (st.get("disposition") or {}).get("attached_pic"):
            cmd += ["-map", f"0:{st['index']}"]
        elif st.get("codec_type") == "data" and ext in (".mp4", ".mov", ".m4v"):
            tag = str(st.get("codec_tag_string") or "").lower()
            # tmcd: o muxer recria a partir da tag timecode; 'text': trilha de capítulos (recriada pelos
            # capítulos copiados); mapear duplicaria. Outros (telemetria gpmd/GoPro, etc.) são mantidos.
            if tag not in ("tmcd", "text"):
                cmd += ["-map", f"0:{st['index']}"]
    if ext == ".mkv":
        cmd += ["-map", "0:t?"]
    cmd += ["-map", "1:0", "-c", "copy", f"-c:s:{n_leg}", codec, f"-metadata:s:s:{n_leg}", f"language={iso}"]
    if titulo:
        cmd += [f"-metadata:s:s:{n_leg}", f"title={titulo}"]
        if codec == "mov_text":  # nome que players de MP4/MOV (QuickTime, VLC) mostram no menu de legendas
            cmd += [f"-metadata:s:s:{n_leg}", f"handler_name={titulo}"]
    if padrao:
        cmd += [f"-disposition:s:{n_leg}", "default"]
        for i in range(n_leg):
            cmd += [f"-disposition:s:{i}", "0"]
    cmd += ["-map_metadata", "0"] + core._faststart(saida) + [str(saida)]
    prog = (lambda f: progresso(0.5 + 0.5 * f)) if (progresso and tr) else progresso
    desc = f"faixa de legenda embutida ({codec}, {iso}{', ' + titulo if titulo else ''})"
    v = core._rodar_versao(doc, saida, [cmd], [core.duracao(probe)], desc, prog)
    v = {**v, "legenda": str(arq), "faixa_legenda": n_leg + 1}
    if tr and tr.get("modelo_baixado"):
        v["detalhes"] = {"modelo_baixado": tr["modelo_baixado"]}
    return v


# ---------------------------------------------------------------- edição pelo texto
def normalizar(texto: str) -> str:
    """Minúsculas, sem acento e sem pontuação (para comparar falas)."""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", t)).strip()


def _palavras_norm(segmentos: list[dict]) -> list[dict]:
    """Palavras normalizadas com tempo ('bem-vindos' vira 'bem' + 'vindos', dividindo o tempo)."""
    out = []
    for w in _palavras_de(segmentos):
        partes = normalizar(w["texto"]).split()
        if not partes:
            continue
        d = (w["fim"] - w["inicio"]) / len(partes)
        for k, p in enumerate(partes):
            out.append({"n": p, "inicio": w["inicio"] + k * d, "fim": w["inicio"] + (k + 1) * d,
                        "texto": w["texto"], "seg": w["seg"]})
    return out


def _vale(arquivo: str, faixa: int, a: float, b: float, alvo: float) -> float | None:
    """Instante de menor energia em [a, b] (quadros de 10 ms), com leve preferência pelo mais perto de `alvo`
    (+40 dB por segundo de distância), para não cair no fechamento de uma consoante longe da fronteira."""
    if b - a < 0.03:
        return None
    try:
        import numpy as np
        r = subprocess.run(core.FFMPEG + ["-ss", f"{max(0.0, a):.3f}", "-t", f"{b - max(0.0, a):.3f}", "-i",
                                          arquivo, "-map", f"0:a:{int(faixa) - 1}",
                                          "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", "16000",
                                          "-f", "s16le", "-"], capture_output=True, timeout=60,
                           preexec_fn=_preexec())
        x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768
    except Exception:  # noqa: BLE001
        return None
    n = len(x) // 160
    if n < 3:
        return None
    rms = np.sqrt(np.mean(x[: n * 160].reshape(n, 160) ** 2, axis=1) + 1e-12)
    rms = np.convolve(rms, np.ones(3) / 3, mode="same")
    db = 20 * np.log10(rms + 1e-9)
    t = max(0.0, a) + (np.arange(n) + 0.5) * 0.01
    k = int(np.argmin(db + 40 * np.abs(t - alvo)))
    return round(float(t[k]), 3)


def _similaridade(alvo: list[str], janela: list[str]) -> float:
    """Semelhança (0–1) entre as palavras procuradas e as de uma janela da transcrição. Base: razão de
    caracteres (difflib) das frases normalizadas. Palavras curtas (até 4 letras) precisam aparecer IGUAIS
    (normalizadas): 'não' ≠ 'no'/'ao', 'arte' ≠ 'até' — com poucas letras, 1 letra diferente é outra palavra.
    Em frases de 4+ palavras tolera 1 palavra curta diferente a cada 4 (erro de reconhecimento em "de"/"do")."""
    casadas: set[int] = set()
    for bl in difflib.SequenceMatcher(None, alvo, janela, autojunk=False).get_matching_blocks():
        casadas.update(range(bl.a, bl.a + bl.size))
    def colada(w):  # frase longa: "é leve" reconhecido como "releve" (palavra curta grudada noutra)
        return len(alvo) >= 4 and len(w) >= 3 and any(len(j) > len(w) and (j.endswith(w) or j.startswith(w))
                                                      for j in janela)
    curtas_erradas = sum(1 for k, w in enumerate(alvo) if len(w) <= 4 and k not in casadas and not colada(w))
    if curtas_erradas > (len(alvo) // 4 if len(alvo) >= 4 else 0):
        return 0.0
    # a 1ª e a última palavra da janela também precisam ser parecidas (não sobrar palavra de fora nas bordas);
    # busca de 1–2 palavras é mais exigente ('parte' ≠ 'até')
    minimo = 0.8 if len(alvo) <= 2 else 0.6
    for a_w, j_w in ((alvo[0], janela[0]), (alvo[-1], janela[-1])):
        if a_w != j_w and difflib.SequenceMatcher(None, a_w, j_w, autojunk=False).ratio() < minimo:
            return 0.0
    return difflib.SequenceMatcher(None, " ".join(alvo), " ".join(janela), autojunk=False).ratio()


def buscar_fala(transcricao: dict, texto: str, limiar: float = 0.75, margem_s: float = 0.1,
                max_resultados: int = 20, refinar: bool = True) -> dict:
    """Acha onde a frase `texto` é dita (busca aproximada: ignora acento, maiúsculas e pontuação; tolera
    pequenas diferenças de reconhecimento). Usa as palavras com tempo da `transcricao` (de `transcrever`).

    Devolve {encontrados: [{inicio, fim, texto, similaridade, contexto}], trechos: [[ini, fim], ...]}.
    `inicio`/`fim` já incluem uma folga de `margem_s` s para dentro da pausa vizinha, sem invadir a palavra
    anterior/seguinte. Sem pausa entre as palavras (fala emendada) e com refinar=True, a borda vai para o ponto
    de menor energia do áudio perto da fronteira estimada. Fluxo de edição pelo texto:
      1. `buscar_fala(texto='bom dia')` → trechos;
      2. (opcional) conferir com `ver_trecho`/ouvir e mostrar ao usuário (`marcar`);
      3. `remover_trechos(trechos=...)` para cortar a fala, ou `cortar(inicio, fim)` para ficar só com ela."""
    arq = transcricao.get("arquivo") if transcricao.get("arquivo") and Path(transcricao["arquivo"]).is_file() else None
    faixa = int(transcricao.get("faixa") or 1)
    alvo = normalizar(texto).split()
    if not alvo:
        raise ValueError("texto vazio")
    ws = _palavras_norm(transcricao.get("segmentos") or [])
    n = len(alvo)
    # nº de palavras da janela: igual ao procurado para 1–2 palavras; senão ±25% (o reconhecimento às vezes
    # junta ou separa palavras: "pra"/"para a")
    tol = 0 if n <= 2 else max(1, round(n * 0.25))
    cands = []
    for i in range(len(ws)):
        melhor = None
        for L in range(max(1, n - tol), n + tol + 1):
            if i + L > len(ws):
                break
            sim = _similaridade(alvo, [w["n"] for w in ws[i:i + L]])
            if melhor is None or sim > melhor[0]:
                melhor = (sim, L)
        if melhor and melhor[0] >= limiar:
            cands.append((melhor[0], i, melhor[1]))
    cands.sort(key=lambda c: (-c[0], c[1]))
    usados: set[int] = set()
    achados = []
    for sim, i, L in cands:
        idx = set(range(i, i + L))
        if idx & usados:
            continue
        usados |= idx
        a, b = ws[i]["inicio"], ws[i + L - 1]["fim"]
        ant = ws[i - 1]["fim"] if i > 0 else 0.0
        prox = ws[i + L]["inicio"] if i + L < len(ws) else None
        ini = max(ant, a - margem_s) if ant <= a else a
        fim = min(prox, b + margem_s) if prox is not None and prox >= b else b + margem_s
        ajuste = []
        if refinar and arq:
            # fala emendada (sem pausa medida entre as palavras): corta no "vale" de energia mais próximo da
            # fronteira estimada, em vez de confiar só no tempo do DTW (erro típico de ±0,15 s)
            if i > 0 and a - ant < 0.15:
                v = _vale(arq, faixa, max(ws[i - 1]["inicio"] + 0.05, a - 0.1), a + 0.1, a)
                if v is not None:
                    ini = v
                    ajuste.append("início")
            if prox is not None and prox - b < 0.15:
                v = _vale(arq, faixa, max(ws[i + L - 1]["inicio"] + 0.1, b - 0.1), b + 0.1, b)
                if v is not None:
                    fim = v
                    ajuste.append("fim")
        # texto original das palavras (sem repetir a mesma palavra dividida)
        orig = []
        for w in ws[i:i + L]:
            if not orig or orig[-1] is not w["texto"]:
                orig.append(w["texto"])
        seg = next((s for s in transcricao["segmentos"] if s["inicio"] - 0.5 <= a <= s["fim"] + 0.5), None)
        achados.append({"inicio": round(ini, 3), "fim": round(fim, 3), "texto": " ".join(orig),
                        "similaridade": round(sim, 3), "contexto": seg["texto"] if seg else None})
        if ajuste:
            achados[-1]["ajustado_pela_energia"] = ajuste
        if len(achados) >= max_resultados:
            break
    achados.sort(key=lambda x: x["inicio"])
    return {"procurado": texto, "encontrados": achados, "trechos": [[x["inicio"], x["fim"]] for x in achados]}


# ---------------------------------------------------------------- linha de comando
def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m ai_editor.fala", description=__doc__.split("\n")[0])
    ap.add_argument("--instalar-whisper", action="store_true", help=f"compila o whisper.cpp {WHISPER_VERSAO}")
    ap.add_argument("--baixar-modelo", metavar="NOME", help="baixa um modelo (" + ", ".join(MODELOS) + ")")
    ap.add_argument("--status", action="store_true", help="mostra motor e modelos")
    ap.add_argument("arquivo", nargs="?", help="transcreve este arquivo e imprime o texto")
    ap.add_argument("--idioma", default="auto")
    ap.add_argument("--modelo", default=MODELO_PADRAO)
    a = ap.parse_args(argv)
    if a.instalar_whisper:
        compilar_whisper()
    if a.baixar_modelo:
        def pr(f):
            print(f"\r  {f * 100:5.1f}%", end="", flush=True)
        p, novo = baixar_modelo(a.baixar_modelo, pr)
        print(f"\n{'baixado' if novo else 'já existia'}: {p}")
    if a.status or not (a.instalar_whisper or a.baixar_modelo or a.arquivo):
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    if a.arquivo:
        r = transcrever(a.arquivo, idioma=a.idioma, modelo=a.modelo, palavras=False)
        for s in r["segmentos"]:
            print(f"[{fmt_tempo(s['inicio'])} → {fmt_tempo(s['fim'])}] {s['texto']}")
        print(f"\n({r['idioma']}, {r['modelo']}, {r['duracao_audio']} s de áudio em {r['segundos_processamento']} s"
              f"{', cache' if r.get('cache') else ''})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
