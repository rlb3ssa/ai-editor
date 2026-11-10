"""Núcleo do AI Editor: caminhos, tempo, versões, ffmpeg e metadados.

Usado tanto pela janela (app.py) quanto pelo servidor MCP (mcp_server.py).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import binarios
from .lixeira import mover_para_lixeira

# ---------------------------------------------------------------- caminhos
# Modo desenvolvimento: rodando do código-fonte (não empacotado). Pode ser forçado com AI_EDITOR_DEV=0/1.
# A instância dev usa outro nome/socket, para conviver com a versão instalada.
_dev_env = os.environ.get("AI_EDITOR_DEV")
DEV = (_dev_env == "1") if _dev_env in ("0", "1") else not getattr(__import__("sys"), "frozen", False)
NOME = "ai-editor-dev" if DEV else "ai-editor"
TITULO = "AI Editor (dev)" if DEV else "AI Editor"
FONTE_DIR = Path(__file__).resolve().parent

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / NOME
SOCKET_PATH = RUNTIME_DIR / "controle.sock"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ai-editor"


def ferramenta(nome: str) -> str | None:
    """Caminho de uma ferramenta externa (pacote → sob demanda → sistema). Ver binarios.py."""
    if nome in binarios.REGISTRO:
        return binarios.localizar(nome)
    return shutil.which(nome)


# ---------------------------------------------------------------- tempo
def fmt_tempo(seg: float | None) -> str:
    if seg is None:
        return "--:--:--.---"
    ms = int(round(abs(seg) * 1000))
    neg = seg < 0 and ms > 0
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{'-' if neg else ''}{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


_KW = {
    "atual": "pos", "agora": "pos", "aqui": "pos", "pos": "pos",
    "in": "in", "entrada": "in", "out": "out", "saida": "out", "saída": "out",
    "inicio": "inicio", "início": "inicio", "começo": "inicio", "fim": "fim", "final": "fim",
}


def parse_tempo(valor, estado: dict | None = None) -> float:
    """Aceita 83.456, "83.456", "1:23.456", "00:01:23,456", "1500ms",
    ou palavras: atual/aqui, inicio, fim, marcas m1, m2... (numeradas por ordem de tempo),
    in/entrada (= primeira marca), out/saida (= última marca).
    Também aceita expressões simples: "atual-2", "m2+0.5", "out - 1:00"."""
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip().lower().replace(",", ".")
    m = re.fullmatch(r"([a-zçíá]+\d*)\s*([+-])\s*(.+)", s)
    if m and (m.group(1) in _KW or re.fullmatch(r"m\d+", m.group(1))):
        base = parse_tempo(m.group(1), estado)
        d = parse_tempo(m.group(3), estado)
        return base + d if m.group(2) == "+" else base - d
    if re.fullmatch(r"m\d+", s):
        if estado is None:
            raise ValueError(f"'{valor}' precisa do player aberto")
        marcas = [m["tempo"] for m in estado.get("marcas") or []]
        n = int(s[1:])
        if not 1 <= n <= len(marcas):
            raise ValueError(f"marca {s} não existe (há {len(marcas)} marca(s))")
        return float(marcas[n - 1])
    if s in _KW:
        k = _KW[s]
        if k == "inicio":
            return 0.0
        if estado is None:
            raise ValueError(f"'{valor}' precisa do player aberto")
        marcas = [m["tempo"] for m in estado.get("marcas") or []]
        v = {"pos": estado.get("posicao"), "in": marcas[0] if marcas else None,
             "out": marcas[-1] if marcas else None, "fim": estado.get("duracao")}[k]
        if v is None:
            raise ValueError(f"'{valor}' não está definido no player")
        return float(v)
    if s.endswith("ms"):
        return float(s[:-2]) / 1000
    if s.endswith("s") and re.fullmatch(r"[\d.]+s", s):
        s = s[:-1]
    partes = s.split(":")
    if len(partes) > 3:
        raise ValueError(f"tempo inválido: {valor}")
    total = 0.0
    for p in partes:
        total = total * 60 + float(p)
    return total


# ---------------------------------------------------------------- execução
FFMPEG = [binarios.exe("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
FFPROBE = binarios.exe("ffprobe")


class ErroFFmpeg(RuntimeError):
    pass


def rodar(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise ErroFFmpeg(f"comando falhou ({r.returncode}): {shlex.join(cmd)}\n{r.stderr[-4000:]}")
    return r


def ffprobe(path: Path | str) -> dict:
    r = rodar([FFPROBE, "-v", "error", "-print_format", "json", "-show_format",
               "-show_streams", "-show_chapters", str(path)])
    return json.loads(r.stdout)


def faixas_audio(probe: dict) -> list[dict]:
    return [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]


def duracao(probe: dict) -> float:
    try:
        return float(probe["format"]["duration"])
    except (KeyError, ValueError):
        return max((float(s.get("duration", 0) or 0) for s in probe.get("streams", [])), default=0.0)


# ---------------------------------------------------------------- codecs
def codec_video(ext: str) -> list[str]:
    ext = ext.lower()
    if ext == ".webm":
        return ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1",
                "-deadline", "good", "-cpu-used", "4"]
    return ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast"]


def codec_audio(ext: str, idx: int | None = None) -> list[str]:
    ext = ext.lower()
    sel = f"-c:a:{idx}" if idx is not None else "-c:a"
    if ext in (".webm", ".ogg", ".opus"):
        return [sel, "libopus", "-b:a", "160k"]
    if ext == ".mp3":
        return [sel, "libmp3lame", "-q:a", "2"]
    if ext == ".wav":
        return [sel, "pcm_s16le"]
    if ext == ".flac":
        return [sel, "flac"]
    return [sel, "aac", "-b:a", "192k"]


MAP_PADRAO = ["-map", "0:V?", "-map", "0:a?", "-map", "0:s?"]


# ---------------------------------------------------------------- versões
class Documento:
    """Um vídeo original + suas versões temporárias (pasta oculta ao lado).

    /pasta/video.mp4
    /pasta/.video.mp4.versoes/v001.mp4, v002.mp4, historico.json
    """

    def __init__(self, original: Path | str):
        original = Path(original).expanduser().resolve()
        # Se abriram uma versão diretamente, volta para o documento original.
        if original.parent.name.endswith(".versoes") and original.parent.name.startswith("."):
            hist = original.parent / "historico.json"
            if hist.exists():
                original = Path(json.loads(hist.read_text())["original"])
        self.original = original
        self.pasta = original.parent / f".{original.name}.versoes"
        self.hist_path = self.pasta / "historico.json"

    # -- histórico
    def _ler(self) -> dict:
        if self.hist_path.exists():
            return json.loads(self.hist_path.read_text())
        return {"original": str(self.original), "versoes": []}

    def _gravar(self, h: dict) -> None:
        self.pasta.mkdir(exist_ok=True)
        tmp = self.hist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(h, ensure_ascii=False, indent=2))
        os.replace(tmp, self.hist_path)

    def versoes(self) -> list[dict]:
        h = self._ler()
        return [v for v in h["versoes"] if (self.pasta / v["arquivo"]).exists()]

    def atual(self) -> Path:
        vs = self.versoes()
        return self.pasta / vs[-1]["arquivo"] if vs else self.original

    def proxima_saida(self, ext: str | None = None) -> Path:
        self.pasta.mkdir(exist_ok=True)
        n = len(self._ler()["versoes"]) + 1
        ext = ext or self.atual().suffix
        if not ext.startswith("."):
            ext = "." + ext
        return self.pasta / f"v{n:03d}{ext}"

    def registrar(self, saida: Path, descricao: str, comandos: list[list[str]]) -> dict:
        h = self._ler()
        h["versoes"] = [v for v in h["versoes"] if (self.pasta / v["arquivo"]).exists()]
        v = {"n": len(h["versoes"]) + 1, "arquivo": saida.name, "descricao": descricao,
             "data": _dt.datetime.now().isoformat(timespec="seconds"),
             "comandos": [shlex.join(c) for c in comandos]}
        h["versoes"].append(v)
        self._gravar(h)
        return v

    def desfazer(self) -> dict | None:
        h = self._ler()
        if not h["versoes"]:
            return None
        v = h["versoes"].pop()
        (self.pasta / v["arquivo"]).unlink(missing_ok=True)
        self._gravar(h)
        if not h["versoes"]:
            shutil.rmtree(self.pasta, ignore_errors=True)
        return v

    def descartar(self) -> int:
        n = len(self.versoes())
        shutil.rmtree(self.pasta, ignore_errors=True)
        return n

    def salvar(self, lixeira: bool = True) -> Path:
        """A última versão substitui o original; original e versões são removidos.
        Por segurança o original vai para a Lixeira (lixeira=False apaga de vez)."""
        vs = self.versoes()
        if not vs:
            raise ValueError("não há versões para salvar")
        final = self.pasta / vs[-1]["arquivo"]
        destino = self.original.with_suffix(final.suffix)
        if self.original.exists():
            if lixeira:
                mover_para_lixeira(self.original)
            else:
                self.original.unlink()
        if destino.exists() and destino != self.original:
            # outro arquivo com o mesmo nome e a nova extensão: não sobrescreve
            destino = destino.with_name(f"{destino.stem}_editado{destino.suffix}")
        os.replace(final, destino)
        shutil.rmtree(self.pasta, ignore_errors=True)
        return destino

    def resumo(self) -> dict:
        return {"original": str(self.original), "arquivo_atual": str(self.atual()),
                "versoes": self.versoes(), "nao_salvo": bool(self.versoes())}


# ---------------------------------------------------------------- edição
def manter_trechos(doc: Documento, trechos: list[tuple[float, float]], preciso: bool,
                   descricao: str) -> dict:
    """Gera nova versão contendo apenas os trechos indicados, na ordem dada."""
    entrada = doc.atual()
    probe = ffprobe(entrada)
    dur = duracao(probe)
    limpos = []
    for a, b in trechos:
        a, b = max(0.0, a), min(dur, b) if dur else b
        if b - a > 0.001:
            limpos.append((a, b))
    if not limpos:
        raise ValueError("nenhum trecho válido (verifique os tempos)")
    saida = doc.proxima_saida()
    ext = saida.suffix
    if preciso:
        cmd = _cmd_preciso(entrada, probe, limpos, saida)
        rodar(cmd)
        return doc.registrar(saida, descricao, [cmd])
    codec = ["-c", "copy"]
    comandos = []

    def segmento(a, b, destino, metadata=True):
        cmd = FFMPEG + ["-ss", f"{a:.3f}", "-i", str(entrada), "-t", f"{b - a:.3f}"] + MAP_PADRAO + codec
        if metadata:
            cmd += ["-map_metadata", "0", "-map_chapters", "-1"]
        cmd += ["-avoid_negative_ts", "make_zero", str(destino)]
        comandos.append(cmd)
        rodar(cmd)

    if len(limpos) == 1:
        segmento(*limpos[0], saida)
    else:
        with tempfile.TemporaryDirectory(dir=doc.pasta) as tmp:
            partes = []
            for i, (a, b) in enumerate(limpos):
                p = Path(tmp) / f"parte{i:03d}{ext}"
                segmento(a, b, p, metadata=False)
                partes.append(p)
            lista = Path(tmp) / "lista.txt"
            lista.write_text("".join(f"file '{p}'\n" for p in partes))
            cmd = FFMPEG + ["-f", "concat", "-safe", "0", "-i", str(lista), "-i", str(entrada),
                            "-map", "0", "-c", "copy", "-map_metadata", "1", "-map_chapters", "-1",
                            str(saida)]
            comandos.append(cmd)
            rodar(cmd)
    return doc.registrar(saida, descricao, comandos)


def _tags_faixa(stream: dict, saida_sel: str) -> list[str]:
    """Reaplica idioma/título/handler de uma faixa de origem numa faixa de saída filtrada."""
    out = []
    for k in ("language", "title", "handler_name"):
        v = stream.get("tags", {}).get(k)
        if v:
            out += [f"-metadata:{saida_sel}", f"{k}={v}"]
    return out


def _cmd_preciso(entrada: Path, probe: dict, trechos: list[tuple[float, float]], saida: Path) -> list[str]:
    """Corte exato: cada trecho é uma entrada com seek próprio; trim/concat num único filtro,
    mantendo áudio e vídeo sincronizados ao quadro em todas as faixas."""
    vids = [s for s in probe["streams"] if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    auds = faixas_audio(probe)
    tem_v = bool(vids)
    cmd = FFMPEG[:]
    for a, b in trechos:
        cmd += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(entrada)]
    n = len(trechos)
    grafo, entradas_concat = [], ""
    for i in range(n):
        if tem_v:
            grafo.append(f"[{i}:V:0]setpts=PTS-STARTPTS[v{i}]")
            entradas_concat += f"[v{i}]"
        for k in range(len(auds)):
            grafo.append(f"[{i}:a:{k}]asetpts=PTS-STARTPTS[a{k}_{i}]")
            entradas_concat += f"[a{k}_{i}]"
    saidas = ("[vo]" if tem_v else "") + "".join(f"[ao{k}]" for k in range(len(auds)))
    grafo.append(f"{entradas_concat}concat=n={n}:v={int(tem_v)}:a={len(auds)}{saidas}")
    cmd += ["-filter_complex", ";".join(grafo)]
    if tem_v:
        cmd += ["-map", "[vo]"] + codec_video(saida.suffix) + _tags_faixa(vids[0], "s:v:0")
    for k, s in enumerate(auds):
        cmd += ["-map", f"[ao{k}]"] + _tags_faixa(s, f"s:a:{k}")
    if auds:
        cmd += codec_audio(saida.suffix)
    cmd += ["-map_metadata", "0", "-map_chapters", "-1", str(saida)]
    return cmd


def complemento(trechos_remover: list[tuple[float, float]], dur: float) -> list[tuple[float, float]]:
    manter, cursor = [], 0.0
    for a, b in sorted(trechos_remover):
        if a > cursor:
            manter.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < dur:
        manter.append((cursor, dur))
    return manter


def cadeia_audio(ganho_db=None, normalizar=False, reduzir_ruido=False, silenciar=False,
                 fade_in=None, fade_out=None, inicio=None, fim=None, dur=0.0) -> str:
    filtros = []
    janela = ""
    if inicio is not None or fim is not None:
        a = inicio or 0.0
        b = fim if fim is not None else dur
        janela = f":enable='between(t,{a:.3f},{b:.3f})'"
    if reduzir_ruido:
        filtros.append("afftdn=nf=-25")
    if normalizar:
        filtros.append("loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000")
    if ganho_db:
        filtros.append(f"volume={ganho_db}dB{janela}")
    if silenciar:
        filtros.append(f"volume=0{janela}")
    if fade_in:
        st = inicio or 0.0
        filtros.append(f"afade=t=in:st={st:.3f}:d={fade_in:.3f}")
    if fade_out:
        end = fim if fim is not None else dur
        filtros.append(f"afade=t=out:st={max(0.0, end - fade_out):.3f}:d={fade_out:.3f}")
    if not filtros:
        raise ValueError("nenhum ajuste de áudio pedido")
    return ",".join(filtros)


def aplicar_filtro_audio(doc: Documento, faixas: list[int], cadeia: str, descricao: str) -> dict:
    """Reprocessa só as faixas indicadas (índice 0 = primeira faixa); vídeo e demais faixas são copiados."""
    entrada = doc.atual()
    probe = ffprobe(entrada)
    n = len(faixas_audio(probe))
    if n == 0:
        raise ValueError("o arquivo não tem áudio")
    for f in faixas:
        if not 0 <= f < n:
            raise ValueError(f"faixa {f + 1} não existe (há {n})")
    saida = doc.proxima_saida()
    grafo = ";".join(f"[0:a:{f}]{cadeia}[a{f}]" for f in faixas)
    cmd = FFMPEG + ["-i", str(entrada), "-filter_complex", grafo, "-map", "0:V?"]
    for i in range(n):
        cmd += ["-map", f"[a{i}]" if i in faixas else f"0:a:{i}"]
    cmd += ["-map", "0:s?", "-c", "copy"]
    auds = faixas_audio(probe)
    for f in faixas:
        cmd += codec_audio(saida.suffix, f) + _tags_faixa(auds[f], f"s:a:{f}")
    cmd += ["-map_metadata", "0", str(saida)]
    rodar(cmd)
    return doc.registrar(saida, descricao, [cmd])


def remover_faixa_audio(doc: Documento, faixa: int) -> dict:
    entrada = doc.atual()
    n = len(faixas_audio(ffprobe(entrada)))
    if not 0 <= faixa < n:
        raise ValueError(f"faixa {faixa + 1} não existe (há {n})")
    saida = doc.proxima_saida()
    cmd = FFMPEG + ["-i", str(entrada), "-map", "0", "-map", f"-0:a:{faixa}", "-c", "copy",
                    "-map_metadata", "0", str(saida)]
    rodar(cmd)
    return doc.registrar(saida, f"removeu faixa de áudio {faixa + 1}", [cmd])


def adicionar_faixa_audio(doc: Documento, arquivo: Path, idioma: str | None, titulo: str | None,
                          atraso: float = 0.0) -> dict:
    entrada = doc.atual()
    n = len(faixas_audio(ffprobe(entrada)))
    saida = doc.proxima_saida()
    cmd = FFMPEG + ["-i", str(entrada)]
    if atraso:
        cmd += ["-itsoffset", f"{atraso:.3f}"]
    cmd += ["-i", str(arquivo), "-map", "0", "-map", "1:a:0", "-c", "copy"]
    cmd += codec_audio(saida.suffix, n)
    if idioma:
        cmd += [f"-metadata:s:a:{n}", f"language={idioma}"]
    if titulo:
        cmd += [f"-metadata:s:a:{n}", f"title={titulo}"]
    cmd += ["-map_metadata", "0", str(saida)]
    rodar(cmd)
    return doc.registrar(saida, f"adicionou faixa de áudio de {Path(arquivo).name}", [cmd])


def ffmpeg_livre(doc: Documento, argumentos: str, descricao: str, extensao: str | None) -> dict:
    """Roda ffmpeg com argumentos livres. Use {entrada} e {saida} nos argumentos."""
    if "{entrada}" not in argumentos or "{saida}" not in argumentos:
        raise ValueError("os argumentos precisam conter {entrada} e {saida}")
    entrada = doc.atual()
    saida = doc.proxima_saida(extensao)
    args = [a.replace("{entrada}", str(entrada)).replace("{saida}", str(saida))
            for a in shlex.split(argumentos)]
    if args and args[0] == "ffmpeg":
        args = args[1:]
    cmd = FFMPEG + args
    rodar(cmd)
    if not saida.exists():
        raise ErroFFmpeg("o ffmpeg terminou mas não gerou {saida}")
    return doc.registrar(saida, descricao, [cmd])


def extrair_quadro(arquivo: Path, t: float, largura: int, destino: Path) -> Path:
    cmd = FFMPEG + ["-ss", f"{t:.3f}", "-i", str(arquivo), "-frames:v", "1",
                    "-vf", f"scale='min({largura},iw)':-2", "-q:v", "3", str(destino)]
    rodar(cmd)
    return destino


def analisar_audio(arquivo: Path, faixa: int, inicio: float | None, fim: float | None,
                   limiar_silencio_db: float = -40, silencio_min: float = 0.5) -> dict:
    cmd = [FFMPEG[0], "-hide_banner", "-nostdin"]
    if inicio:
        cmd += ["-ss", f"{inicio:.3f}"]
    cmd += ["-i", str(arquivo)]
    if fim is not None:
        cmd += ["-t", f"{fim - (inicio or 0):.3f}"]
    cmd += ["-map", f"0:a:{faixa}", "-af",
            f"ebur128=peak=true,volumedetect,silencedetect=n={limiar_silencio_db}dB:d={silencio_min}",
            "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ErroFFmpeg(r.stderr[-3000:])
    err = r.stderr
    off = inicio or 0.0
    res: dict = {"faixa": faixa + 1, "inicio": fmt_tempo(off)}

    def achar(rx):
        m = re.findall(rx, err)
        return float(m[-1]) if m else None

    res["volume_medio_db"] = achar(r"mean_volume:\s*(-?[\d.]+) dB")
    res["volume_max_db"] = achar(r"max_volume:\s*(-?[\d.]+) dB")
    resumo = err.split("Summary:")[-1] if "Summary:" in err else ""
    for nome, rx in [("loudness_integrada_lufs", r"I:\s*(-?[\d.]+) LUFS"),
                     ("faixa_dinamica_lu", r"LRA:\s*(-?[\d.]+) LU"),
                     ("pico_real_dbfs", r"Peak:\s*(-?[\d.]+) dBFS")]:
        m = re.findall(rx, resumo)
        res[nome] = float(m[-1]) if m else None
    silencios, ini = [], None
    for linha in err.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", linha)
        if m:
            ini = float(m.group(1)) + off
        m = re.search(r"silence_end:\s*(-?[\d.]+)", linha)
        if m and ini is not None:
            fimsil = float(m.group(1)) + off
            silencios.append({"de": fmt_tempo(ini), "ate": fmt_tempo(fimsil),
                              "duracao_s": round(fimsil - ini, 3)})
            ini = None
    if ini is not None:
        silencios.append({"de": fmt_tempo(ini), "ate": "fim"})
    res["silencios"] = silencios
    return res


# ---------------------------------------------------------------- metadados
def _rodar_json(cmd: list[str]):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 and not r.stdout.strip():
            return {"erro": r.stderr.strip()[-2000:]}
        return json.loads(r.stdout)
    except FileNotFoundError:
        return {"indisponivel": f"{cmd[0]} não instalado"}
    except Exception as e:  # noqa: BLE001
        return {"erro": str(e)}


def _xattrs(path: Path) -> dict:
    out = {}
    try:
        for k in os.listxattr(path):
            try:
                v = os.getxattr(path, k)
                out[k] = v.decode("utf-8", "replace").rstrip("\x00")
            except OSError:
                pass
    except OSError as e:
        out["_erro"] = str(e)
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloco in iter(lambda: f.read(8 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


_PISTAS_TEXTO = [
    (r"\bLavf[\d.]*", "processado/remuxado com FFmpeg (libavformat)"),
    (r"HandBrake", "convertido com HandBrake"),
    (r"\bOBS\b|obs-output", "gravado com OBS Studio"),
    (r"yt-dlp|youtube-dl", "baixado com yt-dlp/youtube-dl"),
    (r"youtube\.com|youtu\.be", "origem YouTube"),
    (r"instagram", "origem Instagram"),
    (r"tiktok", "origem TikTok"),
    (r"(twitter|x)\.com", "origem X/Twitter"),
    (r"facebook|fbcdn", "origem Facebook"),
    (r"Google", "processado por serviço Google (ex.: YouTube/Fotos)"),
    # "Apple" sozinho dá falso positivo (HandlerVendorID do MP4 vem como Apple em arquivos comuns)
    (r'iPhone|iPad|Core Media|Make": "Apple|com\.apple\.quicktime\.make', "dispositivo/software Apple"),
    (r"com\.android|Android", "dispositivo Android"),
    (r"samsung", "dispositivo Samsung"),
    (r"\bDJI\b", "drone/câmera DJI"),
    (r"GoPro", "câmera GoPro"),
    (r"Adobe|Premiere|After Effects|Media Encoder", "software Adobe"),
    (r"DaVinci|Resolve|Blackmagic", "DaVinci Resolve/Blackmagic"),
    (r"CapCut|Bytedance|ByteDance", "editado no CapCut"),
    (r"Kdenlive|Shotcut|OpenShot|MLT", "editado em editor livre (Kdenlive/Shotcut/MLT)"),
    (r"clideo", "editado/convertido no Clideo (editor online)"),
    (r"kapwing", "editado no Kapwing (editor online)"),
    (r"\bcanva\b", "editado no Canva"),
    (r"\bveed\b", "editado no VEED (editor online)"),
    (r"InShot", "editado no InShot"),
    (r"WhatsApp", "passou pelo WhatsApp"),
    (r"Telegram", "passou pelo Telegram"),
    (r"trainedAlgorithmicMedia|compositeWithTrainedAlgorithmicMedia", "C2PA declara conteúdo gerado por IA"),
    (r"Sora|OpenAI|Firefly|Veo|Runway|Midjourney|Kling|Pika", "possível ferramenta de IA generativa"),
    (r'GPS[A-Z]|ISO6709|quicktime\.location|"location(-eng)?":', "contém localização (GPS)"),
]

_PISTAS_NOME = [
    (r"^VID-\d{8}-WA\d+", "nome típico de vídeo do WhatsApp"),
    (r"^PXL_\d{8}", "nome típico de câmera Google Pixel"),
    (r"^VID_\d{8}_\d{6}", "nome típico de câmera Android"),
    (r"^IMG_\d{4}\.MOV$", "nome típico de iPhone"),
    (r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.(mkv|mp4|flv|mov)$", "nome padrão de gravação do OBS"),
    (r"^(Gravação de tela|Screencast|Screen Recording)", "gravação de tela"),
    (r"^(DJI|GH|GX|GOPR)\d+", "nome típico de DJI/GoPro"),
    (r"\[[A-Za-z0-9_-]{11}\]", "nome com ID de 11 caracteres (padrão yt-dlp/YouTube)"),
    (r"ytdown|savefrom|y2mate|ssyoutube|snaptik|ssstik|snapinsta|ytmp3|9convert",
     "baixado por site de download de vídeos (ytdown, savefrom, y2mate, snaptik...)"),
    (r"youtube|youtu\.be", "nome indica origem YouTube"),
    (r"tiktok", "nome indica origem TikTok"),
    (r"instagram|reels?\b", "nome indica origem Instagram"),
]


def metadados(arquivo: Path | str, profundo: bool = False, hash_: bool = False) -> dict:
    """Coleta TODO metadado disponível de várias fontes independentes."""
    p = Path(arquivo).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    st = p.stat()
    rel: dict = {"arquivo": str(p)}
    rel["sistema_de_arquivos"] = {
        "tamanho_bytes": st.st_size,
        "tamanho_mb": round(st.st_size / 1e6, 2),
        "modificado": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "acessado": _dt.datetime.fromtimestamp(st.st_atime).isoformat(timespec="seconds"),
        "status_alterado": _dt.datetime.fromtimestamp(st.st_ctime).isoformat(timespec="seconds"),
        "atributos_estendidos": _xattrs(p),
    }
    # data de criação real (statx), se o sistema suportar
    try:
        r = subprocess.run(["stat", "-c", "%w", str(p)], capture_output=True, text=True)
        if r.stdout.strip() and r.stdout.strip() != "-":
            rel["sistema_de_arquivos"]["criado"] = r.stdout.strip()
    except FileNotFoundError:
        pass
    if hash_:
        rel["sistema_de_arquivos"]["sha256"] = _sha256(p)

    rel["ffprobe"] = _rodar_json([FFPROBE, "-v", "error", "-print_format", "json",
                                  "-show_format", "-show_streams", "-show_chapters",
                                  "-show_programs", "-show_private_data", str(p)])
    if ferramenta("mediainfo"):
        mi = _rodar_json([ferramenta("mediainfo"), "--Output=JSON", "--Full", str(p)])
        # remove duplicatas de "Full" (chaves repetidas com sufixo/String) para enxugar
        rel["mediainfo"] = mi
    else:
        rel["mediainfo"] = {"indisponivel": "instale: sudo apt install mediainfo"}
    if ferramenta("exiftool"):
        cmd = [ferramenta("exiftool"), "-j", "-a", "-u", "-G1", "-s", "-api", "largefilesupport=1"]
        if profundo:
            cmd += ["-ee3"]
        rel["exiftool"] = _rodar_json(cmd + [str(p)])
        if isinstance(rel["exiftool"], list) and rel["exiftool"]:
            rel["exiftool"] = rel["exiftool"][0]
    else:
        rel["exiftool"] = {"indisponivel": "instale: sudo apt install libimage-exiftool-perl"}
    if ferramenta("c2patool"):
        rel["c2pa"] = _rodar_json([ferramenta("c2patool"), str(p)])
        if "No claim found" in str(rel["c2pa"].get("erro", "")) if isinstance(rel["c2pa"], dict) else False:
            rel["c2pa"] = {"status": "sem Content Credentials (o arquivo não tem manifesto C2PA)"}
    else:
        rel["c2pa"] = {"indisponivel": "c2patool não instalado"}

    rel["pistas_de_origem"] = _pistas(p, rel)
    rel["resumo"] = _resumo(rel)
    return rel


def _pistas(p: Path, rel: dict) -> list[str]:
    pistas = []
    xa = rel["sistema_de_arquivos"]["atributos_estendidos"]
    for k in ("user.xdg.origin.url", "user.xdg.referrer.url", "user.xdg.publisher"):
        if xa.get(k):
            pistas.append(f"{k} = {xa[k]} (gravado pelo navegador/app ao baixar)")
    for rx, txt in _PISTAS_NOME:
        if re.search(rx, p.name, re.I):
            pistas.append(txt)
    blob = json.dumps({k: rel.get(k) for k in ("ffprobe", "exiftool", "c2pa")}, ensure_ascii=False)
    for rx, txt in _PISTAS_TEXTO:
        if re.search(rx, blob, re.I) and txt not in pistas:
            pistas.append(txt)
    urls = sorted(set(re.findall(r"https?://[^\s\"'\\]+", blob)))
    for u in urls[:10]:
        if "c2pa.org" in u or "schema.org" in u or "w3.org" in u or "adobe.com/xap" in u or "ns.adobe.com" in u:
            continue
        pistas.append(f"URL embutida nos metadados: {u}")
    c2 = rel.get("c2pa")
    if isinstance(c2, dict) and c2.get("manifests"):
        pistas.append("possui Content Credentials (C2PA): veja a seção c2pa")
    return pistas


def _resumo(rel: dict) -> dict:
    fp = rel.get("ffprobe", {})
    fmt = fp.get("format", {}) if isinstance(fp, dict) else {}
    tags = fmt.get("tags", {})
    ex = rel.get("exiftool", {}) if isinstance(rel.get("exiftool"), dict) else {}

    def ex_busca(*nomes):
        """Primeiro valor útil, respeitando a ordem de preferência dos nomes."""
        for nome in nomes:
            for k, v in ex.items():
                if k.split(":")[-1] == nome and v not in (None, "") and not str(v).startswith("0000:00:00"):
                    return v
        return None

    faixas = []
    for s in fp.get("streams", []) if isinstance(fp, dict) else []:
        t = s.get("codec_type")
        d = {"indice": s.get("index"), "tipo": t, "codec": s.get("codec_name"),
             "perfil": s.get("profile")}
        st = s.get("tags", {})
        if st.get("language"):
            d["idioma"] = st["language"]
        if st.get("title"):
            d["titulo"] = st["title"]
        if t == "video":
            d.update({"resolucao": f"{s.get('width')}x{s.get('height')}", "fps": s.get("avg_frame_rate"),
                      "pix_fmt": s.get("pix_fmt"), "bitrate": s.get("bit_rate"),
                      "cor": s.get("color_transfer"), "rotacao": next((sd.get("rotation") for sd in s.get("side_data_list", []) if "rotation" in sd), None)})
        elif t == "audio":
            d.update({"canais": s.get("channels"), "layout": s.get("channel_layout"),
                      "taxa_amostragem": s.get("sample_rate"), "bitrate": s.get("bit_rate")})
        faixas.append({k: v for k, v in d.items() if v not in (None, "")})
    return {
        "container": fmt.get("format_long_name"),
        "duracao": fmt_tempo(float(fmt["duration"])) if fmt.get("duration") else None,
        "bitrate_total": fmt.get("bit_rate"),
        "data_criacao": tags.get("creation_time") or ex_busca("CreateDate", "CreationDate", "MediaCreateDate"),
        "dispositivo": " ".join(str(x) for x in (ex_busca("Make"), ex_busca("Model")) if x) or None,
        "software": ex_busca("Software", "Encoder", "CreatorTool") or tags.get("encoder"),
        "gps": ex_busca("GPSCoordinates", "GPSPosition", "LocationInformation"),
        "titulo": tags.get("title"),
        "comentario": tags.get("comment") or tags.get("description"),
        "faixas": faixas,
        "capitulos": len(fp.get("chapters", [])) if isinstance(fp, dict) else 0,
    }


def salvar_relatorio(rel: dict) -> Path:
    d = CACHE_DIR / "relatorios"
    d.mkdir(parents=True, exist_ok=True)
    nome = Path(rel["arquivo"]).name
    out = d / f"{nome}.{_dt.datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(rel, ensure_ascii=False, indent=1, default=str))
    return out


# ---------------------------------------------------------------- exportação
# Exportar cria um ARQUIVO NOVO (ao lado do original ou em `destino`) a partir do arquivo atual do documento
# (a última versão, se houver). Não mexe no original nem nas versões e nunca sobrescreve um arquivo existente.
#
# Cada predefinição é só um dicionário (fácil de estender):
#   tipo: "video" | "audio" | "gif" | "imagem"
#   ext: extensão do arquivo gerado
#   video: "h264" | "h265" | "vp9" | "prores"; crf; preset (x264/x265)
#   audio: "aac" | "opus" | "mp3" | "wav" | "flac" | "pcm"; audio_kbps
#   lado_menor_max: reduz para que o lado menor não passe disso (720 = "720p"); largura: largura máxima
#   tela: (L, A) da tela final (Reels); enquadramento padrão "desfocado" | "recorte" | "barras"
#   fps / fps_max: fps fixo / teto de fps;  limite_mb: tenta caber nesse tamanho (VBV, um passe)
#   faixas: "todas" | "primeira" (faixas de áudio exportadas por padrão); alfa: mantém transparência
#   alternativa: predefinição usada se o encoder não existir no ffmpeg
PREDEFINICOES: dict[str, dict] = {
    "whatsapp": {"rotulo": "WhatsApp", "descricao": "MP4 leve (H.264 + AAC), até 720p, cabe em ~16 MB quando possível",
                 "tipo": "video", "ext": ".mp4", "video": "h264", "crf": 26, "preset": "medium",
                 "lado_menor_max": 720, "fps_max": 30, "audio": "aac", "audio_kbps": 128, "limite_mb": 16,
                 "faixas": "primeira"},
    "youtube": {"rotulo": "YouTube", "descricao": "MP4 H.264 alta qualidade, mantém a resolução, AAC 192k",
                "tipo": "video", "ext": ".mp4", "video": "h264", "crf": 18, "preset": "medium",
                "audio": "aac", "audio_kbps": 192, "faixas": "primeira"},
    "instagram": {"rotulo": "Instagram / Reels", "descricao": "vertical 1080x1920 (9:16); vídeo horizontal ganha fundo desfocado",
                  "tipo": "video", "ext": ".mp4", "video": "h264", "crf": 20, "preset": "medium",
                  "tela": (1080, 1920), "enquadramento": "desfocado", "fps_max": 60,
                  "audio": "aac", "audio_kbps": 128, "faixas": "primeira"},
    "mp4": {"rotulo": "MP4 (H.264)", "descricao": "MP4 H.264 + AAC, boa qualidade, mantém resolução e todas as faixas",
            "tipo": "video", "ext": ".mp4", "video": "h264", "crf": 20, "preset": "medium",
            "audio": "aac", "audio_kbps": 192, "faixas": "todas"},
    "menor": {"rotulo": "Menor tamanho (H.265)", "descricao": "H.265/HEVC com CRF alto: arquivo bem menor, mesma resolução",
              "tipo": "video", "ext": ".mp4", "video": "h265", "crf": 28, "preset": "medium",
              "audio": "aac", "audio_kbps": 96, "faixas": "todas", "alternativa": "menor_vp9"},
    "menor_vp9": {"rotulo": "Menor tamanho (VP9)", "descricao": "WebM VP9 com CRF alto + Opus 96k",
                  "tipo": "video", "ext": ".webm", "video": "vp9", "crf": 40,
                  "audio": "opus", "audio_kbps": 96, "faixas": "todas"},
    "webm": {"rotulo": "WebM (VP9 + Opus)", "descricao": "WebM VP9 + Opus, para a web",
             "tipo": "video", "ext": ".webm", "video": "vp9", "crf": 32,
             "audio": "opus", "audio_kbps": 128, "faixas": "todas"},
    "transparente": {"rotulo": "Transparente (WebM VP9)", "descricao": "WebM VP9 com canal alfa (yuva420p) + Opus",
                     "tipo": "video", "ext": ".webm", "video": "vp9", "crf": 30, "alfa": True,
                     "audio": "opus", "audio_kbps": 128, "faixas": "todas"},
    "transparente_mov": {"rotulo": "Transparente (ProRes 4444 .mov)",
                         "descricao": "ProRes 4444 com alfa (yuva444p10le) + PCM; grande, para editores",
                         "tipo": "video", "ext": ".mov", "video": "prores", "alfa": True,
                         "audio": "pcm", "faixas": "todas"},
    "gif": {"rotulo": "GIF animado", "descricao": "GIF com paleta otimizada, 480 px de largura, 12,5 fps, sem som",
            "tipo": "gif", "ext": ".gif", "largura": 480, "fps": 12.5},
    "mp3": {"rotulo": "Só áudio: MP3", "descricao": "MP3 VBR alta qualidade (~190 kbps)",
            "tipo": "audio", "ext": ".mp3", "audio": "mp3"},
    "m4a": {"rotulo": "Só áudio: M4A (AAC)", "descricao": "AAC 192k em M4A",
            "tipo": "audio", "ext": ".m4a", "audio": "aac", "audio_kbps": 192},
    "opus": {"rotulo": "Só áudio: Opus", "descricao": "Opus 128k (.opus): ótimo e pequeno",
             "tipo": "audio", "ext": ".opus", "audio": "opus", "audio_kbps": 128},
    "ogg": {"rotulo": "Só áudio: OGG (Opus)", "descricao": "Opus 128k em contêiner .ogg",
            "tipo": "audio", "ext": ".ogg", "audio": "opus", "audio_kbps": 128},
    "wav": {"rotulo": "Só áudio: WAV", "descricao": "WAV PCM 16 bits, sem compressão",
            "tipo": "audio", "ext": ".wav", "audio": "wav"},
    "flac": {"rotulo": "Só áudio: FLAC", "descricao": "FLAC, sem perdas",
             "tipo": "audio", "ext": ".flac", "audio": "flac"},
    "png": {"rotulo": "Foto do quadro (PNG)", "descricao": "o quadro exato no tempo pedido, em PNG sem perdas",
            "tipo": "imagem", "ext": ".png"},
    "jpg": {"rotulo": "Foto do quadro (JPG)", "descricao": "o quadro exato no tempo pedido, em JPG",
            "tipo": "imagem", "ext": ".jpg"},
}

APELIDOS_PREDEFINICAO = {
    "reels": "instagram", "insta": "instagram", "tiktok": "instagram", "stories": "instagram",
    "zap": "whatsapp", "wpp": "whatsapp", "h264": "mp4", "h265": "menor", "hevc": "menor",
    "menor_tamanho": "menor", "leve": "menor", "vp9": "webm", "alfa": "transparente",
    "prores": "transparente_mov", "mov_transparente": "transparente_mov", "prores4444": "transparente_mov",
    "jpeg": "jpg", "foto": "png", "print": "png", "quadro": "png", "imagem": "png", "aac": "m4a",
    "audio": "mp3",
}
# contêineres de vídeo sem predefinição própria: usam a "mp4" (H.264 + AAC) com outra extensão
_CONTEINERES_H264 = {"mkv", "mov", "m4v", "avi", "ts"}

_MB = 1_000_000  # "MB" decimal, como mostram os gerenciadores de arquivos


class ExportacaoCancelada(RuntimeError):
    pass


def _tem_encoder(nome: str) -> bool:
    lista = binarios._lista_ffmpeg("encoders")
    return not lista or nome in lista  # lista vazia = não deu para consultar; deixa o ffmpeg decidir


_ENCODER = {"h264": "libx264", "h265": "libx265", "vp9": "libvpx-vp9", "prores": "prores_ks",
            "aac": "aac", "opus": "libopus", "mp3": "libmp3lame"}


def resolver_predefinicao(predefinicao: str | None = None, formato: str | None = None,
                          destino: str | Path | None = None) -> tuple[str, dict]:
    """Escolhe a predefinição pelo nome, pelo formato (extensão) ou pela extensão do destino.
    Devolve (nome, cópia da predefinição com a extensão final)."""
    fmt = (formato or "").lower().lstrip(".") or None
    nome = (predefinicao or "").lower().strip().replace("-", "_").replace(" ", "_") or None
    if not nome and not fmt and destino and Path(str(destino)).suffix:
        fmt = Path(str(destino)).suffix.lower().lstrip(".")
    nome = nome or fmt or "mp4"
    nome = APELIDOS_PREDEFINICAO.get(nome, nome)
    ext = None
    if nome in _CONTEINERES_H264:
        nome, ext = "mp4", "." + nome
    if nome not in PREDEFINICOES:
        raise ValueError(f"predefinição/formato desconhecido: {predefinicao or formato}. "
                         f"Opções: {', '.join(PREDEFINICOES)}")
    p = dict(PREDEFINICOES[nome])
    # encoder ausente → alternativa (ex.: sem libx265, "menor" vira VP9)
    while p.get("alternativa") and not all(_tem_encoder(_ENCODER[p[k]]) for k in ("video", "audio") if p.get(k) in _ENCODER):
        nome = p["alternativa"]
        p = dict(PREDEFINICOES[nome])
    for k in ("video", "audio"):
        enc = _ENCODER.get(p.get(k))
        if enc and not _tem_encoder(enc):
            raise ValueError(f"o ffmpeg encontrado não tem o encoder {enc} (necessário para '{nome}'); "
                             f"veja `diagnostico`")
    if fmt and predefinicao and fmt != p["ext"].lstrip("."):
        ext = "." + fmt  # ex.: predefinicao="youtube", formato="mkv"
    if ext:
        p["ext"] = ext
        if not predefinicao:
            p["sufixo"] = ext.lstrip(".")  # formato="mkv" → video.mkv
    return nome, p


def _livre(p: Path) -> Path:
    """p se não existir; senão nome.2.ext, nome.3.ext..."""
    if not p.exists():
        return p
    i = 2
    while True:
        c = p.with_name(f"{p.stem}.{i}{p.suffix}")
        if not c.exists():
            return c
        i += 1


def _caminho(original: Path, sufixo: str, ext: str, destino: str | Path | None, criar: bool = False) -> Path:
    """`nome.<sufixo>.ext`; quando o sufixo é a própria extensão (mp3, gif, webm...) fica só `nome.ext`
    (e `nome.exportado.ext` se isso coincidir com o original)."""
    pasta = original.parent
    if destino:
        d = Path(str(destino)).expanduser()
        if d.is_dir() or str(destino).endswith("/") or not d.suffix:
            pasta = d
        else:
            if criar:
                d.parent.mkdir(parents=True, exist_ok=True)
            return _livre(d.resolve())
    if criar:
        pasta.mkdir(parents=True, exist_ok=True)
    pasta = pasta.resolve()
    if sufixo.lower() == ext.lstrip(".").lower():
        c = pasta / f"{original.stem}{ext}"
        if c != original:
            return _livre(c)
        sufixo = "exportado"
    return _livre(pasta / f"{original.stem}.{sufixo}{ext}")


def caminho_exportacao(arquivo: Path | str, predefinicao: str | None = None, formato: str | None = None,
                       destino: str | Path | None = None, tempo: float | None = None) -> Path:
    """Caminho que a exportação vai usar: `nome.<predefinição>.ext` ao lado do original (ou na pasta
    `destino`), numerado (`nome.whatsapp.2.mp4`) se já existir. `destino` com extensão = nome de arquivo pedido."""
    original = Documento(arquivo).original
    nome, p = resolver_predefinicao(predefinicao, formato, destino)
    return _caminho(original, _sufixo(nome, p, tempo), p["ext"], destino)


def _sufixo(nome: str, p: dict, tempo: float | None = None) -> str:
    if p["tipo"] == "imagem":
        return f"quadro_{tempo:.3f}s" if tempo is not None else "quadro"
    return p.get("sufixo", nome)


def _fps_stream(s: dict) -> float:
    for k in ("avg_frame_rate", "r_frame_rate"):
        try:
            n, d = s.get(k, "0/0").split("/")
            if float(d) and float(n):
                return float(n) / float(d)
        except (ValueError, ZeroDivisionError):
            pass
    return 0.0


def _dimensoes(s: dict) -> tuple[int, int]:
    """Largura x altura como o vídeo é exibido (o ffmpeg gira automaticamente pela rotação)."""
    w, h = int(s.get("width") or 0), int(s.get("height") or 0)
    rot = next((sd.get("rotation") for sd in s.get("side_data_list", []) if "rotation" in sd), 0) or 0
    if abs(int(rot)) % 180 == 90:
        w, h = h, w
    return w, h


def _crf(p: dict, qualidade) -> int | None:
    if p.get("crf") is None:
        return None
    if qualidade is None:
        return p["crf"]
    if isinstance(qualidade, (int, float)) or str(qualidade).strip().isdigit():
        return int(qualidade)
    desloc = {"maxima": -6, "máxima": -6, "alta": -4, "boa": -2, "normal": 0, "media": 0, "média": 0,
              "baixa": 5, "minima": 10, "mínima": 10}
    q = str(qualidade).lower().strip()
    if q not in desloc:
        raise ValueError("qualidade: número (CRF; menor = melhor) ou alta/normal/baixa")
    return p["crf"] + desloc[q]


def _args_audio(p: dict, canais: int, kbps: int | None = None) -> list[str]:
    a = p.get("audio")
    k = kbps or p.get("audio_kbps")
    if a == "aac":
        return ["-c:a", "aac", "-b:a", f"{k or 192}k"]
    if a == "opus":
        return ["-c:a", "libopus", "-b:a", f"{k or 128}k"]
    if a == "mp3":
        q = ["-b:a", f"{k}k"] if kbps else ["-q:a", "2"]
        return ["-c:a", "libmp3lame", *q] + (["-ac", "2"] if canais > 2 else [])
    if a == "flac":
        return ["-c:a", "flac"]
    return ["-c:a", "pcm_s16le"]  # wav, pcm


def _args_video(p: dict, crf: int | None, kbps: int | None, passe: int | None, log: Path | None,
                teto_kbps: int | None) -> list[str]:
    v = p["video"]
    if v == "h264":
        a = ["-c:v", "libx264", "-preset", p.get("preset", "medium"), "-pix_fmt", "yuv420p"]
        a += ["-b:v", f"{kbps}k"] if kbps else ["-crf", str(crf)]
        if teto_kbps and not kbps:
            a += ["-maxrate", f"{teto_kbps}k", "-bufsize", f"{teto_kbps * 2}k"]
        if passe:
            a += ["-pass", str(passe), "-passlogfile", str(log)]
        return a
    if v == "h265":
        x265 = "log-level=error"
        if passe:
            x265 += f":pass={passe}:stats={log}.x265"
        a = ["-c:v", "libx265", "-preset", p.get("preset", "medium"), "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
        a += ["-b:v", f"{kbps}k"] if kbps else ["-crf", str(crf)]
        if teto_kbps and not kbps:
            x265 += f":vbv-maxrate={teto_kbps}:vbv-bufsize={teto_kbps * 2}"
        return a + ["-x265-params", x265]
    if v == "vp9":
        a = ["-c:v", "libvpx-vp9", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4",
             "-pix_fmt", "yuva420p" if p.get("alfa") else "yuv420p"]
        if p.get("alfa"):
            a += ["-auto-alt-ref", "0"]
        a += ["-b:v", f"{kbps}k"] if kbps else ["-crf", str(crf), "-b:v", "0"]
        if teto_kbps and not kbps:
            a += ["-maxrate", f"{teto_kbps}k"]
        if passe:
            a += ["-pass", str(passe), "-passlogfile", str(log)]
        return a
    if v == "prores":
        return ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-vendor", "apl0"]
    raise ValueError(f"codec de vídeo desconhecido: {v}")


def _cadeia_video(p: dict, w: int, h: int, fps_origem: float, largura, altura, fps, enquadramento,
                  ent: str, sai: str) -> list[str]:
    """Filtros de vídeo da exportação, de [ent] para [sai] (pode ter várias partes)."""
    partes: list[str] = []
    f: list[str] = []
    fps_alvo = fps or p.get("fps")
    if not fps and p.get("fps_max") and fps_origem > p["fps_max"] + 0.01:
        fps_alvo = p["fps_max"]
    if fps_alvo and (fps or fps_origem == 0 or fps_alvo < fps_origem - 0.01):
        f.append(f"fps={fps_alvo:g}")
    tela = p.get("tela")
    if tela:
        L, A = int(largura or tela[0]), int(altura or tela[1])
        modo = (enquadramento or p.get("enquadramento") or "desfocado").lower()
        mesma = w and h and abs(w / h - L / A) < 0.02
        if mesma or modo == "recorte":
            f.append(f"scale={L}:{A}:force_original_aspect_ratio=increase,crop={L}:{A},setsar=1")
        elif modo == "barras":
            f.append(f"scale={L}:{A}:force_original_aspect_ratio=decrease,"
                     f"pad={L}:{A}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
        elif modo == "desfocado":
            # fundo: o próprio vídeo ampliado, recortado e desfocado (em baixa resolução, barato)
            pl, pa = max(2, L // 4 // 2 * 2), max(2, A // 4 // 2 * 2)
            pre = ",".join(f) + "," if f else ""
            partes.append(f"[{ent}]{pre}split=2[xf0][xf1]")
            partes.append(f"[xf0]scale={pl}:{pa}:force_original_aspect_ratio=increase,crop={pl}:{pa},"
                          f"gblur=sigma=12,eq=brightness=-0.06,scale={L}:{A},setsar=1[xbg]")
            partes.append(f"[xf1]scale={L}:{A}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[xfg]")
            partes.append(f"[xbg][xfg]overlay=(W-w)/2:(H-h)/2[{sai}]")
            return partes
        else:
            raise ValueError("enquadramento: 'desfocado', 'recorte' ou 'barras'")
    elif largura or altura:
        if largura and altura:
            f.append(f"scale={int(largura)}:{int(altura)}:force_original_aspect_ratio=decrease:"
                     f"force_divisible_by=2,setsar=1")
        else:
            f.append(f"scale={int(largura) if largura else -2}:{int(altura) if altura else -2},setsar=1")
    elif p.get("lado_menor_max") and min(w, h) > p["lado_menor_max"]:
        m = p["lado_menor_max"]
        f.append(f"scale=-2:{m},setsar=1" if w >= h else f"scale={m}:-2,setsar=1")
    elif p.get("largura") and w > p["largura"]:
        f.append(f"scale={p['largura']}:-2:flags=lanczos,setsar=1")
    elif p["tipo"] == "video" and p.get("video") != "prores" and (w % 2 or h % 2):
        f.append("scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1")  # yuv420p exige dimensões pares
    if p.get("alfa"):
        f.append("format=yuva420p" if p.get("video") == "vp9" else "format=yuva444p10le")
    if p["tipo"] == "gif":
        pre = ",".join(f) + "," if f else ""
        partes.append(f"[{ent}]{pre}split[xg0][xg1]")
        partes.append("[xg0]palettegen=max_colors=256:stats_mode=diff[xpal]")
        partes.append(f"[xg1][xpal]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle[{sai}]")
        return partes
    if f:
        partes.append(f"[{ent}]{','.join(f)}[{sai}]")
    return partes


def _rodar_com_progresso(cmd: list[str], dur: float, progresso=None, base: float = 0.0, peso: float = 1.0,
                         cancelar=None) -> None:
    """Roda o ffmpeg lendo `-progress pipe:1` e chama progresso(fração 0..1)."""
    cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    with tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        try:
            for linha in proc.stdout:
                if cancelar and cancelar():
                    proc.terminate()
                    proc.wait(10)
                    raise ExportacaoCancelada("exportação cancelada")
                k, _, v = linha.strip().partition("=")
                if k == "out_time_us" and progresso and dur > 0:
                    try:
                        progresso(base + peso * min(1.0, max(0.0, int(v) / 1e6 / dur)))
                    except ValueError:
                        pass
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if proc.returncode != 0:
            err.seek(0)
            raise ErroFFmpeg(f"comando falhou ({proc.returncode}): {shlex.join(cmd)}\n{err.read()[-4000:]}")
    if progresso:
        progresso(base + peso)


def _finalizar(tmp: Path, final: Path) -> Path:
    """Move o arquivo temporário para o nome final sem nunca sobrescrever (link atômico)."""
    while True:
        final = _livre(final)
        try:
            os.link(tmp, final)
            tmp.unlink()
            return final
        except FileExistsError:
            continue
        except OSError:  # sistema de arquivos sem hardlink (ex.: FAT)
            if final.exists():
                continue
            os.replace(tmp, final)
            return final


def info_arquivo(caminho: Path) -> dict:
    """Resumo técnico de um arquivo gerado (para o relatório da exportação)."""
    pr = ffprobe(caminho)
    tam = caminho.stat().st_size
    d = duracao(pr)
    r: dict = {"caminho": str(caminho), "nome": caminho.name, "tamanho_bytes": tam,
               "tamanho_mb": round(tam / _MB, 2), "formato": pr.get("format", {}).get("format_name")}
    if d:
        r["duracao"] = round(d, 3)
        r["duracao_fmt"] = fmt_tempo(d)
    vs = [s for s in pr.get("streams", []) if s.get("codec_type") == "video"]
    if vs:
        v = vs[0]
        r["video"] = {"codec": v.get("codec_name"), "resolucao": f"{v.get('width')}x{v.get('height')}",
                      "pix_fmt": v.get("pix_fmt"), "fps": round(_fps_stream(v), 3) or None}
        if v.get("tags", {}).get("alpha_mode") or v.get("tags", {}).get("ALPHA_MODE"):
            r["video"]["alfa"] = True  # o decodificador nativo do VP9 ignora o alfa; o libvpx o lê
            if r["video"]["pix_fmt"] == "yuv420p":
                r["video"]["pix_fmt"] = "yuva420p"
    aus = faixas_audio(pr)
    if aus:
        r["audio"] = [{"codec": a.get("codec_name"), "canais": a.get("channels"),
                       "taxa_amostragem": a.get("sample_rate"), "bitrate": a.get("bit_rate")} for a in aus]
    return r


def exportar(arquivo: Path | str, predefinicao: str | None = None, formato: str | None = None,
             inicio=None, fim=None, trechos: list | None = None, tempo=None,
             destino: str | Path | None = None, alvo_mb: float | None = None,
             largura: int | None = None, altura: int | None = None, qualidade=None, fps: float | None = None,
             sem_audio: bool = False, faixa: int | None = None, enquadramento: str | None = None,
             estado: dict | None = None, progresso=None, cancelar=None) -> dict:
    """Exporta para um arquivo NOVO (não altera o original nem as versões).

    arquivo: documento (o original ou uma versão); lê-se a última versão se houver edições não salvas.
    predefinicao: ver PREDEFINICOES (whatsapp, youtube, instagram, gif, mp3, png...); formato: extensão.
    inicio/fim ou trechos [(ini, fim), ...]: tempos no formato de parse_tempo ('m1', 'atual', '1:23'...),
    resolvidos com `estado` (o estado da janela). Vários trechos são concatenados, com corte exato.
    tempo: instante da foto (predefinições png/jpg; padrão = posição atual).
    alvo_mb: tamanho máximo; calcula o bitrate e codifica em 2 passes.
    progresso(fração 0..1) é chamado durante a codificação; cancelar() → True interrompe.
    """
    arq = Path(arquivo).expanduser().resolve()
    doc = Documento(arq)
    entrada = arq if arq.parent == doc.pasta else doc.atual()
    if not entrada.exists():
        raise FileNotFoundError(f"arquivo não encontrado: {entrada}")
    nome, p = resolver_predefinicao(predefinicao, formato, destino)
    probe = ffprobe(entrada)
    dur = duracao(probe)
    est = dict(estado or {})
    est.setdefault("duracao", dur)
    vids = [s for s in probe["streams"] if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    auds = faixas_audio(probe)
    tipo = p["tipo"]
    if tipo in ("video", "gif", "imagem") and not vids:
        raise ValueError(f"'{nome}' precisa de vídeo, mas o arquivo é só áudio; use mp3/m4a/opus/wav/flac")
    if tipo == "audio" and not auds:
        raise ValueError("o arquivo não tem áudio")
    avisos: list[str] = []

    # ---------------- foto de um quadro
    if tipo == "imagem":
        if tempo is None:
            tempo = "atual" if est.get("posicao") is not None else 0
        t = parse_tempo(tempo, est)
        fq = _fps_stream(vids[0])
        n = None
        ss = max(0.0, t)
        if fq:
            # quadro exibido no instante t = o último que começa em ou antes de t. Com busca precisa o ffmpeg
            # entrega o primeiro quadro com pts >= ss; meio quadro antes garante exatamente o quadro n.
            n = int(t * fq + 1e-6)
            ss = max(0.0, (n - 0.5) / fq)
        final = _caminho(doc.original, _sufixo(nome, p, t), p["ext"], destino, criar=True)
        tmp = final.with_name(f".{final.stem}.parcial{final.suffix}")
        cmd = FFMPEG + ["-ss", f"{ss:.6f}", "-i", str(entrada), "-map", "0:V:0", "-frames:v", "1",
                        "-map_metadata", "-1"]
        if largura or altura:
            cmd += ["-vf", f"scale={int(largura) if largura else -2}:{int(altura) if altura else -2}"]
        if p["ext"] == ".jpg":
            cmd += ["-q:v", "2"]
        cmd += ["-update", "1", str(tmp)]
        try:
            rodar(cmd)
            final = _finalizar(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        if progresso:
            progresso(1.0)
        r = info_arquivo(final)
        r.pop("duracao", None)
        r.pop("duracao_fmt", None)
        r.get("video", {}).pop("fps", None)
        return {**r, "predefinicao": nome, "tempo": round(t, 3), "tempo_fmt": fmt_tempo(t), "quadro": n,
                "origem": str(entrada), "comandos": [shlex.join(cmd)]}

    # ---------------- trechos
    lista = None
    if trechos:
        lista = [(parse_tempo(a, est), parse_tempo(b, est)) for a, b in trechos]
    elif inicio is not None or fim is not None:
        lista = [(parse_tempo(inicio if inicio is not None else 0, est),
                  parse_tempo(fim if fim is not None else "fim", est))]
    if lista is not None:
        limpos = []
        for a, b in lista:
            a, b = max(0.0, a), min(dur, b) if dur else b
            if b - a > 0.001:
                limpos.append((a, b))
        if not limpos:
            raise ValueError("nenhum trecho válido (verifique os tempos)")
        lista = limpos
        dur_saida = sum(b - a for a, b in lista)
    else:
        dur_saida = dur

    # ---------------- faixas de áudio
    if sem_audio or tipo == "gif":
        idx_a: list[int] = []
    elif faixa:
        if not 1 <= int(faixa) <= len(auds):
            raise ValueError(f"faixa {faixa} não existe (há {len(auds)})")
        idx_a = [int(faixa) - 1]
    elif p.get("faixas") == "todas":
        idx_a = list(range(len(auds)))
    else:
        idx_a = [0] if auds else []
    if len(auds) > 1 and len(idx_a) == 1 and not faixa:
        avisos.append(f"o arquivo tem {len(auds)} faixas de áudio; exportada só a A1 (use faixa=N para outra)")
    canais = max((int(auds[i].get("channels") or 2) for i in idx_a), default=2)
    tem_v = tipo != "audio"

    # ---------------- entradas e grafo
    n = len(lista) if lista else 1
    entradas: list[str] = []
    if lista:
        for a, b in lista:
            entradas += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(entrada)]
    else:
        entradas += ["-i", str(entrada)]
    w, h = _dimensoes(vids[0]) if vids else (0, 0)
    fps_origem = _fps_stream(vids[0]) if vids else 0.0

    def montar(com_audio: bool) -> tuple[list[str], list[str]]:
        """(argumentos de filtro/mapa, rótulos) para um passe; sem áudio no 1º passe."""
        grafo: list[str] = []
        ia = idx_a if com_audio else []
        if n > 1:
            ent = ""
            for i in range(n):
                if tem_v:
                    grafo.append(f"[{i}:V:0]setpts=PTS-STARTPTS[v{i}]")
                    ent += f"[v{i}]"
                for k in ia:
                    grafo.append(f"[{i}:a:{k}]asetpts=PTS-STARTPTS[a{k}_{i}]")
                    ent += f"[a{k}_{i}]"
            sai = ("[vc]" if tem_v else "") + "".join(f"[ac{k}]" for k in ia)
            grafo.append(f"{ent}concat=n={n}:v={int(tem_v)}:a={len(ia)}{sai}")
            v_ent, a_map = "vc", [f"[ac{k}]" for k in ia]
        else:
            v_ent, a_map = "0:V:0", [f"0:a:{k}" for k in ia]
        args: list[str] = []
        if tem_v:
            cadeia = _cadeia_video(p, w, h, fps_origem, largura, altura, fps, enquadramento, v_ent, "vo")
            grafo += cadeia
            v_map = "[vo]" if cadeia else (f"[{v_ent}]" if n > 1 else v_ent)
        if grafo:
            args += ["-filter_complex", ";".join(grafo)]
        if tem_v:
            args += ["-map", v_map]
        for j, (k, m) in enumerate(zip(ia, a_map)):
            args += ["-map", m]
            if n > 1:
                args += _tags_faixa(auds[k], f"s:a:{j}")
        return args, a_map

    # ---------------- taxa de bits (alvo_mb / limite da predefinição)
    kbps_audio = 0
    if idx_a and p.get("audio") in ("aac", "opus", "mp3"):
        kbps_audio = (p.get("audio_kbps") or 190) * len(idx_a)
    kbps_video = teto = kbps_a_forcado = None
    if alvo_mb:
        if tipo == "gif" or p.get("video") == "prores" or (tipo == "audio" and p.get("audio") in ("wav", "flac")):
            raise ValueError(f"'{nome}' não tem controle de tamanho; alvo_mb vale para vídeo e áudio comprimido")
        # margem para o contêiner e o erro do controle de taxa (o x265 erra um pouco mais para cima)
        total_kbits = alvo_mb * _MB * 8 / 1000 * (0.97 if p.get("video") == "h265" else 0.985)
        if tipo == "audio":
            k = int(total_kbits / max(dur_saida, 0.1) / len(idx_a))
            if k < 32:
                raise ValueError(f"{alvo_mb} MB é pouco para {fmt_tempo(dur_saida)} de áudio (daria {k} kbps)")
            kbps_a_forcado = min(k, 320 if p["audio"] == "mp3" else 256)
            if p["audio"] == "mp3":  # o LAME só usa taxas padrão (e arredondaria para cima)
                kbps_a_forcado = max(b for b in (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
                                     if b <= kbps_a_forcado)
        else:
            kv = (total_kbits - kbps_audio * dur_saida) / max(dur_saida, 0.1)
            if kv < 150 and kbps_audio:
                kbps_audio = 64 * len(idx_a)  # aperta o áudio antes de desistir
                kbps_a_forcado = 64
                kv = (total_kbits - kbps_audio * dur_saida) / max(dur_saida, 0.1)
                avisos.append("áudio reduzido para 64 kbps para caber no tamanho pedido")
            if kv < 60:
                raise ValueError(f"{alvo_mb} MB é pouco para {fmt_tempo(dur_saida)} de vídeo "
                                 f"(daria {kv:.0f} kbps); aumente o alvo, reduza a resolução ou exporte um trecho")
            if kv < 300:
                avisos.append(f"bitrate de vídeo baixo ({kv:.0f} kbps): a qualidade vai cair; "
                              f"considere reduzir a resolução (largura/altura)")
            kbps_video = int(kv)
    elif p.get("limite_mb") and tem_v:
        kv = (p["limite_mb"] * _MB * 8 / 1000 * 0.95 - kbps_audio * dur_saida) / max(dur_saida, 0.1)
        if kv >= 400:
            teto = int(kv)  # CRF com teto (VBV): fica bonito quando cabe e não passa do limite
        else:
            avisos.append(f"com {fmt_tempo(dur_saida)} não cabe em {p['limite_mb']} MB com qualidade aceitável; "
                          f"exportado sem limite (use alvo_mb para forçar ou exporte um trecho)")

    crf = _crf(p, qualidade) if tem_v else None
    final = _caminho(doc.original, _sufixo(nome, p), p["ext"], destino, criar=True)
    tmp = final.with_name(f".{final.stem}.parcial{final.suffix}")
    meta = ["-map_metadata", "-1"] if tipo == "gif" else ["-map_metadata", "0"]
    if lista or tipo == "gif":
        meta += ["-map_chapters", "-1"]
    saida_extra = ["-movflags", "+faststart"] if p["ext"] in (".mp4", ".m4a", ".mov", ".m4v") else []
    if tipo == "gif":
        saida_extra = ["-loop", "0"]
    comandos: list[list[str]] = []
    logdir = None
    try:
        a_args = _args_audio(p, canais, kbps_a_forcado) if idx_a else ["-an"]
        if tipo == "audio":
            args, _ = montar(True)
            cmd = FFMPEG + entradas + args + ["-vn"] + a_args + meta + saida_extra + [str(tmp)]
            comandos.append(cmd)
            _rodar_com_progresso(cmd, dur_saida, progresso, cancelar=cancelar)
        elif kbps_video:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            logdir = Path(tempfile.mkdtemp(prefix="2passes-", dir=CACHE_DIR))
            log = logdir / "passe"
            args1, _ = montar(False)
            cmd1 = (FFMPEG + entradas + args1 + _args_video(p, crf, kbps_video, 1, log, None)
                    + ["-an", "-f", "null", os.devnull])
            args2, _ = montar(True)
            cmd2 = (FFMPEG + entradas + args2 + _args_video(p, crf, kbps_video, 2, log, None)
                    + a_args + meta + saida_extra + [str(tmp)])
            comandos += [cmd1, cmd2]
            _rodar_com_progresso(cmd1, dur_saida, progresso, 0.0, 0.5, cancelar)
            _rodar_com_progresso(cmd2, dur_saida, progresso, 0.5, 0.5, cancelar)
        else:
            args, _ = montar(True)
            v_args = [] if tipo == "gif" else _args_video(p, crf, None, None, None, teto)
            cmd = FFMPEG + entradas + args + v_args + (a_args if tipo != "gif" else []) + meta + saida_extra + [str(tmp)]
            comandos.append(cmd)
            _rodar_com_progresso(cmd, dur_saida, progresso, cancelar=cancelar)
        final = _finalizar(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
        if logdir:
            shutil.rmtree(logdir, ignore_errors=True)

    r = info_arquivo(final)
    limite = alvo_mb or (p.get("limite_mb") if tem_v else None)
    if limite and r["tamanho_bytes"] > limite * _MB * 1.02:
        avisos.append(f"ficou com {r['tamanho_mb']} MB (acima de {limite} MB)")
    r.update({"predefinicao": nome, "descricao_predefinicao": p["descricao"], "origem": str(entrada),
              "trechos": [[fmt_tempo(a), fmt_tempo(b)] for a, b in lista] if lista else None,
              "comandos": [shlex.join(c) for c in comandos]})
    if kbps_video:
        r["dois_passes"] = {"alvo_mb": alvo_mb, "bitrate_video_kbps": kbps_video}
    if teto:
        r["teto_bitrate_kbps"] = teto
    if avisos:
        r["avisos"] = avisos
    return r
