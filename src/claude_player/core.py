"""Núcleo do claude-player: caminhos, tempo, versões, ffmpeg e metadados.

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

# ---------------------------------------------------------------- caminhos
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "claude-player"
SOCKET_PATH = RUNTIME_DIR / "controle.sock"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-player"
LOCAL_BIN = Path.home() / ".local" / "bin"


def ferramenta(nome: str) -> str | None:
    """Localiza um executável no PATH ou em ~/.local/bin."""
    p = shutil.which(nome)
    if p:
        return p
    cand = LOCAL_BIN / nome
    return str(cand) if cand.exists() else None


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
    ou palavras: atual/aqui, in/entrada, out/saida, inicio, fim.
    Também aceita expressões simples: "atual-2", "in+0.5", "out - 1:00"."""
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip().lower().replace(",", ".")
    m = re.fullmatch(r"([a-zçíá]+)\s*([+-])\s*(.+)", s)
    if m and m.group(1) in _KW:
        base = parse_tempo(m.group(1), estado)
        d = parse_tempo(m.group(3), estado)
        return base + d if m.group(2) == "+" else base - d
    if s in _KW:
        k = _KW[s]
        if k == "inicio":
            return 0.0
        if estado is None:
            raise ValueError(f"'{valor}' precisa do player aberto")
        v = {"pos": estado.get("posicao"), "in": estado.get("marca_in"),
             "out": estado.get("marca_out"), "fim": estado.get("duracao")}[k]
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
FFMPEG = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]


class ErroFFmpeg(RuntimeError):
    pass


def rodar(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise ErroFFmpeg(f"comando falhou ({r.returncode}): {shlex.join(cmd)}\n{r.stderr[-4000:]}")
    return r


def ffprobe(path: Path | str) -> dict:
    r = rodar(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
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
            if lixeira and shutil.which("gio"):
                subprocess.run(["gio", "trash", str(self.original)], check=True)
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
    cmd = ["ffmpeg", "-hide_banner", "-nostdin"]
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
    (r"Apple|iPhone|iPad|Core Media", "dispositivo/software Apple"),
    (r"com\.android|Android", "dispositivo Android"),
    (r"samsung", "dispositivo Samsung"),
    (r"\bDJI\b", "drone/câmera DJI"),
    (r"GoPro", "câmera GoPro"),
    (r"Adobe|Premiere|After Effects|Media Encoder", "software Adobe"),
    (r"DaVinci|Resolve|Blackmagic", "DaVinci Resolve/Blackmagic"),
    (r"CapCut|Bytedance|ByteDance", "editado no CapCut"),
    (r"Kdenlive|Shotcut|OpenShot|MLT", "editado em editor livre (Kdenlive/Shotcut/MLT)"),
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

    rel["ffprobe"] = _rodar_json(["ffprobe", "-v", "error", "-print_format", "json",
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
        if "c2pa.org" in u or "w3.org" in u or "adobe.com/xap" in u or "ns.adobe.com" in u:
            continue
        pistas.append(f"URL embutida nos metadados: {u}")
    c2 = rel.get("c2pa")
    if isinstance(c2, dict) and c2 and not ({"erro", "indisponivel"} & set(c2)):
        pistas.append("possui Content Credentials (C2PA): veja a seção c2pa")
    return pistas


def _resumo(rel: dict) -> dict:
    fp = rel.get("ffprobe", {})
    fmt = fp.get("format", {}) if isinstance(fp, dict) else {}
    tags = fmt.get("tags", {})
    ex = rel.get("exiftool", {}) if isinstance(rel.get("exiftool"), dict) else {}

    def ex_busca(*nomes):
        for k, v in ex.items():
            if k.split(":")[-1] in nomes:
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
        "software": ex_busca("Software", "Encoder", "HandlerDescription") or tags.get("encoder"),
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
