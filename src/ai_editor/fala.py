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

import difflib
import hashlib
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
_VERSAO_CACHE = 1  # muda se o formato do resultado mudar

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
    if re.fullmatch(r"[a-z]{2}", s):
        return s
    raise ValueError(f"idioma desconhecido: {idioma!r}. Use 'auto', 'pt', 'en', 'es'... (código de 2 letras)")


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
    p = caminho_modelo(modelo)
    return p.is_file() and p.stat().st_size == MODELOS[norm_modelo(modelo)][1]


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
def _cmake() -> list[str]:
    c = shutil.which("cmake")
    if c:
        return [c]
    uvx = shutil.which("uvx") or (str(Path.home() / ".local/bin/uvx") if (Path.home() / ".local/bin/uvx").exists()
                                  else None)
    if uvx:
        return [uvx, "--from", "cmake>=3.20,<5", "cmake"]
    raise FalaIndisponivel("para compilar o whisper.cpp falta o cmake: `sudo apt install cmake` "
                           "(ou instale o uv, que baixa o cmake do PyPI sem sudo)")


def compilar_whisper(progresso=None, jobs: int | None = None, log=print) -> Path:
    """Baixa o código-fonte do whisper.cpp WHISPER_VERSAO (SHA-256 conferido), compila só o whisper-cli
    (estático, sem OpenMP/SDL/curl; otimizado para este processador) e instala em BIN_SOB_DEMANDA.
    Sem sudo. Pasta de trabalho em ~/.cache/ai-editor-build/whisper.cpp (apagada no fim)."""
    for f in ("g++", "make"):
        if not shutil.which(f):
            raise FalaIndisponivel(f"para compilar o whisper.cpp falta o {f}: `sudo apt install build-essential`")
    cmake = _cmake()
    trab = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ai-editor-build" / "whisper.cpp"
    shutil.rmtree(trab, ignore_errors=True)
    trab.mkdir(parents=True)
    try:
        tar = trab / f"whisper.cpp-{WHISPER_VERSAO}.tar.gz"
        log(f"baixando {WHISPER_TARBALL}")
        _baixar(WHISPER_TARBALL, tar, WHISPER_TARBALL_SHA256, None, None, "código-fonte do whisper.cpp")
        with tarfile.open(tar) as t:
            t.extractall(trab / "fonte", filter="data")
        fonte = next((trab / "fonte").iterdir())
        build = trab / "build"
        log("configurando (cmake)")
        subprocess.run(cmake + ["-S", str(fonte), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
                                "-DBUILD_SHARED_LIBS=OFF", "-DGGML_OPENMP=OFF", "-DWHISPER_BUILD_TESTS=OFF",
                                "-DWHISPER_BUILD_SERVER=OFF", "-DWHISPER_SDL2=OFF", "-DWHISPER_CURL=OFF"],
                       check=True, capture_output=True, text=True)
        j = jobs or max(1, min(4, os.cpu_count() or 1))  # 4 jobs: pico de RAM ~1 GB
        log(f"compilando com {j} processos (1–2 min)")
        r = subprocess.run(cmake + ["--build", str(build), "--config", "Release", "--target", "whisper-cli",
                                    "-j", str(j)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("falha ao compilar o whisper.cpp:\n" + (r.stdout + r.stderr)[-3000:])
        exe = build / "bin" / "whisper-cli"
        binarios.BIN_SOB_DEMANDA.mkdir(parents=True, exist_ok=True)
        destino = binarios.BIN_SOB_DEMANDA / "whisper-cli"
        tmp = destino.with_name(".whisper-cli.parcial")
        shutil.copy2(exe, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, destino)
        (binarios.BIN_SOB_DEMANDA / "whisper-cli.versao").write_text(
            f"whisper.cpp {WHISPER_VERSAO}\nfonte: {WHISPER_TARBALL}\nsha256: {WHISPER_TARBALL_SHA256}\n")
    finally:
        shutil.rmtree(trab, ignore_errors=True)
    binarios.localizar_com_origem.cache_clear()
    log(f"instalado: {destino}")
    return destino


def status() -> dict:
    """O que está pronto para transcrever (motor, modelos baixados)."""
    return {"whisper_cli": binarios.localizar("whisper-cli"),
            "modelos": {k: {"baixado": modelo_pronto(k), "tamanho": _mb(v[1])} for k, v in MODELOS.items()},
            "pasta_modelos": str(MODELOS_WHISPER_DIR)}


# ---------------------------------------------------------------- transcrição
def _impressao_arquivo(p: Path) -> str:
    """Identidade do conteúdo sem ler o arquivo todo: tamanho + 3 blocos de 4 MB (início, meio, fim)."""
    tam = p.stat().st_size
    h = hashlib.sha256(str(tam).encode())
    bloco = 4 << 20
    with open(p, "rb") as f:
        for pos in sorted({0, max(0, tam // 2 - bloco // 2), max(0, tam - bloco)}):
            f.seek(pos)
            h.update(f.read(bloco))
    return h.hexdigest()


def extrair_audio(arquivo: Path, destino: Path, faixa: int = 1, inicio: float | None = None,
                  fim: float | None = None) -> float:
    """WAV 16 kHz mono PCM (o que o whisper espera) da faixa `faixa` (1 = A1). Devolve a duração."""
    probe = core.ffprobe(arquivo)
    auds = core.faixas_audio(probe)
    if not auds:
        raise ValueError(f"o arquivo não tem áudio: {arquivo.name}")
    if not 1 <= int(faixa) <= len(auds):
        raise ValueError(f"faixa A{faixa} não existe (o arquivo tem {len(auds)} faixa(s) de áudio)")
    cmd = list(core.FFMPEG)
    if inicio:
        cmd += ["-ss", f"{inicio:.3f}"]
    if fim is not None:
        cmd += ["-t", f"{max(0.01, fim - (inicio or 0)):.3f}"]
    cmd += ["-i", str(arquivo), "-map", f"0:a:{int(faixa) - 1}", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(destino)]
    core.rodar(cmd)
    return max(0.0, (destino.stat().st_size - 44) / 32000)


def _rodar_whisper(cmd: list[str], progresso=None) -> str:
    """Roda o whisper-cli lendo o progresso ('progress = N%') do stderr; devolve o stderr (para erros)."""
    linhas: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace",
                            preexec_fn=core._morrer_com_pai if core._prctl is not None else None)
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
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
    limiar = max(-50.0, min(-30.0, float(m.group(1)) - 18)) if m else -40.0
    r = subprocess.run([core.FFMPEG[0], "-hide_banner", "-nostdin", "-i", str(wav), "-af",
                        f"silencedetect=n={limiar:.1f}dB:d=0.25", "-f", "null", "-"], capture_output=True, text=True)
    ini = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", r.stderr)]
    fim = [float(x) for x in re.findall(r"silence_end:\s*(-?[\d.]+)", r.stderr)]
    dur = max(0.0, (wav.stat().st_size - 44) / 32000)
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
    if inicio is not None and fim is not None and fim - inicio <= 0.05:
        raise ValueError(f"intervalo vazio: {fmt_tempo(inicio)} → {fmt_tempo(fim)}")
    params = {"v": _VERSAO_CACHE, "whisper": WHISPER_VERSAO, "idioma": lang, "modelo": nome, "faixa": int(faixa),
              "inicio": round(inicio, 3) if inicio else None, "fim": round(fim, 3) if fim is not None else None}
    chave = hashlib.sha256((_impressao_arquivo(arquivo) + json.dumps(params, sort_keys=True)).encode()).hexdigest()
    cache = CACHE_TRANSCRICOES / f"{chave[:40]}.json"
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

    exe = whisper_cli()  # erro claro antes de baixar o modelo
    baixado = None
    if not modelo_pronto(nome):
        caminho, _ = baixar_modelo(nome, (lambda f: progresso(0.3 * f)) if progresso else None)
        baixado = {"modelo": nome, "tamanho": _mb(MODELOS[nome][1]), "caminho": str(caminho)}
        base = 0.3
    else:
        base = 0.0
    caminho = caminho_modelo(nome)
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ai-editor-fala-") as tmp:
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
        _rodar_whisper(cmd, (lambda f: progresso(base + 0.03 + (0.97 - base) * f)) if progresso else None)
        bruto = (saida.with_suffix(".json")).read_bytes()
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
         "texto": " ".join(s["texto"] for s in segs), "segmentos": segs, "whisper": WHISPER_VERSAO}
    if descartados:
        r["descartados"] = [{"inicio": (s.get("palavras") or [s])[0]["inicio"], "texto": s["texto"],
                             "motivo": "frase típica de alucinação do Whisper com baixa confiança"} for s in descartados]
    try:
        CACHE_TRANSCRICOES.mkdir(parents=True, exist_ok=True)
        tmpc = cache.with_name(f".{cache.name}.{os.getpid()}.tmp")
        tmpc.write_text(json.dumps(r, ensure_ascii=False))
        os.replace(tmpc, cache)
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
            c["fim"] = max(c["inicio"] + 0.2, prox - 0.04)
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


def texto_srt(cues: list[dict]) -> str:
    return "\n".join(f"{i}\n{_ts(c['inicio'], ',')} --> {_ts(c['fim'], ',')}\n" + "\n".join(c["linhas"]) + "\n"
                     for i, c in enumerate(cues, 1))


def texto_vtt(cues: list[dict]) -> str:
    corpo = "\n".join(f"{_ts(c['inicio'], '.')} --> {_ts(c['fim'], '.')}\n"
                      + "\n".join(ln.replace("-->", "->") for ln in c["linhas"]) + "\n" for c in cues)
    return "WEBVTT\n\n" + corpo


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
    if fonte or completo:
        c["Fontname"] = fonte or "DejaVu Sans"
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


def preparar_ass(origem: Path, destino: Path, largura: int, altura: int, estilo: dict, completo: bool) -> Path:
    """Gera o .ass que vai ser queimado. .srt/.vtt: convertido pelo ffmpeg, com PlayRes = tamanho do vídeo
    (medidas em pixels reais) e o estilo Default reescrito. .ass do usuário: copiado, com os campos pedidos
    aplicados a todos os estilos (na escala do PlayResY dele)."""
    if origem.suffix.lower() in (".ass", ".ssa"):
        texto = origem.read_text(encoding="utf-8-sig", errors="replace")
    else:
        r = core.rodar(core.FFMPEG + ["-i", str(origem), "-f", "ass", "-"])
        texto = r.stdout
        texto = re.sub(r"(?m)^PlayResX:.*$", f"PlayResX: {largura}", texto)
        texto = re.sub(r"(?m)^PlayResY:.*$", f"PlayResY: {altura}", texto)
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
        ass = preparar_ass(arq, doc.pasta / f"legenda_v{n:03d}.estilo.ass", info["w"], info["h"], estilo,
                           completo=not eh_ass)
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
    if not lang:  # legenda do usuário: tenta o sufixo do nome (video.pt.srt)
        m = re.search(r"\.([a-z]{2})$", arq.stem.lower())
        lang = m.group(1) if m else None
    iso = _ISO639_2.get(lang or "", "und")
    saida = doc.proxima_saida(ext)
    cmd = core.FFMPEG + ["-i", str(entrada), "-i", str(arq), "-map", "0:V?", "-map", "0:a?", "-map", "0:s?"]
    if ext == ".mkv":
        cmd += ["-map", "0:t?"]
    cmd += ["-map", "1:0", "-c", "copy", f"-c:s:{n_leg}", codec, f"-metadata:s:s:{n_leg}", f"language={iso}"]
    if titulo:
        cmd += [f"-metadata:s:s:{n_leg}", f"title={titulo}"]
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
                                          arquivo, "-map", f"0:a:{int(faixa) - 1}", "-ac", "1", "-ar", "16000",
                                          "-f", "s16le", "-"], capture_output=True, timeout=60)
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
    q = " ".join(alvo)
    cands = []
    for i in range(len(ws)):
        melhor = None
        for L in range(max(1, n - 2), n + 3):
            if i + L > len(ws):
                break
            c = " ".join(w["n"] for w in ws[i:i + L])
            sim = difflib.SequenceMatcher(None, q, c, autojunk=False).ratio()
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
