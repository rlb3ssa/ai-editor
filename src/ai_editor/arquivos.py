"""Canivete suíço de ARQUIVOS, imagens e metadados (sem Qt; usado pelo `mcp_arquivos.py`).

Quadros em arquivo, folha de contatos, capa embutida, vídeo de imagens (slideshow), áudio → vídeo com
visualizador, metadados/privacidade (GPS, câmera, C2PA), remux sem perda e reparo, legendas em arquivo,
rotação sem reencodar e comparação de qualidade (VMAF/SSIM/PSNR).

Regras (as mesmas do `core.exportar`):
- o original do usuário nunca é tocado: ou cria uma VERSÃO do `Documento` (remux sem perda), ou um ARQUIVO NOVO
  ao lado do original, reservado com O_EXCL (`core._reservar`) e gravado num temporário oculto que só no fim
  substitui a reserva (`core._publicar`) — nunca sobrescreve nada;
- ffmpeg/ffprobe/exiftool/c2patool só pelos caminhos de `core`/`binarios`;
- `-threads 2`/`-filter_threads 2` (máquina com pouca memória e outras tarefas rodando).
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import binarios, core
from .core import Documento, ErroFFmpeg, fmt_tempo, parse_tempo

_LEVE = ["-filter_threads", "2", "-threads", "2"]  # opções de saída: limita encoder e filtros
_EXT_MP4 = (".mp4", ".mov", ".m4v", ".m4a")
_EXT_IMAGEM = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif", ".avif",
               ".jxl", ".jfif"}


# ================================================================ utilidades
def _rodar(cmd: list[str], dur: float = 0.0, progresso=None, base: float = 0.0, peso: float = 1.0) -> str:
    """Roda o ffmpeg com `-progress pipe:1` (progresso 0..1) e devolve o stderr. Morre junto com o processo
    pai (PR_SET_PDEATHSIG). Falha → ErroFFmpeg com o comando e o fim do stderr."""
    cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    with tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                                preexec_fn=core._morrer_com_pai if core._prctl is not None else None)
        try:
            for linha in proc.stdout:
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
        err.seek(0)
        texto = err.read()
    if proc.returncode != 0:
        raise ErroFFmpeg(f"comando falhou ({proc.returncode}): {shlex.join(cmd)}\n{texto[-4000:]}")
    if progresso:
        progresso(base + peso)
    return texto


def _arquivo_novo(final: Path, montar, durs: list[float], progresso=None) -> tuple[Path, list[list[str]]]:
    """Cria um ARQUIVO NOVO sem nunca sobrescrever: reserva `final` (ou nome.2.ext...) com O_EXCL, roda os
    comandos de montar(tmp) gravando num temporário oculto ao lado e publica no fim."""
    final = core._reservar(final)
    core._limpar_orfaos(final.parent)
    tmp = core._temporario(final)
    ok = False
    try:
        cmds = montar(tmp)
        n = len(cmds)
        for i, (c, d) in enumerate(zip(cmds, durs)):
            _rodar(c, d, progresso, i / n, 1 / n)
        final = core._publicar(tmp, final)
        ok = True
        return final, cmds
    finally:
        tmp.unlink(missing_ok=True)
        if not ok:
            core._soltar_reserva(final)


def _pasta_nova(original: Path, sufixo: str, destino=None) -> Path:
    """Pasta NOVA `nome.<sufixo>` ao lado do original (ou dentro de `destino`); existe → `nome.<sufixo>.2`..."""
    base = Path(str(destino)).expanduser() if destino else original.parent
    base.mkdir(parents=True, exist_ok=True)
    nome = f"{original.stem}.{sufixo}"
    i = 1
    while True:
        p = base / (nome if i == 1 else f"{nome}.{i}")
        try:
            p.mkdir()
            return p.resolve()
        except FileExistsError:
            i += 1


def _tempo(valor, est: dict, padrao: float) -> float:
    return padrao if valor is None else parse_tempo(valor, est)


def _janela(inicio, fim, est: dict, dur: float) -> tuple[float, float]:
    a = max(0.0, _tempo(inicio, est, 0.0))
    b = min(dur, _tempo(fim, est, dur))
    if b - a < 0.01:
        raise ValueError(f"intervalo vazio: {fmt_tempo(a)} → {fmt_tempo(b)}")
    return a, b


def _tem_filtro(nome: str) -> bool:
    lista = binarios._lista_ffmpeg("filters")
    return not lista or nome in lista


def _eh_capa(s: dict) -> bool:
    return s.get("codec_type") == "video" and bool((s.get("disposition") or {}).get("attached_pic"))


def _tags(s: dict) -> dict:
    return dict(s.get("tags") or {})


def _mapas(probe: dict, ext_saida: str, excluir=()) -> tuple[list[str], dict[int, int], list[str]]:
    """-map de cada fluxo da entrada 0, NA ORDEM, para um remux (-c copy). Fica de fora: `excluir`; a trilha de
    capítulos ('text') e o timecode ('tmcd') do MP4/MOV (o muxer recria; mapear duplicaria); fluxos de dados em
    contêiner que não os guarda; anexos fora do MKV. Devolve (args, índice entrada → índice saída, descartados)."""
    ext = ext_saida.lower()
    args: list[str] = []
    saida: dict[int, int] = {}
    fora: list[str] = []
    nomes: list[str] = []
    for s in probe.get("streams", []):
        i = int(s["index"])
        tipo = s.get("codec_type")
        if i in excluir:
            continue
        if tipo == "data":
            tag = str(s.get("codec_tag_string") or "").lower()
            if tag in ("text", "tmcd"):
                continue
            if ext not in _EXT_MP4 and ext not in (".ts", ".m2ts", ".mts"):
                fora.append(f"fluxo de dados #{i} ({s.get('codec_name') or tag})")
                continue
        if tipo == "attachment" and ext not in (".mkv", ".mka", ".mks"):
            fora.append(f"anexo #{i} ({_tags(s).get('filename') or s.get('codec_name')})")
            continue
        saida[i] = len(saida)
        args += ["-map", f"0:{i}"]
        t = _tags(s)
        if ext in _EXT_MP4 and t.get("name") and not t.get("title"):
            nomes += [f"-metadata:s:{saida[i]}", f"title={t['name']}"]  # o muxer MP4 só grava o nome a partir de 'title'
    return args + nomes, saida, fora


def _ordinal(probe: dict, rotulo: str) -> dict:
    """'A1' → 1º fluxo de áudio, 'V1' → 1º vídeo (sem capas), 'L1'/'S1' → 1ª legenda. Devolve o fluxo."""
    m = re.fullmatch(r"\s*([avls])\s*(\d+)\s*", str(rotulo), re.I)
    if not m:
        raise ValueError(f"faixa '{rotulo}': use A1, A2... (áudio), V1 (vídeo) ou L1, L2... (legenda)")
    tipo = {"a": "audio", "v": "video", "l": "subtitle", "s": "subtitle"}[m.group(1).lower()]
    lista = [s for s in probe.get("streams", []) if s.get("codec_type") == tipo and not _eh_capa(s)]
    n = int(m.group(2))
    if not 1 <= n <= len(lista):
        raise ValueError(f"faixa {rotulo.upper()} não existe (há {len(lista)} faixa(s) desse tipo)")
    return lista[n - 1]


def _iso639_2(idioma: str) -> str:
    from .fala import _ISO639_2
    s = str(idioma).strip().lower().replace("_", "-")
    if re.fullmatch(r"[a-z]{3}", s):
        return s
    base = s.split("-")[0]
    if base in _ISO639_2:
        return _ISO639_2[base]
    nomes = {"portugues": "por", "português": "por", "ingles": "eng", "inglês": "eng", "espanhol": "spa",
             "frances": "fra", "francês": "fra", "alemao": "deu", "alemão": "deu", "italiano": "ita",
             "japones": "jpn", "japonês": "jpn"}
    if s in nomes:
        return nomes[s]
    raise ValueError(f"idioma '{idioma}' não reconhecido: use o código ISO (pt, en, es... ou por, eng, spa...)")


def _resolucao(valor, padrao: tuple[int, int]) -> tuple[int, int]:
    """'1280x720' | '720p' | '1080p' | '4k' | proporção ('16:9', 'vertical', 'quadrado', '4:5'...) com o lado
    menor em 1080. Sempre pares."""
    if valor is None or str(valor).strip() == "":
        return padrao
    s = str(valor).strip().lower()
    nomes = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080), "full hd": (1920, 1080),
             "fullhd": (1920, 1080), "hd": (1280, 720), "4k": (3840, 2160), "2160p": (3840, 2160),
             "1440p": (2560, 1440)}
    if s in nomes:
        return nomes[s]
    m = re.fullmatch(r"(\d{2,5})\s*[x×*]\s*(\d{2,5})", s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    else:
        r, _rot = core.parse_proporcao(s)
        w, h = (round(1080 * r), 1080) if r >= 1 else (1080, round(1080 / r))
    if not (16 <= w <= 7680 and 16 <= h <= 7680):
        raise ValueError(f"resolução fora do razoável: {w}x{h}")
    return core._par(w), core._par(h)


def _cor(valor: str) -> str:
    """Cor para filtros do ffmpeg: nome ('white', 'branco'...) ou #RRGGBB → 0xRRGGBB."""
    pt = {"branco": "white", "preto": "black", "vermelho": "red", "verde": "green", "azul": "blue",
          "amarelo": "yellow", "laranja": "orange", "rosa": "pink", "roxo": "purple", "cinza": "gray",
          "ciano": "cyan", "dourado": "gold"}
    s = str(valor).strip()
    s = pt.get(s.lower(), s)
    if re.fullmatch(r"#?[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", s):
        return "0x" + s.lstrip("#")
    if not re.fullmatch(r"[A-Za-z]+(@[\d.]+)?", s):
        raise ValueError(f"cor inválida: {valor} (use um nome em inglês/português ou #RRGGBB)")
    return s


def _drawtext(texto: str, tamanho: int, x: str, y: str, cor: str = "white", caixa: str | None = None,
              borda: int = 0) -> str:
    op = [f"text={core.esc_filtro(texto)}", "expansion=none", f"fontsize={tamanho}",
          f"fontcolor={core.esc_filtro(cor)}", f"x={core.esc_filtro(x)}", f"y={core.esc_filtro(y)}"]
    fonte = core.fonte_arquivo()
    if fonte:
        op.insert(1, f"fontfile={core.esc_filtro(fonte)}")
    if caixa:
        op += ["box=1", f"boxcolor={core.esc_filtro(caixa)}", f"boxborderw={max(2, tamanho // 4)}"]
    if borda:
        op += [f"borderw={borda}", "bordercolor=black@0.8"]
    return "drawtext=" + ":".join(op)


def _exiftool_json(p: Path) -> dict:
    exe = core.ferramenta("exiftool")
    if not exe:
        return {}
    r = subprocess.run([exe, "-j", "-a", "-G1", "-s", "-api", "largefilesupport=1", str(p)], capture_output=True,
                       text=True, timeout=300)
    try:
        d = json.loads(r.stdout)
        return d[0] if d else {}
    except (ValueError, IndexError):
        return {}


def _c2pa_presente(p: Path) -> bool | None:
    """True/False se o c2patool consegue dizer; None sem c2patool."""
    exe = core.ferramenta("c2patool")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, str(p)], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0 and r.stdout.strip().startswith("{"):
        return True
    return False


# ================================================================ 1. quadros em arquivo
_FORMATOS_IMG = {"jpg": ("jpg", ["-q:v", "2"]), "jpeg": ("jpg", ["-q:v", "2"]), "png": ("png", ["-c:v", "png"]),
                 "webp": ("webp", ["-c:v", "libwebp", "-quality", "90"])}


def _formato_imagem(formato: str) -> tuple[str, list[str]]:
    f = str(formato or "jpg").lower().lstrip(".")
    if f not in _FORMATOS_IMG:
        raise ValueError("formato: 'jpg', 'png' ou 'webp'")
    ext, enc = _FORMATOS_IMG[f]
    if ext == "webp" and not core._tem_encoder("libwebp"):
        raise ValueError("este ffmpeg não tem o codificador WebP (libwebp); use formato='jpg' ou 'png'")
    return ext, enc


def extrair_quadros(arquivo: Path | str, original: Path | str | None = None, modo: str = "intervalo",
                    intervalo_s: float | None = None, tempos: list | None = None, formato: str = "jpg",
                    largura: int | None = None, inicio=None, fim=None, destino=None, limiar_cenas: float = 0.3,
                    maximo: int = 300, estado: dict | None = None, progresso=None) -> dict:
    """Salva quadros do vídeo como IMAGENS numa pasta NOVA `nome.quadros/` ao lado do original (ou em
    `destino`). modo: 'intervalo' (um a cada intervalo_s) | 'cenas' (um por cena, nos cortes de
    `detectar_cenas`) | 'tempos' (lista) | 'todos' (cada quadro). Nomes `quadro_0001_12.345s.jpg`."""
    arq = Path(arquivo)
    original = Path(original or arquivo)
    info = core._midia(arq)
    dur, fps = info["dur"], info["fps"]
    est = dict(estado or {})
    est.setdefault("duracao", dur)
    a, b = _janela(inicio, fim, est, dur)
    ext, enc = _formato_imagem(formato)
    maximo = int(maximo)
    if not 1 <= maximo <= 10000:
        raise ValueError("maximo entre 1 e 10000")
    modo = (modo or "intervalo").lower()
    vf = [info["pre"]] if info["pre"] else []
    if largura:
        vf.append(f"scale='min({int(largura)},iw)':-2:flags=lanczos")
    avisos: list[str] = []
    vezes: list[float] = []
    if modo in ("intervalo", "todos"):
        if modo == "intervalo":
            if not intervalo_s or float(intervalo_s) < 0.04:
                raise ValueError("intervalo_s (segundos, ≥ 0,04) é obrigatório no modo 'intervalo'")
            passo = float(intervalo_s)
            n_est = int((b - a) / passo + 1e-9) + 1
        else:
            passo = 0.0
            n_est = int(math.ceil((b - a) * fps))
        if n_est > maximo:
            raise ValueError(f"isso daria ~{n_est} imagens (máximo {maximo}); aumente intervalo_s, limite com "
                             f"inicio/fim ou passe maximo={min(10000, n_est)} (até 10000)")
    elif modo == "tempos":
        if not tempos:
            raise ValueError("passe `tempos` (lista) no modo 'tempos'")
        vezes = sorted({parse_tempo(t, est) for t in tempos})
        fora = [t for t in vezes if not 0 <= t <= dur + 1e-3]
        if fora:
            raise ValueError("tempo(s) fora do vídeo: " + ", ".join(fmt_tempo(t) for t in fora))
    elif modo == "cenas":
        r = core.detectar_cenas(arq, limiar=limiar_cenas, min_intervalo_s=1.0, inicio=a, fim=b, estado=est,
                                progresso=(lambda f: progresso(0.6 * f)) if progresso else None)
        cortes = [c["tempo"] for c in r["cortes"]]
        lim = [a] + cortes + [b]
        # um quadro um pouco DEPOIS do início de cada cena (o quadro do corte pode estar borrado)
        vezes = [t + min(0.25, (prox - t) / 2) for t, prox in zip(lim, lim[1:])]
    else:
        raise ValueError("modo: 'intervalo', 'cenas', 'tempos' ou 'todos'")
    if vezes and len(vezes) > maximo:
        raise ValueError(f"{len(vezes)} imagens passa do máximo ({maximo}); passe maximo={len(vezes)}")

    pasta = _pasta_nova(original, "quadros", destino)
    feitos: list[tuple[Path, float]] = []
    comandos: list[list[str]] = []
    try:
        if modo in ("intervalo", "todos"):
            log = pasta / ".tempos.txt"
            sel = []
            if modo == "intervalo":
                # o 1º quadro em ou depois de cada múltiplo de `passo` (sem deriva acumulada)
                sel.append(f"select='isnan(prev_t)+gt(floor((t+0.0005)/{passo:.6f}),"
                           f"floor((prev_t+0.0005)/{passo:.6f}))'".replace(",", "\\,"))
            sel += ["metadata=mode=add:key=aie:value=1",
                    f"metadata=mode=print:key=aie:file={core.esc_filtro(str(log))}"]
            padrao = str(pasta).replace("%", "%%") + f"/q%06d.{ext}"
            cmd = core.FFMPEG + (["-ss", f"{a:.6f}"] if a > 0 else []) + ["-t", f"{b - a:.6f}"] + info["dec"]
            cmd += ["-i", str(arq), "-map", "0:V:0", "-an", "-sn", "-dn", "-vf", ",".join(vf + sel),
                    "-fps_mode", "passthrough", "-frames:v", str(maximo)] + enc + _LEVE + [padrao]
            comandos.append(cmd)
            _rodar(cmd, b - a, progresso)
            ts = [float(m) for m in re.findall(r"pts_time:\s*(-?[\d.]+)", log.read_text(errors="replace"))]
            log.unlink(missing_ok=True)
            geradas = sorted(pasta.glob(f"q*.{ext}"))
            for i, g in enumerate(geradas):
                t = a + (ts[i] if i < len(ts) else i * passo)
                novo = pasta / f"quadro_{i + 1:0{max(4, len(str(len(geradas))))}d}_{t:.3f}s.{ext}"
                os.replace(g, novo)
                feitos.append((novo, t))
        else:
            base = 0.6 if modo == "cenas" else 0.0
            larg = max(4, len(str(len(vezes))))
            for i, t in enumerate(vezes):
                t = min(t, max(0.0, dur - 0.5 / (fps or 30)))
                novo = pasta / f"quadro_{i + 1:0{larg}d}_{t:.3f}s.{ext}"
                cmd = core.FFMPEG + ["-ss", f"{t:.6f}"] + info["dec"] + ["-i", str(arq), "-map", "0:V:0",
                                                                          "-frames:v", "1", "-update", "1"]
                if vf:
                    cmd += ["-vf", ",".join(vf)]
                cmd += enc + _LEVE + [str(novo)]
                comandos.append(cmd)
                try:
                    core.rodar(cmd)
                except ErroFFmpeg:
                    if novo.exists() and novo.stat().st_size:
                        raise
                    novo.unlink(missing_ok=True)  # nenhum quadro depois de t (fim do vídeo): tenta antes
                if not (novo.exists() and novo.stat().st_size):  # perto do fim: o último quadro decodificado
                    t = max(0.0, t - 1.0)
                    cmd2 = cmd[:cmd.index("-ss") + 1] + [f"{t:.6f}"] + cmd[cmd.index("-ss") + 2:cmd.index("-frames:v")] \
                        + cmd[cmd.index("-frames:v") + 2:]
                    core.rodar(cmd2)
                    if novo.exists() and novo.stat().st_size:
                        t = core.duracao(info["probe"]) - 1 / (fps or 30)
                        final_n = pasta / f"quadro_{i + 1:0{larg}d}_{t:.3f}s.{ext}"
                        os.replace(novo, final_n)
                        novo = final_n
                if novo.exists() and novo.stat().st_size:
                    feitos.append((novo, t))
                else:
                    novo.unlink(missing_ok=True)
                    avisos.append(f"sem quadro em {fmt_tempo(t)}")
                if progresso:
                    progresso(base + (1 - base) * (i + 1) / len(vezes))
    except BaseException:
        shutil.rmtree(pasta, ignore_errors=True)
        raise
    if not feitos:
        shutil.rmtree(pasta, ignore_errors=True)
        raise ValueError("nenhum quadro extraído (trecho vazio?)")
    total_mb = sum(p.stat().st_size for p, _ in feitos) / core._MB
    r = {"pasta": str(pasta), "quantidade": len(feitos), "formato": ext, "modo": modo,
         "trecho": [fmt_tempo(a), fmt_tempo(b)], "tamanho_total_mb": round(total_mb, 2),
         "arquivos": [p.name for p, _ in feitos[:20]] + ([f"... (+{len(feitos) - 20})"] if len(feitos) > 20 else []),
         "primeiro": str(feitos[0][0]), "comando_exemplo": shlex.join(comandos[0])}
    if avisos:
        r["avisos"] = avisos
    return r


# ================================================================ 2. folha de contatos
def folha_contatos(arquivo: Path | str, original: Path | str | None = None, colunas: int = 4, linhas: int = 4,
                   largura: int = 1920, com_tempos: bool = True, cabecalho: bool = True, inicio=None, fim=None,
                   formato: str = "jpg", cor_fundo: str = "#16181d", destino=None, estado: dict | None = None,
                   progresso=None) -> dict:
    """UMA imagem com colunas×linhas miniaturas espaçadas igualmente pelo vídeo (tempo no canto de cada uma e
    cabeçalho com nome, duração, resolução, codecs e tamanho). Arquivo novo `nome.folha.jpg`."""
    arq = Path(arquivo)
    original = Path(original or arquivo)
    colunas, linhas, largura = int(colunas), int(linhas), int(largura)
    if not (1 <= colunas <= 12 and 1 <= linhas <= 20 and colunas * linhas <= 120):
        raise ValueError("colunas 1–12, linhas 1–20, no máximo 120 miniaturas")
    if not 320 <= largura <= 7680:
        raise ValueError("largura da folha entre 320 e 7680 px")
    ext, enc = _formato_imagem(formato)
    if ext == "jpg":
        enc = ["-q:v", "3"]
    info = core._midia(arq)
    dur = info["dur"]
    est = dict(estado or {})
    est.setdefault("duracao", dur)
    a, b = _janela(inicio, fim, est, dur)
    n = colunas * linhas
    margem = max(4, round(largura * 0.005))
    tw = core._par((largura - margem * (colunas + 1)) / colunas)
    th = core._par(tw * info["h"] / info["w"])
    fundo = _cor(cor_fundo)
    texto_ok = _tem_filtro("drawtext") and core.fonte_arquivo() is not None
    avisos = []
    if (com_tempos or cabecalho) and not texto_ok:
        avisos.append("sem o filtro drawtext/fonte neste ffmpeg: folha sem textos")
    vezes = [a + (b - a) * (i + 0.5) / n for i in range(n)]
    tmp = Path(tempfile.mkdtemp(prefix=".folha-", dir=core.RUNTIME_DIR if core.RUNTIME_DIR.exists() else None))
    comandos: list[list[str]] = []
    try:
        for i, t in enumerate(vezes):
            cadeia = ([info["pre"]] if info["pre"] else []) + [
                f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos",
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color={fundo}", "setsar=1"]
            if com_tempos and texto_ok:
                fs = max(10, round(min(tw * 0.07, th * 0.11)))
                cadeia.append(_drawtext(fmt_tempo(t).lstrip("0:") if t >= 60 else fmt_tempo(t)[3:], fs,
                                        "w-tw-6", "h-th-6", "white", "black@0.6"))
            q = tmp / f"{i:03d}.png"
            cmd = core.FFMPEG + ["-ss", f"{t:.6f}"] + info["dec"] + ["-i", str(arq), "-map", "0:V:0",
                                                                      "-frames:v", "1", "-update", "1",
                                                                      "-vf", ",".join(cadeia)] + _LEVE + [str(q)]
            comandos.append(cmd)
            core.rodar(cmd)
            if not q.exists() or not q.stat().st_size:  # perto do fim pode não sair quadro: repete o anterior
                if i == 0:
                    raise ErroFFmpeg("não consegui extrair o primeiro quadro")
                shutil.copyfile(tmp / f"{i - 1:03d}.png", q)
            if progresso:
                progresso(0.9 * (i + 1) / n)
        vf = [f"tile={colunas}x{linhas}:margin={margem}:padding={margem}:color={fundo}"]
        if cabecalho and texto_ok:
            pr = info["probe"]
            v = info["v"]
            auds = info["auds"]
            tam = arq.stat().st_size / core._MB
            hh = max(40, round(largura * 0.042))
            det = (f"{fmt_tempo(dur)}  ·  {info['w']}x{info['h']}  ·  {info['fps']:.3g} fps  ·  "
                   f"{v.get('codec_name')}{'/' + auds[0].get('codec_name') if auds else ' (sem som)'}  ·  "
                   f"{tam:.1f} MB")
            if a > 0.01 or b < dur - 0.01:
                det += f"  ·  trecho {fmt_tempo(a)}–{fmt_tempo(b)}"
            vf += [f"pad=iw:ih+{hh}:0:{hh}:color={fundo}",
                   _drawtext(original.name, max(12, round(hh * 0.36)), str(margem * 2), str(round(hh * 0.12)),
                             "white"),
                   _drawtext(det, max(10, round(hh * 0.25)), str(margem * 2), str(round(hh * 0.6)), "0xB0B6C0")]
            _ = pr

        def montar(saida: Path):
            cmd = core.FFMPEG + ["-framerate", "1", "-i", str(tmp / "%03d.png"), "-vf", ",".join(vf),
                                 "-frames:v", "1", "-update", "1"] + enc + _LEVE + [str(saida)]
            comandos.append(cmd)
            return [cmd]

        final, _cmds = _arquivo_novo(core._caminho(original, "folha", "." + ext, destino, criar=True), montar, [1.0])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if progresso:
        progresso(1.0)
    pr = core.ffprobe(final)
    s = pr["streams"][0]
    r = {"arquivo": str(final), "resolucao": f"{s.get('width')}x{s.get('height')}",
         "tamanho_mb": round(final.stat().st_size / core._MB, 2), "miniaturas": n,
         "grade": f"{colunas}x{linhas}", "tempos": [fmt_tempo(t) for t in vezes],
         "comando_montagem": shlex.join(comandos[-1])}
    if avisos:
        r["avisos"] = avisos
    return r


# ================================================================ 3. capa embutida
_EXT_CAPA = {".mp4", ".m4a", ".m4v", ".mov", ".mp3", ".mkv", ".mka", ".flac"}


def _preparar_imagem_capa(origem: Path, destino_tmp: Path, ss: float | None = None, dec=None, pre: str = "") -> Path:
    """JPEG/PNG pronto para virar capa: imagem do usuário (JPEG/PNG até 1920 px copiados como estão) ou um quadro
    do vídeo em `ss`. Outros formatos/tamanhos viram JPEG."""
    if ss is None:
        pr = core.ffprobe(origem)
        vs = [s for s in pr.get("streams", []) if s.get("codec_type") == "video"]
        if not vs:
            raise ValueError(f"não é uma imagem: {origem}")
        s = vs[0]
        if s.get("codec_name") in ("mjpeg", "png") and max(int(s.get("width") or 0), int(s.get("height") or 0)) <= 1920:
            alvo = destino_tmp.with_suffix(".jpg" if s["codec_name"] == "mjpeg" else ".png")
            shutil.copyfile(origem, alvo)
            return alvo
        alvo = destino_tmp.with_suffix(".jpg")
        core.rodar(core.FFMPEG + ["-i", str(origem), "-frames:v", "1", "-update", "1", "-vf",
                                  "scale='min(1920,iw)':-2:flags=lanczos", "-q:v", "2", str(alvo)])
        return alvo
    alvo = destino_tmp.with_suffix(".jpg")
    vf = ([pre] if pre else []) + ["scale='min(1920,iw)':-2:flags=lanczos"]
    core.rodar(core.FFMPEG + ["-ss", f"{ss:.6f}"] + list(dec or []) + ["-i", str(origem), "-map", "0:V:0",
               "-frames:v", "1", "-update", "1", "-vf", ",".join(vf), "-q:v", "2", str(alvo)])
    if not alvo.exists() or not alvo.stat().st_size:
        raise ErroFFmpeg(f"não saiu quadro em {fmt_tempo(ss)}")
    return alvo


def capa(doc: Documento, acao: str = "extrair", tempo=None, arquivo_imagem: str | Path | None = None,
         destino=None, estado: dict | None = None, progresso=None) -> dict:
    """Capa embutida (attached_pic / thumbnail) de MP4/M4A/MOV/MP3/MKV/FLAC.
    acao 'extrair' → arquivo novo `nome.capa.jpg`; 'definir' (tempo OU arquivo_imagem) e 'remover' → nova
    versão, sem reencodar áudio/vídeo."""
    entrada = doc.atual()
    ext = entrada.suffix.lower()
    probe = core.ffprobe(entrada)
    capas = [s for s in probe["streams"] if _eh_capa(s)]
    acao = (acao or "extrair").lower()
    if acao == "extrair":
        if not capas:
            raise ValueError("este arquivo não tem capa embutida. Para salvar um quadro do vídeo como foto use "
                             "exportar(predefinicao='jpg', tempo=...); para criar uma capa, capa(acao='definir')")
        s = capas[0]
        ext_c = {"mjpeg": ".jpg", "png": ".png", "bmp": ".bmp", "webp": ".webp"}.get(s.get("codec_name"), ".png")
        copia = ext_c != ".png" or s.get("codec_name") == "png"

        def montar(saida: Path):
            return [core.FFMPEG + ["-i", str(entrada), "-map", f"0:{s['index']}", "-frames:v", "1", "-update", "1"]
                    + (["-c", "copy"] if copia else []) + [str(saida)]]

        final, cmds = _arquivo_novo(core._caminho(doc.original, "capa", ext_c, destino, criar=True), montar, [1.0])
        return {"arquivo": str(final), "resolucao": f"{s.get('width')}x{s.get('height')}",
                "codec": s.get("codec_name"), "capas_no_arquivo": len(capas), "comando": shlex.join(cmds[0]),
                "lembrete": "arquivo novo; o vídeo não foi alterado"}
    if acao not in ("definir", "remover"):
        raise ValueError("acao: 'extrair', 'definir' ou 'remover'")
    if ext not in _EXT_CAPA:
        raise ValueError(f"o formato {ext} não guarda capa embutida; converta antes para .mp4/.m4a/.mkv/.mp3 "
                         f"(converter_sem_perda) ")
    excluir = {int(s["index"]) for s in capas}
    if acao == "remover" and not capas:
        raise ValueError("este arquivo não tem capa embutida (nada a remover)")
    saida = doc.proxima_saida(ext)
    img = None
    try:
        maps, idx, _fora = _mapas(probe, ext, excluir)
        cmd = core.FFMPEG + ["-i", str(entrada)]
        extra: list[str] = []
        if acao == "definir":
            if (tempo is None) == (arquivo_imagem is None):
                raise ValueError("passe tempo (quadro do vídeo) OU arquivo_imagem")
            tmp = Path(tempfile.mkstemp(prefix="capa-", dir=doc.pasta)[1])
            tmp.unlink()
            if arquivo_imagem is not None:
                p = Path(str(arquivo_imagem)).expanduser().resolve()
                if not p.is_file():
                    raise FileNotFoundError(f"imagem não encontrada: {p}")
                img = _preparar_imagem_capa(p, tmp)
            else:
                info = core._midia(entrada)
                est = dict(estado or {})
                est.setdefault("duracao", info["dur"])
                t = parse_tempo(tempo, est)
                if not 0 <= t <= info["dur"]:
                    raise ValueError(f"tempo fora do vídeo: {fmt_tempo(t)}")
                img = _preparar_imagem_capa(entrada, tmp, min(t, max(0.0, info["dur"] - 0.5 / info["fps"])),
                                            info["dec"], info["pre"])
            nv = sum(1 for i in idx if probe["streams"][i].get("codec_type") == "video")
            nt = sum(1 for i in idx if probe["streams"][i].get("codec_type") == "attachment")
            if ext in (".mkv", ".mka"):  # Matroska: capa = anexo cover.jpg/png (o que players e o ffprobe entendem)
                extra = ["-attach", str(img), f"-metadata:s:t:{nt}", f"filename=cover{img.suffix}",
                         f"-metadata:s:t:{nt}", f"mimetype=image/{'jpeg' if img.suffix == '.jpg' else 'png'}"]
            else:
                cmd += ["-i", str(img)]
                extra = ["-map", "1:0", f"-disposition:v:{nv}", "attached_pic"]
            if ext == ".mp3":
                extra += ["-id3v2_version", "3", f"-metadata:s:v:{nv}", "title=Album cover",
                          f"-metadata:s:v:{nv}", "comment=Cover (front)"]
            elif ext == ".flac":
                extra += [f"-metadata:s:v:{nv}", "comment=Cover (front)"]
        cmd += maps + extra + ["-c", "copy", "-map_metadata", "0", "-map_chapters", "0"] + core._faststart(saida)
        cmd.append(str(saida))
        desc = ("capa definida (" + (f"quadro em {fmt_tempo(parse_tempo(tempo, dict(estado or {}, duracao=core.duracao(probe))))}"
                                     if tempo is not None else Path(str(arquivo_imagem)).name) + ")"
                if acao == "definir" else "capa removida")
        v = core._rodar_versao(doc, saida, [cmd], [core.duracao(probe)], desc, progresso)
    finally:
        if img is not None:
            img.unlink(missing_ok=True)
    depois = [s for s in core.ffprobe(saida)["streams"] if _eh_capa(s)]
    if (acao == "definir" and len(depois) != 1) or (acao == "remover" and depois):
        doc.desfazer()
        raise ErroFFmpeg(f"a capa não ficou no arquivo (capas depois: {len(depois)})")
    v = dict(v)
    v["detalhes"] = {"capas_antes": len(capas), "capas_depois": len(depois)}
    return v


# ================================================================ 4. vídeo de imagens (slideshow)
def _ordem_natural(p: Path):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", p.name)]


def listar_imagens(imagens: list | None = None, pasta: str | Path | None = None) -> list[Path]:
    if (not imagens) == (not pasta):
        raise ValueError("passe `imagens` (lista de arquivos) OU `pasta`")
    if pasta:
        d = Path(str(pasta)).expanduser().resolve()
        if not d.is_dir():
            raise FileNotFoundError(f"pasta não encontrada: {d}")
        lista = sorted((p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _EXT_IMAGEM
                        and not p.name.startswith(".")), key=_ordem_natural)
        if not lista:
            raise ValueError(f"nenhuma imagem (jpg, png, webp...) em {d}")
        return lista
    out = []
    for i in imagens:
        p = Path(str(i)).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"imagem não encontrada: {p}")
        out.append(p)
    return out


_TRANSICOES_XFADE = {"fade", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown", "slideleft",
                     "slideright", "slideup", "slidedown", "circleopen", "circleclose", "radial", "smoothleft",
                     "smoothright", "fadeblack", "fadewhite", "distance", "pixelize", "zoomin", "hblur"}


def _normalizar_imagem(img: Path, saida: Path, w: int, h: int, fundo: str, escala_kb: int) -> list[str]:
    """Comando que põe a imagem em w×h (sem distorcer) num PNG: fundo 'desfocado' (a própria imagem ampliada e
    borrada atrás), 'recorte' (preenche cortando) ou uma cor (barras). Com Ken Burns sai `escala_kb`× maior."""
    if fundo == "desfocado":
        g = (f"[0:v]setsar=1,split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
             f"boxblur=luma_radius=min(h\\,w)/20:luma_power=2,eq=brightness=-0.06[bg];"
             f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    elif fundo == "recorte":
        g = f"[0:v]setsar=1,scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}"
    else:
        g = (f"[0:v]setsar=1,scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={fundo}")
    if escala_kb > 1:
        g += f",scale={w * escala_kb}:{h * escala_kb}:flags=bicubic"
    g += ",format=rgb24[o]"
    return core.FFMPEG + ["-i", str(img), "-filter_complex", g, "-map", "[o]", "-frames:v", "1", "-update", "1"] \
        + _LEVE + [str(saida)]


def _clipe_imagem(entrada: int, quadros: int, fps: int, w: int, h: int, kb: bool, desloc: int, total: int,
                  sentido: int) -> str:
    """Cadeia de filtro de UMA imagem (já normalizada) → `quadros` quadros. Ken Burns: zoom contínuo de 1 → 1,12
    (sentido 1) ou de 1,12 → 1 (sentido -1) ao longo de `total` quadros, começando no quadro `desloc` (a mesma
    imagem aparece em dois segmentos quando há transição)."""
    if kb:
        k = 0.12 / max(1, total - 1)
        z = f"1+{k:.8f}*(on+{desloc})" if sentido > 0 else f"1.12-{k:.8f}*(on+{desloc})"
        return (f"[{entrada}:v]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={quadros}:"
                f"s={w}x{h}:fps={fps},setsar=1,format=yuv420p")
    return f"[{entrada}:v]loop=loop={quadros}:size=1:start=0,setpts=N/({fps}*TB),setsar=1,format=yuv420p"  # 1 a mais: -frames:v corta


def video_de_imagens(imagens: list | None = None, pasta: str | Path | None = None,
                     duracao_cada_s: float | None = None, transicao: str = "crossfade",
                     duracao_transicao: float = 0.8, audio: str | Path | None = None, resolucao=None,
                     fundo: str = "desfocado", ken_burns: bool = False, fps: int = 30, destino=None,
                     progresso=None) -> dict:
    """Slideshow: ARQUIVO NOVO .mp4 (H.264 + AAC) com as imagens na ordem. Ver docstring da ferramenta MCP."""
    lista = listar_imagens(imagens, pasta)
    n = len(lista)
    if n > 500:
        raise ValueError(f"{n} imagens é demais (máximo 500)")
    fps = int(fps)
    if not 10 <= fps <= 60:
        raise ValueError("fps entre 10 e 60")
    w, h = _resolucao(resolucao, (1920, 1080))
    fundo_n = str(fundo or "desfocado").strip().lower()
    if fundo_n in ("desfocado", "blur", "borrado"):
        fundo_n = "desfocado"
    elif fundo_n in ("recorte", "preencher", "cortar"):
        fundo_n = "recorte"
    else:
        fundo_n = _cor(fundo_n)
    tr = str(transicao or "corte").strip().lower()
    tr = {"nenhuma": "corte", "seco": "corte", "": "corte", "crossfade": "dissolve", "mistura": "dissolve",
          "fade_preto": "fade", "preto": "fade"}.get(tr, tr)
    if tr not in ("corte", "fade") and tr not in _TRANSICOES_XFADE:
        raise ValueError("transicao: 'corte', 'fade' (escurece e clareia), 'crossfade' (uma imagem se mistura "
                         "na outra) ou um efeito do xfade: " + ", ".join(sorted(_TRANSICOES_XFADE)))
    if tr not in ("corte", "fade") and not _tem_filtro("xfade"):
        raise ValueError("este ffmpeg não tem o filtro xfade; use transicao='fade' ou 'corte'")
    if ken_burns and not _tem_filtro("zoompan"):
        raise ValueError("este ffmpeg não tem o filtro zoompan (Ken Burns)")
    aud = None
    dur_audio = None
    avisos: list[str] = []
    if audio:
        aud = Path(str(audio)).expanduser().resolve()
        if not aud.is_file():
            raise FileNotFoundError(f"áudio não encontrado: {aud}")
        pa = core.ffprobe(aud)
        if not core.faixas_audio(pa):
            raise ValueError(f"o arquivo não tem áudio: {aud.name}")
        dur_audio = core.duracao(pa)
    td = float(duracao_transicao or 0) if tr != "corte" else 0.0
    xf = tr not in ("corte", "fade")
    if duracao_cada_s is None:
        if dur_audio:  # distribui as imagens pela duração da música
            d = (dur_audio + (n - 1) * td) / n if xf else dur_audio / n
            if d < 0.5:
                raise ValueError(f"{n} imagens em {dur_audio:.1f} s de áudio dá {d:.2f} s por imagem; "
                                 f"use menos imagens ou passe duracao_cada_s")
        else:
            d = 3.0
    else:
        d = float(duracao_cada_s)
    if not 0.2 <= d <= 600:
        raise ValueError("duracao_cada_s entre 0,2 e 600 segundos")
    f_img = max(1, round(d * fps))
    t_q = round(td * fps)
    if xf and n > 1:
        lim = (f_img - 2) // 2
        if t_q > lim:
            t_q = max(1, lim)
            avisos.append(f"transição encurtada para {t_q / fps:.2f} s (cada imagem dura {d:.2f} s)")
    if tr == "fade":
        t_q = min(t_q, f_img // 2)
    if xf and n > 1 and f_img - 2 * t_q < 1:
        raise ValueError("imagens curtas demais para a transição; aumente duracao_cada_s")
    escala_kb = 2 if ken_burns else 1  # zoompan arredonda x/y em pixels: ampliar 2x antes evita o tremido

    if destino and core._ext_destino(destino):
        if core._ext_destino(destino).lower() != ".mp4":
            raise ValueError("o slideshow sai em .mp4")
        final_pedido = Path(str(destino)).expanduser()
        final_pedido.parent.mkdir(parents=True, exist_ok=True)
    else:
        pasta_saida = Path(str(destino)).expanduser() if destino else lista[0].parent
        pasta_saida.mkdir(parents=True, exist_ok=True)
        final_pedido = pasta_saida / "apresentacao.mp4"

    trab = Path(tempfile.mkdtemp(prefix=".slideshow-", dir=final_pedido.parent))
    comandos: list[list[str]] = []
    try:
        norm = []
        for i, img in enumerate(lista):
            o = trab / f"n{i:04d}.png"
            cmd = _normalizar_imagem(img, o, w, h, fundo_n, escala_kb)
            try:
                core.rodar(cmd)
            except ErroFFmpeg as e:
                raise ValueError(f"não consegui ler a imagem {img.name} (formato não suportado por este ffmpeg?)"
                                 f"\n{str(e)[-400:]}") from e
            if i == 0:
                comandos.append(cmd)
            norm.append(o)
            if progresso:
                progresso(0.25 * (i + 1) / n)
        segs = []
        x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps),
                "-g", str(fps * 2), "-bf", "0", "-video_track_timescale", str(fps * 1000)]
        tot_q = 0
        for i in range(n):
            sentido = 1 if i % 2 == 0 else -1
            if xf and n > 1:
                ent = t_q if i > 0 else 0
                sai = t_q if i < n - 1 else 0
                sozinho = f_img - ent - sai
                q = sozinho + sai
                g = _clipe_imagem(0, q, fps, w, h, ken_burns, ent, f_img, sentido) + "[a]"
                ins = ["-i", str(norm[i])]
                if sai:
                    g += ";" + _clipe_imagem(1, t_q, fps, w, h, ken_burns, 0, f_img, -sentido) + "[b]"
                    g += f";[a][b]xfade=transition={tr}:duration={t_q / fps:.6f}:offset={sozinho / fps:.6f}[v]"
                    ins += ["-i", str(norm[i + 1])]
                else:
                    g = g[:-3] + "[v]"
            else:
                q = f_img
                g = _clipe_imagem(0, q, fps, w, h, ken_burns, 0, f_img, sentido)
                if tr == "fade" and t_q:
                    g += (f",fade=t=in:st=0:d={t_q / fps:.6f},"
                          f"fade=t=out:st={(q - t_q) / fps:.6f}:d={t_q / fps:.6f}")
                g += "[v]"
                ins = ["-i", str(norm[i])]
            s = trab / f"s{i:04d}.mp4"
            cmd = core.FFMPEG + ins + ["-filter_complex", g, "-map", "[v]", "-frames:v", str(q)] + x264 + _LEVE \
                + [str(s)]
            if i < 2:
                comandos.append(cmd)
            _rodar(cmd, q / fps, progresso, 0.25 + 0.65 * i / n, 0.65 / n)
            segs.append(s)
            tot_q += q
        total = tot_q / fps
        lista_txt = trab / "lista.txt"
        lista_txt.write_text("".join(f"file {core.esc_concat(s)}\n" for s in segs), encoding="utf-8")

        def montar(saida: Path):
            cmd = core.FFMPEG + ["-f", "concat", "-safe", "0", "-i", str(lista_txt)]
            if aud:
                cmd += ["-i", str(aud), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
                if dur_audio and dur_audio > total + 0.05:
                    fo = min(2.0, total / 4)
                    cmd += ["-af", f"afade=t=out:st={total - fo:.3f}:d={fo:.3f}"]
                cmd += ["-t", f"{total:.6f}"]
            else:
                cmd += ["-map", "0:v:0", "-c:v", "copy"]
            cmd += ["-movflags", "+faststart", str(saida)]
            comandos.append(cmd)
            return [cmd]

        final, _ = _arquivo_novo(final_pedido, montar, [total],
                                 (lambda f: progresso(0.9 + 0.1 * f)) if progresso else None)
    finally:
        shutil.rmtree(trab, ignore_errors=True)
    if dur_audio:
        if dur_audio > total + 0.05:
            avisos.append(f"o áudio ({fmt_tempo(dur_audio)}) é mais longo que as imagens ({fmt_tempo(total)}): foi "
                          f"cortado no fim com fade (sem duracao_cada_s as imagens se ajustam à música)")
        elif dur_audio < total - 0.05:
            avisos.append(f"o áudio ({fmt_tempo(dur_audio)}) acaba antes das imagens ({fmt_tempo(total)}): o fim "
                          f"fica em silêncio")
    r = core.info_arquivo(final)
    r.update({"imagens": n, "duracao_cada_s": round(f_img / fps, 3), "transicao": tr,
              "duracao_transicao_s": round(t_q / fps, 3) if tr != "corte" else 0, "ken_burns": bool(ken_burns),
              "fundo": fundo_n, "comandos_exemplo": [shlex.join(c) for c in comandos[:3]] + [shlex.join(comandos[-1])]})
    if avisos:
        r["avisos"] = avisos
    return r


# ================================================================ 5. áudio → vídeo com visualizador
_ESTILOS_AUDIO = ("ondas", "espectro", "barras", "cqt", "imagem")


def audio_para_video(arquivo: Path | str, original: Path | str | None = None, estilo: str = "ondas",
                     imagem: str | Path | None = None, cor: str = "#4FC3F7", fundo: str = "#101418",
                     resolucao=None, titulo_texto: str | None = None, faixa: int = 1, fps: int = 30,
                     destino=None, progresso=None) -> dict:
    """Vídeo .mp4 NOVO a partir do áudio (podcast/música → YouTube/redes): visualizador animado ou imagem
    parada, com título opcional. O áudio é copiado se já for AAC (senão vira AAC 192k)."""
    arq = Path(arquivo)
    original = Path(original or arquivo)
    estilo = str(estilo or "ondas").lower()
    estilo = {"onda": "ondas", "waveform": "ondas", "spectrum": "espectro", "espectrograma": "espectro",
              "bars": "barras", "frequencias": "barras", "foto": "imagem", "estatica": "imagem",
              "capa": "imagem"}.get(estilo, estilo)
    if estilo not in _ESTILOS_AUDIO:
        raise ValueError("estilo: 'ondas', 'espectro', 'barras', 'cqt' ou 'imagem'")
    filtro = {"ondas": "showwaves", "espectro": "showspectrum", "barras": "showfreqs", "cqt": "showcqt"}.get(estilo)
    if filtro and not _tem_filtro(filtro):
        raise ValueError(f"este ffmpeg não tem o filtro {filtro}; use outro estilo")
    probe = core.ffprobe(arq)
    auds = core.faixas_audio(probe)
    if not auds:
        raise ValueError(f"o arquivo não tem áudio: {arq.name}")
    k = core._faixa_valida(faixa, len(auds))
    a_st = auds[k]
    dur = core.duracao(probe)
    w, h = _resolucao(resolucao, (1280, 720))
    fps = 24 if estilo == "imagem" else int(fps)
    cor_v = _cor(cor)
    fundo_c = _cor(fundo)
    avisos: list[str] = []
    entradas = ["-i", str(arq)]
    img_idx = None
    capa_emb = next((s for s in probe["streams"] if _eh_capa(s)), None)
    if imagem:
        p = Path(str(imagem)).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"imagem não encontrada: {p}")
        entradas += ["-i", str(p)]
        img_idx = "1:v:0"
    elif estilo == "imagem":
        if capa_emb is None:
            raise ValueError("estilo 'imagem' precisa de `imagem` (ou de uma capa embutida no áudio)")
        img_idx = f"0:{capa_emb['index']}"
        avisos.append("usada a capa embutida do arquivo como imagem")
    g: list[str] = []
    if img_idx:
        g.append(f"[{img_idx}]setsar=1,split[ia][ib];"
                 f"[ia]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                 f"boxblur=luma_radius=min(h\\,w)/20:luma_power=2,eq=brightness=-0.12[ibg];"
                 f"[ib]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[ifg];"
                 f"[ibg][ifg]overlay=(W-w)/2:(H-h)/2,format=yuv420p,loop=loop=-1:size=1,"
                 f"fps={fps},trim=duration={dur:.6f},setpts=N/({fps}*TB)[bg0]")
        if estilo != "imagem":  # visualizador por cima da imagem: escurece um pouco
            g.append("[bg0]eq=brightness=-0.08[bg]")
        else:
            g.append("[bg0]null[bg]")
    else:
        g.append(f"color=c={fundo_c}:s={w}x{h}:r={fps}:d={dur:.6f},format=yuv420p[bg]")
    tem_titulo = bool(titulo_texto and str(titulo_texto).strip())
    texto_ok = _tem_filtro("drawtext") and core.fonte_arquivo() is not None
    if tem_titulo and not texto_ok:
        avisos.append("sem o filtro drawtext/fonte neste ffmpeg: vídeo sem o título")
        tem_titulo = False
    if estilo != "imagem":
        a = f"[0:{a_st['index']}]"
        if estilo == "ondas":
            vh = core._par(h * 0.38)
            g.append(f"{a}aformat=channel_layouts=mono,showwaves=s={w}x{vh}:mode=cline:rate={fps}:"
                     f"colors={cor_v}:scale=sqrt:draw=full,format=rgba[viz]")
            y = f"(H-h)/2+{round(h * 0.06) if tem_titulo else 0}"
        elif estilo == "espectro":
            vh = core._par(h * (0.62 if tem_titulo else 0.8))
            g.append(f"{a}showspectrum=s={w}x{vh}:mode=combined:slide=scroll:color=intensity:scale=cbrt:"
                     f"fscale=log:legend=0,fps={fps},format=rgba[viz]")
            y = "H-h"
        elif estilo == "barras":
            vh = core._par(h * 0.5)
            g.append(f"{a}aformat=channel_layouts=mono,showfreqs=s={w}x{vh}:mode=bar:ascale=log:fscale=log:"
                     f"win_size=2048:colors={cor_v},fps={fps},format=rgba[viz]")
            y = f"H-h-{round(h * 0.08)}"
        else:  # cqt
            vh = core._par(h * (0.75 if tem_titulo else 1.0))
            g.append(f"{a}showcqt=s={w}x{vh}:fps={fps}:axis=0:count=4,format=rgba[viz]")
            y = "H-h"
        g.append(f"[bg][viz]overlay=x=(W-w)/2:y={y}:shortest=1:format=auto[comp]")
        ultimo = "comp"
    else:
        ultimo = "bg"
    if tem_titulo:
        txt = str(titulo_texto).replace("\r\n", "\n").replace("\\n", "\n")
        fs = max(14, round(h / 14))
        maior = max(len(ln) for ln in txt.split("\n"))
        if maior * 0.58 * fs > 0.9 * w:
            fs = max(12, int(0.9 * w / (0.58 * maior)))
        y = "(h-text_h)/2" if estilo == "imagem" and not img_idx else str(round(h * 0.07))
        g.append(f"[{ultimo}]" + _drawtext(txt, fs, "(w-text_w)/2", y, "white", "black@0.45" if img_idx else None,
                                           0 if img_idx else max(1, fs // 24)) + "[vo]")
    else:
        g.append(f"[{ultimo}]null[vo]")
    copia_aac = a_st.get("codec_name") == "aac"
    sufixo = {"imagem": "imagem"}.get(estilo, estilo)

    def montar(saida: Path):
        cmd = core.FFMPEG + entradas + ["-filter_complex", ";".join(g), "-map", "[vo]", "-map", f"0:{a_st['index']}",
                                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                                        "-r", str(fps)]
        if estilo == "imagem":
            cmd += ["-tune", "stillimage"]
        cmd += (["-c:a", "copy"] if copia_aac else ["-c:a", "aac", "-b:a", "192k"])
        cmd += ["-t", f"{dur:.6f}", "-map_metadata", "0", "-movflags", "+faststart"] + _LEVE + [str(saida)]
        return [cmd]

    final, cmds = _arquivo_novo(core._caminho(original, sufixo, ".mp4", destino, criar=True), montar, [dur],
                                progresso)
    r = core.info_arquivo(final)
    r.update({"estilo": estilo, "resolucao_pedida": f"{w}x{h}", "audio": "copiado (AAC)" if copia_aac
              else "convertido para AAC 192k", "comando": shlex.join(cmds[0])})
    if avisos:
        r["avisos"] = avisos
    return r


# ================================================================ 6. metadados e privacidade
_RX_LOCAL_FF = re.compile(r"location|iso6709|gps|latitude|longitude|xyz", re.I)
_RX_CAMERA_FF = re.compile(r"(^|\.)(make|model|software|manufacturer|serial|firmware|lens|camera|device|"
                           r"android\.version|capture\.fps)|com\.android\.|com\.apple\.(quicktime\.(make|model|"
                           r"software|camera)|photos)", re.I)
_RX_LOCAL_EX = re.compile(r"GPS|Location|ISO6709|^City$|^Country|^State$|^Province|Sub-?location", re.I)
_RX_CAMERA_EX = re.compile(r"^(Make|Model|.*SerialNumber|Lens(Make|Model|Info|ID|SerialNumber)?|Software|"
                           r"HostComputer|CameraModelName|Firmware.*|OwnerName|CameraOwnerName|UniqueCameraModel|"
                           r"AndroidVersion|AndroidMake|AndroidModel|AndroidManufacturer|ContentIdentifier|"
                           r"DeviceManufacturer|DeviceModelName|Encoder|CreatorTool|ComAndroid.*|"
                           r"ComApple.*(Make|Model|Software).*)$", re.I)
_GRUPOS_FIXOS = {"Composite", "File", "System", "ExifTool", "SourceFile"}
_EXT_IMAGEM_META = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif", ".avif", ".jxl", ".dng"}


def _categorias(remover) -> set[str]:
    if remover is None or remover == "" or remover == []:
        return set()
    itens = remover if isinstance(remover, (list, tuple, set)) else re.split(r"[,+ ]+", str(remover))
    out = set()
    for x in itens:
        x = str(x).strip().lower()
        x = {"gps": "localizacao", "localização": "localizacao", "local": "localizacao", "lugar": "localizacao",
             "dispositivo": "camera", "câmera": "camera", "celular": "camera", "aparelho": "camera",
             "todos": "tudo", "all": "tudo", "content_credentials": "c2pa", "procedencia": "c2pa"}.get(x, x)
        if not x:
            continue
        if x not in ("tudo", "localizacao", "camera", "c2pa"):
            raise ValueError("remover: 'tudo', 'localizacao', 'camera' e/ou 'c2pa'")
        out.add(x)
    if "tudo" in out:
        out |= {"localizacao", "camera", "c2pa"}
    return out


def _residuos(ex: dict, cats: set[str]) -> dict[str, list[str]]:
    """Tags do exiftool (chaves 'Grupo:Tag') que ainda revelam localização/câmera."""
    out: dict[str, list[str]] = {}
    for chave, val in ex.items():
        if ":" not in chave:
            continue
        grupo, tag = chave.split(":", 1)
        if grupo in _GRUPOS_FIXOS:
            continue
        if "localizacao" in cats and (_RX_LOCAL_EX.search(tag) or grupo == "GPS"):
            out.setdefault("localizacao", []).append(chave)
        elif "camera" in cats and (_RX_CAMERA_EX.match(tag) or grupo.startswith("MakerNotes")):
            if tag == "Encoder" and re.match(r"(Lav[fc]|libebml|ffmpeg|x264|x265|libx26)", str(val)):
                continue  # 'Lavf' = gravado pelo ffmpeg (não identifica o aparelho)
            out.setdefault("camera", []).append(chave)
    return out


def _limpar_com_exiftool(p: Path, res: dict[str, list[str]]) -> list[str]:
    """Apaga, com o exiftool, exatamente as tags encontradas (no arquivo da VERSÃO, nunca no original)."""
    exe = core.ferramenta("exiftool")
    if not exe or not res:
        return []
    args: list[str] = []
    for chave in sorted({c for lst in res.values() for c in lst}):
        grupo, tag = chave.split(":", 1)
        if grupo == "GPS":
            a = "-GPS:all="
        elif grupo.startswith("MakerNotes"):
            a = "-MakerNotes:all="
        else:
            a = f"-{grupo}:{tag}="
        if a not in args:
            args.append(a)
    cmd = [exe, "-q", "-q", "-overwrite_original", "-api", "largefilesupport=1"] + args + [str(p)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return [shlex.join(cmd)]


def _data_exif(valor: str) -> str:
    s = str(valor).strip()
    m = re.fullmatch(r"(\d{4})[-:/](\d{2})[-:/](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if not m:
        if re.fullmatch(r"\d{4}", s):
            return f"{s}:01:01 00:00:00"
        raise ValueError("data: use AAAA-MM-DD (opcionalmente com HH:MM[:SS]) ou só o ano")
    y, mo, d, hh, mi, ss = m.groups()
    return f"{y}:{mo}:{d} {hh or '00'}:{mi or '00'}:{ss or '00'}"


def editar_metadados(doc: Documento, titulo: str | None = None, artista: str | None = None,
                     album: str | None = None, data: str | None = None, comentario: str | None = None,
                     descricao: str | None = None, genero: str | None = None, idiomas: dict | None = None,
                     nomes_faixas: dict | None = None, remover=None, progresso=None) -> dict:
    """Edita/remove metadados numa NOVA VERSÃO, sem reencodar. Vídeo/áudio: remux do ffmpeg (+ exiftool para
    o que sobrar); imagem (JPG/PNG/HEIC...): exiftool. Confere no fim com o exiftool/c2patool."""
    entrada = doc.atual()
    ext = entrada.suffix.lower()
    cats = _categorias(remover)
    novos = {k: v for k, v in (("title", titulo), ("artist", artista), ("album", album), ("date", data),
                               ("comment", comentario), ("description", descricao), ("genre", genero))
             if v is not None}
    if not novos and not cats and not idiomas and not nomes_faixas:
        raise ValueError("nada a fazer: passe titulo/artista/album/data/comentario/descricao/genero, idiomas, "
                         "nomes_faixas ou remover")
    c2pa_antes = _c2pa_presente(entrada)
    ex_antes = _exiftool_json(entrada)
    avisos: list[str] = []
    comandos: list[list[str]] = []
    desc_partes = []
    if cats:
        desc_partes.append("removeu " + ", ".join(sorted(cats - {"tudo"} if "tudo" not in cats else {"tudo"})))
    if novos:
        desc_partes.append("editou " + ", ".join(novos))
    if idiomas or nomes_faixas:
        desc_partes.append("faixas: " + ", ".join(sorted({str(k).upper() for k in {**(idiomas or {}), **(nomes_faixas or {})}})))
    descricao_v = "metadados: " + "; ".join(desc_partes)

    if ext in _EXT_IMAGEM_META:
        exe = core.ferramenta("exiftool")
        if not exe:
            raise binarios.FerramentaAusente("exiftool não encontrado (necessário para metadados de imagem)")
        if idiomas or nomes_faixas:
            raise ValueError("idiomas/nomes_faixas só valem para vídeo/áudio")
        saida = doc.proxima_saida(ext)
        try:
            shutil.copyfile(entrada, saida)
            args: list[str] = []
            if "tudo" in cats:
                args += ["-all=", "-tagsfromfile", "@", "-ICC_Profile", "-Orientation"]
            elif "c2pa" in cats:
                args += ["-JUMBF:all=", "-XMP-c2pa:all="]
            if "title" in novos:
                args += [f"-XMP-dc:Title={novos['title']}", f"-IPTC:ObjectName={novos['title'][:64]}"]
            if "artist" in novos:
                args += [f"-EXIF:Artist={novos['artist']}", f"-XMP-dc:Creator={novos['artist']}"]
            if "description" in novos or "comment" in novos:
                d = novos.get("description", novos.get("comment"))
                args += [f"-EXIF:ImageDescription={d}", f"-XMP-dc:Description={d}"]
            if "comment" in novos:
                args += [f"-EXIF:UserComment={novos['comment']}"]
            if "date" in novos:
                dt = _data_exif(novos["date"])
                args += [f"-EXIF:DateTimeOriginal={dt}", f"-EXIF:CreateDate={dt}", f"-XMP-xmp:CreateDate={dt}"]
            if "album" in novos or "genre" in novos:
                avisos.append("album/genero não existem em imagens: ignorados")
            if args:
                cmd = [exe, "-q", "-q", "-overwrite_original", "-charset", "UTF8"] + args + [str(saida)]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                comandos.append(cmd)
                if r.returncode != 0:
                    raise RuntimeError(f"exiftool falhou: {r.stderr.strip()[-800:]}")
            res = _residuos(_exiftool_json(saida), cats)
            if res:
                comandos += [shlex.split(c) for c in _limpar_com_exiftool(saida, res)]
        except BaseException:
            saida.unlink(missing_ok=True)
            raise
        v = doc.registrar(saida, descricao_v, comandos or [["cp", str(entrada), str(saida)]])
    else:
        probe = core.ffprobe(entrada)
        maps, idx, fora = _mapas(probe, ext)
        cmd = core.FFMPEG + ["-i", str(entrada)] + maps + ["-c", "copy", "-map_chapters", "0"]
        streams = probe["streams"]
        if "tudo" in cats:
            cmd += ["-map_metadata", "-1", "-fflags", "+bitexact"]
            for i, oi in idx.items():  # idioma e nome de cada faixa não são dados pessoais: ficam
                t = {k.lower(): v for k, v in _tags(streams[i]).items()}
                for k in ("language", "title"):
                    if t.get(k):
                        cmd += [f"-metadata:s:{oi}", f"{k}={t[k]}"]
        else:
            cmd += ["-map_metadata", "0"]
            rx = []
            if "localizacao" in cats:
                rx.append(_RX_LOCAL_FF)
            if "camera" in cats:
                rx.append(_RX_CAMERA_FF)
            for k in _tags(probe.get("format", {})):
                if any(r.search(k) for r in rx):
                    cmd += ["-metadata", f"{k}="]
            for i, oi in idx.items():
                for k in _tags(streams[i]):
                    if k.lower() not in ("language", "title", "handler_name") and any(r.search(k) for r in rx):
                        cmd += [f"-metadata:s:{oi}", f"{k}="]
        for k, val in novos.items():
            cmd += ["-metadata", f"{k}={val}"]
        for rot, lang in (idiomas or {}).items():
            s = _ordinal(probe, rot)
            cmd += [f"-metadata:s:{idx[int(s['index'])]}", f"language={_iso639_2(lang)}"]
        for rot, nome in (nomes_faixas or {}).items():
            s = _ordinal(probe, rot)
            oi = idx[int(s["index"])]
            cmd += [f"-metadata:s:{oi}", f"title={nome}"]
            if ext in _EXT_MP4:
                cmd += [f"-metadata:s:{oi}", f"handler_name={nome}"]
        if ext == ".mp3":
            cmd += ["-id3v2_version", "3"]
        saida = doc.proxima_saida(ext)
        if ext in _EXT_MP4:  # use_metadata_tags: mantém as chaves 'mdta' (com.apple.quicktime.*) que não foram apagadas
            cmd += ["-movflags", "+faststart" + ("" if "tudo" in cats else "+use_metadata_tags")]
        cmd.append(str(saida))
        comandos.append(cmd)
        try:
            _rodar(cmd, core.duracao(probe), progresso)
            if not saida.exists() or not saida.stat().st_size:
                raise ErroFFmpeg("o ffmpeg não gerou a versão")
            res = _residuos(_exiftool_json(saida), cats)
            if res:
                comandos += [shlex.split(c) for c in _limpar_com_exiftool(saida, res)]
        except BaseException:
            saida.unlink(missing_ok=True)
            raise
        if fora:
            avisos.append("ficou de fora (o contêiner não guarda): " + ", ".join(fora))
        v = doc.registrar(saida, descricao_v, comandos)
    # conferência final
    ex_depois = _exiftool_json(saida)
    verif: dict = {}
    if cats & {"localizacao", "camera"}:
        if not ex_depois and not core.ferramenta("exiftool"):
            verif["exiftool"] = "indisponível: não deu para conferir"
        else:
            sobra = _residuos(ex_depois, cats)
            antes = _residuos(ex_antes, cats)
            for c in ("localizacao", "camera"):
                if c in cats:
                    verif[c] = {"encontradas_antes": antes.get(c, []), "restantes": sobra.get(c, [])}
                    if sobra.get(c):
                        avisos.append(f"ainda há tags de {c}: {', '.join(sobra[c])}")
    c2pa_depois = _c2pa_presente(saida)
    if c2pa_antes is not None:
        verif["c2pa"] = {"antes": c2pa_antes, "depois": c2pa_depois}
        if c2pa_antes and not c2pa_depois and "c2pa" not in cats:
            avisos.append("o arquivo tinha Content Credentials (C2PA): a regravação invalida/descarta o manifesto "
                          "(ele assina o conteúdo exato do arquivo)")
        if c2pa_depois and "c2pa" in cats:
            avisos.append("o manifesto C2PA continua no arquivo")
    v = dict(v)
    v["detalhes"] = {"verificacao": verif, "novos": novos} if verif or novos else {}
    if avisos:
        v["avisos"] = avisos
    return v


# ================================================================ 7. remux sem perda e reparo
_COMPAT = {
    ".mp4": {"video": {"h264", "hevc", "av1", "mpeg4", "mpeg2video", "mpeg1video", "vp9", "mjpeg"},
             "audio": {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac", "mp2"}, "legenda": "mov_text"},
    ".m4v": {"video": {"h264", "hevc", "mpeg4"}, "audio": {"aac", "ac3", "eac3", "alac"}, "legenda": "mov_text"},
    ".mov": {"video": {"h264", "hevc", "mpeg4", "mpeg2video", "mjpeg", "prores", "dnxhd", "qtrle", "png", "av1",
                       "rawvideo"},
             "audio": {"aac", "mp3", "ac3", "eac3", "alac", "pcm_s16le", "pcm_s24le", "pcm_s16be", "pcm_s24be",
                       "pcm_f32le"}, "legenda": "mov_text"},
    ".webm": {"video": {"vp8", "vp9", "av1"}, "audio": {"opus", "vorbis"}, "legenda": "webvtt"},
    ".mkv": {"video": None, "audio": None, "legenda": "*"},
    ".ts": {"video": {"h264", "hevc", "mpeg2video", "mpeg1video"}, "audio": {"aac", "mp3", "ac3", "eac3", "mp2",
                                                                             "opus"}, "legenda": None},
    ".avi": {"video": {"mpeg4", "mjpeg", "h264", "msmpeg4v3", "rawvideo"}, "audio": {"mp3", "ac3", "pcm_s16le"},
             "legenda": None},
    ".m4a": {"video": set(), "audio": {"aac", "alac"}, "legenda": None},
    ".mp3": {"video": set(), "audio": {"mp3"}, "legenda": None},
    ".ogg": {"video": set(), "audio": {"vorbis", "opus", "flac"}, "legenda": None},
    ".opus": {"video": set(), "audio": {"opus"}, "legenda": None},
    ".flac": {"video": set(), "audio": {"flac"}, "legenda": None},
    ".wav": {"video": set(), "audio": {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_u8"},
             "legenda": None},
    ".mka": {"video": set(), "audio": None, "legenda": None},
    ".aac": {"video": set(), "audio": {"aac"}, "legenda": None},
}
_APELIDOS_EXT = {".m2ts": ".ts", ".mts": ".ts", ".qt": ".mov", ".m4b": ".m4a", ".oga": ".ogg"}
_TEXTO = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


def _plano_remux(probe: dict, ext: str, audio_incompativel: str) -> tuple[list[str], list[str], list[str]]:
    """(-map/-c, avisos, erros) para copiar sem reencodar para `ext`."""
    tab = _COMPAT[_APELIDOS_EXT.get(ext, ext)]
    args: list[str] = []
    avisos: list[str] = []
    erros: list[str] = []
    cods: list[str] = []
    so_audio = tab["video"] == set()
    oi = 0
    n_aud = 0
    for s in probe.get("streams", []):
        i, tipo, cod = int(s["index"]), s.get("codec_type"), s.get("codec_name") or "?"
        if tipo == "video" and _eh_capa(s):
            if ext in (".mp4", ".m4a", ".m4v", ".mov", ".mp3", ".mkv", ".mka", ".flac") and cod in ("mjpeg", "png"):
                args += ["-map", f"0:{i}"]
                cods += [f"-c:{oi}", "copy"]
                oi += 1
            else:
                avisos.append(f"capa embutida ({cod}) não cabe em {ext}: ficou de fora")
            continue
        if tipo == "video":
            if so_audio:
                avisos.append(f"vídeo ({cod}) descartado: {ext} é só de áudio")
                continue
            if tab["video"] is not None and cod not in tab["video"]:
                erros.append(f"vídeo {cod} não cabe em {ext} sem reencodar")
                continue
        elif tipo == "audio":
            if ext in (".mp3", ".flac", ".wav", ".aac", ".opus") and n_aud >= 1:
                avisos.append(f"faixa de áudio extra #{i} ({cod}) descartada: {ext} guarda uma só")
                continue
            if tab["audio"] is not None and cod not in tab["audio"]:
                if audio_incompativel == "converter":
                    if ext in (".webm", ".ogg", ".opus"):
                        cods += [f"-c:{oi}", "libopus", f"-b:{oi}", "160k"]
                    elif ext == ".mp3":
                        cods += [f"-c:{oi}", "libmp3lame", f"-q:{oi}", "2"]
                    elif ext == ".flac":
                        cods += [f"-c:{oi}", "flac"]
                    elif ext == ".wav":
                        cods += [f"-c:{oi}", "pcm_s16le"]
                    else:
                        cods += [f"-c:{oi}", "aac", f"-b:{oi}", "256k"]
                    avisos.append(f"áudio {cod} reencodado (o formato {ext} não aceita {cod}); o vídeo foi copiado "
                                  f"sem perda")
                    args += ["-map", f"0:{i}"]
                    oi += 1
                    n_aud += 1
                    continue
                erros.append(f"áudio {cod} não cabe em {ext} sem reencodar")
                continue
            n_aud += 1
        elif tipo == "subtitle":
            leg = tab["legenda"]
            if leg is None:
                avisos.append(f"legenda #{i} ({cod}) descartada: {ext} não guarda legendas")
                continue
            if leg == "*":
                if cod == "mov_text":
                    cods += [f"-c:{oi}", "srt"]
                    args += ["-map", f"0:{i}"]
                    oi += 1
                    continue
            elif cod != leg:
                if cod in _TEXTO:
                    cods += [f"-c:{oi}", leg]
                    if cod in ("ass", "ssa"):
                        avisos.append(f"legenda ASS #{i} convertida para {leg}: perde estilos/posições")
                    args += ["-map", f"0:{i}"]
                    oi += 1
                    continue
                avisos.append(f"legenda de imagem #{i} ({cod}) descartada: {ext} só aceita {leg}; use .mkv")
                continue
        elif tipo == "data":
            tag = str(s.get("codec_tag_string") or "").lower()
            if tag in ("text", "tmcd") or ext not in (".mp4", ".mov", ".m4v", ".ts"):
                if tag not in ("text", "tmcd"):
                    avisos.append(f"fluxo de dados #{i} ({cod}) descartado")
                continue
        elif tipo == "attachment":
            if ext not in (".mkv", ".mka"):
                avisos.append(f"anexo #{i} ({_tags(s).get('filename') or cod}) descartado")
                continue
        else:
            continue
        args += ["-map", f"0:{i}"]
        cods += [f"-c:{oi}", "copy"]
        if tipo == "video" and cod == "hevc" and ext in (".mp4", ".mov", ".m4v"):
            cods += [f"-tag:{oi}", "hvc1"]  # Apple/QuickTime só tocam HEVC com a tag hvc1
        oi += 1
    return args + cods, avisos, erros


def converter_sem_perda(arquivo: Path | str, original: Path | str | None = None, formato: str = "mkv",
                        destino=None, audio_incompativel: str = "erro", progresso=None) -> dict:
    """Troca o CONTÊINER sem reencodar (-c copy): ARQUIVO NOVO `nome.ext` ao lado do original."""
    arq = Path(arquivo)
    original = Path(original or arquivo)
    ext = "." + str(formato or "").lower().lstrip(".")
    if _APELIDOS_EXT.get(ext, ext) not in _COMPAT:
        raise ValueError("formato: " + ", ".join(sorted(e.lstrip(".") for e in _COMPAT)))
    if audio_incompativel not in ("erro", "converter"):
        raise ValueError("audio_incompativel: 'erro' ou 'converter'")
    probe = core.ffprobe(arq)
    plano, avisos, erros = _plano_remux(probe, ext, audio_incompativel)
    if erros:
        raise ValueError("não dá para converter sem reencodar: " + "; ".join(erros) + ". Use `exportar` (formato='"
                         + ext.lstrip(".") + "') para reencodar"
                         + ("" if audio_incompativel == "converter" or any("vídeo" in e for e in erros)
                            else ", ou audio_incompativel='converter' (reencoda SÓ o áudio)"))
    if "-map" not in plano:
        raise ValueError("nenhuma faixa cabe no formato pedido")
    dur = core.duracao(probe)

    def montar(saida: Path):
        cmd = core.FFMPEG + ["-i", str(arq)] + plano + ["-map_metadata", "0"]
        if _COMPAT[_APELIDOS_EXT.get(ext, ext)]["video"] != set() or ext in (".m4a", ".mp3", ".mka", ".ogg", ".flac"):
            cmd += ["-map_chapters", "0"]
        if ext == ".mp3":
            cmd += ["-id3v2_version", "3"]
        cmd += core._faststart(saida) + [str(saida)]
        return [cmd]

    try:
        final, cmds = _arquivo_novo(core._caminho(original, ext.lstrip("."), ext, destino, criar=True), montar,
                                    [dur], progresso)
    except ErroFFmpeg as e:
        motivo = [ln for ln in str(e).splitlines()[1:] if ln.strip()][:3]
        raise ValueError(f"o ffmpeg recusou gravar {ext} sem reencodar ({' | '.join(motivo)[:300]}). Use `exportar` "
                         f"(formato='{ext.lstrip('.')}') para reencodar, ou outro formato (mkv aceita quase tudo)") from e
    r = core.info_arquivo(final)
    d2 = core.duracao(core.ffprobe(final))
    if dur and abs(d2 - dur) > max(0.5, dur * 0.01):
        avisos.append(f"duração mudou: {fmt_tempo(dur)} → {fmt_tempo(d2)}")
    r["origem"] = str(arq)
    r["comando"] = shlex.join(cmds[0])
    r["sem_perda"] = not any("reencodado" in a for a in avisos)
    if avisos:
        r["avisos"] = avisos
    return r


def _erros_decodificacao(p: Path, dur: float, progresso=None, base=0.0, peso=1.0) -> int:
    cmd = core.FFMPEG[:1] + ["-hide_banner", "-nostdin", "-loglevel", "error", "-threads", "2", "-i", str(p),
                             "-map", "0:V?", "-map", "0:a?", "-f", "null", "-"]
    try:
        err = _rodar(cmd, dur, progresso, base, peso)
    except ErroFFmpeg as e:
        err = str(e)
    return sum(1 for ln in err.splitlines() if ln.strip())


def reparar_arquivo(arquivo: Path | str, original: Path | str | None = None, formato: str | None = None,
                    destino=None, verificar: bool = True, progresso=None) -> dict:
    """Regrava (remux, sem reencodar) um arquivo com índice/timestamps quebrados: ARQUIVO NOVO
    `nome.reparado.ext`. Regenera pts, ignora dts ruins, descarta pacotes corrompidos, move o índice do MP4
    para o início (faststart). MP4 truncado SEM o átomo moov (gravação interrompida) não tem como: explica."""
    arq = Path(arquivo)
    original = Path(original or arquivo)
    if not arq.is_file():
        raise FileNotFoundError(f"arquivo não encontrado: {arq}")
    r0 = subprocess.run([core.FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                         str(arq)], capture_output=True, text=True, timeout=120)
    if "moov atom not found" in r0.stderr:
        raise ValueError("MP4/MOV truncado sem o índice (átomo 'moov'): típico de gravação interrompida (bateria, "
                         "celular travou, cartão cheio). O índice fica no FIM do arquivo e não foi escrito, então o "
                         "ffmpeg não sabe onde começa cada quadro. A recuperação exige reconstruir o índice a partir "
                         "de um vídeo BOM gravado no mesmo aparelho/configuração (ferramenta 'untrunc'); isso o AI "
                         "Editor ainda não faz.")
    try:
        probe = json.loads(r0.stdout or "{}")
    except ValueError:
        probe = {}
    if not probe.get("streams"):
        raise ValueError(f"o ffmpeg não reconhece nenhuma faixa neste arquivo; não dá para reparar por remux. "
                         f"Erro do ffprobe: {r0.stderr.strip()[-500:] or 'nenhum'}")
    ext = "." + str(formato).lower().lstrip(".") if formato else arq.suffix.lower()
    if _APELIDOS_EXT.get(ext, ext) not in _COMPAT:
        ext = ".mkv"
    dur_antes = core.duracao(probe)
    avisos: list[str] = []
    erros_antes = _erros_decodificacao(arq, dur_antes, progresso, 0.0, 0.3) if verificar else None
    tentativas = [ext] + ([".mkv"] if ext != ".mkv" else [])
    final = None
    cmds: list[list[str]] = []
    ultimo_erro = None
    for k, e in enumerate(tentativas):
        plano, av, erros = _plano_remux(probe, e, "erro")
        if erros:
            ultimo_erro = ValueError("; ".join(erros))
            continue

        def montar(saida: Path, e=e, plano=plano):
            cmd = core.FFMPEG + ["-err_detect", "ignore_err", "-fflags", "+genpts+igndts+discardcorrupt",
                                 "-i", str(arq)] + plano + ["-map_metadata", "0", "-map_chapters", "0",
                                                            "-avoid_negative_ts", "make_zero",
                                                            "-max_interleave_delta", "0"]
            cmd += core._faststart(saida) + [str(saida)]
            return [cmd]

        try:
            final, cmds = _arquivo_novo(core._caminho(original, "reparado", e, destino, criar=True), montar,
                                        [dur_antes or 1.0],
                                        (lambda f: progresso(0.3 + 0.4 * f)) if progresso and verificar else progresso)
            avisos += av
            if k > 0:
                avisos.append(f"o remux para {ext} falhou; o reparo saiu em MKV (aceita quase tudo)")
            break
        except ErroFFmpeg as err:
            ultimo_erro = err
    if final is None:
        raise ValueError(f"não consegui reparar por remux: {str(ultimo_erro)[-800:]}. Se o arquivo abre no player, "
                         f"tente `exportar` (reencodar costuma esconder quadros corrompidos)")
    pr = core.ffprobe(final)
    dur_depois = core.duracao(pr)
    r = core.info_arquivo(final)
    r.update({"origem": str(arq), "duracao_antes": fmt_tempo(dur_antes), "duracao_depois": fmt_tempo(dur_depois),
              "comando": shlex.join(cmds[0])})
    if verificar:
        erros_depois = _erros_decodificacao(final, dur_depois, progresso, 0.7, 0.3)
        r["erros_de_decodificacao"] = {"antes": erros_antes, "depois": erros_depois}
        if erros_depois:
            avisos.append(f"ainda há {erros_depois} erro(s) ao decodificar: são quadros/pacotes realmente "
                          f"danificados (o remux não recria dados perdidos). `exportar` reencoda e costuma "
                          f"esconder os defeitos")
    if dur_antes and dur_depois < dur_antes - max(0.5, dur_antes * 0.02):
        avisos.append(f"recuperados {fmt_tempo(dur_depois)} de {fmt_tempo(dur_antes)} (o arquivo termina antes do "
                      f"previsto ou tem trechos ilegíveis)")
    if avisos:
        r["avisos"] = avisos
    return r


# ================================================================ 8. legendas em arquivo
_EXT_LEG = {"srt": ".srt", "vtt": ".vtt", "webvtt": ".vtt", "ass": ".ass", "ssa": ".ass"}
_CODEC_LEG = {".srt": "srt", ".vtt": "webvtt", ".ass": "ass"}
_NATURAL = {"subrip": ".srt", "srt": ".srt", "mov_text": ".srt", "text": ".srt", "webvtt": ".vtt", "ass": ".ass",
            "ssa": ".ass"}


def _faixas_legenda(probe: dict) -> list[dict]:
    return [s for s in probe.get("streams", []) if s.get("codec_type") == "subtitle"]


def _resumo_legenda(n: int, s: dict) -> dict:
    t = {k.lower(): v for k, v in _tags(s).items()}
    disp = s.get("disposition") or {}
    return {"faixa": n, "codec": s.get("codec_name"), "tipo": "texto" if s.get("codec_name") in _TEXTO else "imagem",
            "idioma": t.get("language"), "titulo": t.get("title") or (t.get("handler_name")
                                                                      if t.get("handler_name") not in ("SubtitleHandler", None) else None),
            "padrao": bool(disp.get("default")), "forcada": bool(disp.get("forced"))}


def _charenc(p: Path) -> list[str]:
    try:
        p.read_bytes().decode("utf-8")
        return []
    except UnicodeDecodeError:
        return ["-sub_charenc", "CP1252"]  # legendas brasileiras antigas costumam vir em Windows-1252


_RX_SRT_T = re.compile(r"(\d+):(\d{2}):(\d{2})([,.])(\d{3})")
_RX_VTT_T = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})")
_RX_ASS_T = re.compile(r"(\d+):(\d{2}):(\d{2})\.(\d{2})")


def deslocar_texto(texto: str, ext: str, desloc: float) -> tuple[str, int]:
    """Desloca os tempos de um .srt/.vtt/.ass em `desloc` segundos (negativo = adianta). Falas que terminariam
    antes de 0 saem; início negativo vira 0. Devolve (texto, falas removidas)."""
    ext = ext.lower()

    def novo(t: float) -> float:
        return max(0.0, t + desloc)

    removidas = 0
    if ext == ".ass":
        out = []
        for ln in texto.splitlines(keepends=True):
            m = re.match(r"(\s*Dialogue:\s*[^,]*,)([^,]+),([^,]+),(.*)", ln, re.S)
            if m:
                def seg(x):
                    h, mi, s, cs = (int(v) for v in _RX_ASS_T.fullmatch(x.strip()).groups())
                    return h * 3600 + mi * 60 + s + cs / 100
                a, b = seg(m.group(2)), seg(m.group(3))
                if b + desloc <= 0:
                    removidas += 1
                    continue
                ln = m.group(1) + fala_tempo_ass(novo(a)) + "," + fala_tempo_ass(novo(b)) + "," + m.group(4)
            out.append(ln)
        return "".join(out), removidas
    blocos = re.split(r"(\r?\n\s*\r?\n)", texto)
    out = []
    for bl in blocos:
        if "-->" not in bl:
            out.append(bl)
            continue
        linhas = bl.split("\n")
        k = next(i for i, ln in enumerate(linhas) if "-->" in ln)
        rx = _RX_VTT_T if ext == ".vtt" else _RX_SRT_T
        tempos = list(rx.finditer(linhas[k]))
        if len(tempos) < 2:
            out.append(bl)
            continue

        def seg(m):
            if ext == ".vtt":
                h, mi, s, ms = m.group(1), m.group(2), m.group(3), m.group(4)
            else:
                h, mi, s, ms = m.group(1), m.group(2), m.group(3), m.group(5)
            return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000

        a, b = seg(tempos[0]), seg(tempos[1])
        if b + desloc <= 0:
            removidas += 1
            out.append("")
            continue
        sep = "." if ext == ".vtt" else (tempos[0].group(4) if ext != ".vtt" else ".")
        resto = linhas[k][tempos[1].end():]
        linhas[k] = (linhas[k][:tempos[0].start()] + _ts(novo(a), sep) + " --> " + _ts(novo(b), sep) + resto)
        out.append("\n".join(linhas))
    texto2 = "".join(out)
    if ext == ".srt" and removidas:  # renumera as falas
        n = 0

        def ren(m):
            nonlocal n
            n += 1
            return f"{m.group(1)}{n}\n"
        texto2 = re.sub(r"(^|\n\n)\d+\n(?=\d+:\d{2}:\d{2}[,.]\d{3}\s*-->)", ren, texto2.lstrip("\n"))
    return texto2, removidas


def _ts(t: float, sep: str) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def fala_tempo_ass(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _criar_texto(final: Path, conteudo: str) -> Path:
    final = core._reservar(final)
    try:
        with open(final, "w", encoding="utf-8") as f:
            f.write(conteudo)
    except BaseException:
        final.unlink(missing_ok=True)
        raise
    return final


def legenda_arquivo(doc: Documento | None, acao: str = "listar", faixa_legenda=None, formato: str | None = None,
                    deslocamento_s: float | None = None, arquivo: str | Path | None = None, destino=None,
                    progresso=None) -> dict:
    """Legendas como ARQUIVOS: listar/extrair faixas embutidas, converter .srt/.vtt/.ass, deslocar tempos
    (arquivo externo → arquivo novo; faixa embutida → versão) e remover faixas (versão)."""
    acao = (acao or "listar").lower()
    ext_f = None
    if formato:
        f = str(formato).lower().lstrip(".")
        if f not in _EXT_LEG:
            raise ValueError("formato: 'srt', 'vtt' ou 'ass'")
        ext_f = _EXT_LEG[f]
    ext_arq = None
    if arquivo is not None:
        arq = Path(str(arquivo)).expanduser().resolve()
        if not arq.is_file():
            raise FileNotFoundError(f"legenda não encontrada: {arq}")
        ext_arq = arq.suffix.lower()
    if acao == "converter" or (acao == "deslocar" and arquivo is not None):
        if arquivo is None:
            raise ValueError("acao 'converter' precisa de `arquivo` (.srt/.vtt/.ass); para uma faixa embutida use "
                             "acao='extrair' com formato")
        if ext_arq not in (".srt", ".vtt", ".ass", ".ssa"):
            raise ValueError("arquivo de legenda: .srt, .vtt ou .ass")
        if acao == "converter":
            if not ext_f:
                raise ValueError("passe formato ('srt', 'vtt' ou 'ass')")
            if ext_f == ext_arq or (ext_f == ".ass" and ext_arq == ".ssa"):
                raise ValueError(f"o arquivo já está em {ext_f}")

            def montar(saida: Path):
                return [core.FFMPEG + _charenc(arq) + ["-i", str(arq), "-c:s", _CODEC_LEG[ext_f], str(saida)]]

            final, cmds = _arquivo_novo(core._caminho(arq, ext_f.lstrip("."), ext_f, destino, criar=True), montar,
                                        [1.0])
            r = {"arquivo": str(final), "falas": _contar_falas(final), "comando": shlex.join(cmds[0])}
            if ext_arq in (".ass", ".ssa"):
                r["aviso"] = "ASS → texto simples: estilos, cores e posições se perdem"
            return r
        if deslocamento_s is None:
            raise ValueError("passe deslocamento_s (positivo = legenda aparece DEPOIS; negativo = ANTES)")
        bruto = arq.read_bytes()
        try:
            texto = bruto.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = bruto.decode("cp1252")
        ext_d = ".ass" if ext_arq == ".ssa" else ext_arq
        novo, removidas = deslocar_texto(texto, ext_d, float(deslocamento_s))
        final = _criar_texto(core._caminho(arq, "deslocado", arq.suffix, destino, criar=True), novo)
        r = {"arquivo": str(final), "deslocamento_s": float(deslocamento_s), "falas": _contar_falas(final)}
        if removidas:
            r["aviso"] = f"{removidas} fala(s) ficariam antes do início do vídeo e foram removidas"
        return r

    if doc is None:
        raise ValueError("nenhum vídeo aberto")
    entrada = doc.atual()
    probe = core.ffprobe(entrada)
    legs = _faixas_legenda(probe)
    if acao == "listar":
        return {"arquivo": str(entrada), "faixas": [_resumo_legenda(i, s) for i, s in enumerate(legs, 1)],
                "quantidade": len(legs)}
    if not legs:
        raise ValueError("o vídeo não tem faixas de legenda embutidas (para criar: gerar_legenda/embutir_legenda)")

    def escolher(todas_ok: bool) -> list[tuple[int, dict]]:
        if faixa_legenda is None or str(faixa_legenda).lower() in ("todas", "all", "*"):
            if faixa_legenda is None and len(legs) > 1 and not todas_ok:
                raise ValueError(f"o vídeo tem {len(legs)} faixas de legenda; diga qual (faixa_legenda=1, 2...) "
                                 f"ou 'todas'")
            return list(enumerate(legs, 1))
        n = int(str(faixa_legenda).lstrip("Ll"))
        if not 1 <= n <= len(legs):
            raise ValueError(f"faixa de legenda {n} não existe (há {len(legs)})")
        return [(n, legs[n - 1])]

    if acao == "extrair":
        saidas = []
        for n, s in escolher(True):
            cod = s.get("codec_name")
            t = {k.lower(): v for k, v in _tags(s).items()}
            lang = t.get("language") if t.get("language") not in (None, "und") else None
            suf = lang or f"L{n}"
            if cod in _TEXTO:
                ext = ext_f or _NATURAL.get(cod, ".srt")
                c = ["-c:s", _CODEC_LEG[ext]]
            elif cod == "hdmv_pgs_subtitle":
                ext, c = ".sup", ["-c:s", "copy"]
            else:
                ext, c = ".mks", ["-c:s", "copy"]

            def montar(saida: Path, s=s, c=c):
                return [core.FFMPEG + ["-i", str(entrada), "-map", f"0:{s['index']}"] + c + [str(saida)]]

            nome = core._caminho(doc.original, suf, ext, destino, criar=True)
            final, cmds = _arquivo_novo(nome, montar, [1.0])
            item = {"faixa": n, "arquivo": str(final), "codec_origem": cod}
            if ext in (".srt", ".vtt", ".ass"):
                item["falas"] = _contar_falas(final)
            if ext == ".mks":
                item["aviso"] = "legenda de imagem (DVD/DVB) salva num .mks (Matroska só com a legenda)"
            saidas.append(item)
        return {"legendas": saidas, "lembrete": "arquivos novos; o vídeo não foi alterado"}
    if acao == "remover":
        alvo = escolher(True) if faixa_legenda is not None else (list(enumerate(legs, 1)) if len(legs) == 1
                                                                 else escolher(False))
        excluir = {int(s["index"]) for _n, s in alvo}
        ext = entrada.suffix.lower()
        maps, _idx, _fora = _mapas(probe, ext, excluir)
        saida = doc.proxima_saida(ext)
        cmd = core.FFMPEG + ["-i", str(entrada)] + maps + ["-c", "copy", "-map_metadata", "0", "-map_chapters", "0"]
        cmd += core._faststart(saida) + [str(saida)]
        desc = "legenda removida: " + ", ".join(f"L{n}" for n, _s in alvo)
        v = core._rodar_versao(doc, saida, [cmd], [core.duracao(probe)], desc, progresso)
        return dict(v, detalhes={"removidas": [_resumo_legenda(n, s) for n, s in alvo]})
    if acao == "deslocar":
        if deslocamento_s is None:
            raise ValueError("passe deslocamento_s (positivo = legenda aparece DEPOIS; negativo = ANTES)")
        alvo = escolher(False)
        ext = entrada.suffix.lower()
        maps, idx, _fora = _mapas(probe, ext)
        tmpdir = Path(tempfile.mkdtemp(prefix="legenda-", dir=core.RUNTIME_DIR if core.RUNTIME_DIR.exists() else None))
        try:
            extras_in: list[str] = []
            subst: dict[int, int] = {}
            removidas = 0
            for k, (n, s) in enumerate(alvo):
                cod = s.get("codec_name")
                if cod not in _TEXTO:
                    raise ValueError(f"a legenda L{n} é de imagem ({cod}): deslocar só funciona com legenda de texto")
                ext_t = _NATURAL.get(cod, ".srt")
                arq_t = tmpdir / f"l{n}{ext_t}"
                core.rodar(core.FFMPEG + ["-i", str(entrada), "-map", f"0:{s['index']}", "-c:s",
                                          _CODEC_LEG[ext_t], str(arq_t)])
                texto, rem = deslocar_texto(arq_t.read_text(encoding="utf-8", errors="replace"), ext_t,
                                            float(deslocamento_s))
                removidas += rem
                arq_t.write_text(texto, encoding="utf-8")
                extras_in += ["-i", str(arq_t)]
                subst[int(s["index"])] = k + 1
            cmd = core.FFMPEG + ["-i", str(entrada)] + extras_in
            novos_maps: list[str] = []
            meta: list[str] = []
            pares = [(maps[k], maps[k + 1]) for k in range(0, len(maps), 2)]
            for a_, b_ in pares:
                if a_ != "-map":
                    novos_maps += [a_, b_]
                    continue
                i = int(b_.split(":")[1])
                if i in subst:
                    novos_maps += ["-map", f"{subst[i]}:0"]
                    oi = idx[i]
                    s = probe["streams"][i]
                    meta += [f"-c:{oi}", s.get("codec_name") if s.get("codec_name") != "subrip" else "srt"]
                    for kk, vv in _tags(s).items():  # idioma, título/nome, handler... (não as estatísticas)
                        if not re.match(r"(duration|encoder|number_of_|_statistics|bps|statistics)", kk, re.I):
                            meta += [f"-metadata:s:{oi}", f"{'title' if kk == 'name' else kk}={vv}"]
                    disp = s.get("disposition") or {}
                    d = "+".join(x for x in ("default", "forced") if disp.get(x)) or "0"
                    meta += [f"-disposition:{oi}", d]
                else:
                    novos_maps += [a_, b_]
            saida = doc.proxima_saida(ext)
            cmd += novos_maps + ["-c", "copy"] + meta + ["-map_metadata", "0", "-map_chapters", "0"]
            cmd += core._faststart(saida) + [str(saida)]
            desc = (f"legenda deslocada {float(deslocamento_s):+.3f} s: " + ", ".join(f"L{n}" for n, _s in alvo))
            v = core._rodar_versao(doc, saida, [cmd], [core.duracao(probe)], desc, progresso)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        v = dict(v)
        if removidas:
            v["avisos"] = [f"{removidas} fala(s) ficariam antes do início e foram removidas"]
        return v
    raise ValueError("acao: 'listar', 'extrair', 'converter', 'deslocar' ou 'remover'")


def _contar_falas(p: Path) -> int:
    try:
        t = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    if p.suffix.lower() in (".ass", ".ssa"):
        return len(re.findall(r"^\s*Dialogue:", t, re.M))
    return t.count("-->")


# ================================================================ 9. rotação sem reencodar
def rotacao_sem_perda(doc: Documento, graus: float, absoluto: bool = False, progresso=None) -> dict:
    """Gira SÓ a marcação de exibição (matriz de rotação do MP4/MOV/MKV), sem reencodar: instantâneo e sem
    perda. graus no sentido horário (90, 180, 270/-90); absoluto=True define a rotação final (0 = tira)."""
    entrada = doc.atual()
    ext = entrada.suffix.lower()
    if ext not in (".mp4", ".mov", ".m4v", ".mkv", ".3gp"):
        raise ValueError(f"o formato {ext} não guarda rotação de exibição; use transformar_imagem(girar=...) "
                         f"(reencoda) ou converta antes (converter_sem_perda para mp4/mkv)")
    g = float(graus)
    if g % 90:
        raise ValueError("sem reencodar só dá para girar em múltiplos de 90° (90, 180, 270); para outros ângulos "
                         "use transformar_imagem(girar=...)")
    probe = core.ffprobe(entrada)
    vids = [s for s in probe["streams"] if s.get("codec_type") == "video" and not _eh_capa(s)]
    if not vids:
        raise ValueError("o arquivo não tem vídeo")
    v = vids[0]
    atual_ccw = next((sd.get("rotation") for sd in v.get("side_data_list", []) if "rotation" in sd), 0) or 0
    atual_h = (-int(round(float(atual_ccw)))) % 360  # rotação horária hoje aplicada na exibição
    final_h = int(g) % 360 if absoluto else (atual_h + int(g)) % 360
    if final_h == atual_h:
        raise ValueError(f"o vídeo já está com {atual_h}° de rotação de exibição: nada a fazer")
    ccw = -final_h if final_h <= 180 else 360 - final_h
    maps, _idx, fora = _mapas(probe, ext)
    saida = doc.proxima_saida(ext)
    cmd = core.FFMPEG + [f"-display_rotation:{v['index']}", str(ccw), "-i", str(entrada)] + maps
    cmd += ["-c", "copy", "-map_metadata", "0", "-map_chapters", "0"] + core._faststart(saida) + [str(saida)]
    desc = f"rotação de exibição {atual_h}° → {final_h}° (sem reencodar)"
    ver = core._rodar_versao(doc, saida, [cmd], [core.duracao(probe)], desc, progresso)
    v2 = [s for s in core.ffprobe(saida)["streams"] if s.get("codec_type") == "video" and not _eh_capa(s)][0]
    lido = next((sd.get("rotation") for sd in v2.get("side_data_list", []) if "rotation" in sd), 0) or 0
    lido_h = (-int(round(float(lido)))) % 360
    if lido_h != final_h:
        doc.desfazer()
        raise ErroFFmpeg(f"o contêiner {ext} não guardou a rotação (pedida {final_h}°, lida {lido_h}°); use "
                         f"transformar_imagem(girar=...) (reencoda)")
    ver = dict(ver)
    ver["detalhes"] = {"rotacao_antes": atual_h, "rotacao_depois": final_h,
                       "resolucao_exibida": "{}x{}".format(*core._dimensoes(v2))}
    ver["avisos"] = ["rotação só de exibição: players e celulares respeitam; alguns editores/sites antigos ignoram "
                     "(aí use transformar_imagem, que reencoda)"] + (["ficou de fora: " + ", ".join(fora)] if fora else [])
    return ver


# ================================================================ 10. comparar qualidade
def _faixa_vmaf(v: float) -> str:
    if v >= 95:
        return "praticamente idêntico ao original (diferença invisível)"
    if v >= 90:
        return "excelente: diferença difícil de notar"
    if v >= 80:
        return "boa: perda leve, visível em detalhes finos ou em tela grande"
    if v >= 70:
        return "razoável: perda perceptível (borrão/blocos em movimento)"
    if v >= 50:
        return "fraca: perda bem visível"
    return "ruim: muito degradado"


def _faixa_ssim(s: float) -> str:
    if s >= 0.99:
        return "idêntico na prática"
    if s >= 0.98:
        return "excelente"
    if s >= 0.95:
        return "boa"
    if s >= 0.90:
        return "perda visível"
    return "perda forte"


def _faixa_psnr(p: float) -> str:
    if p == float("inf") or p >= 48:
        return "idêntico na prática"
    if p >= 40:
        return "muito boa"
    if p >= 35:
        return "boa"
    if p >= 30:
        return "perda visível"
    return "perda forte"


def comparar_qualidade(ref: Path | str, dist: Path | str, inicio: float | None = None,
                       duracao_s: float | None = None, metricas: str = "auto", progresso=None) -> dict:
    """Mede a qualidade de `dist` em relação a `ref` (o original/a melhor versão): VMAF (se o ffmpeg tiver
    libvmaf), SSIM e PSNR, quadro a quadro; devolve médias, o pior momento e uma leitura em português."""
    ref, dist = Path(ref), Path(dist)
    if ref.resolve() == dist.resolve():
        raise ValueError("os dois lados são o mesmo arquivo")
    ir, idd = core._midia(ref), core._midia(dist)
    w, h = ir["w"], ir["h"]
    fps = ir["fps"]
    dur = min(ir["dur"], idd["dur"])
    avisos: list[str] = []
    if abs(ir["dur"] - idd["dur"]) > 0.5:
        avisos.append(f"durações diferentes ({fmt_tempo(ir['dur'])} × {fmt_tempo(idd['dur'])}): compara só os "
                      f"primeiros {fmt_tempo(dur)}, quadro a quadro desde o início — se um deles foi cortado, os "
                      f"números não valem")
    a = float(inicio or 0.0)
    if a >= dur:
        raise ValueError("inicio depois do fim")
    d = min(dur - a, float(duracao_s)) if duracao_s else dur - a
    m = str(metricas or "auto").lower()
    tem_vmaf = _tem_filtro("libvmaf") and bool(binarios._lista_ffmpeg("filters"))
    usar_vmaf = m in ("auto", "vmaf", "todas") and tem_vmaf
    if m == "vmaf" and not tem_vmaf:
        raise ValueError("este ffmpeg não tem libvmaf (o ffmpeg do pacote tem); use metricas='ssim' ou 'auto'")
    if (ir["w"], ir["h"]) != (idd["w"], idd["h"]):
        avisos.append(f"resoluções diferentes ({idd['w']}x{idd['h']} → {w}x{h}): o comparado foi redimensionado para "
                      f"a resolução da referência (a perda da redução entra na nota)")
    if abs(fps - idd["fps"]) > 0.01:
        avisos.append(f"fps diferentes ({idd['fps']:.3g} × {fps:.3g}): o comparado foi convertido para {fps:.3g} fps")
    tmp = Path(tempfile.mkdtemp(prefix="qualidade-"))
    try:
        log_ssim, log_psnr, log_vmaf = tmp / "ssim.txt", tmp / "psnr.txt", tmp / "vmaf.json"
        n = 3 if usar_vmaf else 2
        g = [f"[1:V:0]{idd['pre'] + ',' if idd['pre'] else ''}scale={w}:{h}:flags=bicubic,fps={fps:.6f},"
             f"setsar=1,format=yuv420p,setpts=PTS-STARTPTS,split={n}" + "".join(f"[d{i}]" for i in range(n)),
             f"[0:V:0]{ir['pre'] + ',' if ir['pre'] else ''}fps={fps:.6f},setsar=1,format=yuv420p,"
             f"setpts=PTS-STARTPTS,split={n}" + "".join(f"[r{i}]" for i in range(n)),
             f"[d0][r0]ssim=stats_file={core.esc_filtro(str(log_ssim))}[o0]",
             f"[d1][r1]psnr=stats_file={core.esc_filtro(str(log_psnr))}[o1]"]
        saidas = ["[o0]", "[o1]"]
        if usar_vmaf:
            sub = max(1, int(round(fps * d / 3000)))  # vídeos longos: VMAF em 1 de cada N quadros (~3000 medidos)
            g.append(f"[d2][r2]libvmaf=log_fmt=json:log_path={core.esc_filtro(str(log_vmaf))}:n_threads=2"
                     + (f":n_subsample={sub}" if sub > 1 else "") + "[o2]")
            saidas.append("[o2]")
            if sub > 1:
                avisos.append(f"vídeo longo: VMAF medido em 1 de cada {sub} quadros (SSIM/PSNR em todos)")
        cmd = core.FFMPEG[:1] + ["-hide_banner", "-nostdin", "-loglevel", "error", "-threads", "2"]
        win = (["-ss", f"{a:.6f}"] if a > 0 else []) + ["-t", f"{d:.6f}"]
        cmd += win + ir["dec"] + ["-i", str(ref)] + win + idd["dec"] + ["-i", str(dist)]
        cmd += ["-filter_complex", ";".join(g)]
        for s_ in saidas:
            cmd += ["-map", s_, "-f", "null", "-"]
        cmd = cmd[:1] + ["-filter_threads", "2"] + cmd[1:]
        _rodar(cmd, d, progresso)
        res: dict = {}
        # SSIM: "n:1 Y:0.98 U:.. V:.. All:0.987 (18.9)"
        ss = [(int(mm.group(1)), float(mm.group(2))) for mm in
              re.finditer(r"n:(\d+).*?All:([\d.]+)", log_ssim.read_text(errors="replace"))]
        if ss:
            media = sum(v for _, v in ss) / len(ss)
            pior = min(ss, key=lambda x: x[1])
            res["ssim"] = {"media": round(media, 5), "minimo": round(pior[1], 5),
                           "pior_momento": fmt_tempo(a + (pior[0] - 1) / fps), "leitura": _faixa_ssim(media)}
        ps = []
        for mm in re.finditer(r"n:(\d+).*?psnr_avg:(inf|[\d.]+)", log_psnr.read_text(errors="replace")):
            ps.append((int(mm.group(1)), float("inf") if mm.group(2) == "inf" else float(mm.group(2))))
        if ps:
            finitos = [v for _, v in ps if v != float("inf")]
            # média do PSNR pela média dos erros (MSE), como o ffmpeg faz; quadros idênticos = inf
            if finitos:
                mse = sum(10 ** (-v / 10) for v in finitos) / len(ps)
                media = 10 * math.log10(1 / mse) if mse > 0 else float("inf")
            else:
                media = float("inf")
            pior = min(ps, key=lambda x: x[1])
            res["psnr_db"] = {"media": round(media, 2) if media != float("inf") else "infinito (idênticos)",
                              "minimo": round(pior[1], 2) if pior[1] != float("inf") else "infinito",
                              "pior_momento": fmt_tempo(a + (pior[0] - 1) / fps), "leitura": _faixa_psnr(media)}
        if usar_vmaf and log_vmaf.exists():
            dj = json.loads(log_vmaf.read_text())
            pm = (dj.get("pooled_metrics") or {}).get("vmaf") or {}
            frames = [(f["frameNum"], f["metrics"]["vmaf"]) for f in dj.get("frames", []) if "vmaf" in f.get("metrics", {})]
            if pm:
                pior = min(frames, key=lambda x: x[1]) if frames else (0, pm.get("min"))
                res["vmaf"] = {"media": round(pm["mean"], 2), "minimo": round(pm["min"], 2),
                               "media_harmonica": round(pm.get("harmonic_mean", pm["mean"]), 2),
                               "pior_momento": fmt_tempo(a + pior[0] / fps), "leitura": _faixa_vmaf(pm["mean"])}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not res:
        raise ErroFFmpeg("o ffmpeg não devolveu as medidas")
    if res.get("ssim", {}).get("media", 1) < 0.7:
        avisos.append("as imagens quase não se parecem: são mesmo o MESMO vídeo, alinhado desde o início? (corte, "
                      "atraso ou outro conteúdo fazem a nota despencar sem ser perda de qualidade)")
    if "vmaf" in res:
        resumo = f"VMAF {res['vmaf']['media']}/100 — {res['vmaf']['leitura']}"
    elif "ssim" in res:
        resumo = f"SSIM {res['ssim']['media']} — {res['ssim']['leitura']}"
        if not tem_vmaf:
            avisos.append("este ffmpeg não tem libvmaf: só SSIM/PSNR (o ffmpeg do pacote AppImage mede VMAF)")
    else:
        resumo = ""
    r = {"referencia": str(ref), "comparado": str(dist), "trecho": [fmt_tempo(a), fmt_tempo(a + d)],
         "resumo": resumo, **res,
         "como_ler": "VMAF 0–100 (≥95 invisível, 90 excelente, 80 bom, <70 perda clara); SSIM 0–1 (≥0,98 excelente); "
                     "PSNR em dB (≥40 muito bom, <30 perda forte). Referência = o lado de melhor qualidade."}
    if avisos:
        r["avisos"] = avisos
    return r


def resolver_lado(doc: Documento | None, valor) -> Path:
    """'original'/0 → original; 'atual' → versão atual; 3 / 'v3' → versão 3; senão caminho de arquivo."""
    if isinstance(valor, str) and valor.strip().lower() in ("original", "orig", "v0", "0"):
        valor = 0
    if isinstance(valor, str) and valor.strip().lower() in ("atual", "ultima", "última"):
        if doc is None:
            raise ValueError("'atual' precisa de um vídeo aberto")
        return doc.atual()
    if isinstance(valor, str) and re.fullmatch(r"\s*v?\d+\s*", valor):
        valor = int(valor.strip().lstrip("vV"))
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        if doc is None:
            raise ValueError("número de versão precisa de um vídeo aberto")
        n = int(valor)
        if n == 0:
            return doc.original
        vs = doc.versoes()
        achada = next((v for v in vs if int(v["n"]) == n), None)
        if not achada:
            raise ValueError(f"versão v{n} não existe (há {len(vs)} versão(ões))")
        return doc.pasta / achada["arquivo"]
    p = Path(str(valor)).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"arquivo não encontrado: {p}")
    return p
