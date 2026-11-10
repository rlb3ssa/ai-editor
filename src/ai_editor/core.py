"""Núcleo do AI Editor: caminhos, tempo, versões, ffmpeg e metadados.

Usado tanto pela janela (app.py) quanto pelo servidor MCP (mcp_server.py).
"""
from __future__ import annotations

import datetime as _dt
import functools
import hashlib
import json
import math
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
    probe = json.loads(r.stdout)
    _corrigir_fps_vfr(probe, path)
    return probe


_TAXAS_COMUNS = (23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60, 90, 100, 119.88, 120, 144, 240)


def _corrigir_fps_vfr(probe: dict, path) -> None:
    """WebM/MKV de gravação de tela (Clipchamp, OBS, navegador) e vídeos de taxa variável costumam vir com
    avg_frame_rate 0/0 e r_frame_rate = escala de tempo (1000/1). Tudo que usa a taxa (cortes, grade de
    quadros, efeitos) passaria a trabalhar a 1000 fps: 40× mais quadros, horas de encode e um arquivo
    de 1000 fps. Aqui a taxa média real é medida pelos pacotes (sem decodificar, ~0,2 s por minuto) e
    gravada em avg_frame_rate, arredondada para a taxa comum mais próxima (até 3%) ou para inteiro."""
    for s in probe.get("streams", []):
        if s.get("codec_type") != "video" or (s.get("disposition") or {}).get("attached_pic"):
            continue
        try:
            n, d = (s.get("avg_frame_rate") or "0/0").split("/")
            media = float(n) / float(d) if float(d) else 0.0
        except ValueError:
            media = 0.0
        if 0 < media <= 240:
            continue
        try:
            r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", f"{s.get('index', 0)}",
                                "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)],
                               capture_output=True, text=True, timeout=120)
            pts = sorted(float(x) for x in r.stdout.split() if x not in ("", "N/A"))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue
        if len(pts) < 3 or pts[-1] <= pts[0]:
            continue
        fps = (len(pts) - 1) / (pts[-1] - pts[0])
        perto = min(_TAXAS_COMUNS, key=lambda c: abs(c - fps))
        fps = perto if abs(perto - fps) / fps <= 0.03 else max(1, round(fps))
        s["avg_frame_rate"] = {23.976: "24000/1001", 29.97: "30000/1001", 59.94: "60000/1001",
                               119.88: "120000/1001"}.get(fps, f"{int(fps)}/1")
        if _fps_stream({"r_frame_rate": s.get("r_frame_rate", "0/0")}) > 240:
            s["r_frame_rate"] = s["avg_frame_rate"]
        s["ai_editor_fps_medido"] = True


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

    # -- trava de tarefa (objetos.py grava versões em segundo plano; ver objetos._gravando_versao)
    @staticmethod
    def _ler_trava(trava: Path) -> dict | None:
        """Dados da trava `.versoes/.trava` se o dono (pid/thread) está vivo; None se não existe ou está morta."""
        try:
            d = json.loads(trava.read_text())
            pid, thread = int(d["pid"]), int(d.get("thread") or 0)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if pid == os.getpid():
            import threading
            return d if any(t.ident == thread for t in threading.enumerate()) else None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        return d

    def _conferir_trava(self) -> None:
        """Erro se uma tarefa (outra thread/processo) está gravando uma versão deste documento."""
        d = self._ler_trava(self.pasta / ".trava")
        if d is None:
            return
        import threading
        if int(d["pid"]) == os.getpid() and int(d.get("thread") or 0) == threading.get_ident():
            return  # a própria tarefa
        raise RuntimeError(f"tarefa {d.get('tarefa')} está gravando uma versão ({d.get('descricao')}); "
                           f"espere terminar (`tarefas`) ou cancele")

    def proxima_saida(self, ext: str | None = None) -> Path:
        self._conferir_trava()
        self.pasta.mkdir(exist_ok=True)
        n = len(self._ler()["versoes"]) + 1
        while any(self.pasta.glob(f"v{n:03d}.*")):  # nome reservado por uma tarefa (ou sobra): pula
            n += 1
        ext = ext or self.atual().suffix
        if not ext.startswith("."):
            ext = "." + ext
        return self.pasta / f"v{n:03d}{ext}"

    def registrar(self, saida: Path, descricao: str, comandos: list[list[str]]) -> dict:
        self._conferir_trava()
        h = self._ler()
        h["versoes"] = [v for v in h["versoes"] if (self.pasta / v["arquivo"]).exists()]
        v = {"n": len(h["versoes"]) + 1, "arquivo": saida.name, "descricao": descricao,
             "data": _dt.datetime.now().isoformat(timespec="seconds"),
             "comandos": [shlex.join(c) for c in comandos]}
        h["versoes"].append(v)
        self._gravar(h)
        return v

    def desfazer(self) -> dict | None:
        self._conferir_trava()
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
        self._conferir_trava()
        n = len(self.versoes())
        shutil.rmtree(self.pasta, ignore_errors=True)
        return n

    def salvar(self, lixeira: bool = True) -> Path:
        """A última versão substitui o original; original e versões são removidos.
        Por segurança o original vai para a Lixeira (lixeira=False apaga de vez)."""
        self._conferir_trava()
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


def _cmd_preciso(entrada: Path, probe: dict, trechos: list[tuple[float, float]], saida: Path,
                 args_video: list[str] | None = None) -> list[str]:
    """Corte exato: cada trecho é uma entrada com seek próprio; trim/concat num único filtro,
    mantendo áudio e vídeo sincronizados ao quadro em todas as faixas (ver `_segmentos_av`).
    args_video: codec de vídeo (padrão codec_video da extensão; o dividir usa _args_video_edicao)."""
    vids = [s for s in probe["streams"] if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    auds = faixas_audio(probe)
    tem_v = bool(vids)
    dec = _dec_entrada(vids[0]) if vids else []
    cmd = FFMPEG[:]
    segs = []
    for a, b in trechos:
        # µs arredondado PARA BAIXO: com 3 casas (ou arredondando para cima), um corte exatamente num quadro
        # (8,1666667 → 8,167) perderia esse quadro
        a6, b6 = int(a * 1e6) / 1e6, int(b * 1e6) / 1e6
        segs.append({"a": a6, "b": b6})
        # -t com folga só limita a decodificação; o fim exato vem do trim/atrim
        cmd += ["-ss", f"{a6:.6f}", "-t", f"{b6 - a6 + _FOLGA_T:.6f}"] + dec + ["-i", str(entrada)]
    taxa = _taxa_video(vids[0]) if tem_v else ""
    grafo = _segmentos_av(segs, auds, taxa if tem_v else None, lambda i: f"[{i}:V:0]",
                          atraso=_atraso_video(probe, vids[0]) if tem_v else 0.0)
    cmd += ["-filter_complex", ";".join(grafo)]
    if tem_v:
        cmd += ["-map", "[vc]"] + (args_video or codec_video(saida.suffix)) + _tags_faixa(vids[0], "s:v:0")
    for k, s in enumerate(auds):
        cmd += ["-map", f"[ao{k}]"] + _tags_faixa(s, f"s:a:{k}")
    if auds:
        cmd += codec_audio(saida.suffix)
    if saida.suffix.lower() in (".mkv", ".mka") and any(s.get("codec_type") == "attachment"
                                                       for s in probe["streams"]):
        cmd += ["-map", "0:t?", "-c:t", "copy"]  # fontes das legendas ASS do MKV
    cmd += ["-map_metadata", "0", "-map_chapters", "-1", str(saida)]
    return cmd


def _atraso_video(probe: dict, v: dict) -> float:
    """start_time do vídeo menos o do contêiner (o pts do 1º quadro no -filter_complex; MKV costuma ter 0,021 s)."""
    try:
        return max(0.0, float(v.get("start_time") or 0) - float(probe.get("format", {}).get("start_time") or 0))
    except ValueError:
        return 0.0


_FOLGA_T = 0.5  # s a mais no -t das entradas por trecho (o corte exato é feito por trim/atrim)


def _filtro_layout(s: dict) -> str:
    """Prefixo de filtro que põe a faixa no layout padrão do seu nº de canais (5.1(side) → 5.1, AAC de
    6 canais "unknown" → 5.1), só renomeando os canais pela ordem (`channelmap`, sem mixagem).
    Sem isso: o AAC grava 5.1(side) como layout "unknown", que depois quebra concat/aformat
    ("Channel layout change is not supported") ou vira mono no encoder."""
    n = int(s.get("channels") or 0)
    padrao = _LAYOUT_PADRAO.get(n)
    if not padrao or str(s.get("channel_layout") or "").strip() == padrao:
        return ""
    mapa = "|".join(str(i) for i in range(n))
    return f"channelmap=map={mapa}:channel_layout={padrao},"


def _taxa_video(v: dict) -> str:
    """Taxa de quadros (texto para o filtro fps) de saída das edições que reconstroem a linha do tempo."""
    fps = _fps_stream(v) or 30.0
    taxa = v.get("r_frame_rate") or v.get("avg_frame_rate") or ""
    if not re.fullmatch(r"\d+/[1-9]\d*", taxa) or _fps_stream({"r_frame_rate": taxa}) > 240:
        taxa = f"{fps:g}"
    return taxa


def _fase_quadro(x: float) -> float:
    """Parte fracionária de x (em quadros) em [−0,001, 0,999): perto de inteiro conta como 0."""
    return x - math.floor(x + 1e-3)


def _segmentos_av(segs: list[dict], auds: list[dict], taxa: str | None, ent_video,
                  faixas: list[int] | None = None, atraso: float = 0.0) -> list[str]:
    """Filtros de N segmentos (entrada i = `-ss a -t ... -i` do segmento i) + concat → [vc] e [ao0], [ao1]...
    seg: {"a", "b" (None = até o fim), "fator", "manter_tom", "silenciar"} ou {"congelar": True, "d": s}
    (entrada de congelar: começa meio quadro antes do quadro desejado).
    Sincronia exata, sem acumular erro por segmento:
    - nada de PTS-STARTPTS por faixa (com -ss na entrada todas as faixas já têm 0 = ponto `a`, mantendo o
      atraso entre elas); o fim é trim/atrim `end` (o -t da entrada conta do 1º quadro de CADA faixa);
    - o concat soma a duração de cada segmento como o MAIOR fim entre as faixas, e estima o fim do vídeo
      por "último pts × n/(n−1)"; quadro fora da grade (MKV com vídeo em 0,021 s) inflava cada segmento
      ~26 ms e o áudio ganhava silêncio. Por isso vídeo e áudio têm concats separados: o áudio tem a
      duração exata (aresample first_pts=0 preenche atraso no início, apad no fim) e o vídeo vai para a
      grade `fps` do segmento com exatamente round(T_fim·fps) − round(T_ini·fps) quadros (erro ≤ ½ quadro,
      sem acumular).
    - quadro fora da grade do segmento (MKV com vídeo em 0,021 s, corte entre dois quadros): com -ss a o 1º
      quadro tem pts (atraso + k/fps − a), uma fração φ de quadro; o fps (round=near) o empurrava para o slot 1 e
      duplicava (cada corte perdia um quadro e repetia outro). Por isso o vídeo é adiantado em φ (< 1 quadro, sem
      acumular) e os limites contam quadros na fase do segmento: ceil(t·fps − φ) = nº de quadros que COMEÇAM
      antes de t. Cada quadro fica no segmento em que começa. atraso = start_time do vídeo − o do contêiner.
    ent_video(i) → rótulo de entrada do vídeo do segmento i já com prefixo (ex.: normalização de SAR).
    faixas: índice de cada item de `auds` na entrada (`[i:a:N]`); padrão 0, 1, 2... As saídas são sempre
    [ao0], [ao1]... na ordem de `auds`."""
    fps = _fps_stream({"r_frame_rate": taxa}) if taxa and "/" in taxa else float(taxa or 0)
    grafo, cv, ca = [], "", ""
    t_saida = 0.0

    def fase(sg: dict) -> float | None:  # φ em quadros (None: congelar/velocidade, grade por arredondamento)
        if not fps or "congelar" in sg or float(sg.get("fator") or 1.0) != 1:
            return None
        return _fase_quadro((atraso - float(sg.get("a") or 0.0)) * fps)

    def limite(t: float, ph: float | None) -> int:  # quadros da saída antes de t
        return round(t * fps) if ph is None else math.ceil(t * fps - ph - 1e-3)

    for i, sg in enumerate(segs):
        ultimo = sg.get("b") is None and "congelar" not in sg
        if "congelar" in sg:
            d_out = sg["d"]
        else:
            d_src = (sg["b"] - sg["a"]) if not ultimo else None
            fator = float(sg.get("fator") or 1.0)
            d_out = d_src / fator if d_src is not None else None
        if taxa:
            ph = fase(sg)
            # limite com o próximo segmento na fase DELE (soma telescópica: o total não acumula erro)
            ph_fim = fase(segs[i + 1]) if i + 1 < len(segs) else ph
            adianta = f"setpts=PTS-{ph / fps:.6f}/TB," if ph and abs(ph) > 1e-3 else ""
            if d_out is not None:
                nq = limite(t_saida + d_out, ph_fim) - limite(t_saida, ph)
                cauda = f"{adianta}fps={taxa}:start_time=0,tpad=stop_mode=clone:stop=-1,trim=end_frame={max(nq, 1)}"
            else:
                cauda = f"{adianta}fps={taxa}:start_time=0"
            if "congelar" in sg:
                grafo.append(f"{ent_video(i)}trim=end_frame=1,setpts=PTS-STARTPTS,{cauda}[v{i}]")
            else:
                # o fim do vídeo é o nº de quadros da cauda (sem trim=end antes do fps: um trecho antes do
                # 1º quadro, ex. vídeo que começa 0,3 s depois do áudio, ficaria sem quadro nenhum e o
                # concat falharia; assim o fps repete o 1º quadro disponível)
                vpts = "" if fator == 1 else f"setpts=PTS/{fator:.6g},"
                grafo.append(f"{ent_video(i)}{vpts}{cauda}[v{i}]")
            cv += f"[v{i}]"
        for k, s in enumerate(auds):
            lay = _filtro_layout(s)
            if "congelar" in sg:
                # silêncio tirado da própria faixa (volume 0 + apad): mesmo formato/layout dos outros
                # segmentos (anullsrc + concat falha com layouts como AAC 6 canais "unknown")
                cad = f"{lay}asetpts=PTS-STARTPTS,volume=0,apad,atrim=duration={d_out:.6f}"
            else:
                # aresample ANTES do atrim: "atrim (curto, no início do arquivo) → apad → concat" falha no
                # ffmpeg 6.1 ("Invalid data found when processing input")
                ff = [f"{lay}aresample=async=1:min_hard_comp=0.01:first_pts=0"]
                if d_src is not None:
                    ff.append(f"atrim=end={d_src:.6f}")
                if fator != 1:
                    if sg.get("manter_tom", True):
                        ff.append(_cadeia_atempo(fator))
                    else:
                        sr = int(s.get("sample_rate") or 48000)
                        ff.append(f"asetrate={sr * fator:.0f},aresample={sr}")
                if sg.get("silenciar"):
                    ff.append("volume=0")
                if d_out is not None:
                    ff.append(f"apad=whole_dur={d_out:.6f},atrim=end={d_out:.6f}")
                cad = ",".join(f for f in ff if f)
            grafo.append(f"[{i}:a:{faixas[k] if faixas else k}]{cad}[a{k}_{i}]")
            ca += f"[a{k}_{i}]"
        if d_out is not None:
            t_saida += d_out
    n = len(segs)
    if taxa:
        grafo.append(f"{cv}concat=n={n}:v=1:a=0[vc]")
    if auds:
        grafo.append(f"{ca}concat=n={n}:v=0:a={len(auds)}" + "".join(f"[ao{k}]" for k in range(len(auds))))
    return grafo


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
        # {LAT}/{ST}: preenchidos por faixa em aplicar_filtro_audio (atraso do afftdn ~25 ms e início da faixa);
        # sem compensar, o áudio ficava 25 ms atrasado em relação ao vídeo e perdia esse tanto do fim
        filtros.append("asetpts=PTS-STARTPTS,apad=pad_dur={LAT},afftdn=nf=-25,atrim=start={LAT},"
                       "asetpts=PTS-STARTPTS+{ST}/TB")
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
    if "{LAT}" in cadeia or "{ST}" in cadeia:
        info = _info_audio(entrada)
        partes = []
        for f in faixas:
            s = info["auds"][f]
            st, _ = _extensao_faixa(info, s)
            lat = latencia_filtro("afftdn=nr=10:nf=-50", int(s.get("sample_rate") or 48000))
            partes.append(f"[0:a:{f}]" + cadeia.replace("{LAT}", f"{lat:.6f}").replace("{ST}", f"{st:.6f}")
                          + f"[a{f}]")
        grafo = ";".join(partes)
    else:
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
    try:
        rodar(cmd)
        if destino.is_file() and destino.stat().st_size:
            return destino
    except ErroFFmpeg:
        pass
    # t no fim do vídeo (ou depois do último quadro, comum em VFR): mostra o ÚLTIMO quadro
    # (-update 1 regrava a imagem a cada quadro do último segundo; fica o último)
    destino.unlink(missing_ok=True)
    rodar(FFMPEG + ["-sseof", "-1", "-i", str(arquivo), "-update", "1",
                    "-vf", f"scale='min({largura},iw)':-2", "-q:v", "3", str(destino)])
    if not (destino.is_file() and destino.stat().st_size):
        raise ErroFFmpeg(f"não há quadro de vídeo em {fmt_tempo(t)}")
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
        _validar_conteiner(nome, p, ext)
        p["ext"] = ext
        if not predefinicao:
            p["sufixo"] = ext.lstrip(".")  # formato="mkv" → video.mkv
    return nome, p


# codecs que cada contêiner aceita (None = qualquer um); usado para recusar combinações que o ffmpeg rejeitaria
_CONTEINER_CODECS: dict[str, set | None] = {
    ".mp4": {"h264", "h265", "vp9", "aac", "mp3", "opus"}, ".m4v": {"h264", "h265", "aac", "mp3"},
    ".mov": {"h264", "h265", "prores", "aac", "mp3", "pcm", "wav"}, ".mkv": None,
    ".webm": {"vp9", "opus"}, ".ts": {"h264", "h265", "aac", "mp3"}, ".avi": {"h264", "mp3", "pcm", "wav"},
}


def _validar_conteiner(nome: str, p: dict, ext: str) -> None:
    """Erro claro se a predefinição não cabe na extensão pedida (formato ou destino), em vez de deixar o
    ffmpeg falhar. Decisão: não troca a extensão sozinho; pede outra extensão ou outra predefinição."""
    ext = ext.lower()
    padrao = PREDEFINICOES[nome]["ext"]
    if ext == padrao:
        return
    if p["tipo"] == "imagem":
        ok = ext in (".jpg", ".jpeg") and padrao == ".jpg"
    elif p["tipo"] in ("audio", "gif"):
        ok = p["tipo"] == "audio" and ext == ".mka"
    else:
        aceitos = _CONTEINER_CODECS.get(ext, set())
        ok = ext in _CONTEINER_CODECS and (aceitos is None or all(
            p[k] in aceitos for k in ("video", "audio") if p.get(k)))
    if not ok:
        codecs = "+".join(str(p[k]) for k in ("video", "audio") if p.get(k)) or p["tipo"]
        raise ValueError(f"a predefinição '{nome}' gera {codecs} ({padrao}), que não cabe em '{ext}'; "
                         f"use a extensão {padrao} ou outra predefinição (ex.: webm para .webm, gif para .gif, "
                         f"mp3 para .mp3)")


def _ext_destino(destino) -> str | None:
    """Extensão do destino quando ele é um ARQUIVO (mesma regra de _caminho); None se for pasta."""
    if not destino:
        return None
    d = Path(str(destino)).expanduser()
    if d.is_dir() or str(destino).endswith("/") or not d.suffix:
        return None
    return d.suffix


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
    if _ext_destino(destino):
        _validar_conteiner(nome, p, _ext_destino(destino))
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
                  ent: str, sai: str, cor: str = "black") -> list[str]:
    """Filtros de vídeo da exportação, de [ent] para [sai] (pode ter várias partes).
    Também usada por `mudar_proporcao` (edição). `cor`: cor das barras no enquadramento 'barras'."""
    partes: list[str] = []
    f: list[str] = []
    fps_alvo = fps or p.get("fps")
    if not fps and p.get("fps_max") and fps_origem > p["fps_max"] + 0.01:
        fps_alvo = p["fps_max"]
    if fps_alvo and (fps or fps_origem == 0 or fps_alvo < fps_origem - 0.01):
        f.append(f"fps={fps_alvo:g}")
    tela = p.get("tela")
    if tela:
        L, A = _par_exp(largura or tela[0]), _par_exp(altura or tela[1])
        modo = (enquadramento or p.get("enquadramento") or "desfocado").lower()
        mesma = w and h and abs(w / h - L / A) < 0.02
        if mesma or modo == "recorte":
            f.append(f"scale={L}:{A}:force_original_aspect_ratio=increase,crop={L}:{A},setsar=1")
        elif modo == "barras":
            f.append(f"scale={L}:{A}:force_original_aspect_ratio=decrease,"
                     f"pad={L}:{A}:(ow-iw)/2:(oh-ih)/2:color={cor},setsar=1")
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
        # dimensões pares: yuv420p (H.264/H.265/VP9) não aceita ímpares
        if largura and altura:
            f.append(f"scale={_par_exp(largura)}:{_par_exp(altura)}:force_original_aspect_ratio=decrease:"
                     f"force_divisible_by=2,setsar=1")
        else:
            f.append(f"scale={_par_exp(largura) if largura else -2}:{_par_exp(altura) if altura else -2},setsar=1")
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


def _par_exp(v) -> int:
    """Arredonda uma dimensão pedida para o par mais próximo (mínimo 2)."""
    return max(2, int(round(float(v) / 2)) * 2)


try:  # o ffmpeg recebe SIGTERM se a thread que o criou morrer (fechar/travar a janela ou o MCP)
    import ctypes as _ctypes
    import signal as _signal
    _prctl = _ctypes.CDLL(None, use_errno=True).prctl
except (OSError, AttributeError):
    _prctl = None


def _morrer_com_pai() -> None:  # preexec_fn: roda no filho, entre fork e exec
    if _prctl is not None:
        _prctl(1, int(_signal.SIGTERM), 0, 0, 0)  # PR_SET_PDEATHSIG


def _tem_alfa(s: dict) -> bool:
    tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
    pf = s.get("pix_fmt") or ""
    return str(tags.get("alpha_mode", "")) == "1" or pf.startswith(("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya"))


def _dec_entrada(s: dict | None) -> list[str]:
    """Decodificador a forçar antes do -i: WebM VP8/VP9 com alfa só mantém a transparência no libvpx
    (o decodificador nativo descarta o canal alfa)."""
    if not s:
        return []
    tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
    if str(tags.get("alpha_mode", "")) != "1":
        return []
    dec = {"vp9": "libvpx-vp9", "vp8": "libvpx"}.get(s.get("codec_name"))
    lista = binarios._lista_ffmpeg("decoders")
    if not dec or (lista and dec not in lista):
        return []
    return ["-c:v", dec]


# temporário da exportação: .<nome final>.<8 aleatórios>.parcial.<ext>, ao lado do arquivo final
_RX_PARCIAL = re.compile(r"^\.(?P<final>.+)\.[a-z0-9_]{8}\.parcial(?P<ext>\.[A-Za-z0-9]+)$")
_ORFAO_S = 600  # temporário sem escrita há 10 min = órfão (os ativos são "tocados" a cada 30 s)


def _reservar(final: Path) -> Path:
    """Cria o arquivo final VAZIO com O_EXCL (reserva atômica do nome); se já existir, tenta o próximo."""
    while True:
        final = _livre(final)
        try:
            os.close(os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return final
        except FileExistsError:
            continue


def _temporario(final: Path) -> Path:
    fd, nome = tempfile.mkstemp(prefix=f".{final.name}.", suffix=f".parcial{final.suffix}", dir=final.parent)
    try:  # mkstemp cria com 0600; usa as permissões da reserva (0644 menos a umask), como um arquivo comum
        os.fchmod(fd, final.stat().st_mode & 0o777)
    finally:
        os.close(fd)
    return Path(nome)


def _soltar_reserva(final: Path) -> None:
    try:
        if final.exists() and final.stat().st_size == 0:
            final.unlink()
    except OSError:
        pass


def _limpar_orfaos(pasta: Path) -> list[str]:
    """Apaga temporários de exportações interrompidas (padrão estrito, sem escrita há 10 min) e a reserva vazia
    correspondente. Só na pasta de destino em uso."""
    import time as _time
    apagados = []
    try:
        itens = list(pasta.iterdir())
    except OSError:
        return apagados
    agora = _time.time()
    for f in itens:
        m = _RX_PARCIAL.match(f.name)
        if not m or not f.name.endswith(m.group("ext")) or not f.is_file():
            continue
        try:
            if agora - f.stat().st_mtime < _ORFAO_S:
                continue
            f.unlink()
            apagados.append(f.name)
            reserva = pasta / m.group("final")
            if reserva.suffix == m.group("ext") and reserva.is_file() and reserva.stat().st_size == 0 \
                    and agora - reserva.stat().st_mtime >= _ORFAO_S:
                reserva.unlink()
        except OSError:
            pass
    return apagados


def _rodar_com_progresso(cmd: list[str], dur: float, progresso=None, base: float = 0.0, peso: float = 1.0,
                         cancelar=None) -> None:
    """Roda o ffmpeg lendo `-progress pipe:1` e chama progresso(fração 0..1). O ffmpeg morre junto com a
    thread que o criou (PR_SET_PDEATHSIG); cancelar() → True o interrompe."""
    cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    with tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                                preexec_fn=_morrer_com_pai if _prctl is not None else None)
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


def _publicar(tmp: Path, final: Path) -> Path:
    """O temporário (já completo) substitui a reserva vazia do nome final."""
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise ErroFFmpeg("o ffmpeg terminou sem gerar o arquivo")
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
    ext_dest = _ext_destino(destino)
    if ext_dest:
        _validar_conteiner(nome, p, ext_dest)
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
    dec = _dec_entrada(vids[0]) if vids and tipo != "audio" else []
    if p.get("alfa") and vids and not _tem_alfa(vids[0]):
        avisos.append("a origem não tem transparência: o canal alfa sai todo opaco")
    import time as _time
    toque = [_time.monotonic()]

    def _prog(tmp: Path):
        """Repassa o progresso e mantém o temporário 'vivo' (mtime) para a limpeza de órfãos."""
        def f(x):
            if _time.monotonic() - toque[0] > 30:
                toque[0] = _time.monotonic()
                try:
                    os.utime(tmp)
                except OSError:
                    pass
            if progresso:
                progresso(x)
        return f

    # ---------------- foto de um quadro
    if tipo == "imagem":
        if tempo is None:
            tempo = "atual" if est.get("posicao") is not None else 0
        t = parse_tempo(tempo, est)
        if t < 0:
            raise ValueError(f"tempo negativo: {tempo}")
        fq = _fps_stream(vids[0])
        # atraso do vídeo em relação ao início do arquivo (ex.: MKV com vídeo em 0,021 s), como em
        # congelar_quadro: o quadro n é exibido em o + n/fps no tempo do player
        try:
            o = max(0.0, float(vids[0].get("start_time") or 0) - float(probe.get("format", {}).get("start_time") or 0))
        except ValueError:
            o = 0.0
        # dur_v = duração do stream de vídeo (contada a partir do seu primeiro quadro); MKV não informa a do
        # stream, e a do contêiner inclui o atraso
        try:
            dur_v = float(vids[0].get("duration") or 0) or max(0.0, dur - o)
        except ValueError:
            dur_v = max(0.0, dur - o)
        n = None
        ss = t
        no_fim = False
        if fq:
            # quadro exibido no instante t = o último que começa em ou antes de t. Com busca precisa o ffmpeg
            # entrega o primeiro quadro com pts >= ss; meio quadro antes garante exatamente o quadro n.
            n = max(0, int((t - o) * fq + 1e-6))
            ultimo = max(0, int(-(-(dur_v * fq - 1e-6) // 1)) - 1) if dur_v else None  # teto - 1
            if ultimo is not None and n >= ultimo:
                # 'fim' ou além: vale o último quadro (lido decodificando o final e ficando com o último)
                if t > o + dur_v + 1e-3:
                    avisos.append(f"{fmt_tempo(t)} passa do fim do vídeo ({fmt_tempo(o + dur_v)}): foto do último quadro")
                n, no_fim = ultimo, True
                t = min(t, o + ultimo / fq)
            ss = max(0.0, o + (n - 0.5) / fq)
        elif dur_v and t >= o + dur_v:
            no_fim = True
        final = _reservar(_caminho(doc.original, _sufixo(nome, p, t), p["ext"], destino, criar=True))
        _limpar_orfaos(final.parent)
        tmp = _temporario(final)
        if no_fim:
            cmd = FFMPEG + ["-sseof", f"-{max(1.0, 3 / fq) if fq else 1.0:.3f}"] + dec + ["-i", str(entrada),
                            "-map", "0:V:0", "-map_metadata", "-1"]
        else:
            cmd = FFMPEG + ["-ss", f"{ss:.6f}"] + dec + ["-i", str(entrada), "-map", "0:V:0", "-frames:v", "1",
                            "-map_metadata", "-1"]
        if largura or altura:
            cmd += ["-vf", f"scale={int(largura) if largura else -2}:{int(altura) if altura else -2}"]
        if p["ext"] == ".jpg":
            cmd += ["-q:v", "2"]
        cmd += ["-update", "1", str(tmp)]  # com -update, no fim fica gravado o último quadro decodificado
        ok = False
        try:
            rodar(cmd)
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise ValueError(f"não há quadro em {fmt_tempo(t)} (o vídeo tem {fmt_tempo(o + dur_v)})")
            final = _publicar(tmp, final)
            ok = True
        finally:
            tmp.unlink(missing_ok=True)
            if not ok:
                _soltar_reserva(final)
        if progresso:
            progresso(1.0)
        r = info_arquivo(final)
        r.pop("duracao", None)
        r.pop("duracao_fmt", None)
        r.get("video", {}).pop("fps", None)
        r = {**r, "predefinicao": nome, "tempo": round(t, 3), "tempo_fmt": fmt_tempo(t), "quadro": n,
             "origem": str(entrada), "comandos": [shlex.join(cmd)]}
        if avisos:
            r["avisos"] = avisos
        return r

    # ---------------- trechos
    lista = None
    if trechos:
        lista = [(parse_tempo(a, est), parse_tempo(b, est)) for a, b in trechos]
    elif inicio is not None or fim is not None:
        lista = [(parse_tempo(inicio if inicio is not None else 0, est),
                  parse_tempo(fim if fim is not None else "fim", est))]
    if lista is not None:
        limpos = []
        for i, (a, b) in enumerate(lista, 1):
            rot = f"trecho {i} ({fmt_tempo(a)} → {fmt_tempo(b)})"
            if b - a <= 0.001:
                raise ValueError(f"{rot}: o fim precisa ser depois do início")
            if dur and a >= dur - 0.001:
                raise ValueError(f"{rot} começa depois do fim do arquivo ({fmt_tempo(dur)})")
            if a < 0:
                avisos.append(f"{rot}: início negativo, começa em 0")
            if dur and b > dur + 0.001:
                avisos.append(f"{rot} foi cortado no fim do arquivo ({fmt_tempo(dur)})")
            limpos.append((max(0.0, a), min(dur, b) if dur else b))
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
        # µs arredondados para baixo (3 casas perdiam o quadro de um corte exato, ver _cmd_preciso); com vários
        # trechos o -t tem folga e o fim exato vem do trim/atrim de _segmentos_av
        segs = []
        for a, b in lista:
            a6, b6 = int(a * 1e6) / 1e6, int(b * 1e6) / 1e6
            segs.append({"a": a6, "b": b6})
            entradas += ["-ss", f"{a6:.6f}", "-t", f"{b6 - a6 + (_FOLGA_T if n > 1 else 0):.6f}"] \
                + dec + ["-i", str(entrada)]
    else:
        entradas += dec + ["-i", str(entrada)]
    w, h = _dimensoes(vids[0]) if vids else (0, 0)
    fps_origem = _fps_stream(vids[0]) if vids else 0.0

    def montar(com_audio: bool) -> tuple[list[str], list[str]]:
        """(argumentos de filtro/mapa, rótulos) para um passe; sem áudio no 1º passe."""
        grafo: list[str] = []
        ia = idx_a if com_audio else []
        if n > 1:
            # mesmos filtros do corte preciso: sincronia exata, vídeo com nº exato de quadros por trecho
            grafo += _segmentos_av(segs, [auds[k] for k in ia], _taxa_video(vids[0]) if tem_v else None,
                                   lambda i: f"[{i}:V:0]", faixas=ia,
                                   atraso=_atraso_video(probe, vids[0]) if tem_v else 0.0)
            v_ent, a_map = "vc", [f"[ao{j}]" for j in range(len(ia))]
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
        # margem para o contêiner e o erro do controle de taxa (x265 e libvpx erram um pouco mais para cima)
        total_kbits = alvo_mb * _MB * 8 / 1000 * (0.97 if p.get("video") in ("h265", "vp9") else 0.985)
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
    final = _reservar(_caminho(doc.original, _sufixo(nome, p), p["ext"], destino, criar=True))
    _limpar_orfaos(final.parent)
    tmp = _temporario(final)
    prog = _prog(tmp)
    meta = ["-map_metadata", "-1"] if tipo == "gif" else ["-map_metadata", "0"]
    if lista or tipo == "gif":
        meta += ["-map_chapters", "-1"]
    saida_extra = ["-movflags", "+faststart"] if p["ext"] in (".mp4", ".m4a", ".mov", ".m4v") else []
    if tipo == "gif":
        saida_extra = ["-loop", "0"]
    comandos: list[list[str]] = []
    logdir = None
    ok = False
    try:
        a_args = _args_audio(p, canais, kbps_a_forcado) if idx_a else ["-an"]
        if tipo == "audio":
            args, _ = montar(True)
            cmd = FFMPEG + entradas + args + ["-vn"] + a_args + meta + saida_extra + [str(tmp)]
            comandos.append(cmd)
            _rodar_com_progresso(cmd, dur_saida, prog, cancelar=cancelar)
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
            _rodar_com_progresso(cmd1, dur_saida, prog, 0.0, 0.5, cancelar)
            _rodar_com_progresso(cmd2, dur_saida, prog, 0.5, 0.5, cancelar)
        else:
            args, _ = montar(True)
            v_args = [] if tipo == "gif" else _args_video(p, crf, None, None, None, teto)
            cmd = FFMPEG + entradas + args + v_args + (a_args if tipo != "gif" else []) + meta + saida_extra + [str(tmp)]
            comandos.append(cmd)
            _rodar_com_progresso(cmd, dur_saida, prog, cancelar=cancelar)
        final = _publicar(tmp, final)
        ok = True
    finally:
        tmp.unlink(missing_ok=True)
        if not ok:
            _soltar_reserva(final)
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


# ---------------------------------------------------------------- imagem (Fase 3)
# Edições de imagem: cada uma cria uma VERSÃO (vNNN) do documento, como cortar/ajustar_audio.
# Regras comuns: o vídeo é re-encodado com qualidade alta (codec_video: x264 CRF 18 veryfast, ou VP9 em .webm)
# para aguentar edições sucessivas; dimensões sempre pares; o ffmpeg já aplica a rotação de metadado na
# entrada (autorotate), então coordenadas e tamanhos são os do vídeo COMO ELE É EXIBIDO.
# Edições que não mexem no tempo copiam áudio e legendas sem perda (tags preservadas). Velocidade/congelar
# mudam a linha do tempo: o áudio é re-encodado com as tags reaplicadas e as legendas são descartadas.

_CONTEINERES_EDICAO = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".ts"}


def _midia(entrada: Path) -> dict:
    """Informações do vídeo que as edições de imagem precisam (erro claro se for só áudio)."""
    probe = ffprobe(entrada)
    vids = [s for s in probe["streams"] if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    if not vids:
        raise ValueError(f"o arquivo não tem vídeo (só áudio?): {entrada.name}")
    v = vids[0]
    w, h = _dimensoes(v)
    # pixels não quadrados (anamórfico, ex. DVD 720x576 com SAR 64:45): normaliza para SAR 1 no início do
    # grafo, e todas as coordenadas passam a ser as de exibição (1024x576)
    pre = ""
    m = re.fullmatch(r"(\d+):(\d+)", str(v.get("sample_aspect_ratio") or ""))
    if m and int(m.group(1)) and int(m.group(2)) and m.group(1) != m.group(2):
        sar = int(m.group(1)) / int(m.group(2))
        w = _par(w * sar)  # (a rotação já foi aplicada em _dimensoes; o SAR vale para a largura armazenada)
        if _dimensoes(v) != (int(v.get("width") or 0), int(v.get("height") or 0)):
            w, h = _dimensoes(v)[0], _par(_dimensoes(v)[1] * sar)
        pre = f"scale={w}:{h}:flags=lanczos,setsar=1"
    fps = _fps_stream(v) or 30.0
    taxa = _taxa_video(v)
    try:
        atraso = float(v.get("start_time") or 0) - float(probe.get("format", {}).get("start_time") or 0)
    except ValueError:
        atraso = 0.0
    return {"probe": probe, "v": v, "auds": faixas_audio(probe), "w": w, "h": h, "fps": fps,
            "atraso_video": max(0.0, atraso), "pre": pre, "dec": _dec_entrada(v),
            "taxa": taxa, "dur": duracao(probe), "pix_fmt": v.get("pix_fmt") or "",
            "legendas": any(s.get("codec_type") == "subtitle" for s in probe["streams"]),
            "anexos": any(s.get("codec_type") == "attachment" for s in probe["streams"])}


def _com_pre(info: dict, rotulo: str) -> str:
    """'[rotulo]' seguido da normalização de SAR (se houver) — início de cadeia de vídeo de uma entrada."""
    return f"[{rotulo}]{info['pre']}," if info.get("pre") else f"[{rotulo}]"


def _aplicar_pre(grafo: list[str], info: dict) -> list[str]:
    """Insere a normalização de SAR antes do uso de [0:V:0] num grafo já montado."""
    if not info.get("pre"):
        return grafo
    return [f"[0:V:0]{info['pre']}[vsar]"] + [g.replace("[0:V:0]", "[vsar]") for g in grafo]


_PAR_FILTRO = "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"


def _10bits(pix_fmt: str) -> bool:
    return bool(re.search(r"p1[0-6]", pix_fmt or ""))


def _tags_cor(v: dict | None) -> list[str]:
    """Primárias/transferência/matriz/faixa de cor da origem (HDR continua marcado como HDR)."""
    if not v:
        return []
    out = []
    for campo, opc in (("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc"),
                       ("color_space", "-colorspace"), ("color_range", "-color_range")):
        val = v.get(campo)
        if val and val not in ("unknown", "reserved", "unspecified"):
            out += [opc, val]
    return out


def _avisos_profundidade(info: dict, filtros_8bits: str | None = None) -> list[str]:
    """Avisos para fontes de 10 bits: codec de saída e filtros que só trabalham em 8 bits."""
    if not _10bits(info.get("pix_fmt", "")):
        return []
    av = []
    if not _tem_encoder("libx265"):
        av.append("origem de 10 bits re-encodada em H.264 High 10 (sem libx265 no ffmpeg; pouco compatível)")
    else:
        av.append("origem de 10 bits: saída em H.265 (HEVC) 10 bits, mantendo as tags de cor")
    if filtros_8bits:
        av.append(f"o filtro {filtros_8bits} trabalha em 8 bits: em origem de 10 bits pode surgir 'banding' "
                  f"(degraus) em degradês suaves")
    return av


def _par(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def _args_video_edicao(ext: str, info: dict) -> list[str]:
    """Codec de vídeo das edições: alta qualidade + pix_fmt compatível + tags de cor da origem.
    Origem de 10 bits (ex. iPhone HDR em HEVC) ou HEVC → libx265 10/8 bits (H.264 High 10 quase não toca em
    lugar nenhum); sem libx265, x264 High 10. `info` precisa só de 'pix_fmt' (e opcionalmente 'v')."""
    v = info.get("v") or {}
    cor = _tags_cor(v)
    dez = _10bits(info.get("pix_fmt", ""))
    if ext == ".webm":
        return codec_video(ext) + ["-pix_fmt", "yuv420p"] + cor
    if (dez or v.get("codec_name") == "hevc") and _tem_encoder("libx265"):
        a = ["-c:v", "libx265", "-crf", "20", "-preset", "faster", "-x265-params", "log-level=error",
             "-pix_fmt", "yuv420p10le" if dez else "yuv420p"]
        if ext in (".mp4", ".mov", ".m4v"):
            a += ["-tag:v", "hvc1"]
        return a + cor
    return codec_video(ext) + ["-pix_fmt", "yuv420p10le" if dez else "yuv420p"] + cor


def esc_filtro(valor) -> str:
    """Escapa um valor de opção para o -filter_complex (passado direto ao ffmpeg, sem shell).
    Dois níveis, como manda a documentação do ffmpeg ("Notes on filtergraph escaping"):
      1) opção do filtro: entre aspas simples (dentro delas tudo é literal; ' vira '\\'' );
      2) grafo: \\ ' [ ] , ; ganham barra invertida.
    Serve para texto do drawtext (com expansion=none, então % é literal), caminhos de arquivo e expressões."""
    s = str(valor)
    n1 = "'" + s.replace("'", "'\\''") + "'"
    return re.sub(r"([\\'\[\],;])", r"\\\1", n1)


def _janela_tempo(inicio, fim, est: dict, dur: float) -> tuple[float | None, float | None, str]:
    """(a, b, ":enable=...") para limitar um filtro a [inicio, fim]; vazio se for o vídeo todo."""
    if inicio is None and fim is None:
        return None, None, ""
    a = max(0.0, parse_tempo(inicio, est)) if inicio is not None else 0.0
    b = parse_tempo(fim, est) if fim is not None else dur
    if b - a <= 0.001:
        raise ValueError(f"intervalo vazio: {fmt_tempo(a)} → {fmt_tempo(b)}")
    return a, b, ":enable=" + esc_filtro(f"between(t,{a:.3f},{b:.3f})")


def _desc_janela(a, b) -> str:
    return f" em {fmt_tempo(a)}→{fmt_tempo(b)}" if a is not None else ""


def _saida_edicao(doc: Documento) -> Path:
    ext = doc.atual().suffix.lower()
    return doc.proxima_saida(ext if ext in _CONTEINERES_EDICAO else ".mp4")


def _rodar_versao(doc: Documento, saida: Path, cmds: list[list[str]], durs: list[float], descricao: str,
                  progresso=None, extra: dict | None = None) -> dict:
    """Roda os comandos (com progresso dividido entre eles), registra a versão e devolve o dict da versão.
    Se algo falhar, apaga a saída parcial."""
    try:
        n = len(cmds)
        for i, (c, d) in enumerate(zip(cmds, durs)):
            _rodar_com_progresso(c, d, progresso, i / n, 1 / n)
        if not saida.exists() or saida.stat().st_size == 0:
            raise ErroFFmpeg(f"o ffmpeg terminou mas não gerou {saida.name}")
    except BaseException:
        saida.unlink(missing_ok=True)
        raise
    v = doc.registrar(saida, descricao, cmds)
    if extra:
        v = {**v, **extra}
    return v


def _editar_quadros(doc: Documento, montar, descricao_fn, progresso=None, entradas_extra=None) -> dict:
    """Edição que só mexe nos quadros (não na linha do tempo).
    montar(info) → (partes do grafo de [0:V:0] até [vo], descrição); áudio/legendas copiados sem perda."""
    entrada = doc.atual()
    info = _midia(entrada)
    grafo, desc = montar(info)
    grafo = _aplicar_pre(grafo, info)
    if info["w"] % 2 or info["h"] % 2:  # origem com dimensão ímpar: yuv420p/x264 exigem pares
        grafo[-1] = grafo[-1][: grafo[-1].rindex("[vo]")] + "[vimpar]"
        grafo.append(f"[vimpar]{_PAR_FILTRO}[vo]")
    saida = _saida_edicao(doc)
    cmd = FFMPEG + info["dec"] + ["-i", str(entrada)] + list(entradas_extra or [])
    cmd += ["-filter_complex", ";".join(grafo), "-map", "[vo]"]
    cmd += _mapas_copia(info, entrada, saida, "0")
    cmd += _args_video_edicao(saida.suffix, info) + _tags_faixa(info["v"], "s:v:0")
    cmd += ["-map_metadata", "0"] + _faststart(saida) + [str(saida)]
    extra = {"avisos": _avisos_profundidade(info, info.get("filtros_8bits"))}
    return _rodar_versao(doc, saida, [cmd], [info["dur"]], descricao_fn(desc) if descricao_fn else desc,
                         progresso, extra if extra["avisos"] else None)


def _faststart(saida: Path) -> list[str]:
    return ["-movflags", "+faststart"] if saida.suffix.lower() in (".mp4", ".mov", ".m4v") else []


def _mapas_copia(info: dict, entrada: Path, saida: Path, idx: str, audio: bool = True) -> list[str]:
    """-map/-c para copiar sem perda, da entrada `idx`, o áudio (se audio=True), as legendas e os anexos do MKV
    (fontes de legendas ASS). Se o contêiner mudou, o áudio é re-encodado e legendas/anexos ficam de fora."""
    mesmo = saida.suffix.lower() == entrada.suffix.lower()
    out = []
    if audio:
        out += ["-map", f"{idx}:a?"] + (["-c:a", "copy"] if mesmo else codec_audio(saida.suffix))
    if mesmo:
        out += ["-map", f"{idx}:s?", "-c:s", "copy"]
        if info.get("anexos") and saida.suffix.lower() in (".mkv", ".mka"):
            out += ["-map", f"{idx}:t?", "-c:t", "copy"]
    return out


# ---- geometria
_PROPORCOES_NOMEADAS = {"vertical": "9:16", "stories": "9:16", "reels": "9:16", "tiktok": "9:16",
                        "shorts": "9:16", "horizontal": "16:9", "widescreen": "16:9", "youtube": "16:9",
                        "quadrado": "1:1", "quadrada": "1:1", "retrato": "4:5", "feed": "4:5",
                        "paisagem": "16:9", "cinema": "21:9", "classico": "4:3", "clássico": "4:3"}


def parse_proporcao(valor) -> tuple[float, str]:
    """'9:16', '9/16', '16x9', '1.777', 'vertical', 'quadrado', 'retrato' (4:5)... → (largura/altura, rótulo)."""
    s = str(valor).strip().lower()
    s = _PROPORCOES_NOMEADAS.get(s, s)
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*[:/x×]\s*(\d+(?:[.,]\d+)?)", s)
    try:
        if m:
            a, b = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
            r, rot = a / b, f"{m.group(1)}:{m.group(2)}"
        else:
            r = float(s.replace(",", "."))
            rot = f"{r:g}:1"
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"proporção inválida: {valor} (use '9:16', '1:1', '4:5', '16:9'...)") from None
    if not 0.1 <= r <= 10:
        raise ValueError(f"proporção fora do razoável: {valor}")
    return r, rot


_POSICOES = {"centro": 0.5, "meio": 0.5, "centro_horizontal": 0.5, "esquerda": 0.0, "esq": 0.0, "topo": 0.0,
             "cima": 0.0, "acima": 0.0, "superior": 0.0, "direita": 1.0, "dir": 1.0, "base": 1.0, "baixo": 1.0,
             "inferior": 1.0, "embaixo": 1.0}


def _frac_posicao(posicao) -> float:
    if posicao is None:
        return 0.5
    if isinstance(posicao, (int, float)):
        return min(1.0, max(0.0, float(posicao)))
    s = str(posicao).strip().lower()
    if s in _POSICOES:
        return _POSICOES[s]
    try:
        return min(1.0, max(0.0, float(s.replace(",", "."))))
    except ValueError:
        raise ValueError("posicao: 'centro', 'esquerda'/'topo', 'direita'/'base' ou número de 0 a 1") from None


def _retangulo(ret, w: int, h: int, nome: str = "retangulo") -> tuple[int, int, int, int]:
    """[x, y, largura, altura] em pixels (do vídeo exibido) ou frações 0–1 (todos os valores ≤ 1) →
    inteiros pares dentro do quadro."""
    if isinstance(ret, dict):
        ret = [ret.get("x", 0), ret.get("y", 0), ret.get("largura", ret.get("w")), ret.get("altura", ret.get("h"))]
    try:
        x, y, rw, rh = (float(str(v).replace(",", ".")) for v in ret)
    except (TypeError, ValueError):
        raise ValueError(f"{nome}: use [x, y, largura, altura] em pixels ou frações 0–1") from None
    if all(0 <= v <= 1 for v in (x, y, rw, rh)):
        x, y, rw, rh = x * w, y * h, rw * w, rh * h
    if rw <= 0 or rh <= 0:
        raise ValueError(f"{nome}: largura e altura precisam ser positivas")
    x = int(min(max(0.0, x), w - 2)) // 2 * 2
    y = int(min(max(0.0, y), h - 2)) // 2 * 2
    rw = max(2, int(min(rw, w - x)) // 2 * 2)
    rh = max(2, int(min(rh, h - y)) // 2 * 2)
    return x, y, rw, rh


def _recorte_proporcao(r: float, w: int, h: int, posicao) -> tuple[int, int, int, int]:
    """Maior retângulo com proporção r dentro de w×h, posicionado por `posicao` (0 = esquerda/topo)."""
    f = _frac_posicao(posicao)
    if w / h > r:
        rh = h // 2 * 2
        rw = min(w // 2 * 2, _par(rh * r))
        x = int((w - rw) * f) // 2 * 2
        return x, 0, rw, rh
    rw = w // 2 * 2
    rh = min(h // 2 * 2, _par(rw / r))
    y = int((h - rh) * f) // 2 * 2
    return 0, y, rw, rh


def transformar_imagem(doc: Documento, recorte=None, proporcao=None, posicao=None, girar=None,
                       modo_giro: str = "expandir", cor: str = "black", espelhar: str | None = None,
                       largura: int | None = None, altura: int | None = None, escala: float | None = None,
                       progresso=None) -> dict:
    """Recorta, gira, espelha e/ou redimensiona, NESTA ORDEM, numa única versão.
    recorte=[x, y, largura, altura] (px ou frações 0–1) OU proporcao='9:16' (+ posicao) recorta;
    girar=graus no sentido horário (90/180/270 sem perda de área; outros ângulos: modo_giro
    'expandir' (tela cresce, cantos com `cor`), 'manter' (mesmo tamanho, cantos com `cor`) ou
    'recorte' (amplia para não sobrar canto)); espelhar='horizontal' | 'vertical' | 'ambos';
    largura/altura (uma só = mantém a proporção; as duas = cabe dentro) ou escala (0.5 = metade)."""
    import math

    if recorte is not None and proporcao is not None:
        raise ValueError("use recorte OU proporcao, não os dois")
    if escala is not None and (largura or altura):
        raise ValueError("use escala OU largura/altura")

    def montar(info):
        w, h = info["w"], info["h"]
        f: list[str] = []
        desc: list[str] = []
        if recorte is not None or proporcao is not None:
            if recorte is not None:
                x, y, cw, ch = _retangulo(recorte, w, h, "recorte")
                desc.append(f"recortou {cw}x{ch} em ({x},{y})")
            else:
                r, rot = parse_proporcao(proporcao)
                x, y, cw, ch = _recorte_proporcao(r, w, h, posicao)
                desc.append(f"recortou para {rot} ({cw}x{ch})")
            if (cw, ch) != (w, h):
                f.append(f"crop={cw}:{ch}:{x}:{y}")
            w, h = cw, ch
        if girar is not None and float(girar) % 360:
            g = float(girar) % 360
            if abs(g - 90) < 1e-6:
                f.append("transpose=clock")
                w, h = h, w
            elif abs(g - 270) < 1e-6:
                f.append("transpose=cclock")
                w, h = h, w
            elif abs(g - 180) < 1e-6:
                f.append("hflip,vflip")
            else:
                th = math.radians(g)
                c, s = abs(math.cos(th)), abs(math.sin(th))
                modo = (modo_giro or "expandir").lower()
                cor_f = esc_filtro(cor)
                if modo == "expandir":
                    ow, oh = _par(math.ceil(w * c + h * s)), _par(math.ceil(w * s + h * c))
                    f.append(f"rotate={th:.6f}:ow={ow}:oh={oh}:c={cor_f}")
                    w, h = ow, oh
                elif modo == "manter":
                    f.append(f"rotate={th:.6f}:c={cor_f}")
                elif modo == "recorte":
                    k = c + max(w / h, h / w) * s
                    f.append(f"scale={_par(math.ceil(w * k))}:{_par(math.ceil(h * k))}:flags=bicubic,"
                             f"rotate={th:.6f}:ow={w}:oh={h}")
                else:
                    raise ValueError("modo_giro: 'expandir', 'manter' ou 'recorte'")
            desc.append(f"girou {float(girar):g}°" + ("" if g in (90, 180, 270) else f" ({modo_giro})"))
        if espelhar:
            e = str(espelhar).lower()
            if e in ("horizontal", "h", "lados", "esquerda_direita"):
                f.append("hflip")
            elif e in ("vertical", "v", "cima_baixo", "de_cabeca_para_baixo"):
                f.append("vflip")
            elif e in ("ambos", "os_dois", "hv"):
                f.append("hflip,vflip")
            else:
                raise ValueError("espelhar: 'horizontal', 'vertical' ou 'ambos'")
            desc.append(f"espelhou ({e})")
        if escala is not None:
            if not 0.05 <= float(escala) <= 8:
                raise ValueError("escala entre 0.05 e 8 (0.5 = metade)")
            nw, nh = _par(w * float(escala)), _par(h * float(escala))
            f.append(f"scale={nw}:{nh}:flags=lanczos")
            w, h = nw, nh
            desc.append(f"redimensionou para {nw}x{nh}")
        elif largura or altura:
            if largura and altura:
                k = min(int(largura) / w, int(altura) / h)
            else:
                k = int(largura) / w if largura else int(altura) / h
            nw, nh = _par(w * k), _par(h * k)
            f.append(f"scale={nw}:{nh}:flags=lanczos")
            w, h = nw, nh
            desc.append(f"redimensionou para {nw}x{nh}")
        if not f:
            raise ValueError("nada a fazer: passe recorte, proporcao, girar, espelhar, largura/altura ou escala")
        if w % 2 or h % 2:
            f.append(_PAR_FILTRO)
        return [f"[0:V:0]{','.join(f)},setsar=1[vo]"], "; ".join(desc)

    return _editar_quadros(doc, montar, None, progresso)


def mudar_proporcao(doc: Documento, proporcao, modo: str = "desfocado", cor: str = "black",
                    largura: int | None = None, altura: int | None = None, posicao=None,
                    progresso=None) -> dict:
    """Muda a proporção da tela (16:9 ↔ 9:16 ↔ 1:1 ↔ 4:5...).
    modo 'desfocado': o vídeo inteiro por cima do próprio vídeo ampliado e desfocado (igual ao Instagram
    da exportação); 'barras': barras de `cor`; 'recorte': corta as sobras (posicao escolhe o lado).
    Tamanho: largura/altura se passados; senão o lado maior do vídeo vira o lado maior da nova tela
    (1280x720 → 720x1280); no 'recorte' sem tamanho, os pixels originais são mantidos (sem ampliar)."""
    r, rot = parse_proporcao(proporcao)
    modo = (modo or "desfocado").lower()
    if modo not in ("desfocado", "barras", "recorte"):
        raise ValueError("modo: 'desfocado', 'barras' ou 'recorte'")

    def montar(info):
        w, h = info["w"], info["h"]
        if abs(w / h - r) < 0.01:
            raise ValueError(f"o vídeo já está em {rot} ({w}x{h})")
        if largura and altura:
            L, A = _par(int(largura)), _par(int(altura))
        elif largura:
            L, A = _par(int(largura)), _par(int(largura) / r)
        elif altura:
            L, A = _par(int(altura) * r), _par(int(altura))
        elif modo == "recorte":
            x, y, cw, ch = _recorte_proporcao(r, w, h, posicao)
            return [f"[0:V:0]crop={cw}:{ch}:{x}:{y},setsar=1[vo]"], f"proporção {rot} por recorte ({cw}x{ch})"
        else:
            lado = max(w, h)
            L, A = (_par(lado), _par(lado / r)) if r >= 1 else (_par(lado * r), _par(lado))
        if modo == "recorte" and posicao is not None and _frac_posicao(posicao) != 0.5:
            x, y, cw, ch = _recorte_proporcao(r, w, h, posicao)
            g = [f"[0:V:0]crop={cw}:{ch}:{x}:{y},scale={L}:{A}:flags=lanczos,setsar=1[vo]"]
        else:
            p = {"tipo": "video", "tela": (L, A)}
            g = _cadeia_video(p, w, h, info["fps"], None, None, None, modo, "0:V:0", "vo", cor=esc_filtro(cor))
        return g, f"proporção {rot} ({modo}, {L}x{A})"

    return _editar_quadros(doc, montar, None, progresso)


# ---- cor
def ajustar_cor(doc: Documento, brilho: float | None = None, contraste: float | None = None,
                saturacao: float | None = None, gama: float | None = None, temperatura: float | None = None,
                inicio=None, fim=None, estado: dict | None = None, progresso=None) -> dict:
    """Ajustes de cor (filtro eq + colortemperature), opcionalmente só em [inicio, fim].
    brilho: -1..1 (0 = igual; 0.05–0.15 já é visível); contraste: multiplicador (1 = igual; 1.2 = +20%);
    saturacao: multiplicador (1 = igual; 0 = preto e branco; 1.3 = mais viva); gama: 1 = igual (>1 clareia
    os tons médios); temperatura: kelvin (6500 = neutro; menor = mais quente/alaranjado, maior = mais frio/azulado)."""
    est = dict(estado or {})

    def montar(info):
        est.setdefault("duracao", info["dur"])
        a, b, en = _janela_tempo(inicio, fim, est, info["dur"])
        f, desc = [], []
        eq = []
        if brilho:
            if not -1 <= float(brilho) <= 1:
                raise ValueError("brilho entre -1 e 1 (0 = sem mudança)")
            eq.append(f"brightness={float(brilho):g}")
            desc.append(f"brilho {float(brilho):+g}")
        if contraste is not None and float(contraste) != 1:
            if not 0 <= float(contraste) <= 3:
                raise ValueError("contraste entre 0 e 3 (1 = sem mudança)")
            eq.append(f"contrast={float(contraste):g}")
            desc.append(f"contraste ×{float(contraste):g}")
        if saturacao is not None and float(saturacao) != 1:
            if not 0 <= float(saturacao) <= 3:
                raise ValueError("saturacao entre 0 e 3 (1 = sem mudança, 0 = preto e branco)")
            eq.append(f"saturation={float(saturacao):g}")
            desc.append("preto e branco" if float(saturacao) == 0 else f"saturação ×{float(saturacao):g}")
        if gama is not None and float(gama) != 1:
            if not 0.1 <= float(gama) <= 10:
                raise ValueError("gama entre 0.1 e 10 (1 = sem mudança)")
            eq.append(f"gamma={float(gama):g}")
            desc.append(f"gama {float(gama):g}")
        if eq:
            f.append("eq=" + ":".join(eq) + en)
            info["filtros_8bits"] = "eq"  # eq só processa 8 bits (aviso de banding em origem 10 bits)
        if temperatura is not None and float(temperatura) != 6500:
            k = float(temperatura)
            if not 1000 <= k <= 40000:
                raise ValueError("temperatura em kelvin, de 1000 a 40000 (6500 = neutro)")
            if "colortemperature" in binarios._lista_ffmpeg("filters") or not binarios._lista_ffmpeg("filters"):
                f.append(f"colortemperature=temperature={k:g}{en}")
            else:  # aproximação: ganho nos canais vermelho/azul
                d = max(-0.3, min(0.3, (6500 - k) / 6500 * 0.4))
                f.append(f"colorbalance=rm={d:.3f}:bm={-d:.3f}{en}")
            desc.append(f"temperatura {k:g} K")
        if not f:
            raise ValueError("nenhum ajuste pedido (brilho, contraste, saturacao, gama ou temperatura)")
        return [f"[0:V:0]{','.join(f)}[vo]"], "cor: " + ", ".join(desc) + _desc_janela(a, b)

    return _editar_quadros(doc, montar, None, progresso)


# ---- estabilização
def estabilizar(doc: Documento, suavizacao: int = 15, bordas: str = "zoom", tremor: int = 5,
                metodo: str | None = None, progresso=None) -> dict:
    """Estabiliza a câmera. Com vidstab (2 passes: vidstabdetect analisa o movimento, vidstabtransform
    corrige); sem ele, o filtro `deshake` (1 passe, mais simples).
    suavizacao: nº de quadros usados para suavizar o movimento (maior = câmera mais "parada"; 15 ≈ 0,5 s a 30 fps);
    tremor: 1–10, quanto o vídeo treme (vidstab, padrão 5); bordas: 'zoom' (amplia o mínimo para esconder
    as bordas), 'preto' (bordas pretas) ou 'manter' (preenche com o quadro anterior)."""
    filtros = binarios._lista_ffmpeg("filters")
    tem_vidstab = not filtros or {"vidstabdetect", "vidstabtransform"} <= filtros
    metodo = (metodo or ("vidstab" if tem_vidstab else "deshake")).lower()
    if metodo == "vidstab" and not tem_vidstab:
        raise ValueError("o ffmpeg encontrado não tem vidstab; use metodo='deshake'")
    if bordas not in ("zoom", "preto", "manter"):
        raise ValueError("bordas: 'zoom', 'preto' ou 'manter'")
    suavizacao = max(1, min(int(suavizacao), 200))
    tremor = max(1, min(int(tremor), 10))
    entrada = doc.atual()
    info = _midia(entrada)
    saida = _saida_edicao(doc)
    ent_v = _com_pre(info, "0:V:0")  # [0:V:0] (+ normalização de SAR)

    def final(grafo: str) -> list[str]:
        c = FFMPEG + info["dec"] + ["-i", str(entrada), "-filter_complex", grafo, "-map", "[vo]"]
        c += _mapas_copia(info, entrada, saida, "0")
        c += _args_video_edicao(saida.suffix, info) + _tags_faixa(info["v"], "s:v:0")
        return c + ["-map_metadata", "0"] + _faststart(saida) + [str(saida)]

    par = "" if info["w"] % 2 == 0 and info["h"] % 2 == 0 else "," + _PAR_FILTRO
    if metodo == "deshake":
        if bordas == "zoom":
            # deshake procura deslocamentos de até 16 px (rx/ry padrão): corta essa margem e volta ao tamanho
            m = 16
            w, h = info["w"], info["h"]
            filtro = (f"deshake=edge=clamp,crop={w - 2 * m}:{h - 2 * m}:{m}:{m},"
                      f"scale={_par(w)}:{_par(h)}:flags=bicubic,setsar=1")
        else:
            filtro = "deshake=" + {"preto": "edge=blank", "manter": "edge=original"}[bordas]
        cmd = final(f"{ent_v}{filtro}{par}[vo]")
        desc = f"estabilizou (deshake, bordas {bordas})"
        av = _avisos_profundidade(info, "deshake")
        return _rodar_versao(doc, saida, [cmd], [info["dur"]], desc, progresso, {"avisos": av} if av else None)
    tmp = Path(tempfile.mkdtemp(prefix="vidstab-", dir=doc.pasta))
    try:
        trf = esc_filtro(tmp / "movimento.trf")
        cmd1 = FFMPEG + info["dec"] + ["-i", str(entrada), "-filter_complex",
                                       f"{ent_v}vidstabdetect=shakiness={tremor}:accuracy=15:result={trf}",
                                       "-an", "-f", "null", os.devnull]
        borda = {"zoom": "optzoom=1:crop=black", "preto": "optzoom=0:crop=black",
                 "manter": "optzoom=0:crop=keep"}[bordas]
        cmd2 = final(f"{ent_v}vidstabtransform=input={trf}:smoothing={suavizacao}:{borda}:interpol=bicubic,"
                     f"unsharp=5:5:0.8:3:3:0.4{par}[vo]")
        desc = f"estabilizou (vidstab, suavização {suavizacao}, bordas {bordas})"
        av = _avisos_profundidade(info)
        return _rodar_versao(doc, saida, [cmd1, cmd2], [info["dur"], info["dur"]], desc, progresso,
                             {"avisos": av} if av else None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- linha do tempo: velocidade e congelar
def _cadeia_atempo(f: float) -> str:
    """atempo encadeado: cada estágio entre 0.5 e 2 (melhor qualidade que um único fator grande)."""
    estagios = []
    while f > 2.0 + 1e-9:
        estagios.append(2.0)
        f /= 2.0
    while f < 0.5 - 1e-9:
        estagios.append(0.5)
        f /= 0.5
    estagios.append(f)
    return ",".join(f"atempo={e:.6g}" for e in estagios)


_LAYOUT_PADRAO = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}


def _layout(s: dict) -> str:
    """Layout de canais de uma faixa para aformat/anullsrc: o nome do ffprobe ('5.1(side)', 'stereo'...)
    ou, se vier vazio/'unknown'/'6 channels', o layout padrão para o nº de canais."""
    lay = str(s.get("channel_layout") or "").strip()
    if lay and lay != "unknown" and "channels" not in lay:
        return lay
    n = int(s.get("channels") or 2)
    return _LAYOUT_PADRAO.get(n, f"{n}c")


def _editar_linha_tempo(doc: Documento, segmentos: list[dict], descricao: str, progresso=None,
                        copiar_audio: bool = False, info: dict | None = None) -> dict:
    """Reconstrói o vídeo a partir de segmentos, cada um uma entrada própria (`-ss a -t d -i`, como no
    corte preciso; nada de split, que guardaria quadros na memória).
    segmento: {"a", "b" (None = até o fim), "fator" (velocidade; 1 = normal), "manter_tom", "silenciar"}
              ou {"congelar": t, "d": segundos}.
    Filtros e sincronia: `_segmentos_av` (o mesmo do corte preciso).
    copiar_audio=True: o áudio original (e legendas/anexos) é copiado inteiro (a duração total não muda)."""
    entrada = doc.atual()
    info = info or _midia(entrada)
    auds = [] if copiar_audio else info["auds"]
    saida = _saida_edicao(doc)
    dec = info["dec"]
    cmd = FFMPEG[:]
    segs, dur_saida = [], 0.0
    for sg in segmentos:
        if "congelar" in sg:
            t = sg["congelar"]
            # mesmo truque da foto de quadro: meio quadro antes pega exatamente o quadro exibido em t,
            # descontando o atraso do vídeo em relação ao início do arquivo (ex.: MKV com vídeo em 0,021 s)
            o = info["atraso_video"]
            n = max(0, int((t - o) * info["fps"] + 1e-6))
            ss = max(0.0, o + (n - 0.5) / info["fps"])
            cmd += ["-ss", f"{ss:.6f}", "-t", f"{2 / info['fps']:.6f}"] + dec + ["-i", str(entrada)]
            segs.append({"congelar": True, "d": sg["d"]})
            dur_saida += sg["d"]
            continue
        a, b = sg["a"], sg.get("b")
        # fim pelo trim/atrim `end` (ver _segmentos_av): o -t da entrada conta do 1º quadro de cada faixa
        cmd += ["-ss", f"{a:.6f}"] + (["-t", f"{b - a + _FOLGA_T:.6f}"] if b is not None else []) \
            + dec + ["-i", str(entrada)]
        segs.append(sg)
        dur_saida += ((b if b is not None else info["dur"]) - a) / float(sg.get("fator") or 1.0)
    n = len(segmentos)
    grafo = _segmentos_av(segs, auds, info["taxa"], lambda i: _com_pre(info, f"{i}:V:0"),
                          atraso=info["atraso_video"])
    par = "" if info["w"] % 2 == 0 and info["h"] % 2 == 0 else "," + _PAR_FILTRO
    grafo.append(f"[vc]fps={info['taxa']}{par}[vo]")  # volta a ter fps constante
    if copiar_audio:
        cmd += ["-i", str(entrada)]  # só para copiar áudio/legendas/anexos (não decodifica vídeo)
    cmd += ["-filter_complex", ";".join(grafo), "-map", "[vo]"]
    cmd += _args_video_edicao(saida.suffix, info) + _tags_faixa(info["v"], "s:v:0")
    extra: dict = {}
    if copiar_audio:
        cmd += _mapas_copia(info, entrada, saida, str(n))
    else:
        for k, s in enumerate(auds):
            cmd += ["-map", f"[ao{k}]"] + _tags_faixa(s, f"s:a:{k}")
        if auds:
            cmd += codec_audio(saida.suffix)
        # legendas não (o tempo mudou); anexos do MKV (fontes de legenda) sim
        if info.get("anexos") and saida.suffix.lower() in (".mkv", ".mka") \
                and saida.suffix.lower() == entrada.suffix.lower():
            cmd += ["-map", "0:t?", "-c:t", "copy"]
        if info["legendas"]:
            extra["avisos"] = ["as legendas embutidas foram removidas (a linha do tempo mudou)"]
    av = _avisos_profundidade(info)
    if av:
        extra["avisos"] = extra.get("avisos", []) + av
    cmd += ["-map_metadata", "0", "-map_chapters", "-1"] + _faststart(saida) + [str(saida)]
    return _rodar_versao(doc, saida, [cmd], [dur_saida], descricao, progresso, extra or None)


def velocidade_trecho(doc: Documento, fator: float, inicio=None, fim=None, manter_tom: bool = True,
                      silenciar: bool = False, estado: dict | None = None, progresso=None) -> dict:
    """Muda a velocidade só de [inicio, fim] (padrão: o vídeo todo). fator 0.5 = câmera lenta (metade),
    2 = o dobro; de 0.1 a 10. Áudio: atempo encadeado mantém o tom (manter_tom=False muda o tom junto,
    como fita); silenciar=True deixa o trecho alterado mudo. Quadros: duplicados/descartados para manter
    o fps original (sem interpolação)."""
    fator = float(fator)
    if not 0.1 <= fator <= 10:
        raise ValueError("fator entre 0.1 e 10 (0.5 = câmera lenta pela metade, 2 = dobro da velocidade)")
    if abs(fator - 1) < 1e-6:
        raise ValueError("fator 1 não muda nada")
    info = _midia(doc.atual())
    est = dict(estado or {})
    est.setdefault("duracao", info["dur"])
    a = max(0.0, parse_tempo(inicio, est)) if inicio is not None else 0.0
    b = min(info["dur"], parse_tempo(fim, est)) if fim is not None else info["dur"]
    if b - a < 0.05:
        raise ValueError(f"trecho curto demais: {fmt_tempo(a)} → {fmt_tempo(b)}")
    segs = []
    if a > 0.01:
        segs.append({"a": 0.0, "b": a})
    segs.append({"a": a, "b": b if info["dur"] - b > 0.01 else None, "fator": fator,
                 "manter_tom": manter_tom, "silenciar": silenciar})
    if info["dur"] - b > 0.01:
        segs.append({"a": b, "b": None})
    nome = "câmera lenta" if fator < 1 else "acelerou"
    desc = f"{nome} ×{fator:g} em {fmt_tempo(a)}→{fmt_tempo(b)}"
    if silenciar:
        desc += " (mudo)"
    elif not manter_tom:
        desc += " (tom alterado)"
    v = _editar_linha_tempo(doc, segs, desc, progresso, info=info)
    v["trecho_novo"] = [fmt_tempo(a), fmt_tempo(a + (b - a) / fator)]
    return v


def congelar_quadro(doc: Documento, tempo, duracao_s: float, modo: str = "inserir",
                    estado: dict | None = None, progresso=None) -> dict:
    """Congela o quadro exibido em `tempo` por `duracao_s` segundos.
    modo 'inserir': o vídeo pausa nesse quadro e depois continua (fica mais longo); o áudio fica em
    silêncio durante a pausa. modo 'substituir': o quadro parado cobre os próximos `duracao_s` segundos
    e o áudio continua normal por baixo (duração igual; áudio copiado sem perda)."""
    d = float(duracao_s)
    if not 0.04 <= d <= 600:
        raise ValueError("duracao_s entre 0.04 e 600 segundos")
    modo = (modo or "inserir").lower()
    if modo not in ("inserir", "substituir"):
        raise ValueError("modo: 'inserir' ou 'substituir'")
    info = _midia(doc.atual())
    est = dict(estado or {})
    est.setdefault("duracao", info["dur"])
    t = min(max(0.0, parse_tempo(tempo, est)), max(0.0, info["dur"] - 1 / info["fps"]))
    segs: list[dict] = []
    if t > 0.01:
        segs.append({"a": 0.0, "b": t})
    segs.append({"congelar": t, "d": d})
    if modo == "inserir":
        if info["dur"] - t > 0.01:
            segs.append({"a": t, "b": None})
        desc = f"congelou o quadro de {fmt_tempo(t)} por {d:g} s (inserido, áudio mudo)"
        return _editar_linha_tempo(doc, segs, desc, progresso, info=info)
    fim_c = t + d
    if fim_c >= info["dur"] - 0.01:
        d = segs[-1]["d"] = info["dur"] - t
    else:
        segs.append({"a": fim_c, "b": None})
    desc = f"congelou o quadro de {fmt_tempo(t)} por {d:g} s (por cima, áudio mantido)"
    return _editar_linha_tempo(doc, segs, desc, progresso, copiar_audio=True, info=info)


# ---- texto, marca d'água, borrar
_FONTE_PADRAO: str | None = None


@functools.cache
def _tem_opcao_filtro(filtro: str, opcao: str) -> bool:
    try:
        r = subprocess.run([FFMPEG[0], "-hide_banner", "-h", f"filter={filtro}"], capture_output=True,
                           text=True, timeout=15)
        return re.search(rf"^\s+{re.escape(opcao)}\s", r.stdout, re.M) is not None
    except (OSError, subprocess.TimeoutExpired):
        return False


def fonte_arquivo(nome: str | None = None) -> str | None:
    """Arquivo da fonte (fc-match); padrão DejaVu Sans. Aceita caminho direto de .ttf/.otf."""
    global _FONTE_PADRAO
    if nome and Path(nome).expanduser().is_file():
        return str(Path(nome).expanduser())
    if nome and ("/" in nome or re.search(r"\.(ttf|otf|ttc|pfb)$", nome, re.I)):
        return None  # caminho de arquivo que não existe
    if not nome and _FONTE_PADRAO:
        return _FONTE_PADRAO
    fc = ferramenta("fc-match")
    achado = None
    if fc:
        try:
            r = subprocess.run([fc, "-f", "%{family}\n%{file}", nome or "DejaVu Sans"], capture_output=True,
                               text=True, timeout=10)
            familia, _, arq = r.stdout.strip().partition("\n")
            # o fc-match sempre devolve ALGUMA fonte (substituta): confere se a família é a pedida
            pedida = re.sub(r"[\s_-]+", "", (nome or "DejaVu Sans").split(":")[0]).lower()
            familias = [re.sub(r"[\s_-]+", "", f).lower() for f in familia.split(",")]
            if r.returncode == 0 and arq and Path(arq).is_file() and any(
                    f == pedida or (pedida.startswith(f) and f) for f in familias):
                achado = arq
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not achado and not nome:
        for c in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
                  "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
            if Path(c).is_file():
                achado = c
                break
    if not nome:
        _FONTE_PADRAO = achado
    return achado


_ANCORAS = {
    "centro": ("c", "c"), "meio": ("c", "c"), "topo": ("c", "t"), "cima": ("c", "t"), "base": ("c", "b"),
    "baixo": ("c", "b"), "rodape": ("c", "b"), "rodapé": ("c", "b"),
    "esquerda": ("l", "c"), "direita": ("r", "c"),
    "sup_esq": ("l", "t"), "superior_esquerdo": ("l", "t"), "topo_esquerda": ("l", "t"),
    "sup_dir": ("r", "t"), "superior_direito": ("r", "t"), "topo_direita": ("r", "t"),
    "inf_esq": ("l", "b"), "inferior_esquerdo": ("l", "b"), "base_esquerda": ("l", "b"),
    "inf_dir": ("r", "b"), "inferior_direito": ("r", "b"), "base_direita": ("r", "b"),
}


def _posicao_xy(posicao, w: int, h: int, ow: str, oh: str, margem: int) -> tuple[str, str]:
    """Expressões x, y para colocar um objeto de tamanho (ow, oh) no quadro w×h.
    posicao: nome (topo, base, centro, sup_esq, inf_dir...) ou [x, y] em pixels ou frações 0–1."""
    if isinstance(posicao, (list, tuple)) or (isinstance(posicao, str) and re.fullmatch(r"\s*[\d.]+\s*[,;]\s*[\d.]+\s*", posicao)):
        px, py = (float(v) for v in (posicao if isinstance(posicao, (list, tuple)) else re.split(r"[,;]", posicao)))
        if px <= 1 and py <= 1:
            px, py = px * w, py * h
        return str(int(px)), str(int(py))
    p = str(posicao or "base").strip().lower().replace(" ", "_").replace("-", "_")
    if p not in _ANCORAS:
        raise ValueError(f"posicao desconhecida: {posicao}. Use {', '.join(sorted(_ANCORAS))} ou [x, y]")
    ax, ay = _ANCORAS[p]
    x = {"l": f"{margem}", "c": f"(W-{ow})/2", "r": f"W-{ow}-{margem}"}[ax]
    y = {"t": f"{margem}", "c": f"(H-{oh})/2", "b": f"H-{oh}-{margem}"}[ay]
    return x, y


def sobrepor_texto(doc: Documento, texto: str, posicao="base", tamanho=None, cor: str = "white",
                   caixa: bool = False, cor_caixa: str = "black@0.55", contorno: int | None = None,
                   cor_contorno: str = "black", fonte: str | None = None, inicio=None, fim=None,
                   estado: dict | None = None, progresso=None) -> dict:
    """Escreve `texto` por cima do vídeo (drawtext). Quebras de linha com \\n viram linhas.
    tamanho: pixels (ex. 48) ou fração da altura (ex. 0.06; padrão 1/15 da altura).
    cor/cor_caixa/cor_contorno: nome ou #RRGGBB, com @opacidade (ex. 'black@0.5').
    caixa=True: fundo atrás do texto; contorno: espessura do contorno em px (padrão 2 sem caixa, 0 com caixa)."""
    if not texto or not str(texto).strip():
        raise ValueError("texto vazio")
    # "\n" literal (barra + n, como vem às vezes de JSON escapado duas vezes) também vira quebra de linha
    texto = str(texto).replace("\r\n", "\n").replace("\\n", "\n")
    est = dict(estado or {})
    arq_fonte = fonte_arquivo(fonte)
    if fonte and not arq_fonte:
        raise ValueError(f"fonte não encontrada: {fonte}")

    def montar(info):
        w, h = info["w"], info["h"]
        est.setdefault("duracao", info["dur"])
        a, b, en = _janela_tempo(inicio, fim, est, info["dur"])
        if tamanho is None:
            px = max(10, round(h / 15))
            # cabe na largura: DejaVu Sans tem ~0,58 em de largura média por caractere
            maior = max(len(linha) for linha in texto.split("\n"))
            if maior * 0.58 * px > 0.92 * w:
                px = max(10, int(0.92 * w / (0.58 * maior)))
        else:
            t = float(tamanho)
            px = max(6, round(t * h)) if t < 1 else int(t)
        margem = max(8, round(h * 0.04))
        x, y = _posicao_xy(posicao, w, h, "text_w", "text_h", margem)
        borda = (0 if caixa else max(1, round(px / 24))) if contorno is None else int(contorno)
        op = [f"text={esc_filtro(texto)}", "expansion=none", f"fontsize={px}", f"fontcolor={esc_filtro(cor)}",
              f"x={esc_filtro(x)}", f"y={esc_filtro(y)}", "line_spacing=" + str(round(px * 0.2))]
        if arq_fonte:
            op.insert(1, f"fontfile={esc_filtro(arq_fonte)}")
        if "\n" in texto and _tem_opcao_filtro("drawtext", "text_align"):  # ffmpeg 6.1+
            ax = "c"
            if isinstance(posicao, str) and posicao.strip().lower().replace(" ", "_") in _ANCORAS:
                ax = _ANCORAS[posicao.strip().lower().replace(" ", "_")][0]
            op.append("text_align=" + {"l": "left", "c": "center", "r": "right"}[ax])
        if caixa:
            op += ["box=1", f"boxcolor={esc_filtro(cor_caixa)}", f"boxborderw={max(4, round(px * 0.3))}"]
        if borda:
            op += [f"borderw={borda}", f"bordercolor={esc_filtro(cor_contorno)}"]
        g = "drawtext=" + ":".join(op) + en
        resumo = texto if len(texto) <= 40 else texto[:37] + "..."
        return [f"[0:V:0]{g}[vo]"], f"texto “{resumo}” ({posicao}){_desc_janela(a, b)}"

    return _editar_quadros(doc, montar, None, progresso)


def marca_dagua(doc: Documento, imagem: str | Path, posicao="inf_dir", escala: float = 0.15,
                opacidade: float = 1.0, margem: float = 0.03, inicio=None, fim=None,
                estado: dict | None = None, progresso=None) -> dict:
    """Sobrepõe uma imagem (PNG com transparência, JPG...) ao vídeo.
    escala: largura da imagem como fração da largura do vídeo (0.15 = 15%) ou pixels (> 1);
    opacidade 0–1; margem: fração da altura do vídeo; posicao como em sobrepor_texto."""
    img = Path(str(imagem)).expanduser().resolve()
    if not img.is_file():
        raise FileNotFoundError(f"imagem não encontrada: {img}")
    if not 0 < float(opacidade) <= 1:
        raise ValueError("opacidade entre 0 e 1")
    est = dict(estado or {})

    def montar(info):
        w, h = info["w"], info["h"]
        est.setdefault("duracao", info["dur"])
        a, b, en = _janela_tempo(inicio, fim, est, info["dur"])
        e = float(escala)
        lw = _par(e * w) if e <= 1 else _par(e)
        if not 2 <= lw <= 4 * w:
            raise ValueError("escala inválida")
        m = max(0, round(float(margem) * h)) if float(margem) < 1 else int(margem)
        x, y = _posicao_xy(posicao, w, h, "w", "h", m)
        f = [f"[1:v]format=rgba,scale={lw}:-2:flags=lanczos"]
        if float(opacidade) < 1:
            f[0] += f",colorchannelmixer=aa={float(opacidade):g}"
        f[0] += "[marca]"
        f.append(f"[0:V:0][marca]overlay=x={esc_filtro(x)}:y={esc_filtro(y)}:eof_action=repeat:format=auto"
                 f"{en}[vo]")
        return f, f"marca d'água {img.name} ({posicao}, {lw}px, opacidade {float(opacidade):g}){_desc_janela(a, b)}"

    # imagem de um quadro só: o overlay a repete até o fim (eof_action=repeat).
    # '%' no nome faria o demuxer image2 tratar o caminho como sequência (logo%03d.png): usa uma cópia.
    if "%" not in img.name:
        return _editar_quadros(doc, montar, None, progresso, entradas_extra=["-i", str(img)])
    with tempfile.TemporaryDirectory(prefix="marca-") as tmp:
        copia = Path(tmp) / ("marca" + img.suffix.replace("%", ""))
        shutil.copyfile(img, copia)
        return _editar_quadros(doc, montar, None, progresso, entradas_extra=["-i", str(copia)])


def _interp_expr(pontos: list[tuple[float, float]]) -> str:
    """Expressão do ffmpeg (variável t) que interpola linearmente os pontos (tempo, valor);
    antes do primeiro e depois do último, fica parado."""
    pontos = sorted(pontos)
    if len(pontos) == 1:
        return f"{pontos[0][1]:.2f}"
    expr = f"{pontos[-1][1]:.2f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(pontos, pontos[1:]))):
        if t1 - t0 < 1e-6:
            continue
        seg = f"{v0:.2f}+({v1 - v0:.2f})*(t-{t0:.3f})/{t1 - t0:.3f}"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return f"if(lt(t,{pontos[0][0]:.3f}),{pontos[0][1]:.2f},{expr})"


def borrar_area(doc: Documento, retangulos=None, trajetoria=None, modo: str = "borrar",
                intensidade: float | None = None, inicio=None, fim=None, estado: dict | None = None,
                progresso=None) -> dict:
    """Borra ou pixela áreas retangulares (placa, rosto) em [inicio, fim] (padrão: o vídeo todo).
    retangulos: [x, y, largura, altura] ou lista deles (px ou frações 0–1), parados.
    trajetoria: área que se move: [{"tempo": t, "retangulo": [x, y, l, a]}, ...] (≥ 2 pontos);
    o centro é interpolado linearmente entre os pontos e o tamanho é o maior dos pontos.
    Antes do 1º/depois do último ponto a área fica parada (o intervalo padrão é do 1º ao último ponto).
    modo 'borrar' (gblur) ou 'pixelar' (blocos); intensidade: sigma do desfoque / tamanho do bloco em px
    (padrão proporcional à área)."""
    modo = (modo or "borrar").lower()
    if modo not in ("borrar", "pixelar"):
        raise ValueError("modo: 'borrar' ou 'pixelar'")
    if (retangulos is None) == (trajetoria is None):
        raise ValueError("passe retangulos (área parada) OU trajetoria (área que se move)")
    est = dict(estado or {})

    def efeito(cw, ch):
        if modo == "pixelar":
            bl = int(intensidade) if intensidade else max(6, min(cw, ch) // 8)
            bl = max(2, min(bl, cw, ch))
            return f"pixelize=width={bl}:height={bl}", bl
        sg = float(intensidade) if intensidade else max(6.0, min(cw, ch) / 6)
        return f"gblur=sigma={sg:g}:steps=3", sg

    def montar(info):
        w, h = info["w"], info["h"]
        est.setdefault("duracao", info["dur"])
        ini, fi = inicio, fim
        areas = []  # (crop_x_expr, crop_y_expr, cw, ch)
        if trajetoria is not None:
            if not isinstance(trajetoria, list) or len(trajetoria) < 2:
                raise ValueError("trajetoria precisa de pelo menos 2 pontos {tempo, retangulo}")
            pts = []
            for p in trajetoria:
                t = parse_tempo(p.get("tempo"), est)
                x, y, rw, rh = _retangulo(p.get("retangulo"), w, h, "trajetoria.retangulo")
                pts.append((t, x + rw / 2, y + rh / 2, rw, rh))
            cw = max(p[3] for p in pts) // 2 * 2
            ch = max(p[4] for p in pts) // 2 * 2
            ex = _interp_expr([(t, cx - cw / 2) for t, cx, _, _, _ in pts])
            ey = _interp_expr([(t, cy - ch / 2) for t, _, cy, _, _ in pts])
            # limites: a área inteira dentro do quadro
            ex = f"max(0,min({w - cw},{ex}))"
            ey = f"max(0,min({h - ch},{ey}))"
            areas.append((ex, ey, cw, ch))
            if ini is None and fim is None:
                ini, fi = min(p[0] for p in pts), max(p[0] for p in pts)
            desc_area = f"área móvel {cw}x{ch} ({len(pts)} pontos)"
        else:
            lista = retangulos
            if lista and not isinstance(lista[0], (list, tuple, dict)):
                lista = [lista]
            for r in lista:
                x, y, rw, rh = _retangulo(r, w, h, "retangulos")
                areas.append((str(x), str(y), rw, rh))
            desc_area = ", ".join(f"{a[2]}x{a[3]} em ({a[0]},{a[1]})" for a in areas)
        a, b, en = _janela_tempo(ini, fi, est, info["dur"])
        partes = []
        n = len(areas)
        rotulos = "".join(f"[s{i}]" for i in range(n))
        partes.append(f"[0:V:0]split={n + 1}[base]{rotulos}")
        atual = "base"
        valor = None
        for i, (ex, ey, cw, ch) in enumerate(areas):
            ef, valor = efeito(cw, ch)
            partes.append(f"[s{i}]crop=w={cw}:h={ch}:x={esc_filtro(ex)}:y={esc_filtro(ey)}:exact=1,{ef}[b{i}]")
            prox = "vo" if i == n - 1 else f"m{i}"
            partes.append(f"[{atual}][b{i}]overlay=x={esc_filtro(ex)}:y={esc_filtro(ey)}{en}[{prox}]")
            atual = prox
        nome = "pixelou" if modo == "pixelar" else "borrou"
        return partes, f"{nome} {desc_area}{_desc_janela(a, b)}"

    return _editar_quadros(doc, montar, None, progresso)


# ---------------------------------------------------------------- áudio (Fase 4)
# Edições de áudio práticas, só com o ffmpeg: cada uma cria uma VERSÃO (vNNN), como as demais.
# Faixas: índice 0 = A1. O vídeo e as faixas não tocadas são copiados sem perda (exceto o fade de vídeo,
# que re-encoda a imagem); a faixa editada é re-encodada com codec_audio(ext) e as tags reaplicadas.
# Medições (piso de ruído, voz/silêncio) vêm de janelas de 50 ms do astats (`niveis_audio`).

MODELOS_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ai-editor" / "modelos"
_INTENSIDADES = {"leve": 10.0, "media": 20.0, "média": 20.0, "medio": 20.0, "forte": 32.0}
# limitador depois de somar música e voz: teto 0,97 (≈ -0,26 dBFS), sem auto-nível (level=1, o padrão do
# ffmpeg 6.1, normaliza a saída para 0 dB) e com o atraso de lookahead compensado (latency=1)
_LIMITADOR = "alimiter=limit=0.97:level=0:latency=1"
_LOTE_CORTES = 8  # trechos por comando no corte preciso (cada trecho = uma entrada/decodificador aberto;
# 12 entradas 720p com threads automáticas ≈ 800 MB, 8 com -threads 2 ≈ 350 MB)


def _faixa_valida(faixa, n: int) -> int:
    """faixa 1..n (A1 = 1) → índice 0..n-1, com erro claro."""
    if n == 0:
        raise ValueError("o arquivo não tem áudio")
    f = int(faixa or 1)
    if not 1 <= f <= n:
        raise ValueError(f"faixa A{f} não existe (há {n} faixa(s) de áudio)")
    return f - 1


def _info_audio(entrada: Path) -> dict:
    """Como `_midia`, mas aceita arquivo só de áudio (v = None)."""
    probe = ffprobe(entrada)
    vids = [s for s in probe["streams"] if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    v = vids[0] if vids else None
    return {"probe": probe, "v": v, "auds": faixas_audio(probe), "dur": duracao(probe),
            "fps": (_fps_stream(v) or 30.0) if v else None,
            "legendas": any(s.get("codec_type") == "subtitle" for s in probe["streams"])}


def _extensao_faixa(info: dict, s: dict) -> tuple[float, float]:
    """(início, fim) de uma faixa na linha do tempo dos filtros (= tempo do arquivo; o ffmpeg desconta o
    start_time do contêiner). Áudio que começa em 0,478 s tem pts 0,478 no -filter_complex; uma faixa mais
    curta que o vídeo acaba antes da duração do contêiner. Sem duração própria (MKV/WebM), usa a tag DURATION
    ou o fim do contêiner."""
    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    fmt = info["probe"].get("format", {})
    st = max(0.0, (num(s.get("start_time")) or 0.0) - (num(fmt.get("start_time")) or 0.0))
    d = num(s.get("duration"))
    if d is None:
        tag = next((v for k, v in (s.get("tags") or {}).items() if k.upper() == "DURATION"), None)
        if tag:
            try:
                h, m, seg = str(tag).split(":")
                d = int(h) * 3600 + int(m) * 60 + float(seg)
            except ValueError:
                d = None
    fim = st + d if d else info["dur"]
    return st, min(max(fim, st), info["dur"]) if info["dur"] else fim


@functools.lru_cache(maxsize=16)
def latencia_filtro(filtro: str, sr: int) -> float:
    """Atraso (s) que um filtro de áudio introduz, medido uma vez por (filtro, taxa) com um clique sintético
    (pico de 0,9 em 0,5 s sobre ruído baixo). Medido no ffmpeg 6.1: afftdn 25 ms e anlmdn 8 ms (= r + p), em
    qualquer taxa e com qualquer nr/nf/s, também com `enable` (o atraso vale fora da janela). O filtro devolve
    o mesmo nº de amostras: sem compensar, o áudio fica atrasado e perde esse tanto do fim."""
    import array
    n0 = sr // 2
    src = f"aevalsrc=if(eq(n\\,{n0})\\,0.9\\,0)+0.001*(random(0)-0.5):s={sr}:d=1.5"
    r = subprocess.run([FFMPEG[0], "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", src,
                        "-af", filtro, "-ac", "1", "-f", "f32le", "-"], capture_output=True, timeout=60)
    x = array.array("f")
    x.frombytes(r.stdout[:len(r.stdout) // 4 * 4])
    if r.returncode != 0 or len(x) <= n0:
        return 0.0
    pico = max(range(len(x)), key=lambda i: abs(x[i]))
    # o filtro engoliu o clique (ex.: arnndn): sem medida confiável. Limite baixo de propósito: o afftdn com
    # nf ≥ -30 dB atenua o clique para ~0,09 (o ruído de fundo do teste fica em ~0,0005)
    if abs(x[pico]) < 0.02:
        return 0.0
    return max(0, pico - n0) / sr


def _niveis_fala(dbs: list[float]) -> tuple[float, float]:
    """(piso, fala) em dB a partir dos níveis em janelas. piso = p10 (≥ -90); fala = p80 só das janelas acima
    do piso + 10 dB (o p80 de tudo falha quando há menos de 20% de fala: 4 bipes em 12 s davam "fala" =
    ruído). Sem janelas suficientes acima do piso (< 3), usa o p95."""
    piso = max(-90.0, _percentil(dbs, 10))
    altos = [d for d in dbs if d > piso + 10]
    fala = _percentil(altos, 80) if len(altos) >= 3 else _percentil(dbs, 95)
    return piso, max(fala, piso)


def niveis_audio(arquivo: Path | str, faixa: int = 0, inicio: float | None = None, fim: float | None = None,
                 janela: float = 0.05) -> list[tuple[float, float]]:
    """Nível RMS (dBFS) em janelas de `janela` s da faixa (índice 0 = A1): [(t, db), ...].
    Silêncio digital vira -120 dB. Tempos absolutos no arquivo."""
    probe = ffprobe(arquivo)
    auds = faixas_audio(probe)
    k = _faixa_valida(faixa + 1, len(auds))
    sr = int(auds[k].get("sample_rate") or 48000)
    n = max(64, int(round(sr * janela)))
    cmd = [FFMPEG[0], "-hide_banner", "-nostdin", "-v", "error"]
    if inicio:
        cmd += ["-ss", f"{inicio:.3f}"]
    cmd += ["-i", str(arquivo)]
    if fim is not None:
        cmd += ["-t", f"{fim - (inicio or 0):.3f}"]
    cmd += ["-map", f"0:a:{k}", "-af",
            f"aformat=channel_layouts=mono,asetnsamples=n={n}:p=0,"
            "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
            "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ErroFFmpeg(f"medição de nível falhou: {r.stderr[-2000:]}")
    off, t, res = inicio or 0.0, None, []
    for linha in r.stdout.splitlines():
        m = re.search(r"pts_time:\s*(-?[\d.]+)", linha)
        if m:
            t = float(m.group(1)) + off
            continue
        if linha.startswith("lavfi.astats.Overall.RMS_level=") and t is not None:
            v = linha.split("=", 1)[1].strip()
            try:
                db = float(v)
            except ValueError:
                db = -120.0
            res.append((t, max(-120.0, db) if db == db else -120.0))
    return res


def _percentil(vals: list[float], p: float) -> float:
    s = sorted(vals)
    if not s:
        return -120.0
    return s[min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))]


def _media_db(vals: list[float]) -> float:
    import math
    if not vals:
        return -120.0
    pot = sum(10 ** (v / 10) for v in vals) / len(vals)
    return max(-120.0, 10 * math.log10(pot)) if pot > 0 else -120.0


def medir_ruido(niveis: list[tuple[float, float]], janela: float = 0.05) -> dict:
    """Estima o piso de ruído a partir dos níveis em janelas.
    Procura o trecho quieto mais longo (≥ 0,3 s, nível ≤ p10 + 3 dB) quando há contraste entre fala e
    pausa (fala − p10 ≥ 10 dB; fala como em `_niveis_fala`): é o ruído "puro", usado também como amostra do
    perfil do afftdn. Sem pausas (som contínuo), o piso é estimado pelo p10 (origem 'estimado')."""
    dbs = [d for _, d in niveis]
    p10 = _percentil(dbs, 10)
    fala = _niveis_fala(dbs)[1]
    res = {"p10_db": round(p10, 1), "fala_db": round(fala, 1), "amostra": None}
    if fala - p10 >= 10:
        lim = p10 + 3
        melhor, atual = None, None
        for t, d in niveis:
            if d <= lim:
                atual = [atual[0], t + janela] if atual else [t, t + janela]
                if not melhor or atual[1] - atual[0] > melhor[1] - melhor[0]:
                    melhor = list(atual)
            else:
                atual = None
        if melhor and melhor[1] - melhor[0] >= 0.3:
            a, b = melhor
            if b - a > 0.5:  # tira as bordas (transição fala → pausa)
                a, b = a + 0.1, b - 0.1
            if b - a > 2.0:
                c = (a + b) / 2
                a, b = c - 1.0, c + 1.0
            ruido = _media_db([d for t, d in niveis if a <= t < b])
            res.update({"ruido_db": round(ruido, 1), "origem": "medido", "amostra": (a, b)})
            return res
    res.update({"ruido_db": round(p10, 1), "origem": "estimado"})
    return res


def _intensidade_db(intensidade) -> float:
    if intensidade is None:
        return _INTENSIDADES["media"]
    if isinstance(intensidade, str):
        k = intensidade.strip().lower()
        if k in _INTENSIDADES:
            return _INTENSIDADES[k]
        try:
            intensidade = float(k.replace(",", ".").removesuffix("db").strip())
        except ValueError:
            raise ValueError("intensidade: 'leve', 'media', 'forte' ou dB de redução (3 a 60)") from None
    x = float(intensidade)
    if not 3 <= x <= 60:
        raise ValueError("intensidade em dB de redução entre 3 e 60 (leve=10, media=20, forte=32)")
    return x


def modelo_rnnoise() -> Path | None:
    """Primeiro modelo .rnnn em ~/.local/share/ai-editor/modelos/rnnoise (nada é baixado)."""
    pasta = MODELOS_DIR / "rnnoise"
    ms = sorted(pasta.glob("*.rnnn")) if pasta.is_dir() else []
    return ms[0] if ms else None


def _editar_audio(doc: Documento, grafo: list[str], saidas: dict[int, str], descricao: str,
                  progresso=None, entradas_extra: list[str] | None = None, novas: list[tuple[str, dict]] = (),
                  video: str | None = None, info: dict | None = None, extra: dict | None = None,
                  dur_saida: float | None = None) -> dict:
    """Monta e roda uma edição de áudio.
    grafo: partes do -filter_complex; saidas: {índice da faixa: rótulo} (faixas substituídas);
    novas: [(rótulo, {"title":..., "language":...})] faixas acrescentadas no fim;
    video: rótulo do vídeo re-encodado (None = vídeo copiado sem perda)."""
    entrada = doc.atual()
    info = info or _info_audio(entrada)
    auds = info["auds"]
    ext = entrada.suffix.lower()
    # .m4a é MP4: aceita várias faixas de áudio e continua .m4a
    if novas and ext in (".wav", ".mp3", ".flac", ".ogg", ".opus", ".aac") and not info["v"]:
        ext = ".mka"  # contêiner de áudio com uma faixa só: a nova faixa pede um que aceite várias
        extra = {**(extra or {}), "avisos": (extra or {}).get("avisos", []) +
                 ["o arquivo agora é .mka (o formato original só guarda uma faixa de áudio)"]}
    saida = doc.proxima_saida(ext)
    trocou_ext = saida.suffix.lower() != entrada.suffix.lower()
    cmd = FFMPEG + (_dec_entrada(info["v"]) if video else []) + ["-i", str(entrada)] + list(entradas_extra or [])
    cmd += ["-filter_complex", ";".join(grafo)]
    cmd += ["-map", f"[{video}]"] if video else ["-map", "0:V?"]
    for i in range(len(auds)):
        cmd += ["-map", f"[{saidas[i]}]" if i in saidas else f"0:a:{i}"]
    for rot, _ in novas:
        cmd += ["-map", f"[{rot}]"]
    if not trocou_ext:
        cmd += ["-map", "0:s?"]
    cmd += ["-c", "copy"]
    if video:
        cmd += _args_video_edicao(saida.suffix, {"pix_fmt": info["v"].get("pix_fmt") or ""})
        cmd += _tags_faixa(info["v"], "s:v:0")
    for i, s in enumerate(auds):
        if i in saidas:
            cmd += codec_audio(saida.suffix, i) + _tags_faixa(s, f"s:a:{i}")
    for j, (_, tags) in enumerate(novas):
        k = len(auds) + j
        cmd += codec_audio(saida.suffix, k)
        for chave, valor in tags.items():
            if valor:
                cmd += [f"-metadata:s:a:{k}", f"{chave}={valor}"]
    cmd += ["-map_metadata", "0"]
    if saida.suffix in (".mp4", ".mov", ".m4v", ".m4a"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(saida))
    return _rodar_versao(doc, saida, [cmd], [dur_saida or info["dur"]], descricao, progresso, extra)


# ---- redução de ruído
def reduzir_ruido(doc: Documento, metodo: str = "afftdn", intensidade="media", faixa: int = 1,
                  inicio=None, fim=None, piso_ruido_db: float | None = None, estado: dict | None = None,
                  progresso=None) -> dict:
    """Reduz ruído de fundo constante (chiado, ventilador, zumbido largo) de uma faixa.
    metodo 'afftdn' (padrão; FFT): mede o ruído sozinho — acha a pausa mais quieta da faixa, usa-a como
      amostra do perfil de ruído (o trecho é colocado antes do áudio só para o filtro aprender e depois é
      retirado) e ajusta o piso (nf) a partir do nível medido. Sem pausas, estima pelo nível mais baixo.
    metodo 'anlmdn' (médias não locais): mais suave, sem perfil; ~10× mais lento.
    metodo 'rnnoise' (arnndn, rede neural para voz): precisa de um modelo .rnnn em
      ~/.local/share/ai-editor/modelos/rnnoise/ (não é baixado automaticamente).
    metodo 'deepfilternet' (rede neural DeepFilterNet 3, melhor para voz com ruído variado: trânsito,
      teclado, eco, vento): baixado no primeiro uso (~36 MB, `deepfilter.py`); ~3× o tempo real por núcleo,
      blocos em paralelo. A intensidade vira o limite de atenuação (leve 12 dB, media 24 dB, forte = sem limite).
    intensidade: 'leve' (10 dB), 'media' (20 dB), 'forte' (32 dB) ou dB de redução (3–60).
    inicio/fim: aplica só nesse trecho (o resto fica igual). piso_ruido_db: força o nível do ruído (dBFS RMS).
    Sincronia: o atraso do filtro (afftdn 25 ms, anlmdn 8 ms; `latencia_filtro`) é compensado (silêncio desse
    tamanho no fim, corte do mesmo tanto no começo) e o início da faixa (start_time) é mantido."""
    metodo = (metodo or "afftdn").lower().replace("-", "").replace("_", "")
    metodo = {"dfn": "deepfilternet", "deepfilter": "deepfilternet", "ia": "deepfilternet"}.get(metodo, metodo)
    if metodo not in ("afftdn", "anlmdn", "rnnoise", "deepfilternet"):
        raise ValueError("metodo: 'afftdn' (padrão), 'anlmdn', 'rnnoise' ou 'deepfilternet'")
    if metodo == "deepfilternet":
        return _reduzir_ruido_dfn(doc, intensidade, faixa, inicio, fim, estado, progresso)
    nr = _intensidade_db(intensidade)
    entrada = doc.atual()
    info = _info_audio(entrada)
    k = _faixa_valida(faixa, len(info["auds"]))
    est = dict(estado or {})
    est.setdefault("duracao", info["dur"])
    a, b, _ = _janela_tempo(inicio, fim, est, info["dur"])
    s = info["auds"][k]
    sr = int(s.get("sample_rate") or 48000)
    extra_in: list[str] = []
    detalhes: dict = {"metodo": metodo, "faixa": f"A{k + 1}"}
    avisos: list[str] = []
    # Linha do tempo do filtro: começa em 0 no início da faixa (st, ex. 0,478 s), ou no início da amostra de
    # ruído, que o afftdn põe antes do áudio (desloc s). Os tempos do enable e o corte final andam junto.
    st, _ = _extensao_faixa(info, s)
    desloc = 0.0
    origem = f"[0:a:{k}]asetpts=PTS-STARTPTS"

    def enable(d):
        if a is None:
            return ""
        return ":enable=" + esc_filtro(f"between(t,{max(0.0, a + d):.3f},{max(0.0, b + d):.3f})")

    if metodo == "afftdn":
        niv = niveis_audio(entrada, k, a, b)
        med = medir_ruido(niv)
        if piso_ruido_db is not None:
            med.update({"ruido_db": float(piso_ruido_db), "origem": "informado", "amostra": None})
        ruido = med["ruido_db"]
        # o nf do afftdn fica ~18 dB acima do RMS de banda larga do ruído (calibrado com ruído rosa:
        # nesse ponto a redução passa a ser a pedida em nr, sem tocar no sinal acima do ruído)
        nf = min(-20.0, max(-80.0, ruido + 18))
        filtro = f"afftdn@dn=nr={nr:g}:nf={nf:.1f}"
        detalhes.update({"ruido_db": ruido, "origem_medicao": med["origem"], "reducao_db": nr,
                         "nf": round(nf, 1)})
        if ruido <= -100:
            avisos.append("a pausa mais quieta é silêncio digital: não há ruído de fundo para reduzir")
        amostra = med.get("amostra")
        if amostra and ruido > -100:
            ra, rb = amostra
            desloc = rb - ra
            extra_in = ["-ss", f"{ra:.3f}", "-t", f"{desloc:.3f}", "-i", str(entrada)]
            cmds = esc_filtro(f"0 afftdn@dn sn start;{desloc:.3f} afftdn@dn sn stop")
            # as duas entradas começam em 0: o concat NÃO desconta o pts inicial do 2º segmento (áudio em
            # 0,478 s ficaria em desloc + 0,478 e o atrim abaixo cortaria no lugar errado)
            origem = (f"[1:a:{k}]asetpts=PTS-STARTPTS[amo];[0:a:{k}]asetpts=PTS-STARTPTS[aor];"
                      f"[amo][aor]concat=n=2:v=0:a=1")
            meio = f"asendcmd=c={cmds},{filtro}{enable(desloc - st)}"
            detalhes["perfil_ruido"] = f"amostrado em {fmt_tempo(ra)}→{fmt_tempo(rb)}"
        else:
            meio = f"{filtro}{enable(-st)}"
        # o atraso do afftdn não depende de nr/nf: mede com parâmetros neutros (com nf alto o clique de teste
        # era atenuado e a medida falhava → 0 ms → áudio 25 ms atrasado em relação ao vídeo)
        lat = latencia_filtro("afftdn=nr=10:nf=-50", sr)
        desc = f"reduziu ruído A{k + 1} (afftdn, {nr:g} dB; ruído {ruido:.0f} dB {med['origem']})"
    elif metodo == "anlmdn":
        forca = min(1000.0, 10 ** (nr / 10 - 1))  # leve 1, media 10, forte ~160
        lat = latencia_filtro(f"anlmdn=s={forca:.4g}", sr)
        meio = f"anlmdn=s={forca:.4g}{enable(-st)}"
        detalhes["forca"] = round(forca, 3)
        desc = f"reduziu ruído A{k + 1} (anlmdn, força {forca:.3g})"
    else:
        modelo = modelo_rnnoise()
        if not modelo:
            raise ValueError("o método 'rnnoise' precisa de um modelo .rnnn em "
                             f"{MODELOS_DIR / 'rnnoise'}/ (ex.: os modelos do projeto "
                             "GregorR/rnnoise-models). Nada é baixado automaticamente; "
                             "use metodo='afftdn' (padrão) ou coloque o modelo nessa pasta.")
        mix = min(1.0, max(0.2, nr / 25))
        rnn = f"aresample=48000,arnndn=m={esc_filtro(modelo)}:mix={mix:.2f}"
        lat = latencia_filtro(f"{rnn},aresample={sr}", sr)
        meio = f"{rnn}{enable(-st)},aresample={sr}"
        detalhes.update({"modelo": modelo.name, "mix": round(mix, 2)})
        desc = f"reduziu ruído A{k + 1} (rnnoise {modelo.stem}, mix {mix:.2f})"
    desc += _desc_janela(a, b)
    # compensa o atraso: lat s de silêncio no fim (o filtro devolve o mesmo nº de amostras) e corta lat s do
    # começo; depois a faixa volta ao início original (st) — sem isso a amostra/concat a puxava para 0
    pad = f"apad=pad_dur={lat:.6f}," if lat > 0 else ""
    corte = f"atrim=start={desloc + lat:.6f}," if desloc + lat > 0 else ""
    cadeia = f"{origem},{pad}{meio},{corte}asetpts=PTS-STARTPTS+{st:.6f}/TB[ar]"
    detalhes["latencia_compensada_ms"] = round(lat * 1000, 1)
    extra = {"detalhes": detalhes}
    if avisos:
        extra["avisos"] = avisos
    return _editar_audio(doc, [cadeia], {k: "ar"}, desc, progresso, extra_in, info=info, extra=extra)


def _reduzir_ruido_dfn(doc: Documento, intensidade, faixa: int, inicio, fim, estado: dict | None,
                       progresso=None) -> dict:
    """reduzir_ruido com o DeepFilterNet (ver deepfilter.py). O trecho processado é trocado amostra por
    amostra; fora dele a faixa é copiada do original (concat sem refazer nada)."""
    from . import deepfilter
    if isinstance(intensidade, str) and intensidade.strip().lower() in ("leve", "media", "média", "medio", "forte"):
        aten = {"leve": 12.0, "forte": 100.0}.get(intensidade.strip().lower(), 24.0)
    else:
        aten = _intensidade_db(intensidade)
    entrada = doc.atual()
    info = _info_audio(entrada)
    k = _faixa_valida(faixa, len(info["auds"]))
    est = dict(estado or {})
    est.setdefault("duracao", info["dur"])
    a, b, _ = _janela_tempo(inicio, fim, est, info["dur"])
    s = info["auds"][k]
    sr = int(s.get("sample_rate") or 48000)
    canais = int(s.get("channels") or 2)
    st, fim_faixa = _extensao_faixa(info, s)
    # tempos relativos ao começo da faixa (é assim que o deep-filter e o asetpts=PTS-STARTPTS os veem)
    ra = max(0.0, (a - st)) if a is not None else 0.0
    rb = max(ra, min(b, fim_faixa) - st) if a is not None else None
    if rb is not None and rb - ra < 0.05:
        raise ValueError("o trecho pedido não tem áudio nessa faixa")
    baixou = binarios.localizar("deep-filter") is None
    p_ini = 0.02 if baixou else 0.0
    if baixou:
        if progresso:
            progresso(0.0)
        deepfilter.executavel(progresso=(lambda f: progresso(0.02 * f)) if progresso else None)
    doc._conferir_trava()
    doc.pasta.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".deepfilter-", dir=doc.pasta))
    try:
        raw, n = deepfilter.processar(entrada, k, ra, rb, canais, aten, tmp,
                                      progresso=(lambda f: progresso(p_ini + (0.85 - p_ini) * f))
                                      if progresso else None)
        extra_in = ["-f", "s16le", "-ar", str(deepfilter.SR), "-ac", str(canais), "-i", str(raw)]
        # o PCM cru entra com o layout padrão do ffmpeg para N canais; o mesmo channelmap do original o põe
        # no layout padrão da tabela (_LAYOUT_PADRAO), senão o concat recusa ("Channel layout change")
        lay = _filtro_layout({"channels": canais, "channel_layout": ""})
        meio = f"[1:a]{lay}aresample={sr},asetpts=PTS-STARTPTS"
        orig = f"[0:a:{k}]{_filtro_layout(s)}asetpts=PTS-STARTPTS"
        if rb is None:
            cadeia = f"{meio},asetpts=PTS-STARTPTS+{st:.6f}/TB[ar]"
            grafo = [cadeia]
        else:
            ia, ib = round(ra * sr), round(rb * sr)
            partes, rot = [], []
            tem_antes, tem_depois = ia > 0, rb < fim_faixa - st - 0.001
            n_orig = int(tem_antes) + int(tem_depois)
            if n_orig:
                partes.append(f"{orig}" + (f",asplit={n_orig}" if n_orig > 1 else "")
                              + "".join(f"[o{i}]" for i in range(n_orig)))
            i = 0
            if tem_antes:
                partes.append(f"[o{i}]atrim=end_sample={ia}[antes]")
                rot.append("[antes]")
                i += 1
            partes.append(f"{meio},apad,atrim=end_sample={ib - ia}[meio]")
            rot.append("[meio]")
            if tem_depois:
                partes.append(f"[o{i}]atrim=start_sample={ib},asetpts=PTS-STARTPTS[depois]")
                rot.append("[depois]")
            partes.append(f"{''.join(rot)}concat=n={len(rot)}:v=0:a=1,asetpts=PTS-STARTPTS+{st:.6f}/TB[ar]")
            grafo = partes
        desc = (f"reduziu ruído A{k + 1} (DeepFilterNet 3, "
                + ("sem limite de atenuação" if aten >= 100 else f"atenuação até {aten:g} dB") + ")"
                + _desc_janela(a, b))
        detalhes = {"metodo": "deepfilternet", "faixa": f"A{k + 1}", "atenuacao_max_db": aten,
                    "versao": deepfilter.VERSAO, "amostras_processadas_48k": n,
                    "latencia_compensada_ms": "exata (-D do deep-filter)"}
        extra = {"detalhes": detalhes}
        if baixou:
            extra["avisos"] = [f"DeepFilterNet {deepfilter.VERSAO} baixado agora (~36 MB, SHA-256 conferido) "
                               f"para {binarios.BIN_SOB_DEMANDA}"]
        prog2 = (lambda f: progresso(0.85 + 0.15 * f)) if progresso else None
        return _editar_audio(doc, grafo, {k: "ar"}, desc, prog2, extra_in, info=info, extra=extra)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- silêncios
def detectar_silencios(arquivo: Path | str, faixa: int = 0, limiar_db: float = -40, duracao_min: float = 0.5,
                       inicio: float | None = None, fim: float | None = None) -> list[tuple[float, float]]:
    """silencedetect na faixa (índice 0 = A1): [(de, até)] em segundos absolutos."""
    dur = duracao(ffprobe(arquivo))
    cmd = [FFMPEG[0], "-hide_banner", "-nostdin"]
    if inicio:
        cmd += ["-ss", f"{inicio:.3f}"]
    cmd += ["-i", str(arquivo)]
    if fim is not None:
        cmd += ["-t", f"{fim - (inicio or 0):.3f}"]
    cmd += ["-map", f"0:a:{faixa}", "-af", f"silencedetect=n={limiar_db:g}dB:d={duracao_min:g}",
            "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ErroFFmpeg(r.stderr[-3000:])
    off = inicio or 0.0
    fim_real = fim if fim is not None else dur
    res, ini = [], None
    for linha in r.stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", linha)
        if m:
            ini = max(0.0, float(m.group(1))) + off
        m = re.search(r"silence_end:\s*(-?[\d.]+)", linha)
        if m and ini is not None:
            res.append((ini, min(fim_real, float(m.group(1)) + off)))
            ini = None
    if ini is not None:
        res.append((ini, fim_real))
    return res


def _cortar_preciso(doc: Documento, manter: list[tuple[float, float]], descricao: str, progresso=None,
                    extra: dict | None = None) -> dict:
    """Mantém os trechos com o corte preciso (`_cmd_preciso`). Com muitos trechos, corta em lotes de
    `_LOTE_CORTES` (cada trecho abre um decodificador; todos de uma vez estourariam a memória) para
    partes .mkv (vídeo já no codec do contêiner final — VP9 para .webm — + áudio FLAC sem perda) e junta
    com o demuxer concat (vídeo copiado, áudio codificado uma vez só)."""
    if not manter:
        raise ValueError("nada sobraria depois do corte")
    entrada = doc.atual()
    probe = ffprobe(entrada)
    saida = doc.proxima_saida()
    total = sum(b - a for a, b in manter)
    _limpar_lotes_orfaos(doc.pasta)
    if len(manter) <= _LOTE_CORTES:
        cmd = _poucas_threads(_cmd_preciso(entrada, probe, manter, saida))
        return _rodar_versao(doc, saida, [cmd], [total], descricao, progresso, extra)
    auds = faixas_audio(probe)
    ca_mkv = codec_audio(".mkv")
    cv_mkv, cv_final = codec_video(".mkv"), codec_video(saida.suffix)
    with tempfile.TemporaryDirectory(dir=doc.pasta, prefix="lotes-") as tmp:
        cmds, durs, partes = [], [], []
        for i in range(0, len(manter), _LOTE_CORTES):
            lote = manter[i:i + _LOTE_CORTES]
            p = Path(tmp) / f"parte{i // _LOTE_CORTES:03d}.mkv"
            c = _poucas_threads(_cmd_preciso(entrada, probe, lote, p))
            _trocar_args(c, ca_mkv, ["-c:a", "flac"])  # áudio intermediário sem perda
            # vídeo da parte já no codec final: a junção copia o vídeo (x264 no .mkv não entra num .webm)
            _trocar_args(c, cv_mkv, cv_final)
            cmds.append(c)
            durs.append(sum(b - a for a, b in lote))
            partes.append(p)
        lista = Path(tmp) / "lista.txt"
        lista.write_text("".join(f"file {esc_concat(p)}\n" for p in partes))
        final = FFMPEG + ["-f", "concat", "-safe", "0", "-i", str(lista), "-i", str(entrada),
                          "-map", "0:v?", "-map", "0:a?", "-c:v", "copy"]
        for k, s in enumerate(auds):
            final += _tags_faixa(s, f"s:a:{k}")
        if auds:
            final += codec_audio(saida.suffix)
        final += ["-map_metadata", "1", "-map_chapters", "-1"]
        if saida.suffix in (".mp4", ".mov", ".m4v"):
            final += ["-movflags", "+faststart"]
        final.append(str(saida))
        # pesos de progresso: cortes ≈ 90%, junção ≈ 10%
        n = len(cmds)
        try:
            for i, (c, d) in enumerate(zip(cmds, durs)):
                _rodar_com_progresso(c, d, progresso, 0.9 * i / n, 0.9 / n)
            _rodar_com_progresso(final, total, progresso, 0.9, 0.1)
            if not saida.exists() or saida.stat().st_size == 0:
                raise ErroFFmpeg(f"o ffmpeg terminou mas não gerou {saida.name}")
        except BaseException:
            saida.unlink(missing_ok=True)
            raise
    v = doc.registrar(saida, descricao, cmds + [final])
    return {**v, **(extra or {})}


def _trocar_args(cmd: list[str], de: list[str], para: list[str]) -> bool:
    """Troca (no lugar) a 1ª ocorrência da sequência `de` por `para` na lista de argumentos."""
    if de == para:
        return True
    for j in range(len(cmd) - len(de) + 1):
        if cmd[j:j + len(de)] == de:
            cmd[j:j + len(de)] = para
            return True
    return False


def _limpar_lotes_orfaos(pasta: Path, prefixo: str = "lotes-") -> list[str]:
    """Apaga pastas `lotes-*` (partes do corte em lotes) esquecidas em `.versoes/` por um corte interrompido
    (processo morto antes de o TemporaryDirectory limpar): sem escrita há 10 min (`_ORFAO_S`), olhando a
    pasta e os arquivos dentro (um corte ativo grava as partes sem parar)."""
    import time as _time
    apagadas = []
    try:
        pastas = [d for d in pasta.iterdir() if d.name.startswith(prefixo) and d.is_dir()]
    except OSError:
        return apagadas
    agora = _time.time()
    for d in pastas:
        try:
            ultima = max([d.stat().st_mtime] + [f.stat().st_mtime for f in d.iterdir()])
        except OSError:
            continue
        if agora - ultima >= _ORFAO_S:
            shutil.rmtree(d, ignore_errors=True)
            apagadas.append(d.name)
    return apagadas


def _poucas_threads(cmd: list[str]) -> list[str]:
    """Limita cada decodificador de entrada (`-ss` só aparece nas entradas do _cmd_preciso) a 2 threads."""
    out = []
    for x in cmd:
        if x == "-ss":
            out += ["-threads", "2"]
        out.append(x)
    return out


def esc_concat(p: Path) -> str:
    """Caminho entre aspas simples para a lista do demuxer concat."""
    return "'" + str(p).replace("'", "'\\''") + "'"


def remover_silencios(doc: Documento, modo: str = "cortar", limiar_db: float | None = None,
                      duracao_min_s: float = 0.6, margem_s: float = 0.15, faixa: int = 1, inicio=None, fim=None,
                      estado: dict | None = None, progresso=None) -> dict:
    """Acha silêncios na faixa (silencedetect) e os corta do vídeo e de todas as faixas juntos.
    limiar_db: abaixo disso é silêncio (None = automático: entre o piso de ruído e o nível da fala).
    duracao_min_s: só pausas pelo menos desse tamanho. margem_s: deixa esse tanto de pausa antes e depois
    da fala (respiração, para não ficar "picotado"). modo 'analisar' só lista (não cria versão)."""
    modo = (modo or "cortar").lower()
    if modo not in ("cortar", "analisar"):
        raise ValueError("modo: 'cortar' ou 'analisar'")
    if not 0.1 <= float(duracao_min_s) <= 60:
        raise ValueError("duracao_min_s entre 0.1 e 60")
    if not 0 <= float(margem_s) <= 2:
        raise ValueError("margem_s entre 0 e 2")
    entrada = doc.atual()
    info = _info_audio(entrada)
    k = _faixa_valida(faixa, len(info["auds"]))
    dur = info["dur"]
    est = dict(estado or {})
    est.setdefault("duracao", dur)
    a, b, _ = _janela_tempo(inicio, fim, est, dur)
    origem_limiar = "informado"
    if limiar_db is None:
        niv = niveis_audio(entrada, k, a, b)
        dbs = [d for _, d in niv]
        p10, fala = _niveis_fala(dbs)
        limiar_db = min(-20.0, max(-60.0, p10 + max(6.0, 0.3 * (fala - p10))))
        origem_limiar = f"automático (piso {p10:.0f} dB, fala {fala:.0f} dB)"
    sil = detectar_silencios(entrada, k, limiar_db, duracao_min_s, a, b)
    ini_janela = a or 0.0
    fim_janela = b if b is not None else dur
    remover = []
    for s0, s1 in sil:
        r0 = s0 if s0 <= ini_janela + 0.001 and ini_janela == 0 else s0 + margem_s
        r1 = s1 if s1 >= dur - 0.001 else s1 - margem_s
        r0, r1 = max(ini_janela, r0), min(fim_janela, r1)
        if r1 - r0 >= 0.1:
            remover.append((r0, r1))
    if info["v"]:  # cortes no limite dos quadros: vídeo e áudio somam a mesma duração
        fps = info["fps"]
        # 0,4 ms antes do quadro: margem contra erro de ponto flutuante (o _cmd_preciso já escreve -ss em µs
        # arredondado para baixo; antes, com 3 casas, 8,16667 virava 8,167 e o quadro desse instante saía)
        remover = [(max(0.0, round(x * fps) / fps - 0.0004), round(y * fps) / fps - 0.0004) for x, y in remover]
        remover = [(x, min(y, dur)) for x, y in remover if y - x >= 0.05]
    total = sum(y - x for x, y in remover)
    res = {"faixa": f"A{k + 1}", "limiar_db": round(limiar_db, 1), "limiar": origem_limiar,
           "duracao_min_s": duracao_min_s, "margem_s": margem_s,
           "silencios": [{"de": fmt_tempo(x), "ate": fmt_tempo(y), "duracao_s": round(y - x, 3)} for x, y in sil],
           "a_remover": [[fmt_tempo(x), fmt_tempo(y)] for x, y in remover],
           "total_removido_s": round(total, 3), "duracao_atual": fmt_tempo(dur),
           "duracao_depois": fmt_tempo(dur - total)}
    if modo == "analisar":
        res["dica"] = ("para ver na régua: `marcar` com os tempos de a_remover; para cortar: modo='cortar' "
                       "(ou `remover_trechos` com a lista ajustada)")
        return res
    if not remover:
        dicas = []
        if limiar_db < -12:
            dicas.append(f"limiar_db mais alto, ex. {min(-10.0, limiar_db + 10):.0f} (hoje {limiar_db:.0f}; "
                         "mais alto = mais coisa conta como silêncio)")
        if duracao_min_s > 0.25:
            dicas.append(f"duracao_min_s menor, ex. {max(0.2, duracao_min_s / 2):g}")
        raise ValueError(f"nenhum silêncio ≥ {duracao_min_s:g} s abaixo de {limiar_db:.0f} dB "
                         f"(limiar {origem_limiar})" + (f"; tente {' ou '.join(dicas)}" if dicas else ""))
    manter = complemento(remover, dur)
    manter = [(x, y) for x, y in manter if y - x >= 0.02]
    if sum(y - x for x, y in manter) < 0.1:
        raise ValueError(f"a faixa A{k + 1} está toda abaixo de {limiar_db:.0f} dB (silêncio do começo ao fim, "
                         f"limiar {origem_limiar}): cortar os silêncios não deixaria nada. Se há fala baixa, "
                         "passe limiar_db menor (ex. -60) ou use modo='analisar'")
    desc = (f"removeu {len(remover)} silêncio(s) ({total:.1f} s; A{k + 1} < {limiar_db:.0f} dB, "
            f"≥ {duracao_min_s:g} s, margem {margem_s:g} s)")
    detalhes = {k2: res[k2] for k2 in ("limiar_db", "limiar", "total_removido_s", "a_remover")}
    v = _cortar_preciso(doc, manter, desc, progresso, {"detalhes": detalhes})
    if info["legendas"]:
        v["avisos"] = ["as legendas embutidas foram removidas (a linha do tempo mudou)"]
    return v


# ---- fades
def fade_audio(doc: Documento, entrada_s: float | None = None, saida_s: float | None = None,
               faixas: list[int] | None = None, video: bool = False, cor: str = "black",
               curva: str = "tri", progresso=None) -> dict:
    """Fade in no começo e/ou fade out no fim do áudio (faixas: [1], [1,2]...; padrão: todas).
    video=True escurece a imagem junto (para `cor`, padrão preto; re-encoda o vídeo).
    curva: 'tri' (linear), 'qsin' (suave), 'exp', 'log'... (nomes do afade)."""
    if not entrada_s and not saida_s:
        raise ValueError("passe entrada_s (fade in) e/ou saida_s (fade out), em segundos")
    entrada = doc.atual()
    info = _info_audio(entrada)
    dur = info["dur"]
    for d in (entrada_s, saida_s):
        if d and not 0.01 <= float(d) <= dur:
            raise ValueError(f"duração do fade entre 0.01 e {dur:.1f} s")
    curvas = {"tri", "qsin", "hsin", "esin", "log", "ipar", "qua", "cub", "squ", "cbr", "par", "exp",
              "iqsin", "ihsin", "dese", "desi", "losi", "sinc", "isinc", "nofade"}
    if curva not in curvas:
        raise ValueError(f"curva: uma de {', '.join(sorted(curvas))}")
    n = len(info["auds"])
    idx = [_faixa_valida(f, n) for f in faixas] if faixas else list(range(n))
    if video and not info["v"]:
        raise ValueError("o arquivo não tem vídeo (video=True só vale para vídeos)")
    if not idx and not video:
        raise ValueError("o arquivo não tem áudio; use video=True para escurecer a imagem")
    grafo, saidas = [], {}
    for i in idx:
        # cada faixa tem o próprio início e fim (áudio que começa em 0,478 s ou acaba antes do vídeo):
        # o afade usa o pts, então o fade vai para o começo/fim DA FAIXA, não do contêiner
        ini, fim_f = _extensao_faixa(info, info["auds"][i])
        ff = []
        if entrada_s:
            d = min(float(entrada_s), fim_f - ini)
            ff.append(f"afade=t=in:st={ini:.3f}:d={d:.3f}:curve={curva}")
        if saida_s:
            d = min(float(saida_s), fim_f - ini)
            ff.append(f"afade=t=out:st={fim_f - d:.3f}:d={d:.3f}:curve={curva}")
        grafo.append(f"[0:a:{i}]{','.join(ff)}[af{i}]")
        saidas[i] = f"af{i}"
    vrot = None
    if video:
        c = esc_filtro(cor)
        vi, vfim = _extensao_faixa(info, info["v"])
        vf = []
        if entrada_s:
            vf.append(f"fade=t=in:st={vi:.3f}:d={min(float(entrada_s), vfim - vi):.3f}:color={c}")
        if saida_s:
            d = min(float(saida_s), vfim - vi)
            vf.append(f"fade=t=out:st={vfim - d:.3f}:d={d:.3f}:color={c}")
        w, h = _dimensoes(info["v"])
        par = "" if w % 2 == 0 and h % 2 == 0 else "," + _PAR_FILTRO  # scale par + setsar=1
        grafo.append(f"[0:V:0]{','.join(vf)}{par}[vf]")
        vrot = "vf"
    partes = []
    if entrada_s:
        partes.append(f"fade in {float(entrada_s):g} s")
    if saida_s:
        partes.append(f"fade out {float(saida_s):g} s")
    alvo = ("A" + ",".join(str(i + 1) for i in idx)) if idx else ""
    if video:
        alvo = (alvo + " + vídeo" if alvo else "vídeo") + (f" ({cor})" if cor != "black" else "")
    return _editar_audio(doc, grafo, saidas, f"{' e '.join(partes)} em {alvo}", progresso, video=vrot,
                         info=info)


# ---- música de fundo
def musica_fundo(doc: Documento, arquivo: str | Path, modo: str = "mixar", faixa: int = 1,
                 volume_db: float | None = None, loop: bool = True, fade_in_s: float = 1.0,
                 fade_out_s: float = 2.0, inicio=None, fim=None, musica_inicio=None,
                 ducking: bool = False, ducking_db: float = 12.0, titulo: str | None = None,
                 estado: dict | None = None, progresso=None) -> dict:
    """Põe uma música (arquivo de áudio do usuário) no vídeo.
    modo 'mixar': soma a música à faixa `faixa` (amix normalize=0; a voz fica igual);
         'trocar': a música substitui a faixa `faixa`; 'nova_faixa': a música vira uma faixa nova (A1 fica).
         Com inicio/fim, 'trocar' silencia o original só dentro do trecho (rampa de 0,1 s nas bordas) e põe a
         música ali; fora dele o som original continua.
    volume_db: ganho da música (padrão -15 dB em mixar/nova_faixa, 0 em trocar). loop: repete a música se
    ela for menor que o trecho. fade_in_s/fade_out_s: fades da música. inicio/fim: onde a música toca no
    vídeo (padrão: o vídeo todo). musica_inicio: pula o começo da música (ex. '0:30') — só na 1ª volta: com
    loop, as voltas seguintes tocam a música INTEIRA desde o começo (como um player em repetição); feito com
    duas entradas (`-ss m0 -i` + `-stream_loop -1 -i`) e `concat`, sem combinar `-ss` com `-stream_loop`
    (isso corrompe os timestamps no ffmpeg 6.1) e sem guardar a música na memória (aloop).
    ducking: abaixa a música enquanto há voz na faixa `faixa` (sidechaincompress), em ~ducking_db dB.
    Em 'mixar'/'trocar' a música segue a extensão da faixa (voz que começa em 0,478 s ou acaba antes do
    vídeo) e a soma passa por um limitador (alimiter, teto -0,26 dBFS) para não estourar."""
    modo = (modo or "mixar").lower()
    if modo not in ("mixar", "trocar", "nova_faixa"):
        raise ValueError("modo: 'mixar', 'trocar' ou 'nova_faixa'")
    musica = Path(arquivo).expanduser().resolve()
    if not musica.is_file():
        raise FileNotFoundError(f"música não encontrada: {musica}")
    pm = ffprobe(musica)
    if not faixas_audio(pm):
        raise ValueError(f"o arquivo da música não tem áudio: {musica.name}")
    dur_m = duracao(pm)
    entrada = doc.atual()
    info = _info_audio(entrada)
    auds, dur = info["auds"], info["dur"]
    avisos: list[str] = []
    if not auds:
        if modo == "mixar":
            avisos.append("o vídeo não tinha áudio: a música entrou como faixa nova")
        modo, ducking = "nova_faixa", False
        k = None
        ref = {"sample_rate": 48000, "channels": 2}
    else:
        k = _faixa_valida(faixa, len(auds))
        ref = auds[k]
    if ducking and modo == "trocar":
        raise ValueError("ducking abaixa a música sob a voz; em modo 'trocar' a voz sai. "
                         "Use modo='mixar' ou 'nova_faixa'")
    if ducking and not 3 <= float(ducking_db) <= 40:
        raise ValueError("ducking_db entre 3 e 40 dB")
    if volume_db is None:
        volume_db = 0.0 if modo == "trocar" else -15.0
    est = dict(estado or {})
    est.setdefault("duracao", dur)
    a = max(0.0, parse_tempo(inicio, est)) if inicio is not None else 0.0
    b = min(dur, parse_tempo(fim, est)) if fim is not None else dur
    # mixar/trocar: a música vive na linha do tempo da faixa (pts de st_f a fim_f), senão a soma começaria
    # em st_f com a música de 0 e passaria do fim do vídeo
    st_f, fim_f = (0.0, dur) if modo == "nova_faixa" else _extensao_faixa(info, auds[k])
    a, b = max(a, st_f), min(b, fim_f)
    if b - a < 0.1:
        raise ValueError(f"trecho curto demais para a música: {fmt_tempo(a)} → {fmt_tempo(b)}"
                         + (f" (a faixa A{k + 1} vai de {fmt_tempo(st_f)} a {fmt_tempo(fim_f)})"
                            if modo != "nova_faixa" else ""))
    m0 = max(0.0, parse_tempo(musica_inicio, est)) if musica_inicio is not None else 0.0
    if m0 >= dur_m:
        raise ValueError(f"musica_inicio além do fim da música ({fmt_tempo(dur_m)})")
    toca = b - a
    if not loop and dur_m - m0 < toca:
        toca = dur_m - m0
        avisos.append(f"a música acaba em {fmt_tempo(a + toca)} (loop=False); depois fica sem música")
    sr = int(ref.get("sample_rate") or 48000)
    lay = _layout(ref) if auds else "stereo"
    # AAC de 6 canais vem com layout "unknown": renomeia para o padrão antes de somar (amix/aformat quebram)
    pre = _filtro_layout(ref) if auds else ""
    fi = min(float(fade_in_s or 0), toca / 2)
    fo = min(float(fade_out_s or 0), toca / 2)
    em_loop = bool(loop and dur_m - m0 < b - a)
    prep = f"aresample={sr},aformat=sample_rates={sr}:channel_layouts={lay},asetpts=PTS-STARTPTS"
    grafo = []
    if em_loop and m0:  # 1ª volta a partir de m0, depois a música inteira em loop
        entradas = ["-ss", f"{m0:.3f}", "-i", str(musica), "-stream_loop", "-1", "-i", str(musica)]
        grafo.append(f"[1:a:0]{prep}[m1];[2:a:0]{prep}[m2]")
        mc = ["[m1][m2]concat=n=2:v=0:a=1"]
    else:
        entradas = (["-stream_loop", "-1"] if em_loop else []) + (["-ss", f"{m0:.3f}"] if m0 else [])
        entradas += ["-i", str(musica)]
        mc = [f"[1:a:0]{prep}"]
    # pts pela contagem de amostras: as emendas do loop (e do concat) ficam contínuas e exatas
    mc += ["asetpts=N/SR/TB", f"volume={float(volume_db):g}dB", f"atrim=duration={toca:.6f}"]
    if fi > 0:
        mc.append(f"afade=t=in:st=0:d={fi:.3f}")
    if fo > 0:
        mc.append(f"afade=t=out:st={toca - fo:.3f}:d={fo:.3f}")
    if a > 0:
        mc.append(f"adelay=delays={int(round(a * 1000))}:all=1")
    # pts aqui = tempo do vídeo a partir de 0; recorta a extensão da faixa (de st_f a fim_f, pts mantidos)
    mc.append(f"apad=whole_dur={fim_f:.6f},atrim=start={st_f:.6f}:end={fim_f:.6f}")
    grafo.append(",".join(mc) + "[mus]")
    detalhes: dict = {"modo": modo, "musica": musica.name, "duracao_musica": fmt_tempo(dur_m),
                      "volume_db": float(volume_db), "toca": f"{fmt_tempo(a)}→{fmt_tempo(a + toca)}",
                      "loop": em_loop}
    if em_loop and m0:
        detalhes["loop_desde"] = "1ª volta a partir de musica_inicio; as seguintes, a música inteira"
    rot_mus = "mus"
    if ducking:
        # limiar adaptado à voz: entre o piso e o nível da fala; razão calculada para a redução pedida
        niv = niveis_audio(entrada, k)
        dbs = [d for _, d in niv]
        piso, fala = _niveis_fala(dbs)
        lim = (piso + fala) / 2 if fala - piso >= 12 else fala - 10
        alvo = ducking_db * 1.2  # o detector RMS do compressor mede ~1,2× menos excesso que as janelas de 50 ms
        lim = min(lim, fala - alvo / 0.9)
        lim = max(lim, -60.0)
        excesso = fala - lim
        razao = 20.0 if alvo >= 0.9 * excesso else 1 / (1 - alvo / excesso)
        razao = min(20.0, max(1.5, razao))
        thr = 10 ** (lim / 20)
        voz_sc = f"[0:a:{k}]{pre}"
        if modo == "mixar":
            grafo.append(f"[0:a:{k}]{pre}asplit=2[voz][sc0]")
            voz_sc = "[sc0]"
        grafo.append(f"{voz_sc}highpass=f=120,lowpass=f=6000,aresample={sr},"
                     f"aformat=sample_rates={sr}:channel_layouts={lay},apad[sc]")  # apad: a música não acaba antes
        grafo.append(f"[mus][sc]sidechaincompress=threshold={thr:.6f}:ratio={razao:.2f}:attack=20:"
                     f"release=350:knee=2:makeup=1[musd]")
        rot_mus = "musd"
        detalhes["ducking"] = {"reducao_db": ducking_db, "limiar_db": round(lim, 1), "razao": round(razao, 2),
                               "voz_db": round(fala, 1), "piso_db": round(piso, 1)}
    saidas, novas = {}, []
    if modo == "mixar":
        if not ducking:
            grafo.append(f"[0:a:{k}]{pre}anull[voz]")
        voz = "[voz]"
        # duration=longest (a música já tem a duração exata): com duration=first e o vídeo copiado, o ffmpeg 6.1
        # perdia os últimos ~85 ms do áudio
        grafo.append(f"{voz}[{rot_mus}]amix=inputs=2:normalize=0:duration=longest,{_LIMITADOR}[mix]")
        saidas[k] = "mix"
        desc = f"música '{musica.name}' mixada em A{k + 1}"
    elif modo == "trocar":
        # o original some só dentro de [a, b] (rampa de 0,1 s nas bordas que ficam dentro da faixa)
        r = 0.1
        lados = ([f"{a + r:.3f}-t"] if a > st_f + 0.01 else []) + ([f"t-{b - r:.3f}"] if b < fim_f - 0.01 else [])
        if lados:
            g = lados[0] if len(lados) == 1 else f"max({lados[0]},{lados[1]})"
            ganho = esc_filtro(f"clip(({g})/{r},0,1)")
            grafo.append(f"[0:a:{k}]{pre}volume={ganho}:eval=frame[orig]")
            grafo.append(f"[orig][{rot_mus}]amix=inputs=2:normalize=0:duration=longest,{_LIMITADOR}[troca]")
            saidas[k] = "troca"
        else:
            saidas[k] = rot_mus
        desc = f"trocou A{k + 1} pela música '{musica.name}'"
    else:
        novas.append((rot_mus, {"title": titulo or f"Música ({musica.stem})"}))
        desc = f"música '{musica.name}' como nova faixa A{len(auds) + 1}"
    desc += f" ({float(volume_db):+g} dB"
    if detalhes["loop"]:
        desc += ", em loop"
    if ducking:
        desc += f", ducking −{ducking_db:g} dB"
    desc += ")" + (f" em {fmt_tempo(a)}→{fmt_tempo(b)}" if a > 0 or b < dur - 0.01 else "")
    extra = {"detalhes": detalhes}
    if avisos:
        extra["avisos"] = avisos
    return _editar_audio(doc, grafo, saidas, desc, progresso, entradas, novas=novas, info=info, extra=extra)


# ---------------------------------------------------------------- montagem (Fase 5)
# Juntar arquivos (versão), dividir em partes (ARQUIVOS NOVOS, como exportar), detectar cortes de cena,
# capítulos (versão sem reencodar) e relatório técnico. Reaproveita _midia/_args_video_edicao/_rodar_versao
# (imagem), _layout (linha do tempo), _cmd_preciso (cortes) e _reservar/_temporario/_publicar (exportação).

_TRANSICOES = {None: "corte", "": "corte", "corte": "corte", "nenhuma": "corte", "seco": "corte",
               "fade": "fade", "preto": "fade", "fade_preto": "fade",
               "crossfade": "crossfade", "dissolver": "crossfade", "dissolve": "crossfade", "mistura": "crossfade"}
_EXT_AUDIO = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".mka", ".aac"}


def _fonte(p: Path) -> dict:
    """Informações de um arquivo para a montagem: as de _midia (se tiver vídeo) ou só as do áudio."""
    probe = ffprobe(p)
    vids = [s for s in probe["streams"] if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    if vids:
        info = _midia(p)
    else:
        info = {"probe": probe, "v": None, "auds": faixas_audio(probe), "dur": duracao(probe), "pre": "",
                "dec": [], "w": 0, "h": 0, "fps": 0.0, "taxa": "", "pix_fmt": "", "legendas": False,
                "anexos": False, "atraso_video": 0.0}
    info["caminho"] = p
    return info


def _esc_ffmeta(s) -> str:
    """Escape de chave/valor no formato FFMETADATA: = ; # \\ e quebra de linha ganham barra invertida."""
    s = str(s).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


def _limpar_capitulos(caps: list[tuple[float, str]], dur: float) -> list[tuple[float, str]]:
    """Ordena, descarta os fora do arquivo e junta capítulos a menos de 1 ms um do outro (fica o último título)."""
    out: list[tuple[float, str]] = []
    for t, titulo in sorted(((max(0.0, float(t)), str(titulo or "")) for t, titulo in caps), key=lambda x: x[0]):
        if dur and t >= dur - 0.001:
            continue
        if out and t - out[-1][0] < 0.001:
            out[-1] = (out[-1][0], titulo or out[-1][1])
        else:
            out.append((t, titulo))
    return out


def _texto_ffmeta(caps: list[tuple[float, str]], dur: float) -> str:
    """Arquivo FFMETADATA só com capítulos (TIMEBASE 1/1000; o fim de cada um é o início do próximo)."""
    linhas = [";FFMETADATA1"]
    for i, (t, titulo) in enumerate(caps):
        fim = caps[i + 1][0] if i + 1 < len(caps) else dur
        linhas += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(t * 1000))}",
                   f"END={max(int(round(fim * 1000)), int(round(t * 1000)) + 1)}",
                   f"title={_esc_ffmeta(titulo)}"]
    return "\n".join(linhas) + "\n"


def ler_capitulos(arquivo: Path | str, probe: dict | None = None) -> list[dict]:
    """Capítulos do arquivo (ffprobe -show_chapters): n (1 = o primeiro), inicio, fim, titulo."""
    probe = probe or ffprobe(arquivo)
    caps = []
    for i, c in enumerate(probe.get("chapters", []), 1):
        tags = {k.lower(): v for k, v in (c.get("tags") or {}).items()}
        a, b = float(c.get("start_time") or 0), float(c.get("end_time") or 0)
        caps.append({"n": i, "inicio": round(a, 3), "fim": round(b, 3), "inicio_fmt": fmt_tempo(a),
                     "fim_fmt": fmt_tempo(b), "titulo": tags.get("title", "")})
    return caps


def _alinhar(t: float, fps: float, atraso: float = 0.0) -> float:
    """Tempo no início do quadro mais próximo, na grade do vídeo (quadro k em atraso + k/fps; MKV costuma ter
    atraso 0,021 s). Sem fps, o próprio tempo."""
    if not fps:
        return t
    return max(0.0, atraso + round((t - atraso) * fps) / fps)


_MARGEM_SEEK = 0.0005  # s antes do início do quadro no -ss: tempos guardados em ms (MKV) não perdem o quadro


# ---- juntar
_LOTE_JUNTAR = 6  # entradas por comando na junção (cada uma abre um decodificador; mais que isso vai em lotes)
_HDR_TRC_JUNTAR = ("smpte2084", "arib-std-b67")
# HDR (PQ/HLG) → SDR BT.709: linear, primárias BT.709, tonemap hable
_TONEMAP_SDR = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv")


def _e_hdr(v: dict | None) -> bool:
    return bool(v) and str(v.get("color_transfer") or "") in _HDR_TRC_JUNTAR


def juntar_arquivos(doc: Documento, arquivos: list, posicao="fim", transicao: str | None = None,
                    duracao_transicao: float = 1.0, so_audio: str = "erro", capitulos: bool = False,
                    estado: dict | None = None, progresso=None) -> dict:
    """Emenda outros arquivos ao documento (nova versão), normalizando tudo para o formato do documento:
    resolução (escala mantendo a proporção + barras pretas), SAR 1, fps, formato de pixel, taxa de amostragem e
    canais; HDR → SDR (zscale+tonemap) quando o documento é SDR. Arquivo sem áudio entra com silêncio; arquivo
    só de áudio num documento com vídeo dá erro (so_audio='preto' põe imagem preta). Documento só de áudio:
    junta só os áudios.
    posicao: 'fim' | 'inicio' | um tempo (parse_tempo) → divide o documento ali (no início de um quadro) e
    encaixa os arquivos.
    transicao: None/'corte' | 'fade' (escurece e clareia; duração total igual) | 'crossfade' (mistura imagem e
    som com xfade/acrossfade; cada emenda encurta o total em duracao_transicao).
    capitulos=True: um capítulo no início de cada arquivo emendado (os capítulos do documento são mantidos,
    deslocados).
    Exatidão: cada segmento tem nº inteiro de quadros, e o vídeo fora da grade (MKV com vídeo em 0,021 s) é
    adiantado a fração de quadro, com o áudio cortado junto (ver _segmentos_av). Mais de _LOTE_JUNTAR entradas:
    em lotes (partes intermediárias), para não abrir todos os decodificadores de uma vez."""
    if not arquivos:
        raise ValueError("passe pelo menos um arquivo em `arquivos`")
    if isinstance(arquivos, (str, Path)):
        arquivos = [arquivos]
    tipo_tr = _TRANSICOES.get((transicao or "").lower() if isinstance(transicao, str) else transicao)
    if tipo_tr is None:
        raise ValueError("transicao: 'corte' (padrão), 'fade' ou 'crossfade'")
    so_audio = (so_audio or "erro").lower()
    if so_audio not in ("erro", "preto"):
        raise ValueError("so_audio: 'erro' ou 'preto'")
    entrada = doc.atual()
    ref = _fonte(entrada)
    tem_v = ref["v"] is not None
    outros = []
    for a in arquivos:
        p = Path(str(a)).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"arquivo não encontrado: {p}")
        f = _fonte(p)
        if f["dur"] < 0.1:
            raise ValueError(f"{p.name} tem {f['dur']:.3f} s (é uma imagem?); só vídeo ou áudio com duração")
        if not f["auds"] and f["v"] is None:
            raise ValueError(f"{p.name} não tem vídeo nem áudio")
        if tem_v and f["v"] is None and so_audio == "erro":
            raise ValueError(f"{p.name} é só áudio e o documento tem vídeo: use so_audio='preto' para entrar com "
                             f"imagem preta, ou musica_fundo/faixa_audio para usar o som por baixo do vídeo")
        outros.append(f)
    avisos: list[str] = []
    if not tem_v and any(f["v"] is not None for f in outros):
        avisos.append("o documento é só áudio: dos arquivos emendados entra só o som")

    # ---- formato de saída (o do documento)
    fps = ref["fps"] if tem_v else 0.0
    W, H = (max(2, ref["w"] // 2 * 2), max(2, ref["h"] // 2 * 2)) if tem_v else (0, 0)
    pix = "yuv420p10le" if tem_v and _10bits(ref["pix_fmt"]) and not entrada.suffix.lower() == ".webm" else "yuv420p"
    alvo_a: list[tuple[int, str, int]] = []  # (taxa, layout, canais) de cada faixa de saída
    for s in ref["auds"]:  # layout padrão do nº de canais (AAC 6 canais "unknown"/5.1(side) → 5.1; ver _filtro_layout)
        ch = int(s.get("channels") or 2)
        alvo_a.append((int(s.get("sample_rate") or 48000), _LAYOUT_PADRAO.get(ch) or _layout(s), ch))
    if not alvo_a:
        com_audio = [s for f in outros for s in f["auds"][:1]]
        if com_audio:
            canais = 2 if any(int(s.get("channels") or 2) > 1 for s in com_audio) else 1
            alvo_a.append((int(com_audio[0].get("sample_rate") or 48000), "stereo" if canais == 2 else "mono",
                           canais))
            avisos.append("o documento não tinha áudio: ganhou uma faixa (silêncio na parte dele)")
    na = len(alvo_a)

    # ---- HDR x SDR
    tonemap: set[int] = set()  # id() dos arquivos HDR convertidos para SDR
    if tem_v:
        filtros = binarios._lista_ffmpeg("filters")
        pode_tm = {"zscale", "tonemap"} <= filtros
        hdr_doc = _e_hdr(ref["v"])
        hdr_f = [f for f in outros if _e_hdr(f["v"])]
        sdr_f = [f for f in outros if f["v"] is not None and not _e_hdr(f["v"])]
        if not hdr_doc and hdr_f:
            if pode_tm:
                tonemap = {id(f) for f in hdr_f}
                avisos.append("HDR convertido para SDR (zscale + tonemap hable) para combinar com o documento: "
                              + ", ".join(f["caminho"].name for f in hdr_f))
            else:
                avisos.append("HDR emendado num documento SDR SEM conversão (o ffmpeg não tem zscale/tonemap): as "
                              "cores desses trechos vão sair lavadas: " + ", ".join(f["caminho"].name for f in hdr_f))
        if hdr_doc and sdr_f:
            avisos.append("SDR emendado num documento HDR sem conversão (os trechos SDR são marcados como HDR e "
                          "podem sair com cores/brilho errados): " + ", ".join(f["caminho"].name for f in sdr_f))

    # ---- onde entra (no início de um quadro, na grade do vídeo: quadro k em atraso + k/fps)
    dur_doc = ref["dur"]
    atr_doc = ref.get("atraso_video", 0.0) if tem_v else 0.0
    est = dict(estado or {})
    est.setdefault("duracao", dur_doc)
    pos_txt = str(posicao).lower().strip() if posicao is not None else "fim"
    if pos_txt in ("fim", "final", "depois", "end"):
        t_ins = dur_doc
    elif pos_txt in ("inicio", "início", "comeco", "começo", "antes", "start"):
        t_ins = 0.0
    else:
        t_ins = _alinhar(min(max(0.0, parse_tempo(posicao, est)), dur_doc), fps, atr_doc)
    pecas_doc_antes = t_ins > 0.05
    pecas_doc_depois = dur_doc - t_ins > 0.05
    if not pecas_doc_antes:
        t_ins = 0.0
    if not pecas_doc_depois:
        t_ins = dur_doc

    # segmentos na ordem final: {"info", "ss" (seek), "t" (limite de leitura), "span", "d" (duração), ...}
    margem = _MARGEM_SEEK if fps else 0.0  # só áudio: o próprio tempo
    segs: list[dict] = []
    if pecas_doc_antes:
        segs.append({"info": ref, "ss": None, "t": t_ins + _FOLGA_T, "span": t_ins, "rotulo": doc.original.stem,
                     "doc": True, "inicio_doc": 0.0})
    for f in outros:
        segs.append({"info": f, "ss": None, "t": None, "span": f["dur"], "rotulo": f["caminho"].stem, "doc": False})
    if pecas_doc_depois:
        ss = max(0.0, t_ins - margem) if t_ins > 0 else None
        segs.append({"info": ref, "ss": ss, "t": None, "span": dur_doc - (ss or 0.0), "rotulo": doc.original.stem,
                     "doc": True, "inicio_doc": t_ins})
    n = len(segs)
    for i, sg in enumerate(segs):
        inf = sg["info"]
        # fração de quadro do 1º quadro (pts = atraso − seek): o vídeo é adiantado e o áudio cortado junto
        delta = 0.0
        if fps and inf["v"] is not None:
            ph = _fase_quadro((inf.get("atraso_video", 0.0) - (sg["ss"] or 0.0)) * fps)
            delta = ph / fps if ph > 1e-3 else 0.0
        sg["delta"] = delta
        # duração em quadros inteiros: áudio e vídeo de cada segmento com o mesmo tamanho exato
        sg["d"] = max(1, round((sg["span"] - delta) * fps)) / fps if fps else sg["span"]
        sg["fade_in"], sg["fade_out"] = i > 0, i < n - 1
        sg["tonemap"] = id(inf) in tonemap

    d_tr = 0.0
    if tipo_tr != "corte":
        d_tr = float(duracao_transicao)
        if fps:
            d_tr = max(1, round(d_tr * fps)) / fps
        menor = min(sg["d"] for sg in segs)
        if d_tr <= 0 or d_tr > menor / 2:
            raise ValueError(f"duracao_transicao precisa ser > 0 e no máximo metade do menor trecho "
                             f"({menor / 2:.2f} s)")

    def montar(lista: list[dict], cross: bool) -> tuple[list[str], list[str], str | None, list[str], float]:
        """Entradas, grafo, rótulo do vídeo, rótulos do áudio e duração de uma sequência de segmentos."""
        cmd_in: list[str] = []
        grafo: list[str] = []
        m = len(lista)
        for i, sg in enumerate(lista):
            inf = sg["info"]
            thr = ["-threads", "2"] if m > 1 else []
            cmd_in += thr + (["-ss", f"{sg['ss']:.6f}"] if sg["ss"] is not None else []) \
                + (["-t", f"{sg['t']:.6f}"] if sg["t"] is not None else []) + inf["dec"] + ["-i", str(inf["caminho"])]
            D, nq = sg["d"], max(1, round(sg["d"] * fps)) if fps else 0
            fades_v, fades_a = "", ""
            if tipo_tr == "fade" and not sg.get("pronto"):
                h = d_tr / 2
                if sg["fade_in"]:
                    fades_v += f",fade=t=in:st=0:d={h:.6f}"
                    fades_a += f",afade=t=in:st=0:d={h:.6f}"
                if sg["fade_out"]:
                    fades_v += f",fade=t=out:st={D - h:.6f}:d={h:.6f}"
                    fades_a += f",afade=t=out:st={D - h:.6f}:d={h:.6f}"
            if tem_v:
                if inf["v"] is None:  # só áudio → imagem preta
                    grafo.append(f"color=c=black:s={W}x{H}:r={ref['taxa']}:d={D:.6f},format={pix},setsar=1"
                                 f"{fades_v}[v{i}]")
                else:
                    cadeia = [inf["pre"]] if inf["pre"] else []
                    if sg["tonemap"]:
                        cadeia.append(_TONEMAP_SDR)
                    if sg["delta"]:
                        cadeia.append(f"setpts=PTS-{sg['delta']:.6f}/TB")
                    if (inf["w"], inf["h"]) != (W, H):
                        cadeia.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2:"
                                      f"flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
                    # fps com start_time=0: completa o começo (vídeo que começa depois do áudio); tpad+trim: o
                    # fim, com o nº exato de quadros
                    cadeia += ["setsar=1", f"fps={ref['taxa']}:start_time=0", f"format={pix}",
                               f"tpad=stop_mode=clone:stop_duration={D:.6f}", f"trim=end_frame={nq}",
                               "setpts=PTS-STARTPTS"]
                    grafo.append(f"[{i}:V:0]" + ",".join(cadeia) + fades_v + f"[v{i}]")
            corta = f"atrim=start={sg['delta']:.6f},asetpts=PTS-STARTPTS," if sg["delta"] else ""
            for k, (sr, lay, ch) in enumerate(alvo_a):
                if k < len(inf["auds"]):
                    s = inf["auds"][k]
                    mono_estereo = int(s.get("channels") or 0) == 1 and ch == 2
                    # mono → estéreo: o mesmo sinal nos dois lados (o aformat sozinho poria -3 dB); senão só
                    # renomeia os canais para o layout padrão (channelmap, sem mixagem) antes do aformat
                    pre_a = "pan=stereo|c0=c0|c1=c0," if mono_estereo else _filtro_layout(s)
                    grafo.append(f"[{i}:a:{k}]{pre_a}aresample={sr}:first_pts=0,"
                                 f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={lay},{corta}"
                                 f"apad,atrim=end={D:.6f},asetpts=PTS-STARTPTS{fades_a}[a{k}_{i}]")
                else:
                    grafo.append(f"anullsrc=r={sr}:cl={lay},aformat=sample_fmts=fltp,atrim=end={D:.6f}[a{k}_{i}]")
        if cross:
            pv, acc = "v0", lista[0]["d"]
            pa = [f"a{k}_0" for k in range(na)]
            for i in range(1, m):
                if tem_v:
                    grafo.append(f"[{pv}][v{i}]xfade=transition=fade:duration={d_tr:.6f}:offset={acc - d_tr:.6f}[x{i}]")
                    pv = f"x{i}"
                for k in range(na):
                    grafo.append(f"[{pa[k]}][a{k}_{i}]acrossfade=d={d_tr:.6f}:c1=tri:c2=tri[ax{k}_{i}]")
                    pa[k] = f"ax{k}_{i}"
                acc += lista[i]["d"] - d_tr
            return cmd_in, grafo, (pv if tem_v else None), pa, acc
        conc = "".join((f"[v{i}]" if tem_v else "") + "".join(f"[a{k}_{i}]" for k in range(na)) for i in range(m))
        sai = ("[vc]" if tem_v else "") + "".join(f"[ac{k}]" for k in range(na))
        grafo.append(f"{conc}concat=n={m}:v={int(tem_v)}:a={na}{sai}")
        return cmd_in, grafo, ("vc" if tem_v else None), [f"ac{k}" for k in range(na)], sum(sg["d"] for sg in lista)

    # início de cada segmento na saída
    inicios, acc = [], 0.0
    for sg in segs:
        inicios.append(acc)
        acc += sg["d"] - (d_tr if tipo_tr == "crossfade" else 0.0)
    dur_saida = acc + (d_tr if tipo_tr == "crossfade" else 0.0)

    # ---- capítulos: os do documento (deslocados) + um por arquivo emendado (capitulos=True)
    caps: list[tuple[float, str]] = []
    for c in ler_capitulos(entrada, ref["probe"]):
        for sg, ini in zip(segs, inicios):
            if sg["doc"] and sg["inicio_doc"] - 1e-6 <= c["inicio"] < sg["inicio_doc"] + sg["d"] - 1e-6:
                caps.append((ini + c["inicio"] - sg["inicio_doc"], c["titulo"]))
                break
    if capitulos:
        meio = d_tr / 2 if tipo_tr == "crossfade" else 0.0
        novos = []
        for j, (sg, ini) in enumerate(zip(segs, inicios)):
            t = ini + (meio if j else 0.0)
            if any(abs(t - tc) < 0.5 for tc, _ in caps):  # já há um capítulo ali (do documento): mantém o dele
                continue
            novos.append((t, sg["rotulo"] + (" (continuação)" if sg["doc"] and sg["inicio_doc"] > 0 else "")))
        caps += novos
    caps = _limpar_capitulos(caps, dur_saida)

    # ---- saída
    if tem_v:
        saida = _saida_edicao(doc)
    else:
        ext = entrada.suffix.lower()
        saida = doc.proxima_saida(ext if ext in _EXT_AUDIO else ".mka")
    args_v = _args_video_edicao(saida.suffix, ref) if tem_v else []
    if caps and saida.suffix.lower() in (".ogg", ".opus", ".oga"):
        avisos.append(f"capítulos não gravados: o {saida.suffix} grava os tempos deles errados (use .mka/.mp3/.m4a)")
        caps = []

    def args_saida(rot_v, rot_a, meta_idx: int | None, meta_doc: int, args_video, args_audio) -> list[str]:
        out = []
        if rot_v:
            out += ["-map", f"[{rot_v}]"] + args_video + _tags_faixa(ref["v"], "s:v:0")
        for k, r in enumerate(rot_a):
            out += ["-map", f"[{r}]"]
            if k < len(ref["auds"]):
                out += _tags_faixa(ref["auds"][k], f"s:a:{k}")
        if rot_a:
            out += args_audio
        return out + ["-map_metadata", str(meta_doc),
                      "-map_chapters", str(meta_idx) if meta_idx is not None else "-1"]

    meta = None
    if caps:
        meta = Path(tempfile.mkstemp(prefix="capitulos-", suffix=".txt", dir=doc.pasta)[1])
        meta.write_text(_texto_ffmeta(caps, dur_saida), encoding="utf-8")
    cross = tipo_tr == "crossfade"
    _limpar_lotes_orfaos(doc.pasta)
    tmpdir = Path(tempfile.mkdtemp(prefix="lotes-", dir=doc.pasta)) if n > _LOTE_JUNTAR else None
    cmds: list[list[str]] = []
    durs: list[float] = []
    try:
        atuais = segs
        nivel = 0
        # lotes: corte/fade → partes no codec de vídeo final + FLAC, juntadas com o demuxer concat (vídeo copiado);
        # crossfade → partes intermediárias de alta qualidade, emendadas com xfade no nível de cima
        while len(atuais) > _LOTE_JUNTAR:
            prox = []
            for b in range(0, len(atuais), _LOTE_JUNTAR):
                lote = atuais[b:b + _LOTE_JUNTAR]
                p = tmpdir / f"n{nivel}_{b // _LOTE_JUNTAR:03d}.{'mkv' if tem_v else 'mka'}"
                ci, gr, rv, ra, dl = montar(lote, cross)
                sem_tag = [x for k, x in enumerate(args_v) if x != "-tag:v" and (k == 0 or args_v[k - 1] != "-tag:v")]
                av = (sem_tag if not cross else ["-c:v", "libx264", "-crf", "10", "-preset", "veryfast",
                                                "-pix_fmt", pix]) if tem_v else []
                c = FFMPEG + ci + ["-filter_complex", ";".join(gr)]
                c += ["-map", f"[{rv}]"] + av if rv else []
                for r in ra:
                    c += ["-map", f"[{r}]"]
                c += (["-c:a", "flac"] if ra else []) + [str(p)]
                cmds.append(c)
                durs.append(dl)
                inf = {"v": ref["v"] if tem_v else None, "auds": [{"channels": ch, "channel_layout": lay,
                                                                    "sample_rate": sr} for sr, lay, ch in alvo_a],
                       "pre": "", "dec": [], "w": W, "h": H, "atraso_video": 0.0, "caminho": p}
                prox.append({"info": inf, "ss": None, "t": None, "span": dl, "d": dl, "delta": 0.0,
                             "tonemap": False, "pronto": True, "doc": False})
            atuais = prox
            nivel += 1
            if not cross:
                break
        extra_meta = []
        if tmpdir and not cross:
            # junção das partes: concat demuxer, vídeo copiado, áudio codificado uma vez só
            lista = tmpdir / "lista.txt"
            lista.write_text("".join(f"file {esc_concat(sg['info']['caminho'])}\n" for sg in atuais))
            final = FFMPEG + ["-f", "concat", "-safe", "0", "-i", str(lista), "-i", str(entrada)]
            if meta:
                final += ["-f", "ffmetadata", "-i", str(meta)]
            final += ["-map", "0:v?"] + (["-c:v", "copy"] if tem_v else [])
            for k in range(na):
                final += ["-map", f"0:a:{k}"]
                if k < len(ref["auds"]):
                    final += _tags_faixa(ref["auds"][k], f"s:a:{k}")
            if na:
                final += codec_audio(saida.suffix)
            if tem_v:
                final += _tags_faixa(ref["v"], "s:v:0")
            final += ["-map_metadata", "1", "-map_chapters", "2" if meta else "-1"] + _faststart(saida) + [str(saida)]
        else:
            ci, gr, rv, ra, dl = montar(atuais, cross)
            m = len(atuais)
            if meta:
                extra_meta = ["-f", "ffmetadata", "-i", str(meta)]
            # metadados do contêiner: os do documento (a entrada do 1º segmento dele; posicao='inicio' → não é a 0)
            idx_doc = next((i for i, sg in enumerate(atuais) if sg.get("doc")), None)
            if idx_doc is None:
                extra_meta += ["-i", str(entrada)]
                idx_doc = m + (1 if meta else 0)
            final = FFMPEG + ci + extra_meta + ["-filter_complex", ";".join(gr)]
            final += args_saida(rv, ra, m if meta else None, idx_doc, args_v, codec_audio(saida.suffix))
            final += _faststart(saida) + [str(saida)]
        cmds.append(final)
        durs.append(dur_saida)

        if ref["legendas"] or any(f["legendas"] for f in outros):
            avisos.append("legendas embutidas não entram na junção (a linha do tempo mudou)")
        redim = [f["caminho"].name for f in outros if tem_v and f["v"] is not None and (f["w"], f["h"]) != (W, H)]
        if redim:
            avisos.append(f"redimensionado(s) para {W}x{H} com barras pretas mantendo a proporção: {', '.join(redim)}")
        sem_som = [f["caminho"].name for f in outros if not f["auds"] and alvo_a]
        if sem_som:
            avisos.append(f"sem áudio (entrou com silêncio): {', '.join(sem_som)}")
        pretos = [f["caminho"].name for f in outros if tem_v and f["v"] is None]
        if pretos:
            avisos.append(f"só áudio (entrou com imagem preta): {', '.join(pretos)}")
        avisos += _avisos_profundidade(ref) if tem_v else []
        nomes = ", ".join(f["caminho"].name for f in outros)
        onde = ("no fim" if t_ins >= dur_doc else "no início" if t_ins <= 0 else f"em {fmt_tempo(t_ins)}")
        desc = f"juntou {nomes} {onde}"
        if tipo_tr != "corte":
            desc += f" ({tipo_tr} {d_tr:.2f} s)"
        detalhes = {"formato": (f"{W}x{H} {fps:.3f} fps {pix}" if tem_v else "só áudio"),
                    "audio": [f"{sr} Hz {lay}" for sr, lay, _ in alvo_a],
                    "trechos": [{"origem": sg["rotulo"], "inicio": fmt_tempo(ini), "duracao_s": round(sg["d"], 3)}
                                for sg, ini in zip(segs, inicios)],
                    "duracao_esperada_s": round(dur_saida, 3)}
        if len(cmds) > 1:
            detalhes["lotes"] = len(cmds) - 1
        if caps:
            detalhes["capitulos"] = [{"inicio": fmt_tempo(t), "titulo": ti} for t, ti in caps]
        extra = {"detalhes": detalhes}
        if avisos:
            extra["avisos"] = avisos
        v = _rodar_versao(doc, saida, cmds, durs, desc, progresso, extra)
        if caps and len(ler_capitulos(saida)) != len(caps):
            v.setdefault("avisos", []).append(f"o contêiner {saida.suffix} não guardou os capítulos")
        return v
    finally:
        if meta:
            meta.unlink(missing_ok=True)
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---- dividir
def dividir(doc: Documento, modo: str = "marcas", intervalo_s: float | None = None, tempos: list | None = None,
            partes: int | None = None, destino: str | Path | None = None, rapido: bool = False,
            estado: dict | None = None, progresso=None) -> dict:
    """Divide o arquivo atual do documento em ARQUIVOS NOVOS (nome.parte01.ext, nome.parte02.ext...; nunca
    sobrescreve; não cria versão). modo: 'marcas' (corta em cada marca: 0→m1, m1→m2, ..., mN→fim) |
    'trechos' (só os trechos entre marcas consecutivas m1→m2, m2→m3...) | 'intervalo' (a cada intervalo_s) |
    'tempos' (corta nos tempos dados) | 'partes' (N partes iguais).
    rapido=False: corte exato no quadro (re-encoda, _cmd_preciso). rapido=True: sem re-encodar (-c copy, muxer
    segment): instantâneo, mas cada corte cai no keyframe seguinte ao ponto pedido."""
    import math
    entrada = doc.atual()
    probe = ffprobe(entrada)
    dur = duracao(probe)
    vids = [s for s in probe["streams"] if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    fps = _fps_stream(vids[0]) if vids else 0.0
    atraso = _atraso_video(probe, vids[0]) if vids else 0.0
    est = dict(estado or {})
    est.setdefault("duracao", dur)
    modo = (modo or "marcas").lower()
    marcas = sorted(float(m["tempo"] if isinstance(m, dict) else m) for m in est.get("marcas") or [])
    trechos: list[tuple[float, float]]
    if modo == "trechos":
        if len(marcas) < 2:
            raise ValueError("modo 'trechos' precisa de pelo menos 2 marcas na régua")
        trechos = [(_alinhar(a, fps, atraso), _alinhar(b, fps, atraso)) for a, b in zip(marcas, marcas[1:])]
    else:
        if modo == "marcas":
            if not marcas:
                raise ValueError("não há marcas na régua; marque os pontos de corte ou use modo='intervalo'/'tempos'")
            pontos = marcas
        elif modo == "intervalo":
            if not intervalo_s or float(intervalo_s) < 0.5:
                raise ValueError("intervalo_s (segundos, ≥ 0,5) é obrigatório no modo 'intervalo'")
            passo = float(intervalo_s)
            pontos = [passo * i for i in range(1, int(dur / passo) + 1)]
            if pontos and dur - pontos[-1] < min(0.5, passo / 4):  # sobra minúscula vai para a parte anterior
                pontos.pop()
        elif modo == "tempos":
            if not tempos:
                raise ValueError("passe `tempos` (lista de pontos de corte) no modo 'tempos'")
            pontos = [parse_tempo(t, est) for t in tempos]
        elif modo == "partes":
            if not partes or int(partes) < 2:
                raise ValueError("partes (≥ 2) é obrigatório no modo 'partes'")
            pontos = [dur * i / int(partes) for i in range(1, int(partes))]
        else:
            raise ValueError("modo: 'marcas', 'trechos', 'intervalo', 'tempos' ou 'partes'")
        pontos = sorted({_alinhar(p, fps, atraso) for p in pontos if 0.05 < p < dur - 0.05})
        if not pontos:
            raise ValueError("nenhum ponto de corte dentro do arquivo")
        lim = [0.0] + pontos + [dur]
        trechos = [(a, b) for a, b in zip(lim, lim[1:]) if b - a > 0.001]
    if len(trechos) > 500:
        raise ValueError(f"{len(trechos)} partes é demais (máximo 500); aumente o intervalo")
    if destino and _ext_destino(destino):
        raise ValueError("destino é a PASTA das partes (os nomes são automáticos: nome.parte01.ext...)")
    ext = entrada.suffix
    largura = max(2, len(str(len(trechos))))
    sufixos = [f"parte{i:0{largura}d}" for i in range(1, len(trechos) + 1)]
    total = sum(b - a for a, b in trechos) or 1.0
    avisos: list[str] = []
    feitos: list[dict] = []
    comandos: list[list[str]] = []
    if not rapido:
        feito = 0.0
        for (a, b), suf in zip(trechos, sufixos):
            # pontos na grade do vídeo (_alinhar) e meio ms antes no -ss: cada parte começa exatamente no quadro do
            # ponto e termina no anterior ao próximo (_segmentos_av conta os quadros na fase do segmento). Só
            # áudio: o próprio tempo
            m = _MARGEM_SEEK if fps else 0.0
            ar = max(0.0, a - m) if a > 0 else 0.0
            br = b - m if b < dur - 0.001 else dur
            final = _reservar(_caminho(doc.original, suf, ext, destino, criar=True))
            _limpar_orfaos(final.parent)
            tmp = _temporario(final)
            ok = False
            try:
                # mesmo codec das edições: HEVC/10 bits continua HEVC/10 bits, tags de cor (HDR) mantidas
                cmd = _cmd_preciso(entrada, probe, [(ar, br)], tmp,
                                   _args_video_edicao(ext.lower(), {"pix_fmt": vids[0].get("pix_fmt", ""),
                                                                    "v": vids[0]}) if vids else None)
                comandos.append(cmd)
                _rodar_com_progresso(cmd, b - a, progresso, feito / total, (b - a) / total)
                final = _publicar(tmp, final)
                ok = True
            finally:
                tmp.unlink(missing_ok=True)
                if not ok:
                    _soltar_reserva(final)
            feito += b - a
            feitos.append({"final": final, "inicio_pedido": a, "fim_pedido": b})
    else:
        if modo == "trechos":
            raise ValueError("rapido=True não vale no modo 'trechos' (use o corte preciso)")
        pasta = (Path(str(destino)).expanduser() if destino else doc.original.parent)
        pasta.mkdir(parents=True, exist_ok=True)
        # pontos reais = primeiro keyframe do vídeo em ou depois de cada ponto pedido (lidos só perto dos pontos)
        pedidos = [a for a, _ in trechos[1:]]
        if vids:
            vidx = int(vids[0]["index"])
            kfs = sorted({round(t, 6) for i, t, _tam, k in _pacotes(entrada, [(max(0.0, p - 1), 31.0) for p in pedidos],
                                                                     _inicio_conteiner(probe))
                          if i == vidx and k})
            reais = []
            for p in pedidos:
                kf = next((k for k in kfs if k >= p - 0.001), None)
                if kf is None or kf >= dur - 0.05:
                    avisos.append(f"sem keyframe depois de {fmt_tempo(p)}: esse corte foi ignorado")
                elif kf not in reais:
                    reais.append(kf)
        else:
            reais = pedidos
        if not reais:
            raise ValueError("nenhum keyframe perto dos pontos pedidos; use o corte preciso (rapido=False)")
        _limpar_lotes_orfaos(pasta, ".dividir-")  # de um dividir rápido interrompido (sem escrita há 10 min)
        tmpdir = Path(tempfile.mkdtemp(prefix=".dividir-", dir=pasta))
        try:
            cortes = ",".join(f"{a:.6f}" for a in reais)
            # o muxer segment interpreta % no caminho inteiro: o da pasta (e da extensão) vira %%
            padrao = str(tmpdir).replace("%", "%%") + "/p%04d" + ext.replace("%", "%%")
            # o muxer segment compara com o pts de saída (deslocado pelo atraso dos B-frames/priming do áudio):
            # a tolerância faz ele aceitar exatamente o keyframe escolhido
            delta = max(0.05, 1.5 / fps) if fps else 0.0
            cmd = FFMPEG + ["-i", str(entrada)] + MAP_PADRAO + ["-c", "copy", "-map_metadata", "0",
                                                                 "-map_chapters", "-1", "-f", "segment",
                                                                 "-segment_times", cortes, "-reset_timestamps", "1"]
            if delta:
                cmd += ["-segment_time_delta", f"{delta:.4f}"]
            if ext.lower() in (".mp4", ".mov", ".m4v"):
                cmd += ["-segment_format_options", "movflags=+faststart"]
            cmd.append(padrao)
            comandos.append(cmd)
            _rodar_com_progresso(cmd, dur, progresso)
            geradas = sorted(tmpdir.glob(f"p*{ext}"))
            if len(geradas) != len(trechos):
                avisos.append(f"pedidas {len(trechos)} partes, saíram {len(geradas)}: pontos de corte sem keyframe "
                              f"entre eles caíram no mesmo lugar")
            larg2 = max(2, len(str(len(geradas))))
            lim = [0.0] + reais + [dur]
            for i, g in enumerate(geradas, 1):
                final = _reservar(_caminho(doc.original, f"parte{i:0{larg2}d}", ext, destino, criar=True))
                os.replace(g, final)
                f = {"final": final}
                if len(geradas) == len(reais) + 1:
                    f.update({"inicio_pedido": lim[i - 1], "fim_pedido": lim[i]})
                feitos.append(f)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        avisos.append("modo rápido (sem reencodar): cada corte caiu no keyframe seguinte ao ponto pedido; "
                      "pedidos " + ", ".join(fmt_tempo(p) for p in pedidos) + " → reais "
                      + ", ".join(fmt_tempo(p) for p in reais))
    res_partes, acc = [], 0.0
    for f in feitos:
        pr = ffprobe(f["final"])
        d = duracao(pr)
        item = {"arquivo": str(f["final"]), "inicio": fmt_tempo(f.get("inicio_pedido", acc)),
                "fim": fmt_tempo(f.get("fim_pedido", acc + d)), "duracao_s": round(d, 3),
                "tamanho_mb": round(f["final"].stat().st_size / _MB, 2)}
        res_partes.append(item)
        acc += d
    r = {"partes": res_partes, "quantidade": len(res_partes), "soma_duracoes_s": round(acc, 3),
         "duracao_original_s": round(dur, 3), "origem": str(entrada), "modo": modo,
         "corte": "rápido (keyframes, sem perda)" if rapido else "preciso (no quadro, re-encodado)",
         "comandos": [shlex.join(c) for c in comandos]}
    if avisos:
        r["avisos"] = avisos
    return r


# ---- cenas
def detectar_cenas(arquivo: Path | str, limiar: float = 0.3, min_intervalo_s: float = 1.0, inicio=None, fim=None,
                   estado: dict | None = None, progresso=None) -> dict:
    """Cortes de cena (mudanças bruscas de imagem) com select='gt(scene,limiar)' numa cópia reduzida a 320 px de
    largura. limiar 0..1 (0,3 padrão; menor = mais sensível). Cortes a menos de min_intervalo_s um do outro: fica
    o de maior score. Transições suaves (fade/dissolve) quase não são detectadas."""
    arq = Path(arquivo)
    probe = ffprobe(arq)
    vids = [s for s in probe["streams"] if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    if not vids:
        raise ValueError("o arquivo não tem vídeo")
    limiar = float(limiar)
    if not 0 < limiar < 1:
        raise ValueError("limiar entre 0 e 1 (0.3 padrão; 0.15 = mais sensível, 0.5 = só cortes fortes)")
    dur = duracao(probe)
    est = dict(estado or {})
    est.setdefault("duracao", dur)
    a = max(0.0, parse_tempo(inicio, est)) if inicio is not None else 0.0
    b = min(dur, parse_tempo(fim, est)) if fim is not None else dur
    if b - a < 0.1:
        raise ValueError("intervalo vazio")
    import time as _time
    t0 = _time.monotonic()
    fd, nome = tempfile.mkstemp(prefix="cenas-", suffix=".txt")
    os.close(fd)
    log = Path(nome)
    try:
        cmd = FFMPEG + ["-threads", "4"]
        if a > 0:
            cmd += ["-ss", f"{a:.3f}"]
        if b < dur:
            cmd += ["-t", f"{b - a:.3f}"]  # na entrada: o filtro não vê nada depois de `fim`
        # sem o filtro de deblocagem o H.264/HEVC decodifica ~15% mais rápido; não muda nada a 320 px
        cmd += ["-skip_loop_filter", "all"] + _dec_entrada(vids[0]) + ["-i", str(arq)]
        cmd += ["-map", "0:V:0", "-an", "-sn", "-dn", "-filter_threads", "4",
                "-vf", f"scale=320:-2:flags=fast_bilinear,select=gt(scene\\,{limiar:.4f}),"
                       f"metadata=mode=print:key=lavfi.scene_score:file={esc_filtro(str(log))}",
                "-fps_mode", "passthrough", "-f", "null", "-"]
        _rodar_com_progresso(cmd, b - a, progresso)
        brutos, t = [], None
        for linha in log.read_text(errors="replace").splitlines():
            m = re.search(r"pts_time:\s*(-?[\d.]+)", linha)
            if m:
                t = float(m.group(1))
                continue
            m = re.match(r"lavfi\.scene_score=([\d.]+)", linha.strip())
            if m and t is not None and t + a <= b + 1e-6:
                brutos.append((t + a, float(m.group(1))))
                t = None
    finally:
        log.unlink(missing_ok=True)
    cortes: list[tuple[float, float]] = []
    for t, s in sorted(brutos):
        if cortes and t - cortes[-1][0] < float(min_intervalo_s or 0):
            if s > cortes[-1][1]:
                cortes[-1] = (t, s)
            continue
        cortes.append((t, s))
    r = {"cortes": [{"tempo": round(t, 3), "tempo_fmt": fmt_tempo(t), "score": round(s, 3)} for t, s in cortes],
         "quantidade": len(cortes), "limiar": limiar, "min_intervalo_s": min_intervalo_s,
         "analisado": [fmt_tempo(a), fmt_tempo(b)], "tempo_analise_s": round(_time.monotonic() - t0, 2),
         "comando": shlex.join(cmd)}
    if not cortes:
        r["dica"] = "nenhum corte acima do limiar; tente limiar=0.15 (transições suaves quase não aparecem)"
    return r


# ---- capítulos
_EXT_CAP_MP4 = (".mp4", ".mov", ".m4v", ".m4a")  # contêineres em que o 1º capítulo sempre começa em 0


def _gravar_capitulos(doc: Documento, caps: list[tuple[float, str]], descricao: str) -> dict:
    """Nova versão com estes capítulos (lista completa), sem reencodar (-c copy)."""
    entrada = doc.atual()
    probe = ffprobe(entrada)
    dur = duracao(probe)
    caps = _limpar_capitulos(caps, dur)
    # título vazio: o MP3 (ID3 CHAP) relê como "ch1"; todos ganham o nome padrão, como no 'adicionar'
    vazios = [i for i, (_t, ti) in enumerate(caps, 1) if not ti.strip()]
    caps = [(t, ti.replace("\r\n", "\n") if ti.strip() else f"Capítulo {i}") for i, (t, ti) in enumerate(caps, 1)]
    saida = doc.proxima_saida()
    mp4 = saida.suffix.lower() in _EXT_CAP_MP4
    meta = None
    try:
        base = FFMPEG + ["-i", str(entrada)]
        if caps:
            meta = Path(tempfile.mkstemp(prefix="capitulos-", suffix=".txt", dir=doc.pasta)[1])
            meta.write_text(_texto_ffmeta(caps, dur), encoding="utf-8")
            base += ["-f", "ffmetadata", "-i", str(meta)]
        fim = ["-c", "copy", "-map_metadata", "0", "-map_chapters", "1" if caps else "-1"] + _faststart(saida)
        # a trilha de texto com os capítulos antigos do MP4/MOV aparece como fluxo de dados: fica de fora (o muxer
        # cria outra com os novos)
        velhos = [x for s in probe["streams"] if s.get("codec_type") == "data"
                  and s.get("codec_tag_string") == "text" for x in ("-map", f"-0:{s['index']}")]
        # -map 0 leva tudo (inclusive dados/anexos); se o contêiner recusar algum fluxo, fica o essencial
        cmds = [base + ["-map", "0"] + velhos + fim + [str(saida)],
                base + ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?"] + fim + [str(saida)]]
        erro = None
        for cmd in cmds:
            try:
                rodar(cmd)
                erro = None
                break
            except ErroFFmpeg as e:
                erro = e
                saida.unlink(missing_ok=True)
        if erro:
            raise erro
        lidos = ler_capitulos(saida)
        if len(lidos) != len(caps):
            saida.unlink(missing_ok=True)
            raise ValueError(f"o contêiner {entrada.suffix} não guarda capítulos (gravados {len(caps)}, lidos "
                             f"{len(lidos)}); exporte para .mp4 ou .mkv antes")
        # os tempos relidos têm de bater (Ogg/Opus grava com a base de tempo errada e o ffprobe nem reclama);
        # no MP4/MOV/M4A o 1º capítulo sempre começa em 0
        for i, (c, (t, _ti)) in enumerate(zip(lidos, caps)):
            lido = float(c["inicio"])
            if abs(lido - t) > 0.0015 and not (mp4 and i == 0 and lido < 0.0015):
                saida.unlink(missing_ok=True)
                raise ValueError(f"o contêiner {entrada.suffix} não guarda os tempos dos capítulos corretamente "
                                 f"(capítulo {i + 1}: gravado {fmt_tempo(t)}, relido {fmt_tempo(lido)}); exporte "
                                 f"para .mkv, .mka ou .mp4 antes")
    finally:
        if meta:
            meta.unlink(missing_ok=True)
    v = doc.registrar(saida, descricao, [cmd])
    v["capitulos"] = lidos
    avisos = []
    if caps and caps[0][0] > 0.001 and mp4:
        avisos.append(f"no MP4/MOV/M4A o primeiro capítulo sempre começa em 0 (limitação do formato): "
                      f"'{caps[0][1]}' aparece de 0 em diante; adicione um capítulo em 0 (ex.: 'Início') para "
                      f"separar (o YouTube também exige o primeiro em 0:00)")
    if vazios:
        avisos.append("capítulo(s) sem título ganharam o nome padrão: " + ", ".join(f"Capítulo {i}" for i in vazios))
    if avisos:
        v["avisos"] = avisos
    return v


def editar_capitulos(doc: Documento, acao: str = "listar", tempo=None, titulo: str | None = None,
                     numero: int | None = None, itens: list | None = None, estado: dict | None = None) -> dict:
    """acao: 'listar' | 'adicionar' (tempo+titulo, ou itens=[[tempo, titulo], ...]) | 'remover' (numero, ou
    itens=[n, ...]) | 'renomear' (numero+titulo) | 'limpar' | 'definir' (itens substitui todos).
    Gravar = nova versão sem reencodar."""
    acao = (acao or "listar").lower()
    entrada = doc.atual()
    probe = ffprobe(entrada)
    dur = duracao(probe)
    atuais = ler_capitulos(entrada, probe)
    if acao == "listar":
        return {"capitulos": atuais, "quantidade": len(atuais), "arquivo": str(entrada)}
    est = dict(estado or {})
    est.setdefault("duracao", dur)
    caps = [(c["inicio"], c["titulo"]) for c in atuais]

    def novos(lista):
        out = []
        for it in lista:
            if isinstance(it, dict):
                t, ti = it.get("tempo", it.get("inicio")), it.get("titulo", "")
            elif isinstance(it, (list, tuple)):
                t, ti = it[0], (it[1] if len(it) > 1 else "")
            else:
                t, ti = it, ""
            tt = parse_tempo(t, est)
            if not 0 <= tt < dur:
                raise ValueError(f"{t} ({fmt_tempo(tt)}) está fora do arquivo (0 → {fmt_tempo(dur)})")
            out.append((tt, str(ti or "")))
        return out

    def num_ok(nn):
        nn = int(nn)
        if not 1 <= nn <= len(caps):
            raise ValueError(f"capítulo {nn} não existe (há {len(caps)})")
        return nn

    if acao == "adicionar":
        lista = itens if itens else ([[tempo if tempo is not None else "atual", titulo or ""]])
        ad = novos(lista)
        for i, (t, ti) in enumerate(ad):
            if not ti:
                ad[i] = (t, f"Capítulo {len(caps) + i + 1}")
        caps += ad
        desc = "capítulos: adicionou " + ", ".join(f"'{ti}' em {fmt_tempo(t)}" for t, ti in ad)
    elif acao == "remover":
        nums = sorted({num_ok(x) for x in (itens or ([numero] if numero is not None else []))})
        if not nums:
            raise ValueError("passe `numero` (ou itens=[n, ...]) do capítulo a remover")
        desc = "capítulos: removeu " + ", ".join(f"{x} ('{caps[x - 1][1]}')" for x in nums)
        caps = [c for i, c in enumerate(caps, 1) if i not in nums]
    elif acao == "renomear":
        if numero is None or titulo is None:
            raise ValueError("renomear precisa de `numero` e `titulo`")
        nn = num_ok(numero)
        desc = f"capítulos: renomeou {nn} ('{caps[nn - 1][1]}' → '{titulo}')"
        caps[nn - 1] = (caps[nn - 1][0], str(titulo))
    elif acao == "limpar":
        if not caps:
            return {"aviso": "o arquivo não tem capítulos", "capitulos": []}
        desc = f"capítulos: removeu todos ({len(caps)})"
        caps = []
    elif acao == "definir":
        caps = novos(itens or [])
        desc = f"capítulos: definiu {len(caps)}"
    else:
        raise ValueError("acao: 'listar', 'adicionar', 'remover', 'renomear', 'limpar' ou 'definir'")
    return _gravar_capitulos(doc, caps, desc)


# ---- relatório técnico
_EFICIENCIA = {"h264": 1.0, "mpeg4": 1.4, "mpeg2video": 2.0, "hevc": 0.6, "vp9": 0.65, "av1": 0.5, "vp8": 1.1}
_HDR_TRC = {"smpte2084": "HDR10/PQ", "arib-std-b67": "HLG"}


def _inicio_conteiner(probe: dict) -> float:
    try:
        return float(probe.get("format", {}).get("start_time") or 0)
    except ValueError:
        return 0.0


def _pacotes(arq: Path, janelas: list[tuple[float, float]] | None,
             inicio: float = 0.0) -> list[tuple[int, float, int, bool]]:
    """(índice do fluxo, pts, tamanho, keyframe) de todos os pacotes (ou só das janelas [ini, dur]).
    Tempos relativos ao início do contêiner (`inicio` = format.start_time; o ffprobe usa tempo absoluto nos
    pacotes e no -read_intervals, e TS/MP4 com deslocamento não começam em 0)."""
    cmd = [FFPROBE, "-v", "error", "-show_entries", "packet=stream_index,pts_time,dts_time,size,flags",
           "-of", "csv=p=0"]
    if janelas:
        cmd += ["-read_intervals", ",".join(f"{a + inicio:.3f}%+{d:.3f}" for a, d in janelas)]
    r = rodar(cmd + [str(arq)])
    out = []
    for linha in r.stdout.splitlines():
        p = linha.split(",")
        if len(p) < 5:
            continue
        try:
            t = float(p[1]) if p[1] not in ("", "N/A") else float(p[2])
            out.append((int(p[0]), t - inicio, int(p[3]), "K" in p[4]))
        except ValueError:
            continue
    return out


def _ffmpeg_stderr(cmd: list[str], timeout: float | None = None) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise ErroFFmpeg(f"comando falhou ({r.returncode}): {shlex.join(cmd)}\n{r.stderr[-3000:]}")
    return r.stderr


def relatorio_tecnico(arquivo: Path | str, faixa: int = 1) -> dict:
    """Relatório técnico: contêiner, codecs/perfil/nível, bitrate (médio, por faixa, pico em 1 s), GOP/keyframes,
    VFR, entrelaçamento (idet), cor/HDR, áudio (LUFS integrado, pico verdadeiro, clipping), indícios de
    recompressão (heurísticos) e recomendações. Em arquivos longos, mede por amostragem (janelas espalhadas)."""
    arq = Path(arquivo)
    if not arq.is_file():
        raise FileNotFoundError(f"arquivo não encontrado: {arq}")
    try:
        probe = ffprobe(arq)
    except ErroFFmpeg as e:
        motivo = str(e).strip().splitlines()[-1] if str(e).strip() else ""
        raise ValueError(f"{arq.name} não pôde ser lido: arquivo corrompido, incompleto (download/gravação "
                         f"interrompidos?) ou não é mídia ({motivo})") from None
    fmt = probe.get("format", {})
    dur = duracao(probe)
    vids = [s for s in probe["streams"] if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    auds = faixas_audio(probe)
    if not vids and not auds:
        raise ValueError(f"{arq.name} não tem vídeo nem áudio legíveis (corrompido ou não é mídia)")
    rel: dict = {"arquivo": str(arq), "duracao": fmt_tempo(dur), "duracao_s": round(dur, 3),
                 "tamanho_mb": round(arq.stat().st_size / _MB, 2),
                 "conteiner": fmt.get("format_long_name") or fmt.get("format_name"),
                 "bitrate_total_kbps": round(int(fmt["bit_rate"]) / 1000) if str(fmt.get("bit_rate", "")).isdigit()
                 else None}
    problemas: list[str] = []   # impedem/atrapalham publicar
    sugestoes: list[str] = []
    amostrado = dur > 600
    janelas = None
    if amostrado:
        nj = 8
        janelas = [(max(0.0, dur * (i + 0.5) / nj - 10), 20.0) for i in range(nj)]
    pac = _pacotes(arq, janelas, _inicio_conteiner(probe))
    tempo_amostra = sum(min(d, dur - a) for a, d in janelas) if janelas else dur
    por_fluxo: dict[int, list] = {}
    for idx, t, tam, k in pac:
        por_fluxo.setdefault(idx, []).append((t, tam, k))

    def kbps(idx: int) -> float | None:
        ps = por_fluxo.get(idx)
        if not ps or tempo_amostra <= 0:
            return None
        return sum(x[1] for x in ps) * 8 / 1000 / tempo_amostra

    # ---- vídeo
    if vids:
        v = vids[0]
        w, h = _dimensoes(v)
        fps = _fps_stream(v)
        idx = int(v["index"])
        kb = kbps(idx)
        if kb is None and str(v.get("bit_rate", "")).isdigit():
            kb = int(v["bit_rate"]) / 1000
        ps = sorted(por_fluxo.get(idx, []))
        vid: dict = {"codec": v.get("codec_name"), "perfil": v.get("profile"), "nivel": v.get("level"),
                     "resolucao": f"{w}x{h}", "fps": round(fps, 3), "pix_fmt": v.get("pix_fmt"),
                     "bitrate_kbps": round(kb) if kb else None}
        if isinstance(v.get("level"), int) and v["level"] > 0:  # H.264: 31 = 3.1; HEVC: 93 = 3.1
            div = {"h264": 10, "hevc": 30}.get(v.get("codec_name"))
            if div:
                vid["nivel"] = round(v["level"] / div, 1)
        sar = v.get("sample_aspect_ratio")
        if sar and sar not in ("1:1", "0:1"):
            vid["sar"] = sar
        # pico de bitrate (janelas de 1 s)
        baldes: dict[int, int] = {}
        for t, tam, _k in ps:
            baldes[int(t)] = baldes.get(int(t), 0) + tam
        if len(baldes) > 2:
            vid["bitrate_pico_1s_kbps"] = round(max(list(baldes.values())[1:-1] or baldes.values()) * 8 / 1000)
        # GOP
        chaves = [t for t, _tam, k in ps if k]
        if janelas:  # intervalos só dentro de cada janela
            difs = [b - a for a, b in zip(chaves, chaves[1:]) if b - a < 20]
        else:
            difs = [b - a for a, b in zip(chaves, chaves[1:])]
        if difs:
            vid["gop"] = {"keyframes_intervalo_medio_s": round(sum(difs) / len(difs), 2),
                          "keyframes_intervalo_max_s": round(max(difs), 2),
                          "keyframes_contados": len(chaves)}
            if fps:
                vid["gop"]["quadros_medio"] = round(sum(difs) / len(difs) * fps)
        elif len(chaves) <= 1 and ps:
            vid["gop"] = {"keyframes_contados": len(chaves), "obs": "um keyframe só na amostra (GOP muito longo)"}
        # VFR
        pts = sorted(t for t, _tam, _k in ps)
        deltas = [b - a for a, b in zip(pts, pts[1:]) if 0 < b - a < 1]
        vfr = False
        if len(deltas) > 10:
            med = sorted(deltas)[len(deltas) // 2]
            fora = sum(1 for d in deltas if abs(d - med) > 0.2 * med) / len(deltas)
            vfr = fora > 0.02
            vid["vfr"] = vfr
            if vfr:
                vid["vfr_detalhe"] = f"{fora:.0%} dos intervalos entre quadros fogem da mediana ({med * 1000:.1f} ms)"
        # cor / HDR
        cor = {k: v.get(c) for k, c in (("faixa", "color_range"), ("espaco", "color_space"),
                                        ("primarias", "color_primaries"), ("transferencia", "color_transfer"))
               if v.get(c)}
        hdr = _HDR_TRC.get(v.get("color_transfer"))
        side = [sd.get("side_data_type", "") for sd in v.get("side_data_list", [])]
        if any("DOVI" in s or "Dolby" in s for s in side):
            hdr = "Dolby Vision" + (f" ({hdr})" if hdr else "")
        if cor:
            vid["cor"] = cor
        vid["bits"] = 10 if _10bits(v.get("pix_fmt", "")) else 8
        vid["hdr"] = hdr or False
        # amostras decodificadas: entrelaçamento (idet) e blocagem (blockdetect)
        pos = [dur * f for f in (0.2, 0.5, 0.8)] if dur > 6 else [0.0]
        tff = bff = prog = 0
        blocos = []
        disp = binarios._lista_ffmpeg("filters")
        filtros_amostra = ",".join(f for f in ("idet", "blockdetect") if not disp or f in disp)
        for t in (pos if filtros_amostra else []):
            # -noaccurate_seek: analisa a partir do keyframe anterior (não decodifica quadros só para chegar em t)
            err = _ffmpeg_stderr([FFMPEG[0], "-hide_banner", "-nostdin", "-threads", "4", "-noaccurate_seek",
                                  "-ss", f"{t:.3f}"]
                                 + _dec_entrada(v) + ["-i", str(arq), "-map", "0:V:0", "-frames:v", "60",
                                 "-vf", filtros_amostra, "-an", "-sn", "-f", "null", "-"], timeout=120)
            m = re.findall(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)", err)
            if m:
                a1, b1, c1 = max(m, key=lambda x: sum(map(int, x)))
                tff, bff, prog = tff + int(a1), bff + int(b1), prog + int(c1)
            m = re.findall(r"block mean:\s*([\d.]+)", err)
            if m:
                blocos.append(float(m[-1]))
        fo = v.get("field_order") or "desconhecido"
        detectado = tff + bff > 2 * prog and tff + bff >= 20
        sinalizado = fo in ("tt", "bb", "tb", "bt")
        # sinalizado progressivo + idet "entrelaçado" costuma ser falso positivo (linhas finas em movimento,
        # gráficos): vira só "possível"
        vid["entrelacado"] = True if sinalizado or (detectado and fo != "progressive") else (
            "possível" if detectado else False)
        vid["entrelacamento"] = {"sinalizado": fo, "idet": {"tff": tff, "bff": bff, "progressivo": prog}}
        # recompressão (heurística)
        indicios = []
        if kb and w and h and fps:
            bpp = kb * 1000 / (w * h * fps)
            equiv = bpp / _EFICIENCIA.get(v.get("codec_name"), 1.0)
            vid["bits_por_pixel"] = round(bpp, 4)
            if equiv < 0.035:
                indicios.append(f"bitrate muito baixo para {w}x{h} a {fps:g} fps ({bpp:.3f} bit/pixel): compressão "
                                f"forte; pode já ter passado por redes sociais/WhatsApp")
            elif equiv < 0.06:
                indicios.append(f"bitrate baixo para a resolução ({bpp:.3f} bit/pixel)")
        if blocos:
            vid["blocagem_estimada"] = round(sum(blocos) / len(blocos), 2)
            if vid["blocagem_estimada"] >= 3 and indicios:
                indicios.append(f"blocagem estimada alta ({vid['blocagem_estimada']}; blockdetect)")
        enc = (fmt.get("tags") or {}).get("encoder") or (v.get("tags") or {}).get("encoder")
        if enc:
            vid["encoder"] = enc
        rel["video"] = vid
        rel["recompressao"] = {"indicios": indicios, "obs": "heurística: bits/pixel (ajustado pelo codec) e "
                               "blockdetect em 180 quadros; conteúdo simples (tela, animação) comprime muito "
                               "sem perder qualidade, e a blocagem depende do conteúdo"}
        # recomendações de vídeo
        if vid["entrelacado"] is True:
            problemas.append("vídeo entrelaçado: desentrelaçar (filtro bwdif/yadif) antes de publicar")
        elif vid["entrelacado"]:
            sugestoes.append("o idet viu padrão de entrelaçamento, mas o arquivo diz ser progressivo: confira com "
                             "ver_quadros (pente/serrilhado em movimento); gráficos com linhas finas enganam o idet")
        if vfr:
            sugestoes.append("taxa de quadros variável (VFR): converter para fps constante (exportar com fps=...) "
                             "evita dessincronia em outros editores")
        if hdr:
            sugestoes.append(f"{hdr}: em telas/redes sem HDR as cores ficam lavadas; para WhatsApp/telas comuns "
                             f"converta para SDR")
        g = vid.get("gop", {}).get("keyframes_intervalo_medio_s")
        if g and g > 10:
            sugestoes.append(f"keyframes a cada {g} s: navegar/cortar sem reencodar fica impreciso (cortes rápidos "
                             f"caem longe do ponto)")
        if v.get("codec_name") not in ("h264", "hevc", "vp9", "av1", "prores", "mpeg4", "dnxhd", "mpeg2video"):
            problemas.append(f"codec de vídeo {v.get('codec_name')} pouco aceito: exporte em MP4 (H.264)")
        if indicios:
            sugestoes.append("qualidade já comprometida: evite recomprimir de novo (exporte com qualidade alta, "
                             "ex.: predefinição youtube/mp4 com qualidade='alta')")
    # ---- áudio
    if auds:
        faixas = []
        for k, s in enumerate(auds):
            kb = kbps(int(s["index"]))
            if kb is None and str(s.get("bit_rate", "")).isdigit():
                kb = int(s["bit_rate"]) / 1000
            faixas.append({"faixa": k + 1, "codec": s.get("codec_name"), "perfil": s.get("profile"),
                           "taxa_amostragem": int(s.get("sample_rate") or 0), "canais": s.get("channels"),
                           "layout": s.get("channel_layout"), "bitrate_kbps": round(kb) if kb else None,
                           "idioma": (s.get("tags") or {}).get("language")})
        rel["audio"] = faixas
        k = max(0, min(int(faixa) - 1, len(auds) - 1))
        # loudness: tudo até 5 min (o pico verdadeiro custa ~1 s por minuto); acima disso, 10 janelas de 30 s
        # espalhadas (estimativa)
        longo_a = dur > 300
        if longo_a:
            js = [(max(0.0, dur * (i + 0.5) / 10 - 15), 30.0) for i in range(10)]
            cmd = [FFMPEG[0], "-hide_banner", "-nostdin"]
            for a, d in js:
                cmd += ["-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", str(arq)]
            grafo = "".join(f"[{i}:a:{k}]" for i in range(len(js))) + f"concat=n={len(js)}:v=0:a=1,"
            cmd += ["-filter_complex", grafo + "ebur128=peak=true,astats=measure_perchannel=none[out]",
                    "-map", "[out]", "-f", "null", "-"]
        else:
            cmd = [FFMPEG[0], "-hide_banner", "-nostdin", "-i", str(arq), "-map", f"0:a:{k}",
                   "-af", "ebur128=peak=true,astats=measure_perchannel=none", "-f", "null", "-"]
        err = _ffmpeg_stderr(cmd)
        resumo = err.split("Summary:")[-1] if "Summary:" in err else ""

        def achar(rx, txt):
            m = re.findall(rx, txt)
            try:
                x = float(m[-1]) if m else None
            except ValueError:
                return None
            return x if x is not None and math.isfinite(x) else None  # -inf (silêncio) → None: JSON válido
        ana = {"faixa": k + 1, "lufs_integrado": achar(r"I:\s*(-?[\d.]+) LUFS", resumo),
               "lra_lu": achar(r"LRA:\s*(-?[\d.]+) LU", resumo),
               "pico_verdadeiro_dbtp": achar(r"Peak:\s*(-?[\d.]+|-inf) dBFS", resumo),
               "pico_amostra_db": achar(r"Peak level dB:\s*(-?[\d.]+)", err),
               "rms_db": achar(r"RMS level dB:\s*(-?[\d.]+)", err),
               "fator_plano": achar(r"Flat factor:\s*(-?[\d.]+)", err),
               "contagem_pico": achar(r"Peak count:\s*(-?[\d.]+)", err),
               "amostragem": "10 janelas de 30 s (estimativa)" if longo_a else "arquivo inteiro"}
        clip = (ana["pico_amostra_db"] is not None and ana["pico_amostra_db"] >= -0.1
                and ((ana["fator_plano"] or 0) > 0 or (ana["contagem_pico"] or 0) >= 10))
        ana["clipping"] = clip
        rel["audio_analise"] = ana
        lufs, tp = ana["lufs_integrado"], ana["pico_verdadeiro_dbtp"]
        if clip:
            problemas.append("áudio estourado (clipping): picos achatados em 0 dBFS; reduzir o ganho não desfaz, "
                             "mas evita piorar")
        if lufs is not None and lufs > -70:
            if lufs < -20:
                sugestoes.append(f"áudio baixo ({lufs:.1f} LUFS): normalizar (ajustar_audio normalizar=True → "
                                 f"-16 LUFS; YouTube usa -14)")
            elif lufs > -9:
                sugestoes.append(f"áudio muito alto ({lufs:.1f} LUFS): as plataformas vão abaixar e pode "
                                 f"distorcer; normalizar")
        if tp is not None and tp > -1.0:
            sugestoes.append(f"pico verdadeiro {tp:.1f} dBTP (acima de -1): risco de distorção ao recomprimir; "
                             f"normalizar com limitador")
        if any(f["codec"] not in ("aac", "opus", "mp3", "vorbis", "flac", "alac", "ac3", "eac3") and
               not str(f["codec"]).startswith("pcm") for f in faixas):
            sugestoes.append("codec de áudio pouco comum: exportar em AAC/Opus")
    elif vids:
        sugestoes.append("sem áudio")
    # ---- veredito
    ok_youtube = not problemas and bool(vids) and (fmt.get("format_name") or "").split(",")[0] in (
        "mov", "matroska", "avi", "mpegts", "flv", "webm")
    rec = []
    if vids:
        rec.append("ok para YouTube" if ok_youtube else "precisa de ajuste antes do YouTube: " + "; ".join(problemas))
    elif problemas:
        rec += problemas
    rec += sugestoes
    if vids and not sugestoes and not problemas:
        rec.append("nada a corrigir")
    rel["recomendacoes"] = rec
    rel["amostragem"] = ("bitrate/GOP/VFR em 8 janelas de 20 s; entrelaçamento e blocagem em 3×60 quadros"
                         if amostrado else "bitrate/GOP/VFR no arquivo inteiro; entrelaçamento e blocagem em "
                         f"{len(pos) * 60 if vids else 0} quadros")
    return rel
