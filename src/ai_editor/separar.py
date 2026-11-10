"""Separar voz, música e instrumentos de uma faixa de áudio (Demucs v4 em ONNX, sem PyTorch).

Modelos (Meta, Demucs v4 "Hybrid Transformer", código e pesos MIT), exportados para ONNX pelo StemSplit
(Hugging Face, MIT, STFT/iSTFT dentro do grafo, pesos guardados em fp16 e calculados em fp32). Vão DENTRO do
AppImage (`"embutir": True`; `objetos._pasta_modelo` olha o pacote primeiro); do código-fonte são baixados no
primeiro uso para ~/.local/share/ai-editor/modelos/<nome>/ com revisão, tamanho e SHA-256 fixos:
  - `htdemucs-ft-voz`: o especialista em voz do htdemucs_ft (o melhor Demucs para voz) — modos de voz;
  - `htdemucs-ft-bateria`/`-baixo`/`-outros`: os outros especialistas (instrumentos, qualidade alta/máxima);
  - `htdemucs`: 4 fontes numa rede só (instrumentos rápida; reforço na máxima);
  - `htdemucs-6s`: 6 fontes (+ guitarra/violão e piano).

Como o áudio passa pela rede:
  - a faixa (ou só o trecho, com 3 s de folga de cada lado) sai do ffmpeg em 44,1 kHz estéreo float32 por um
    pipe (mono é duplicado; 5.1 vira estéreo) — nada do áudio inteiro fica na memória;
  - blocos fixos de 7,8 s (343 980 amostras, o tamanho do modelo) com sobreposição (25%; 50% na máxima) e
    transição linear (as rampas somam 1); cada passada vai para um .f32 em disco;
  - qualidade = mais passadas (grade deslocada e canais trocados, média ponderada; ver `plano`); limpeza
    opcional = filtro de Wiener com todas as fontes; `fala` = a voz passa pelo DeepFilterNet;
  - "música" = mistura − voz final (voz + música = original); `so_voz`/`so_musica` = original − a outra parte,
    no ffmpeg na taxa original da faixa (`amerge` + `pan`), então fora do trecho fica o original;
  - a volta para a linha do tempo é amostra por amostra: `aresample` para a taxa da faixa, `adelay` em
    amostras até o início do trecho, e o tamanho final vem da faixa original (`amerge` com uma referência muda
    dela), com o mesmo start_time (`asetpts=PTS-STARTPTS+st/TB`).
Sessão ONNX: 4 threads, sem a arena de memória e SEM `ConstantFolding` — com ele o ONNX Runtime desdobra os
índices da iSTFT do export em constantes enormes (pico medido de 6–8 GB); sem ele, ~1,4 GB e a mesma velocidade.

Sem Qt (usado pelo servidor MCP). `python -m ai_editor.separar [--baixar MODELO...]`: situação dos modelos.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import binarios, core, objetos as ob
from .core import Documento, fmt_tempo

np = ob.np

SR = 44100
N_BLOCO = 343980                 # 7,8 s: entrada fixa do modelo
FOLGA_S = 3.0                    # contexto extra antes/depois de um trecho
FADE_S = 0.02                    # rampa nas bordas de um trecho (troca sem estalo)
FONTES = ("drums", "bass", "other", "vocals", "guitar", "piano")  # ordem de saída dos modelos

_REV_FT = "2ef0d757d3e226d0da85fb8c71514f464fcabdd0"
MODELOS = {
    "htdemucs-ft-voz": {
        "repo": "StemSplitio/htdemucs-ft-vocals-onnx", "revisao": _REV_FT,
        "licenca": "MIT (Demucs v4 htdemucs_ft, Meta; export ONNX do StemSplit)",
        "arquivos": [("htdemucs_ft_vocals_fp16weights.onnx", 165612636,
                      "0cbe651f535415c9d26a7bb614f7d322dd5a080fa0298f2e50f478030a994dce")],
        "embutir": True,  # vai no AppImage (todas as qualidades usam)
        "fontes": ("vocals",),  # as outras 3 saídas do especialista são subprodutos ruins: ignoradas
    },
    **{f"htdemucs-ft-{nome}": {
        "repo": "StemSplitio/htdemucs-ft-onnx", "revisao": "8616370ed541bc183dbe15fb0d54d5f49918f47e",
        "licenca": "MIT (Demucs v4 htdemucs_ft, Meta; export ONNX do StemSplit)",
        "arquivos": [(f"htdemucs_ft_{fonte}_fp16weights.onnx", 165612636, sha)],
        "embutir": True,  # alta/máxima (instrumentos)
        "fontes": (fonte,),
    } for nome, fonte, sha in (
        ("bateria", "drums", "047764dff888cfb87da917013377d4ec7a134f7419cbe486d9c339aa17975ddd"),
        ("baixo", "bass", "b533037176b14b2df31c92a5d5b3d5660d0811b9b360d3db761964768b079961"),
        ("outros", "other", "b739171a7057b3107bb0711c6222d4a619b41b13a8f04026431d30f32ad2bd71"))},
    "htdemucs": {
        "repo": "StemSplitio/htdemucs-onnx", "revisao": "d54ed9eb60e258ea82131c6ee14578628816456a",
        "licenca": "MIT (Demucs v4 htdemucs, Meta; export ONNX do StemSplit)",
        "arquivos": [("htdemucs_fp16weights.onnx", 165612636,
                      "d05c269d0178d2a72ad484b10b11dd370193fc923201c3b27a99f848745db70a")],
        "embutir": True,  # rápida (instrumentos) e máxima
        "fontes": FONTES[:4],
    },
    "htdemucs-6s": {
        "repo": "StemSplitio/htdemucs-6s-onnx", "revisao": "49df9b6989cf2150840ea65b0bef77a2e471b678",
        "licenca": "MIT (Demucs v4 htdemucs_6s, Meta; export ONNX do StemSplit)",
        "arquivos": [("htdemucs_6s_fp16weights.onnx", 136428532,
                      "7ce55792e2231c93fbf92de95f5fd5b3a5e6c89f7db690dfd693e8f1dce56869")],
        "embutir": True,  # guitarra/piano
        "fontes": FONTES,
    },
}
# os downloads, o status (`objetos.disponivel`, ferramenta `objetos_ia`) e o cache de modelos são os da Fase 7
ob.MODELOS.update({k: {c: v for c, v in m.items() if c != "fontes"} for k, m in MODELOS.items()})

# nomes em português → fonte do modelo; títulos das faixas novas e sufixos dos arquivos
_NOMES = {
    "voz": "vocals", "vozes": "vocals", "vocal": "vocals", "vocais": "vocals", "canto": "vocals",
    "vocals": "vocals", "bateria": "drums", "percussao": "drums", "percussão": "drums", "drums": "drums",
    "baixo": "bass", "bass": "bass", "guitarra": "guitar", "violao": "guitar", "violão": "guitar",
    "guitar": "guitar", "piano": "piano", "teclado": "piano", "teclados": "piano", "outros": "other",
    "outro": "other", "resto": "other", "other": "other",
}
TITULOS = {"vocals": "Voz", "drums": "Bateria", "bass": "Baixo", "other": "Outros", "guitar": "Guitarra/violão",
           "piano": "Piano", "musica": "Música (sem voz)"}
SUFIXOS = {"vocals": "voz", "drums": "bateria", "bass": "baixo", "other": "outros", "guitar": "guitarra",
           "piano": "piano", "musica": "musica"}
_MODOS = {"faixas": "faixas", "voz_musica": "faixas", "so_voz": "so_voz", "voz": "so_voz",
          "so_musica": "so_musica", "karaoke": "so_musica", "karaokê": "so_musica", "sem_voz": "so_musica",
          "instrumentos": "instrumentos", "stems": "instrumentos"}
_FORMATOS = {"wav": ".wav", "flac": ".flac", "mp3": ".mp3", "m4a": ".m4a", "aac": ".m4a", "opus": ".opus",
             "ogg": ".ogg"}
_QUALIDADES = {"rapida": "rapida", "rápida": "rapida", "alta": "alta", "maxima": "maxima", "máxima": "maxima"}
_ESPECIALISTA = {"vocals": "htdemucs-ft-voz", "drums": "htdemucs-ft-bateria", "bass": "htdemucs-ft-baixo",
                 "other": "htdemucs-ft-outros"}


def _fontes_pedidas(instrumentos) -> list[str]:
    if not instrumentos:
        return list(FONTES[:4])
    if isinstance(instrumentos, str):
        instrumentos = instrumentos.replace(";", ",").split(",")
    r = []
    for x in instrumentos:
        k = str(x).strip().lower().replace(" ", "_")
        if k in ("todos", "tudo", "all"):
            return list(FONTES)
        if k not in _NOMES:
            raise ValueError(f"instrumento desconhecido: {x!r}; use voz, bateria, baixo, guitarra (violão), "
                             f"piano, outros ou 'todos'")
        if _NOMES[k] not in r:
            r.append(_NOMES[k])
    return r


def _p(modelo: str, desloc_s: float = 0.0, inverter: bool = False, peso: float = 1.0, fontes=None) -> dict:
    return {"modelo": modelo, "desloc": round(desloc_s * SR), "inverter": inverter, "peso": peso,
            "fontes": list(fontes or MODELOS[modelo]["fontes"])}


def plano(modo: str, pedidas: list[str], qualidade: str) -> dict:
    """Passadas da rede (modelo, deslocamento da grade, canais trocados, peso), sobreposição e o conjunto de
    fontes do filtro de Wiener, por qualidade. Medido na máquina alvo (ver plano.md):
      rapida: 1 passada, sobreposição 25%;
      alta:   + 1 passada deslocada 0,5 s com os canais trocados ("shifts" + inversão do Demucs) — no modo de
              instrumentos (4 fontes), os 4 especialistas do htdemucs_ft no lugar do htdemucs;
      maxima: 3–4 passadas deslocadas, sobreposição 50%, conjunto com outro modelo (htdemucs/6s) de peso menor."""
    if modo != "instrumentos":  # voz × acompanhamento: o especialista em voz do htdemucs_ft
        conjunto = ["vocals", "acomp"]
        v = "htdemucs-ft-voz"
        if qualidade == "rapida":
            ps, sob = [_p(v)], 0.25
        elif qualidade == "alta":
            ps, sob = [_p(v), _p(v, 0.5, True)], 0.25
        else:
            ps, sob = [_p(v), _p(v, 0.25, True), _p(v, 0.5), _p("htdemucs", 0.75, True, 0.5, ["vocals"])], 0.5
        return _adaptar({"passadas": ps, "sobrepor": sob, "conjunto": conjunto, "alvo": "vocals"})
    seis = any(f in ("guitar", "piano") for f in pedidas)
    if seis:
        m6 = "htdemucs-6s"
        if qualidade == "rapida":
            ps, sob = [_p(m6)], 0.25
        elif qualidade == "alta":
            ps, sob = [_p(m6), _p(m6, 0.5, True)], 0.25
        else:
            ps, sob = [_p(m6), _p(m6, 0.33, True), _p(m6, 0.66),
                       _p("htdemucs-ft-voz", 0.17, True, 1.0)], 0.5
        conjunto = list(FONTES)
    else:
        if qualidade == "rapida":
            ps, sob = [_p("htdemucs")], 0.25
        elif qualidade == "alta":
            ps, sob = [_p(_ESPECIALISTA[f]) for f in FONTES[:4]], 0.25
        else:
            ps = [_p(_ESPECIALISTA[f]) for f in FONTES[:4]] + [_p(_ESPECIALISTA[f], 0.5, True) for f in FONTES[:4]]
            ps.append(_p("htdemucs", 0.25, False, 0.5))
            sob = 0.5
        conjunto = list(FONTES[:4])
    alvo = pedidas[0] if len(pedidas) == 1 else None
    return _adaptar({"passadas": ps, "sobrepor": sob, "conjunto": conjunto, "alvo": alvo})


def _tem(modelo: str) -> bool:
    """Modelo utilizável: já em disco (pacote ou usuário) ou pode ser baixado (código-fonte)."""
    return binarios.downloads_permitidos() or ob.modelo_pronto(modelo)


def _adaptar(pl: dict) -> dict:
    """Pacote sem algum modelo (build com MODELOS_EMBUTIDOS reduzido): troca pelo que houver em vez de falhar.
    Passada de reforço (peso < 1) sem modelo é só retirada; htdemucs ↔ especialistas do htdemucs_ft se substituem."""
    novas = []
    for ps in pl["passadas"]:
        if _tem(ps["modelo"]):
            novas.append(ps)
        elif ps["peso"] < 1:
            continue
        elif ps["modelo"] == "htdemucs" and all(_tem(_ESPECIALISTA[f]) for f in ps["fontes"]):
            novas += [dict(ps, modelo=_ESPECIALISTA[f], fontes=[f]) for f in ps["fontes"]]
        elif ps["modelo"] in _ESPECIALISTA.values() and _tem("htdemucs"):
            novas.append(dict(ps, modelo="htdemucs", fontes=list(MODELOS[ps["modelo"]]["fontes"])))
        else:
            novas.append(ps)  # baixar_modelo dá o erro claro (o que falta e o que está embutido)
    vistas, unicas = set(), []
    for ps in novas:  # o mesmo modelo/deslocamento/inversão uma vez só (fontes juntas)
        c = (ps["modelo"], ps["desloc"], ps["inverter"])
        if c in vistas:
            u = next(u for u in unicas if (u["modelo"], u["desloc"], u["inverter"]) == c)
            u["fontes"] = sorted(set(u["fontes"]) | set(ps["fontes"]), key=FONTES.index)
        else:
            vistas.add(c); unicas.append(dict(ps))
    return dict(pl, passadas=unicas)


# ---------------------------------------------------------------- sessão ONNX
def _sessao(caminho: Path):
    """Como `objetos._sessao` (mesmo cache, solto após 5 min ocioso), mas sem `ConstantFolding` e sem arena:
    ver o cabeçalho do módulo (pico de memória)."""
    chave = f"separar:{caminho}"
    with ob._TRAVA_SESSOES:
        s = ob._SESSOES.get(chave)
        if s is None:
            ort = ob._ort()
            op = ort.SessionOptions()
            op.intra_op_num_threads = ob.THREADS
            op.inter_op_num_threads = 1
            op.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            op.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            op.enable_cpu_mem_arena = False
            op.enable_mem_pattern = False
            op.log_severity_level = 3
            s = ort.InferenceSession(str(caminho), op, providers=["CPUExecutionProvider"],
                                     disabled_optimizers=["ConstantFolding"])
            ob._SESSOES[chave] = s
        ob._USO["ultimo"] = time.monotonic()
        if ob._USO["vigia"] is None:
            ob._USO["vigia"] = threading.Thread(target=ob._vigiar_sessoes, name="ia-sessoes", daemon=True)
            ob._USO["vigia"].start()
        return s


# ---------------------------------------------------------------- separação em fluxo (uma passada da rede)
def _ler(f, n: int):
    """Até n quadros estéreo float32 do pipe (menos só no fim)."""
    precisa, partes = n * 8, []
    while precisa > 0:
        b = f.read(precisa)
        if not b:
            break
        partes.append(b)
        precisa -= len(b)
    dados = b"".join(partes)
    dados = dados[: len(dados) // 8 * 8]
    return np.frombuffer(dados, "<f4").reshape(-1, 2).T.copy()


@contextlib.contextmanager
def _extracao(entrada: Path, k: int, e0: int, e1: int | None):
    """ffmpeg → pipe: faixa k em 44,1 kHz estéreo float32, amostras [e0, e1) da linha do tempo da faixa."""
    trim = f"atrim=start_sample={e0}" + (f":end_sample={e1}" if e1 is not None else "")
    cmd = core.FFMPEG + ["-i", str(entrada), "-map", f"0:a:{k}", "-vn", "-sn", "-dn",
                         "-af", f"asetpts=PTS-STARTPTS,aresample={SR},{trim}",
                         "-ac", "2", "-f", "f32le", "-c:a", "pcm_f32le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            preexec_fn=core._morrer_com_pai if core._prctl is not None else None)
    err: list[bytes] = []
    leitor = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    leitor.start()
    try:
        yield proc.stdout
    except BaseException:
        proc.kill()
        raise
    finally:
        # quem lê pode parar antes do fim (trecho já coberto): aí o ffmpeg ainda roda e só morre no pipe fechado
        # (código de saída qualquer) — não é erro. Só vale o código de quem terminou sozinho.
        cedo = proc.poll() is None
        if cedo:
            proc.kill()
        with contextlib.suppress(Exception):
            proc.stdout.close()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        leitor.join(5)
    if not cedo and proc.returncode not in (0, -9, -13, -15, 255):
        raise core.ErroFFmpeg(f"extração do áudio falhou: {b''.join(err).decode(errors='replace')[-2000:]}")


def _passada(entrada: Path, k: int, e0: int, e1: int | None, lim0: int, lim1: int | None, modelo: str,
             fontes: list[str], desloc: int, inverter: bool, sobrepor: float, saidas: dict[str, Path],
             mistura: Path | None = None, esperado: int = 0, progresso=None, cancelar=None, etapa=None,
             rotulo: str = "") -> int:
    """Uma passada da rede sobre a faixa: blocos de 7,8 s com sobreposição `sobrepor` (fração) e transição
    linear (as rampas somam 1), grade deslocada `desloc` amostras (`shifts` do Demucs) e canais trocados se
    `inverter`. Grava em `saidas[fonte]` (f32 estéreo) as amostras [lim0, lim1) do trecho extraído [e0, e1);
    `mistura`: grava também a entrada nas mesmas posições. Devolve o nº de quadros gravados."""
    cancelar = cancelar or (lambda: False)
    sess = _sessao(ob.baixar_modelo(modelo) / MODELOS[modelo]["arquivos"][0][0])
    nome_saida, nome_entrada = sess.get_outputs()[0].name, sess.get_inputs()[0].name
    idx = [FONTES.index(f) for f in fontes]
    sob = max(1, min(N_BLOCO // 2, round(N_BLOCO * sobrepor)))
    passo = N_BLOCO - sob
    rampa = np.linspace(0, 1, sob, dtype=np.float32)  # rampa + rampa invertida = 1 em cada amostra
    a0, a1 = lim0 + desloc, (lim1 + desloc) if lim1 is not None else None  # na linha do bloco (com o deslocamento)
    arqs = {f: open(p, "wb") for f, p in saidas.items()}
    arq_mix = open(mistura, "wb") if mistura else None
    escritos = 0

    def emitir(y, p, x):
        nonlocal escritos
        a = max(p, a0)
        b = p + y.shape[2] if a1 is None else min(p + y.shape[2], a1)
        if b <= a:
            return
        for j, f in enumerate(fontes):
            arqs[f].write(np.ascontiguousarray(y[j, :, a - p:b - p].T, dtype="<f4").tobytes())
        if arq_mix:
            arq_mix.write(np.ascontiguousarray(x[:, a - p:b - p].T, dtype="<f4").tobytes())
        escritos += b - a

    try:
        with _extracao(entrada, k, e0, e1) as pipe:
            bloco = np.concatenate([np.zeros((2, desloc), np.float32), _ler(pipe, N_BLOCO - desloc)], axis=1)
            pos, primeiro, sobra, t0 = 0, True, None, time.monotonic()
            while bloco.shape[1] > 0:
                if cancelar():
                    raise ob.Cancelado("cancelado")
                ob._pausa_memoria(cancelar, etapa)
                n = bloco.shape[1]
                ultimo = n < N_BLOCO
                x = bloco if n == N_BLOCO else np.pad(bloco, ((0, 0), (0, N_BLOCO - n)))
                if inverter:
                    x = x[::-1]
                y = sess.run([nome_saida], {nome_entrada: np.ascontiguousarray(x)[None]})[0][0][idx]
                if inverter:
                    y = y[:, ::-1]
                if not primeiro:
                    y[:, :, :sob] *= rampa
                    y[:, :, :sob] += sobra
                if ultimo:
                    emitir(y[:, :, :n], pos, bloco[:, :n])
                    break
                y[:, :, passo:] *= rampa[::-1]
                emitir(y[:, :, :passo], pos, bloco[:, :passo])
                sobra = y[:, :, passo:].copy()
                primeiro = False
                pos += passo
                if a1 is not None and pos >= a1:
                    break  # o resto é só folga
                bloco = np.concatenate([bloco[:, passo:], _ler(pipe, passo)], axis=1)
                if progresso and esperado:
                    progresso(min(0.999, (pos - a0) / esperado))
                if etapa and esperado:
                    gasto = time.monotonic() - t0
                    falta = gasto / max(pos, 1) * max(0, esperado + a0 - pos)
                    etapa(f"{rotulo}separando: {fmt_tempo(max(0, pos - a0) / SR)} de ~{fmt_tempo(esperado / SR)} "
                          f"(esta passada: faltam ~{max(1, round(falta / 60))} min)")
    finally:
        for f in arqs.values():
            f.close()
        if arq_mix:
            arq_mix.close()
    if progresso:
        progresso(1.0)
    return escritos


# ---------------------------------------------------------------- combinação + filtro de Wiener (numpy, em blocos)
N_FFT, HOP = 4096, 1024
_JANELA = None


def _janela():
    global _JANELA
    if _JANELA is None:
        _JANELA = np.hanning(N_FFT + 1)[:-1].astype(np.float32)
    return _JANELA


def _stft(x):
    """x (C, L), L = HOP·(T−1) + N_FFT → (C, N_FFT/2+1, T) complex64."""
    w = _janela()
    t = 1 + (x.shape[-1] - N_FFT) // HOP
    q = np.lib.stride_tricks.as_strided(x, (x.shape[0], t, N_FFT), (x.strides[0], x.strides[1] * HOP, x.strides[1]))
    return np.fft.rfft(q * w, axis=-1).astype(np.complex64).transpose(0, 2, 1)


def _istft(z, n: int):
    """Inversa com soma ponderada (WOLA); n amostras."""
    w = _janela()
    q = np.fft.irfft(z.transpose(0, 2, 1), n=N_FFT, axis=-1).astype(np.float32) * w
    out = np.zeros((z.shape[0], n), np.float32)
    norma = np.zeros(n, np.float32)
    for i in range(q.shape[1]):
        out[:, i * HOP:i * HOP + N_FFT] += q[:, i]
        norma[i * HOP:i * HOP + N_FFT] += w * w
    return out / np.maximum(norma, 1e-8)


def _suavizar(a):
    """Média 3×3 (frequência × tempo) — tira o "ruído musical" das máscaras."""
    p = np.pad(a, [(0, 0)] * (a.ndim - 2) + [(1, 1), (1, 1)], mode="edge")
    return sum(p[..., i:i + a.shape[-2], j:j + a.shape[-1]] for i in range(3) for j in range(3)) / 9


_LIMPEZAS = {"nao": 0, "não": 0, "nenhuma": 0, "off": 0, "leve": 1, "media": 2, "média": 2, "forte": 3}


def _mascaras(S: list, alvo: int | None, nivel: int, gama_t=None):
    """Máscaras de Wiener (|S_j|^p / Σ|S|^p) que somam 1. nivel 1: p=1; 2: p=2; 3: p=2, máscaras suavizadas e a
    do alvo afiada (m^γ, γ=2: some onde as outras fontes dominam), as demais reescaladas para a soma continuar 1.
    gama_t (T,): γ por quadro (limpeza mais forte num trecho)."""
    p = 1.0 if nivel == 1 else 2.0
    a = [np.abs(s) ** p + 1e-10 for s in S]
    tot = sum(a)
    m = [x / tot for x in a]
    if nivel >= 3 or gama_t is not None:
        m = [_suavizar(x) for x in m] if nivel >= 3 else m
        if alvo is None:  # várias fontes pedidas: afia todas (equivale a p maior)
            g = 2.0 if gama_t is None else gama_t[None, None, :]
            m = [x ** g for x in m]
            tot = sum(m)
            m = [x / tot for x in m]
        else:
            g = (2.0 if nivel >= 3 else 1.0) if gama_t is None else gama_t[None, None, :]
            mt = m[alvo] ** g
            resto = (1 - mt) / np.maximum(1 - m[alvo], 1e-6)
            m = [mt if j == alvo else x * resto for j, x in enumerate(m)]
    return m


def _combinar(passadas: list[dict], mistura: Path, n: int, conjunto: list[str], pedidas: list[str],
              saidas: dict[str, Path], limpeza: int, alvo: str | None, borda_ini: bool, borda_fim: bool,
              gama_trecho: tuple[int, int, float] | None = None, cancelar=None, progresso=None) -> None:
    """Média ponderada das passadas por fonte; com `limpeza` > 0, filtro de Wiener sobre a MISTURA com as
    máscaras de todas as fontes do `conjunto` (as fontes passam a somar exatamente a mistura e o vazamento entre
    elas cai). 'acomp' no conjunto = mistura − média da voz; 'musica' pedida = mistura − voz final.
    Grava as `pedidas` em `saidas`, com rampas de 20 ms nas bordas do trecho. Blocos de 20 s + margem."""
    cancelar = cancelar or (lambda: False)
    mm = np.memmap(mistura, "<f4", "r", shape=(n, 2))
    pm = [(np.memmap(p["arquivos"][f], "<f4", "r", shape=(n, 2)), f, p["peso"]) for p in passadas
          for f in p["arquivos"]]
    pesos = {f: sum(w for _, g, w in pm if g == f) for f in conjunto}
    arqs = {f: open(saidas[f], "wb") for f in pedidas}
    nf = max(1, round(FADE_S * SR))
    nucleo, margem = 20 * SR, 2 * N_FFT
    try:
        for c0 in range(0, n, nucleo):
            if cancelar():
                raise ob.Cancelado("cancelado")
            c1 = min(n, c0 + nucleo)
            s0, s1 = max(0, c0 - margem), min(n, c1 + margem)
            x = np.asarray(mm[s0:s1]).T
            est = {}
            for f in conjunto:
                if f == "acomp":
                    continue
                acc = sum(np.asarray(arr[s0:s1]).T * w for arr, g, w in pm if g == f)
                est[f] = (acc / pesos[f]).astype(np.float32)
            if "acomp" in conjunto:
                est["acomp"] = x - est["vocals"]
            if limpeza > 0:
                # STFT com margem e enchimento até um nº inteiro de quadros; a inversa volta ao mesmo tamanho
                pad_e = N_FFT if s0 == 0 else 0           # no começo do arquivo, cobertura completa da janela
                L = x.shape[1] + pad_e
                t = -(-L // HOP) + 1                        # quadros até cobrir L + N_FFT
                tot_l = HOP * (t - 1) + N_FFT
                def prep(a):
                    return np.pad(a, ((0, 0), (pad_e, tot_l - L)))
                X = _stft(prep(x))
                S = [_stft(prep(est[f])) for f in conjunto]
                gama_t = None
                if gama_trecho is not None:
                    g0, g1, g = gama_trecho
                    centros = (np.arange(X.shape[-1]) * HOP + N_FFT // 2 - pad_e + s0)
                    dentro = ((centros >= g0) & (centros < g1)).astype(np.float32)
                    base = 2.0 if limpeza >= 3 else 1.0
                    gama_t = (base + (g - base) * dentro).astype(np.float32)
                ia = conjunto.index(alvo) if alvo in conjunto else None
                ms = _mascaras(S, ia, limpeza, gama_t)
                for j, f in enumerate(conjunto):
                    if f in pedidas or (f == "vocals" and "musica" in pedidas):
                        est[f] = _istft(ms[j] * X, tot_l)[:, pad_e:pad_e + x.shape[1]]
                del X, S, ms
            if "musica" in pedidas:
                est["musica"] = x - est["vocals"]
            pos = np.arange(c0, c1)
            g = np.ones(c1 - c0, np.float32)
            if borda_ini:
                g *= np.clip(pos / nf, 0, 1)
            if borda_fim:
                g *= np.clip((n - 1 - pos) / nf, 0, 1)
            for f in pedidas:
                y = est[f][:, c0 - s0:c1 - s0] * g[None, :]
                arqs[f].write(np.ascontiguousarray(y.T, dtype="<f4").tobytes())
            if progresso:
                progresso(c1 / n)
    finally:
        for a in arqs.values():
            a.close()
        del mm, pm


FALA_ATENUACAO_DB = 24.0  # medido: 24 ≈ sem limite na voz com vazamento e um pouco melhor na voz já limpa


def _deepfilter(voz: Path, n: int, pasta: Path, cancelar=None) -> Path:
    """Voz separada (f32 estéreo 44,1 kHz, n quadros) → DeepFilterNet (48 kHz) → f32 44,1 kHz com n quadros."""
    from . import deepfilter
    d = pasta / "dfn"
    d.mkdir(exist_ok=True)
    wav = d / "voz.wav"
    core.rodar(core.FFMPEG + ["-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", str(voz),
                              "-af", f"aresample={deepfilter.SR}", "-c:a", "pcm_f32le", str(wav)])
    if cancelar and cancelar():
        raise ob.Cancelado("cancelado")
    s16, _ = deepfilter.processar(wav, 0, 0.0, None, 2, FALA_ATENUACAO_DB, d)
    wav.unlink(missing_ok=True)
    out = pasta / "voz.dfn.f32"
    core.rodar(core.FFMPEG + ["-f", "s16le", "-ar", str(deepfilter.SR), "-ac", "2", "-i", str(s16),
                              "-af", f"aresample={SR}", "-f", "f32le", "-c:a", "pcm_f32le", str(out)])
    shutil.rmtree(d, ignore_errors=True)
    with open(out, "r+b") as f:  # exatamente n quadros (a reamostragem ida e volta pode errar por 1)
        f.truncate(min(f.seek(0, 2), n * 8))
        f.seek(0, 2)
        if f.tell() < n * 8:
            f.write(b"\0" * (n * 8 - f.tell()))
    return out


def _finalizar_fala(mistura: Path, crus: dict[str, Path], n: int, pedidas: list[str], saidas: dict[str, Path],
                    borda_ini: bool, borda_fim: bool) -> None:
    """Saídas finais com a voz limpa pelo DeepFilterNet: voz = V, música = mistura − V (soma = mistura), as
    outras fontes como vieram; rampas de 20 ms nas bordas do trecho."""
    mm = np.memmap(mistura, "<f4", "r", shape=(n, 2))
    fs = {f: np.memmap(p, "<f4", "r", shape=(n, 2)) for f, p in crus.items()}
    arqs = {f: open(saidas[f], "wb") for f in pedidas}
    nf = max(1, round(FADE_S * SR))
    try:
        for c0 in range(0, n, 20 * SR):
            c1 = min(n, c0 + 20 * SR)
            pos = np.arange(c0, c1)
            g = np.ones(c1 - c0, np.float32)
            if borda_ini:
                g *= np.clip(pos / nf, 0, 1)
            if borda_fim:
                g *= np.clip((n - 1 - pos) / nf, 0, 1)
            v = np.asarray(fs["vocals"][c0:c1])
            for f in pedidas:
                y = (np.asarray(mm[c0:c1]) - v) if f == "musica" else (v if f == "vocals" else np.asarray(fs[f][c0:c1]))
                arqs[f].write(np.ascontiguousarray(y * g[:, None], dtype="<f4").tobytes())
    finally:
        for a in arqs.values():
            a.close()
        del mm, fs


# ---------------------------------------------------------------- volta para a linha do tempo (ffmpeg)
def _fmt(sr: int, canais: int) -> str:
    """aformat completo antes do `amerge` (sem taxa e layout fixos ele não negocia os formatos)."""
    return f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={'mono' if canais == 1 else 'stereo'}"


def _cadeia_stem(i_in: int, sr: int, canais: int, atraso: int) -> str:
    """Fonte separada (f32 44,1 kHz) na taxa da faixa, mono se ela é mono, deslocada `atraso` amostras e com
    silêncio infinito no fim (quem a usa corta pelo tamanho da faixa original, via `amerge`)."""
    mono = ",pan=mono|c0=0.5*c0+0.5*c1" if canais == 1 else ""
    ad = f",adelay=delays={atraso}S:all=1" if atraso > 0 else ""
    return f"[{i_in}:a]aresample={sr},asetpts=PTS-STARTPTS{mono}{ad},apad,{_fmt(sr, canais)}"


def _pan(canais: int, desloc: int, menos: bool) -> str:
    """pan sobre a saída de `amerge` [A (desloc canais) | fonte (canais)]: só a fonte, ou A − fonte."""
    lay = "mono" if canais == 1 else "stereo"
    return f"pan={lay}|" + "|".join(
        f"c{i}=" + (f"c{i}-c{i + desloc}" if menos else f"c{i + desloc}") for i in range(canais))


def _orig(s: dict, k: int, sr: int, canais: int) -> str:
    """Faixa original na linha do tempo da faixa (0 = início dela), em mono/estéreo (5.1 → estéreo)."""
    return f"[0:a:{k}]{core._filtro_layout(s)}asetpts=PTS-STARTPTS,{_fmt(sr, canais)}"


def _ref(k: int, sr: int, canais: int) -> str:
    """Referência de tamanho: silêncio com `canais` canais, do tamanho exato da faixa original (mono+mono e
    estéreo+estéreo no `amerge` dão layouts padrão; mono+estéreo = 3 canais, que o amerge não negocia)."""
    pan = "mono|c0=0*c0" if canais == 1 else "stereo|c0=0*c0|c1=0*c0"
    return f"[0:a:{k}]asetpts=PTS-STARTPTS,pan={pan},{_fmt(sr, canais)}"


def _entradas_brutas(arqs: list[Path]) -> list[str]:
    r = []
    for p in arqs:
        r += ["-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", str(p)]
    return r


# ---------------------------------------------------------------- função principal
def separar(doc: Documento, modo: str = "faixas", faixa: int = 1, inicio=None, fim=None,
            instrumentos=None, qualidade: str = "alta", limpeza: str | None = None, limpeza_inicio=None,
            limpeza_fim=None, destino: str = "faixas", formato: str = "wav", pasta_destino: str | None = None,
            fala: bool | None = None, estado: dict | None = None, progresso=None, cancelar=None,
            etapa=None) -> dict:
    """Separa a faixa `faixa` (1 = A1) do documento.
    modo 'faixas' (padrão): acrescenta 2 faixas — Voz e Música (mistura − voz; voz + música = mistura).
    modo 'so_voz': a faixa passa a ter só a voz ("tirar a música"). 'so_musica' (karaokê): só o acompanhamento.
    modo 'instrumentos': fontes do Demucs (`instrumentos`: voz, bateria, baixo, guitarra/violão, piano, outros
      ou 'todos'; padrão = as 4 do htdemucs; guitarra/piano usam o htdemucs-6s).
    qualidade: 'rapida' | 'alta' (padrão) | 'maxima' (ver `plano`).
    limpeza: 'nao' | 'leve' | 'media' | 'forte' — filtro de Wiener com todas as fontes sobre a mistura (tira
      vazamento de uma fonte na outra); padrão 'nao' (medido: tira vazamento já inaudível e custa ~4 dB de SI-SDR;
      não tira o que a rede acha que é voz). limpeza_inicio/limpeza_fim: trecho onde a limpeza é mais forte.
    fala: a voz é FALA (narração, entrevista, vlog), não canto → a voz separada passa pelo DeepFilterNet
      (`deepfilter.py`, atenuação 24 dB) e a música vira mistura − essa voz. Medido com um sintetizador "vocal" no
      fundo: SI-SDR da voz 6,2 → 15,4 dB e vazamento −4,9 → −19,4 dB; sem vazamento custa ~3,5 dB (26,4 → 22,9).
      Padrão: True em 'so_voz', False nos outros modos. Não use em canto (o modelo é de fala).
    destino ('faixas' e 'instrumentos'): 'faixas' (novas faixas no documento → versão), 'arquivos' (um arquivo de
      áudio por fonte ao lado do original, `<nome>.<fonte>.<formato>`, nunca sobrescreve; não cria versão) ou 'ambos'.
    inicio/fim: só esse trecho (faixas novas: silêncio fora dele; so_*: fora dele fica o original, com rampas de
      20 ms nas bordas; arquivos: só o trecho)."""
    cancelar = cancelar or (lambda: False)
    m = _MODOS.get(str(modo or "faixas").strip().lower().replace("-", "_").replace(" ", "_"))
    if not m:
        raise ValueError("modo: 'faixas' (voz + música como faixas novas), 'so_voz', 'so_musica' (karaokê) "
                         "ou 'instrumentos'")
    q = _QUALIDADES.get(str(qualidade or "alta").strip().lower())
    if not q:
        raise ValueError("qualidade: 'rapida', 'alta' ou 'maxima'")
    nivel = _LIMPEZAS.get(str(limpeza).strip().lower()) if limpeza is not None else 0
    if nivel is None:
        raise ValueError("limpeza: 'nao', 'leve', 'media' ou 'forte'")
    destino = str(destino or "faixas").strip().lower()
    if destino not in ("faixas", "arquivos", "ambos"):
        raise ValueError("destino: 'faixas', 'arquivos' ou 'ambos'")
    if m in ("so_voz", "so_musica"):
        destino = "faixas"  # troca a faixa: é sempre uma versão
    fmt = _FORMATOS.get(str(formato or "wav").strip().lower().lstrip("."))
    if not fmt:
        raise ValueError(f"formato: {', '.join(_FORMATOS)}")
    if m == "instrumentos":
        pedidas = _fontes_pedidas(instrumentos)
    else:
        pedidas = {"faixas": ["vocals", "musica"], "so_voz": ["musica"], "so_musica": ["vocals"]}[m]
    pl = plano(m, pedidas, q)
    if m == "so_voz":
        pl["alvo"] = "vocals"
    usa_voz = any(f in ("vocals", "musica") for f in pedidas)
    fala = (m == "so_voz") if fala is None else bool(fala)
    fala = fala and usa_voz
    modelos = list(dict.fromkeys(p["modelo"] for p in pl["passadas"]))
    ob._ort()

    entrada = doc.atual()
    info = core._info_audio(entrada)
    k = core._faixa_valida(faixa, len(info["auds"]))
    est = dict(estado or {})
    est.setdefault("duracao", info["dur"])
    a, b, _ = core._janela_tempo(inicio, fim, est, info["dur"])
    s = info["auds"][k]
    sr = int(s.get("sample_rate") or 48000)
    canais_orig = int(s.get("channels") or 2)
    canais = 1 if canais_orig == 1 else 2
    st, fim_faixa = core._extensao_faixa(info, s)
    ra = max(0.0, a - st) if a is not None else 0.0
    rb = max(ra, min(b, fim_faixa) - st) if a is not None else None
    if rb is not None and rb - ra < 0.05:
        raise ValueError("o trecho pedido não tem áudio nessa faixa")
    gama = None
    if limpeza_inicio is not None or limpeza_fim is not None:
        la = core.parse_tempo(limpeza_inicio, est) if limpeza_inicio is not None else 0.0
        lb = core.parse_tempo(limpeza_fim, est) if limpeza_fim is not None else info["dur"]
        if lb <= la:
            raise ValueError("limpeza_fim deve ser depois de limpeza_inicio")
        nivel = max(nivel, 1)
        r0 = round(ra * SR)
        gama = (round((la - st) * SR) - r0, round((lb - st) * SR) - r0, 3.0)  # γ = 3 dentro do trecho
    dur_proc = (rb if rb is not None else fim_faixa - st) - ra
    avisos: list[str] = []
    if canais_orig > 2:
        avisos.append(f"a faixa A{k + 1} tem {canais_orig} canais: a separação é feita em estéreo"
                      + (" e a faixa resultante fica estéreo" if m in ("so_voz", "so_musica") else ""))

    # disco: um f32 estéreo 44,1 kHz por fonte de cada passada + mistura + saídas (~1,27 GB por hora cada)
    n_arqs = sum(len(p["fontes"]) for p in pl["passadas"]) + 1 + len(pedidas)
    precisa = int(n_arqs * dur_proc * SR * 8 * 1.05) + (200 << 20)
    doc.pasta.mkdir(parents=True, exist_ok=True)
    livre = shutil.disk_usage(doc.pasta).free
    if livre < precisa:
        raise OSError(f"pouco espaço em {doc.pasta}: precisa de ~{precisa >> 20} MB temporários, há {livre >> 20} MB")

    # modelos (antes de travar o documento)
    faltam = [x for x in modelos if not ob.modelo_pronto(x)]
    p_dl = 0.05 if faltam else 0.0
    for i, x in enumerate(faltam):
        if etapa:
            etapa(f"baixando o modelo {x} ({ob._mb(ob.tamanho_modelo(x))})")
        ob.baixar_modelo(x, (lambda f, i=i: progresso(p_dl * (i + f) / len(faltam))) if progresso else None,
                         cancelar)
    if faltam:
        avisos.append(f"baixados agora ({ob._mb(sum(ob.tamanho_modelo(x) for x in faltam))}, SHA-256 conferido): "
                      + ", ".join(faltam))
    ob.esperar_memoria(cancelar=cancelar, aviso=etapa)

    versao = destino in ("faixas", "ambos")
    trava = ob._gravando_versao(doc, f"separando áudio A{k + 1}") if versao else contextlib.nullcontext()
    t_ini = time.monotonic()
    with ob.trava_ia(cancelar, etapa), trava:
        if versao and doc.atual() != entrada:
            raise RuntimeError("a versão atual mudou antes de começar; tente de novo")
        tmp = Path(tempfile.mkdtemp(prefix=".separar-", dir=doc.pasta))
        try:
            r0 = round(ra * SR)
            e0 = max(0, r0 - round(FOLGA_S * SR))
            r1 = round(rb * SR) if rb is not None else None
            e1 = r1 + round(FOLGA_S * SR) if r1 is not None else None
            p_fim = 0.85 if versao else 0.8
            npass = len(pl["passadas"])
            mistura = tmp / "mistura.f32"
            ns = []
            for i, ps in enumerate(pl["passadas"]):
                ps["arquivos"] = {f: tmp / f"p{i}.{SUFIXOS[f]}.f32" for f in ps["fontes"]}
                if etapa:
                    etapa(f"passada {i + 1}/{npass} ({ps['modelo']}): carregando o modelo")
                base = p_dl + (p_fim - p_dl) * i / npass
                ns.append(_passada(
                    entrada, k, e0, e1, r0 - e0, (r1 - e0) if r1 is not None else None, ps["modelo"], ps["fontes"],
                    ps["desloc"], ps["inverter"], pl["sobrepor"], ps["arquivos"], mistura if i == 0 else None,
                    (r1 if r1 is not None else round((fim_faixa - st) * SR)) - r0,
                    (lambda f, base=base: progresso(base + (p_fim - p_dl) / npass * f)) if progresso else None,
                    cancelar, etapa, f"passada {i + 1}/{npass}: "))
            n = min(ns)
            if n <= 0:
                raise ValueError("o trecho não tem áudio nessa faixa")
            if max(ns) - n > 1:
                avisos.append(f"passadas com tamanhos diferentes ({min(ns)}–{max(ns)} amostras); usado o menor")
            if etapa:
                etapa("combinando as passadas" + (" e limpando (filtro de Wiener)" if nivel else ""))
            brutos = {f: tmp / f"{SUFIXOS[f]}.f32" for f in pedidas}
            borda_ini, borda_fim = ra > 0, rb is not None and rb < fim_faixa - st - 0.001
            if not fala:
                _combinar(pl["passadas"], mistura, n, pl["conjunto"], pedidas, brutos, nivel, pl["alvo"],
                          borda_ini, borda_fim, gama_trecho=gama, cancelar=cancelar,
                          progresso=(lambda f: progresso(p_fim + 0.05 * f)) if progresso else None)
            else:  # sem rampas aqui: a voz ainda passa pelo DeepFilterNet; rampas no fim (_finalizar_fala)
                meio = [f for f in pedidas if f not in ("vocals", "musica")] + ["vocals"]
                crus = {f: tmp / f"cru.{SUFIXOS[f]}.f32" for f in meio}
                _combinar(pl["passadas"], mistura, n, pl["conjunto"], meio, crus, nivel, pl["alvo"],
                          False, False, gama_trecho=gama, cancelar=cancelar,
                          progresso=(lambda f: progresso(p_fim + 0.02 * f)) if progresso else None)
                if etapa:
                    etapa("limpando a voz (DeepFilterNet, voz falada)")
                crus["vocals"] = _deepfilter(crus["vocals"], n, tmp, cancelar)
                _finalizar_fala(mistura, crus, n, pedidas, brutos, borda_ini, borda_fim)
            for ps in pl["passadas"]:
                for arq in ps["arquivos"].values():
                    arq.unlink(missing_ok=True)
            seg = round(time.monotonic() - t_ini, 1)
            detalhes = {"qualidade": q, "limpeza": [k_ for k_, v in _LIMPEZAS.items() if v == nivel][0],
                        "fala_deepfilternet": fala,
                        "passadas": [f"{p['modelo']}{' +' + str(round(p['desloc'] / SR, 2)) + ' s' if p['desloc'] else ''}"
                                     f"{' invertida' if p['inverter'] else ''}" for p in pl["passadas"]],
                        "sobreposicao": pl["sobrepor"], "licenca": "MIT (Demucs v4, Meta; ONNX do StemSplit)",
                        "faixa": f"A{k + 1}", "processado_s": round(n / SR, 3), "tempo_s": seg,
                        "x_tempo_real": round(seg / max(n / SR, 0.001), 2)}
            r: dict = {}
            if destino in ("arquivos", "ambos"):
                if etapa:
                    etapa("gravando os arquivos")
                r["arquivos"] = _exportar(entrada, info, k, pedidas, brutos, sr, canais, st, ra, rb, a, fmt,
                                          pasta_destino, doc, cancelar)
            if versao:
                if etapa:
                    etapa("gravando a versão (vídeo copiado sem perda)")
                v = _versao(doc, info, s, k, m, pedidas, brutos, sr, canais, st, round(ra * sr), a, b, q,
                            detalhes, avisos, (lambda f: progresso(p_fim + 0.05 + 0.1 * f)) if progresso else None)
                r = {**v, **r}
            else:
                r = {"descricao": f"separou A{k + 1} em arquivos (qualidade {q})" + core._desc_janela(a, b), **r,
                     "detalhes": detalhes}
                if avisos:
                    r["avisos"] = avisos
            return r
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _versao(doc, info, s, k, m, fontes, brutos, sr, canais, st, atraso, a, b, nome_modelo, detalhes, avisos,
            progresso) -> dict:
    """Nova versão. so_voz: original − música; so_musica: original − voz (fontes já com rampas nas bordas do
    trecho: fora dele sobra o original). Outros modos: cada fonte vira uma faixa nova, do tamanho exato da original
    (silêncio fora do trecho). Tudo com o start_time da faixa original."""
    grafo: list[str] = []
    saidas: dict[int, str] = {}
    novas: list[tuple[str, dict]] = []
    lang = {kk.lower(): vv for kk, vv in (s.get("tags") or {}).items()}.get("language")
    fim_ts = f"asetpts=PTS-STARTPTS+{st:.6f}/TB"
    for j in range(len(fontes)):
        grafo.append(f"{_cadeia_stem(1 + j, sr, canais, atraso)}[f{j}]")
    if m in ("so_voz", "so_musica"):
        grafo.append(f"{_orig(s, k, sr, canais)}[o];[o][f0]amerge=inputs=2,{_pan(canais, canais, True)},{fim_ts}[ar]")
        saidas[k] = "ar"
        desc = (f"deixou só a voz em A{k + 1}" if m == "so_voz" else f"tirou a voz de A{k + 1} (karaokê)")
    else:
        n = len(fontes)
        grafo.append(_ref(k, sr, canais) + (f",asplit={n}" if n > 1 else "") + "".join(f"[r{j}]" for j in range(n)))
        for j, f in enumerate(fontes):
            grafo.append(f"[r{j}][f{j}]amerge=inputs=2,{_pan(canais, canais, False)},{fim_ts}[n{j}]")
            novas.append((f"n{j}", {"title": f"{TITULOS[f]} (A{k + 1})", "language": lang}))
        desc = f"separou A{k + 1} em faixas novas ({', '.join(TITULOS[f].lower() for f in fontes)})"
    desc += f" — qualidade {nome_modelo}" + (" + DeepFilterNet (fala)" if detalhes.get("fala_deepfilternet") else "") + core._desc_janela(a, b)
    extra = {"detalhes": detalhes}
    if avisos:
        extra["avisos"] = avisos
    return core._editar_audio(doc, grafo, saidas, desc, progresso, _entradas_brutas([brutos[f] for f in fontes]),
                              novas=novas, info=info, extra=extra)


def _exportar(entrada, info, k, fontes, brutos, sr, canais, st, ra, rb, a, fmt, pasta_destino, doc,
              cancelar) -> list[dict]:
    """Um arquivo de áudio por fonte, ao lado do original (ou em `pasta_destino`), sem sobrescrever nada
    (`core._reservar` + temporário + `_publicar`). Sem trecho: começa no 0 do vídeo (com o silêncio do
    start_time da faixa) e tem o tamanho da faixa — alinhado ao vídeo; com trecho: só o trecho."""
    pasta = Path(pasta_destino).expanduser() if pasta_destino else doc.original.parent
    pasta.mkdir(parents=True, exist_ok=True)
    resultado = []
    for f in fontes:
        if a is None:
            g = (f"{_cadeia_stem(1, sr, canais, 0)}[s];{_ref(k, sr, canais)}[r];[r][s]amerge=inputs=2,{_pan(canais, canais, False)}"
                 + (f",adelay=delays={round(st * sr)}S:all=1" if st > 0 else ""))
        else:
            g = f"{_cadeia_stem(1, sr, canais, 0)},atrim=end_sample={round(rb * sr) - round(ra * sr)}"
        final = core._reservar(pasta / f"{doc.original.stem}.{SUFIXOS[f]}{fmt}")
        tmp = core._temporario(final)
        try:
            cmd = (core.FFMPEG + ["-i", str(entrada)] + _entradas_brutas([brutos[f]])
                   + ["-filter_complex", g + "[x]", "-map", "[x]"] + core.codec_audio(fmt)
                   + ["-map_metadata", "-1", "-metadata", f"title={TITULOS[f]}", str(tmp)])
            core._rodar_com_progresso(cmd, max(1.0, info["dur"]), None, cancelar=cancelar)
            core._publicar(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                if final.stat().st_size == 0:
                    final.unlink()
            raise
        resultado.append({"fonte": TITULOS[f], "arquivo": str(final),
                          "tamanho_mb": round(final.stat().st_size / 1e6, 1)})
    return resultado


# ---------------------------------------------------------------- ver o resultado (o Claude não ouve)
def espectrograma(fontes: list[tuple[Path, int, str]], inicio: float, fim: float, destino: Path,
                  largura: int = 1000, altura: int = 260, f_min: float = 40, f_max: float = 16000) -> Path:
    """Uma imagem com o espectrograma de cada (arquivo, índice da faixa de áudio, rótulo), um embaixo do outro,
    no intervalo [inicio, fim] do arquivo (s). Frequência em escala log (f_min–f_max Hz), intensidade em dBFS
    (90 dB de faixa), legenda com tempo (a partir de `inicio`) e frequência. Serve para achar vazamento:
    harmônicos de um teclado na faixa de voz aparecem como linhas horizontais nos trechos sem canto."""
    if not fontes:
        raise ValueError("nada para mostrar")
    if len(fontes) > 4:
        raise ValueError("no máximo 4 espectrogramas por imagem")
    d = fim - inicio
    if d <= 0.05:
        raise ValueError("intervalo vazio")
    largura = max(300, min(int(largura), 1800))
    altura = max(120, min(int(altura), 600))
    cmd = list(core.FFMPEG)
    grafo = []
    for j, (arq, k, _) in enumerate(fontes):
        cmd += ["-ss", f"{inicio:.6f}", "-t", f"{d:.6f}", "-i", str(arq)]
        grafo.append(f"[{j}:a:{k}]showspectrumpic=s={largura}x{altura}:legend=1:fscale=log:scale=log:"
                     f"mode=combined:drange=90:start={f_min:g}:stop={f_max:g}[e{j}]")
    n = len(fontes)
    grafo.append("".join(f"[e{j}]" for j in range(n)) + (f"vstack=inputs={n}" if n > 1 else "null") + "[v]")
    destino.parent.mkdir(parents=True, exist_ok=True)
    core.rodar(cmd + ["-filter_complex", ";".join(grafo), "-map", "[v]", "-frames:v", "1", str(destino)])
    return destino


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python -m ai_editor.separar", description=__doc__.split("\n")[0])
    ap.add_argument("--baixar", nargs="*", metavar="MODELO", help=f"baixa modelos ({', '.join(MODELOS)})")
    args = ap.parse_args(argv)
    for nome in args.baixar or []:
        print(ob.baixar_modelo(nome, lambda f: print(f"\r{nome}: {f:5.0%}", end="", flush=True)))
    print(json.dumps({n: {"baixado": ob.modelo_pronto(n), "tamanho": ob._mb(ob.tamanho_modelo(n)),
                          "licenca": m["licenca"], "fontes": [SUFIXOS[f] for f in m["fontes"]]}
                      for n, m in MODELOS.items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
