"""Efeitos de vídeo do "canivete suíço" (sem Qt): filtros visuais, LUT, chroma key, sobreposição (PiP/tela
dividida), zoom animado, inverter/boomerang/repetir, fade de imagem, gráficos (caixa, barra de progresso,
cronômetro), melhorar imagem, fps/câmera lenta suave, bordas pretas, logo e perspectiva/360.

Tudo com o ffmpeg (6.1 do sistema no modo dev, 8.1 no pacote) e no mesmo padrão das edições de imagem do
`core.py` (seção "imagem (Fase 3)"): cada função cria uma VERSÃO do documento; o vídeo é re-encodado com
`_args_video_edicao` (qualidade alta, pix_fmt e tags de cor da origem); edições que não mexem no tempo copiam o
áudio e as legendas sem perda. As que mudam a linha do tempo (inverter, repetir, câmera lenta) reconstroem por
segmentos com nº EXATO de quadros e áudio com a mesma duração (ver `_segmentos`), sem acumular desvio.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from . import binarios, core
from .core import (FFMPEG, Documento, esc_filtro, fmt_tempo, parse_tempo)

# ---------------------------------------------------------------- utilidades
_IMAGENS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
_MAX_ENTRADAS = 16  # entradas (decodificadores abertos, cada um com -threads 2) por comando nas edições por segmentos


def _tem_filtro(nome: str) -> bool:
    """O ffmpeg encontrado tem o filtro? (lista vazia = não deu para consultar: tenta assim mesmo)."""
    lista = binarios._lista_ffmpeg("filters")
    return not lista or nome in lista


def _exigir_filtro(nome: str, para: str) -> None:
    if not _tem_filtro(nome):
        raise RuntimeError(f"o ffmpeg encontrado não tem o filtro '{nome}' ({para}); use o ffmpeg do pacote "
                           f"(AppImage) ou um build completo (ex.: BtbN GPL)")


def _chave(s) -> str:
    """'Preto e Branco' → 'preto_e_branco' (sem acentos, minúsculas)."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[\s\-/]+", "_", s.strip().lower())


def _num(valor, nome: str, minimo: float, maximo: float, padrao: float | None = None) -> float:
    if valor is None or valor == "":
        if padrao is None:
            raise ValueError(f"{nome} é obrigatório")
        return float(padrao)
    try:
        v = float(str(valor).replace(",", "."))
    except ValueError:
        raise ValueError(f"{nome}: número inválido ({valor})") from None
    if not minimo <= v <= maximo:
        raise ValueError(f"{nome} entre {minimo:g} e {maximo:g} (recebi {v:g})")
    return v


def _fmt_trabalho(info: dict) -> str:
    return "yuv420p10le" if core._10bits(info.get("pix_fmt", "")) else "yuv420p"


def _arquivo(caminho, nome: str = "arquivo") -> Path:
    p = Path(str(caminho)).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"{nome} não encontrado: {p}")
    return p


def _e_imagem(p: Path, probe: dict | None = None) -> bool:
    if p.suffix.lower() in _IMAGENS - {".gif"}:
        return True
    fmt = (probe or {}).get("format", {}).get("format_name", "")
    return "image2" in fmt or fmt.endswith("_pipe")


def _janela(inicio, fim, estado, info) -> tuple[float | None, float | None, str]:
    est = dict(estado or {})
    est.setdefault("duracao", info["dur"])
    return core._janela_tempo(inicio, fim, est, info["dur"])


def _tempo(valor, estado, info) -> float:
    est = dict(estado or {})
    est.setdefault("duracao", info["dur"])
    return parse_tempo(valor, est)


def _executar(doc: Documento, info: dict, entrada: Path, grafo: list[str], descricao: str, progresso=None,
              entradas: list[str] | None = None, extras: list[str] = (), audios: dict[int, str] | None = None,
              novas: list[str] = (), ext: str | None = None, args_video: list[str] | None = None,
              tempo_mudou: bool = False, dur_saida: float | None = None, avisos: list[str] | None = None,
              filtros_8bits: str | None = None) -> dict:
    """Monta e roda uma edição de vídeo e registra a versão.
    grafo: partes do -filter_complex terminando em [vo] (com [0:V:0] = vídeo da entrada principal; a
    normalização de SAR é aplicada aqui). entradas: substitui a entrada única padrão (edições por segmentos).
    audios: {índice da faixa: rótulo} faixas substituídas por saídas do grafo (re-encodadas); as outras são
    copiadas sem perda. None (e tempo igual) = áudio, legendas e anexos copiados como estão.
    novas: rótulos de faixas de áudio novas (vão no fim). tempo_mudou: sem legendas/capítulos (os tempos mudaram)."""
    grafo = core._aplicar_pre(list(grafo), info)
    if info["w"] % 2 or info["h"] % 2:
        grafo[-1] = grafo[-1][: grafo[-1].rindex("[vo]")] + "[vimpar]"
        grafo.append(f"[vimpar]{core._PAR_FILTRO}[vo]")
    saida = doc.proxima_saida(ext) if ext else core._saida_edicao(doc)
    trocou = saida.suffix.lower() != entrada.suffix.lower()
    cmd = FFMPEG[:]
    cmd += list(entradas) if entradas is not None else info["dec"] + ["-i", str(entrada)]
    cmd += list(extras)
    cmd += ["-filter_complex", ";".join(grafo), "-map", "[vo]"]
    av = list(avisos or [])
    if audios is None and not novas and not tempo_mudou:
        cmd += core._mapas_copia(info, entrada, saida, "0")
    else:
        audios = audios or {}
        for k, s in enumerate(info["auds"]):
            if k in audios:
                cmd += ["-map", f"[{audios[k]}]"] + core.codec_audio(saida.suffix, k)
            elif tempo_mudou:
                continue
            else:
                cmd += ["-map", f"0:a:{k}"] + ([f"-c:a:{k}", "copy"] if not trocou else
                                                core.codec_audio(saida.suffix, k))
            cmd += core._tags_faixa(s, f"s:a:{k}")
        base = len(info["auds"])
        for j, rot in enumerate(novas):
            cmd += ["-map", f"[{rot}]"] + core.codec_audio(saida.suffix, base + j)
        if not tempo_mudou and not trocou:
            cmd += ["-map", "0:s?", "-c:s", "copy"]
            if info.get("anexos") and saida.suffix.lower() in (".mkv", ".mka"):
                cmd += ["-map", "0:t?", "-c:t", "copy"]
        elif info.get("legendas"):
            av.append("as legendas embutidas foram removidas" + (" (a linha do tempo mudou)" if tempo_mudou else ""))
    cmd += args_video or core._args_video_edicao(saida.suffix, info)
    cmd += core._tags_faixa(info["v"], "s:v:0")
    cmd += ["-map_metadata", "0"] + (["-map_chapters", "-1"] if tempo_mudou else [])
    cmd += core._faststart(saida) + [str(saida)]
    av += core._avisos_profundidade(info, filtros_8bits)
    return core._rodar_versao(doc, saida, [cmd], [dur_saida or info["dur"]], descricao, progresso,
                              {"avisos": av} if av else None)


def _efeito_em_janela(info: dict, construir, en: str, mistura: float = 1.0) -> list[str]:
    """[0:V:0] → [vo] aplicando `construir(ent, sai)` (partes do grafo), opcionalmente misturado com o original
    (mistura 0–1) e/ou só em [inicio, fim] (`en` = ':enable=...').
    Com janela/mistura: original e efeito vão para um `blend` cuja 1ª entrada é o original (fora da janela o
    blend passa a 1ª entrada intacta) e opacidade 1 − mistura (A·op + B·(1 − op))."""
    if not en and mistura >= 0.999:
        return construir("0:V:0", "vo")
    fmt = _fmt_trabalho(info)
    partes = [f"[0:V:0]format={fmt},split[vorig][vefe]"]
    partes += construir("vefe", "vef0")
    partes.append(f"[vef0]format={fmt}[vef]")
    partes.append(f"[vorig][vef]blend=all_mode=normal:all_opacity={1 - mistura:.4f}{en}[vo]")
    return partes


def _cadeia(filtros: str):
    return lambda ent, sai: [f"[{ent}]{filtros}[{sai}]"]


# ---------------------------------------------------------------- 1. efeito visual
def _sepia(k: float) -> str:
    """colorchannelmixer: sépia clássica misturada com a identidade (k = 0..1)."""
    s = [[.393, .769, .189], [.349, .686, .168], [.272, .534, .131]]
    m = []
    for i in range(3):
        for j in range(3):
            m.append((1 - k) * (1.0 if i == j else 0.0) + k * s[i][j])
    nomes = ["rr", "rg", "rb", "gr", "gg", "gb", "br", "bg", "bb"]
    return "colorchannelmixer=" + ":".join(f"{n}={v:.4f}" for n, v in zip(nomes, m))


def _ef(info: dict, nome: str, i: float):
    """(construir, mistura, filtro de 8 bits ou None) de cada efeito; i = intensidade 0–1."""
    w, h = info["w"], info["h"]
    if nome == "preto_e_branco":
        return _cadeia("hue=s=0"), i, None
    if nome == "sepia":
        return _cadeia(_sepia(i)), 1.0, None
    if nome == "vinheta":
        return _cadeia(f"vignette=angle={0.2 + 0.9 * i:.3f}"), 1.0, "vignette"
    if nome == "negativo":
        return _cadeia("negate"), i, None
    if nome == "desfoque":
        return _cadeia(f"gblur=sigma={max(0.5, i * h / 30):.2f}:steps=2"), 1.0, None
    if nome == "nitidez":
        f = f"cas=strength={min(1.0, i * 1.2):.3f}"
        if i > 0.6:  # o CAS sozinho é sutil: acima de 0,6 soma uma máscara de nitidez
            f += f",unsharp=5:5:{(i - 0.6) * 2.5:.2f}:5:5:0"
        return _cadeia(f), 1.0, None
    if nome == "pixelar":
        b = max(2, round(min(w, h) * (0.005 + 0.06 * i)))
        if _tem_filtro("pixelize"):
            f = f"pixelize=width={b}:height={b}"
        else:  # reduz e amplia sem suavizar
            f = f"scale={max(1, w // b)}:{max(1, h // b)}:flags=area,scale={w}:{h}:flags=neighbor"
        return _cadeia(f), 1.0, None
    if nome == "desenho":
        f = (f"format=gray,gblur=sigma=0.8,edgedetect=low={0.02 + 0.05 * (1 - i):.3f}:"
             f"high={0.05 + 0.1 * (1 - i):.3f},negate,erosion,format={_fmt_trabalho(info)}")
        return _cadeia(f), 1.0, "edgedetect"
    if nome == "contorno":
        fmt = _fmt_trabalho(info)

        def g(ent, sai):
            # contornos pretos multiplicados só na luminância (croma do original): visual de cartum
            return [f"[{ent}]format={fmt},split[ct0][ct1]",
                    f"[ct1]format=gray,gblur=sigma=1,edgedetect=low=0.04:high=0.1,negate,erosion,format={fmt}[ct2]",
                    f"[ct0][ct2]blend=c0_mode=multiply[{sai}]"]
        return g, i, "edgedetect"
    if nome == "granulado":
        return _cadeia(f"noise=alls={round(4 + 36 * i)}:allf=t+u"), 1.0, "noise"
    if nome == "filme_antigo":
        f = (f"{_sepia(0.35 + 0.5 * i)},noise=alls={round(8 + 22 * i)}:allf=t+u,"
             f"eq=brightness='{0.04 * i:.3f}*sin(2*PI*t*7)+{0.02 * i:.3f}*sin(2*PI*t*13)':eval=frame,"
             f"vignette=angle={0.5 + 0.4 * i:.3f}")
        return _cadeia(f), 1.0, "noise/eq"
    if nome == "vhs":
        d = max(1, round(w / 1280 * (2 + 6 * i)))
        lin = max(2, round(h / 180))
        f = (f"gblur=sigma={0.6 + 1.2 * i:.2f}:sigmaV=0.3,rgbashift=rh=-{d}:bh={d},"
             f"eq=saturation={1.15 + 0.3 * i:.2f}:contrast=1.05,noise=alls={round(6 + 14 * i)}:allf=t,"
             f"drawgrid=w=iw:h={lin * 2}:t={lin}:c=black@{0.12 + 0.2 * i:.2f}")
        return _cadeia(f), 1.0, "noise/eq"
    if nome == "espelho":
        def g(ent, sai):
            return [f"[{ent}]split[em0][em1]", f"[em1]crop=iw/2:ih:0:0,hflip[em2]",
                    f"[em0][em2]overlay=x=W-w:y=0[{sai}]"]
        return g, i, None
    if nome == "brilho_suave":
        sg = max(4.0, h / 40)

        def g(ent, sai):
            # só a luminância recebe o "screen" (no croma o screen tingiria as cores)
            return [f"[{ent}]split[gl0][gl1]", f"[gl1]gblur=sigma={sg:.1f}[gl2]",
                    f"[gl0][gl2]blend=c0_mode=screen:c0_opacity={0.35 + 0.5 * i:.2f}[{sai}]"]
        return g, 1.0, None
    raise AssertionError(nome)


EFEITOS = {
    "preto_e_branco": ("preto e branco (intensidade < 1 = cores desbotadas)", 1.0),
    "sepia": ("tom sépia de foto antiga", 0.8),
    "vinheta": ("escurece as bordas", 0.5),
    "negativo": ("cores invertidas", 1.0),
    "desfoque": ("desfoque geral (gaussiano)", 0.3),
    "nitidez": ("realça detalhes (CAS + unsharp)", 0.5),
    "pixelar": ("imagem em blocos (8 bits)", 0.5),
    "desenho": ("linhas pretas sobre branco, como esboço a lápis", 0.5),
    "contorno": ("contornos coloridos (cartum)", 1.0),
    "granulado": ("grão de filme", 0.4),
    "filme_antigo": ("sépia + grão + cintilação + vinheta", 0.6),
    "vhs": ("fita VHS / retrô: cores vazadas, borrão, ruído e linhas", 0.5),
    "espelho": ("metade esquerda espelhada na direita", 1.0),
    "brilho_suave": ("brilho difuso/sonho (glow)", 0.5),
}
_ALIASES = {
    "pb": "preto_e_branco", "p&b": "preto_e_branco", "preto_branco": "preto_e_branco", "cinza": "preto_e_branco",
    "monocromatico": "preto_e_branco", "grayscale": "preto_e_branco", "sepia_antiga": "sepia",
    "inverter_cores": "negativo", "invertido": "negativo", "desfocar": "desfoque", "borrar": "desfoque",
    "blur": "desfoque", "nitido": "nitidez", "sharpen": "nitidez", "pixelado": "pixelar", "pixel": "pixelar",
    "8bit": "pixelar", "8_bits": "pixelar", "esboco": "desenho", "sketch": "desenho", "lapis": "desenho",
    "bordas": "contorno", "neon": "contorno", "cartoon": "contorno", "desenho_animado": "contorno",
    "grao": "granulado", "ruido": "granulado", "filme": "granulado", "antigo": "filme_antigo",
    "velho": "filme_antigo", "retro": "vhs", "fita": "vhs", "glow": "brilho_suave", "sonho": "brilho_suave",
    "espelhar": "espelho",
}


def efeito_visual(doc: Documento, efeito: str, intensidade: float | None = None, inicio=None, fim=None,
                  estado: dict | None = None, progresso=None) -> dict:
    """Aplica um efeito visual pronto (ver EFEITOS) no vídeo todo ou só em [inicio, fim].
    intensidade 0–1 (padrão por efeito)."""
    nome = _chave(efeito)
    nome = _ALIASES.get(nome, nome)
    if nome not in EFEITOS:
        raise ValueError(f"efeito desconhecido: {efeito}. Opções: {', '.join(EFEITOS)}")
    i = _num(intensidade, "intensidade", 0.0, 1.0, EFEITOS[nome][1])
    entrada = doc.atual()
    info = core._midia(entrada)
    a, b, en = _janela(inicio, fim, estado, info)
    construir, mistura, f8 = _ef(info, nome, i)
    grafo = _efeito_em_janela(info, construir, en, mistura)
    desc = f"efeito {nome.replace('_', ' ')} ({i:g}){core._desc_janela(a, b)}"
    return _executar(doc, info, entrada, grafo, desc, progresso, filtros_8bits=f8)


# ---------------------------------------------------------------- 2. LUT
_EXT_LUT = {".cube", ".3dl", ".dat", ".m3d", ".csp"}


def aplicar_lut(doc: Documento, arquivo, intensidade: float = 1.0, inicio=None, fim=None,
                estado: dict | None = None, progresso=None) -> dict:
    """Aplica uma LUT de cor: .cube/.3dl/.dat/.m3d/.csp (lut3d) ou imagem Hald CLUT (.png/.tif; haldclut).
    intensidade 0–1 mistura com o original."""
    lut = _arquivo(arquivo, "LUT")
    k = _num(intensidade, "intensidade", 0.0, 1.0, 1.0)
    entrada = doc.atual()
    info = core._midia(entrada)
    a, b, en = _janela(inicio, fim, estado, info)
    extras: list[str] = []
    if lut.suffix.lower() in _EXT_LUT:
        _exigir_filtro("lut3d", "LUT 3D")
        construir = _cadeia(f"lut3d=file={esc_filtro(lut)}:interp=tetrahedral")
    elif lut.suffix.lower() in _IMAGENS:
        _exigir_filtro("haldclut", "LUT em imagem Hald CLUT")
        p = core.ffprobe(lut)
        s = next((x for x in p.get("streams", []) if x.get("codec_type") == "video"), None)
        lw, lh = (int(s.get("width") or 0), int(s.get("height") or 0)) if s else (0, 0)
        nivel = round(lw ** (1 / 3)) if lw else 0
        if lw != lh or nivel ** 3 != lw:
            raise ValueError(f"{lut.name} não parece uma Hald CLUT (precisa ser quadrada com lado = N³, ex. "
                             f"512x512 = nível 8); tem {lw}x{lh}")
        if "%" in lut.name:
            raise ValueError("renomeie a imagem da LUT sem '%' no nome")
        extras = ["-i", str(lut)]

        def construir(ent, sai):
            return [f"[1:v]format=rgb24[hald]", f"[{ent}][hald]haldclut=interp=tetrahedral[{sai}]"]
    else:
        raise ValueError(f"formato de LUT não suportado: {lut.suffix} (use .cube, .3dl, .dat, .m3d, .csp ou "
                         f"imagem Hald CLUT .png)")
    grafo = _efeito_em_janela(info, construir, en, k)
    desc = f"LUT {lut.name}" + (f" ({k:.0%})" if k < 1 else "") + core._desc_janela(a, b)
    return _executar(doc, info, entrada, grafo, desc, progresso, extras=extras)


# ---------------------------------------------------------------- 3. chroma key
_CORES_KEY = {"verde": "0x00B140", "green": "0x00B140", "azul": "0x0047BB", "blue": "0x0047BB"}


def _cor_hex(cor: str) -> str | None:
    s = str(cor).strip().lower()
    m = re.fullmatch(r"(?:#|0x)?([0-9a-f]{6})", s)
    return "0x" + m.group(1).upper() if m else None


def _amostrar_borda(entrada: Path, info: dict) -> tuple[int, int, int] | None:
    """Cor mediana da borda de alguns quadros (o fundo da tela verde costuma ocupar as bordas)."""
    rs, gs, bs = [], [], []
    for f in (0.2, 0.5, 0.8):
        t = info["dur"] * f
        cmd = [FFMPEG[0], "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{t:.3f}"] + info["dec"] + \
              ["-i", str(entrada), "-frames:v", "1", "-vf", "scale=64:36:flags=area,format=rgb24",
               "-f", "rawvideo", "-"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        d = r.stdout
        if len(d) < 64 * 36 * 3:
            continue
        for y in range(36):
            for x in range(64):
                if x in (0, 1, 62, 63) or y in (0, 1, 34, 35):
                    o = (y * 64 + x) * 3
                    rs.append(d[o])
                    gs.append(d[o + 1])
                    bs.append(d[o + 2])
    if not rs:
        return None
    med = lambda v: sorted(v)[len(v) // 2]  # noqa: E731
    return med(rs), med(gs), med(bs)


def chroma_key(doc: Documento, cor: str = "verde", similaridade: float = 0.15, suavidade: float = 0.08,
               fundo: str = "transparente", remover_reflexo: bool = True, progresso=None) -> dict:
    """Tira o fundo de tela verde/azul (chromakey + despill) e põe outro fundo.
    cor: 'verde' | 'azul' (detecta o tom real da tela pelas bordas do vídeo) | '#RRGGBB' | 'auto'.
    fundo: caminho de imagem ou vídeo, uma cor ('black', '#203040'...) ou 'transparente' (versão .webm VP9 com
    canal alfa)."""
    _exigir_filtro("chromakey", "chroma key")
    sim = _num(similaridade, "similaridade", 0.01, 0.6)
    suave = _num(suavidade, "suavidade", 0.0, 0.5)
    entrada = doc.atual()
    info = core._midia(entrada)
    w, h = info["w"], info["h"]
    c = _chave(cor)
    rgb = None
    if c in ("verde", "green", "azul", "blue", "auto"):
        amostra = _amostrar_borda(entrada, info)
        if amostra:
            r, g, b = amostra
            dom = "verde" if g >= b else "azul"
            # aceita a amostra se for de fato a cor pedida (canal dominante bem acima dos outros)
            canal = g if (c in ("verde", "green") or (c == "auto" and dom == "verde")) else b
            if canal > max(r, (b if canal == g else g)) + 30:
                rgb = amostra
        if rgb is None and c == "auto":
            raise ValueError("não achei uma tela verde/azul nas bordas do vídeo; passe cor='#RRGGBB'")
        hexa = "0x%02X%02X%02X" % rgb if rgb else _CORES_KEY[c]
    else:
        hexa = _cor_hex(cor)
        if not hexa:
            raise ValueError("cor: 'verde', 'azul', 'auto' ou '#RRGGBB'")
    rr, gg, bb = int(hexa[2:4], 16), int(hexa[4:6], 16), int(hexa[6:8], 16)
    tipo_despill = "green" if gg > max(rr, bb) else ("blue" if bb > max(rr, gg) else None)
    key = f"chromakey=color={hexa}:similarity={sim:g}:blend={suave:g}"
    if remover_reflexo and tipo_despill and _tem_filtro("despill"):
        key += f",format=gbrap,despill=type={tipo_despill}:mix=0.5:expand=0"
    fd = str(fundo or "transparente").strip()
    extras: list[str] = []
    avisos: list[str] = []
    if _chave(fd) in ("transparente", "alfa", "transparencia", "nenhum"):
        if not core._tem_encoder("libvpx-vp9"):
            raise RuntimeError("fundo transparente precisa do encoder libvpx-vp9 no ffmpeg")
        grafo = [f"[0:V:0]{key},format=yuva420p[vo]"]
        args = ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-crf", "28", "-b:v", "0",
                "-row-mt", "1", "-deadline", "good", "-cpu-used", "4"] + core._tags_cor(info["v"])
        avisos.append("versão em .webm (VP9 com transparência); para guardar com alfa use `exportar` com "
                      "predefinicao='transparente' (WebM) ou 'transparente_mov' (ProRes); outras edições de "
                      "imagem depois desta tiram a transparência")
        return _executar(doc, info, entrada, grafo, f"chroma key {hexa} → fundo transparente", progresso,
                         ext=".webm", args_video=args, avisos=avisos)
    p = Path(fd).expanduser()
    if p.is_file():
        p = p.resolve()
        pr = core.ffprobe(p)
        cobrir = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
        if _e_imagem(p, pr):
            extras = ["-loop", "1", "-framerate", info["taxa"], "-i", str(p)]
        else:
            sv = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video"), None)
            if not sv:
                raise ValueError(f"{p.name} não tem imagem")
            extras = ["-stream_loop", "-1"] + core._dec_entrada(sv) + ["-i", str(p)]
            cobrir = f"fps={info['taxa']}," + cobrir
        grafo = [f"[1:v]{cobrir},setpts=PTS-STARTPTS[fundo]", f"[0:V:0]{key}[fg]",
                 f"[fundo][fg]overlay=shortest=1:format=auto[vo]"]
        desc = f"chroma key {hexa} → fundo {p.name}"
    else:
        if _cor_hex(fd) is None and not re.fullmatch(r"[a-zA-Z]+(@[\d.]+)?", fd):
            raise ValueError(f"fundo: arquivo não encontrado ou cor inválida: {fd}")
        cor_f = ("#" + _cor_hex(fd)[2:]) if _cor_hex(fd) else fd
        grafo = [f"color=c={esc_filtro(cor_f)}:s={w}x{h}:r={info['taxa']}[fundo]", f"[0:V:0]{key}[fg]",
                 f"[fundo][fg]overlay=shortest=1:format=auto[vo]"]
        desc = f"chroma key {hexa} → fundo {fd}"
    return _executar(doc, info, entrada, grafo, desc, progresso, extras=extras, avisos=avisos)


# ---------------------------------------------------------------- 4. sobrepor vídeo / tela dividida
def _mix_audio(info: dict, entradas_sec: list[tuple[int, dict | None, float, float]], modo: str,
               a: float | None, b: float | None, volume: float) -> tuple[list[str], dict[int, str], list[str], list[str]]:
    """Áudio de sobrepor_video. entradas_sec: (índice da entrada, faixa de áudio ou None, atraso s, duração máx s).
    modo 'mixar' (soma à A1) ou 'sobreposto' (A1 muda para o som do sobreposto durante a janela).
    → (grafo, {0: rótulo} ou {}, novas faixas, avisos)."""
    grafo, avisos = [], []
    com_som = [(i, s, d, lim) for i, s, d, lim in entradas_sec if s]
    if not com_som:
        return [], {}, [], ["o vídeo sobreposto não tem som: ficou só o áudio principal"]
    if info["auds"]:
        s0 = info["auds"][0]
        ini, fim = core._extensao_faixa(info, s0)
        sr = int(s0.get("sample_rate") or 48000)
        lay = core._layout(s0)
    else:
        ini, fim, sr, lay = 0.0, info["dur"], 48000, "stereo"
    L = max(0.01, fim - ini)
    rot = []
    for j, (i, s, atraso, lim) in enumerate(com_som):
        d_ms = max(0.0, atraso - ini) * 1000
        # mono → estéreo duplicando o canal (o aresample faria −3 dB por canal)
        dup = "pan=stereo|c0=c0|c1=c0," if int(s.get("channels") or 0) == 1 and lay == "stereo" else ""
        f = (f"[{i}:a:0]{dup}aresample={sr},aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={lay},"
             f"asetpts=PTS-STARTPTS,atrim=end={lim:.6f}")
        if volume != 1:
            f += f",volume={volume:g}"
        if d_ms > 0.5:
            f += f",adelay={d_ms:.1f}:all=1"
        f += f",apad=whole_dur={L:.6f},atrim=end={L:.6f}[sa{j}]"
        grafo.append(f)
        rot.append(f"[sa{j}]")
    if not info["auds"]:
        if len(rot) == 1:
            grafo[-1] = grafo[-1].replace("[sa0]", "[anova]")
        else:
            grafo.append("".join(rot) + f"amix=inputs={len(rot)}:normalize=0:duration=longest,"
                         f"{core._LIMITADOR}[anova]")
        return grafo, {}, ["anova"], ["o vídeo principal não tinha som: o som do sobreposto virou a faixa A1"]
    principal = f"[0:a:0]{core._filtro_layout(info['auds'][0])}aformat=sample_fmts=fltp"
    if modo == "sobreposto":
        aa = a if a is not None else 0.0
        bb = b if b is not None else info["dur"]
        principal += f",volume=0:enable={esc_filtro(f'between(t,{aa:.3f},{bb:.3f})')}"
    grafo.append(principal + "[am]")
    grafo.append("[am]" + "".join(rot) + f"amix=inputs={len(rot) + 1}:normalize=0:duration=longest,"
                 f"{core._LIMITADOR}[amix]")
    return grafo, {0: "amix"}, [], avisos


def _entrada_sec(p: Path, info: dict, laco: bool) -> tuple[list[str], dict, bool]:
    """Argumentos de entrada de um arquivo sobreposto → (args, probe, é imagem)."""
    pr = core.ffprobe(p)
    img = _e_imagem(p, pr)
    sv = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video"
               and not s.get("disposition", {}).get("attached_pic")), None)
    if not sv:
        raise ValueError(f"{p.name} não tem imagem (só áudio?)")
    if img:
        return ["-i", str(p)], pr, True
    return (["-stream_loop", "-1"] if laco else []) + core._dec_entrada(sv) + ["-i", str(p)], pr, False


def _cantos_alfa(r: int) -> str:
    """geq que deixa transparente o lado de fora de cantos arredondados de raio r (com 1 px de suavização)."""
    dx = f"max(0,abs(X-(W-1)/2)-((W-1)/2-{r}))"
    dy = f"max(0,abs(Y-(H-1)/2)-((H-1)/2-{r}))"
    alfa = f"clip(255*({r}+0.5-hypot({dx},{dy})),0,255)"
    return f"format=yuva420p,geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':a={esc_filtro(alfa)}"


def sobrepor_video(doc: Documento, arquivos, modo: str = "pip", posicao="sup_dir", escala: float = 0.3,
                   inicio=None, fim=None, audio: str = "principal", volume: float = 1.0, borda: int = 0,
                   cor_borda: str = "white", cantos: float = 0, ao_acabar: str = "sumir",
                   estado: dict | None = None, progresso=None) -> dict:
    """Picture-in-picture (modo 'pip') ou tela dividida ('lado_a_lado', 'empilhado', 'grade') com outro(s)
    vídeo(s)/imagem(ns). A duração do vídeo principal não muda.
    ao_acabar (sobreposto mais curto): 'sumir' (pip) / 'congelar' (último quadro) / 'repetir' (loop) /
    'preto' (tela dividida). audio: 'principal' (copiado sem perda), 'mixar' ou 'sobreposto'."""
    lista = [arquivos] if isinstance(arquivos, (str, Path)) else list(arquivos or [])
    if not lista:
        raise ValueError("passe o arquivo a sobrepor (ou uma lista para tela dividida)")
    paths = [_arquivo(x, "arquivo") for x in lista]
    modo = _chave(modo)
    modo = {"picture_in_picture": "pip", "janela": "pip", "lado": "lado_a_lado", "horizontal": "lado_a_lado",
            "split": "lado_a_lado", "vertical": "empilhado", "em_cima": "empilhado", "mosaico": "grade",
            "grid": "grade"}.get(modo, modo)
    if modo not in ("pip", "lado_a_lado", "empilhado", "grade"):
        raise ValueError("modo: 'pip', 'lado_a_lado', 'empilhado' ou 'grade'")
    audio = _chave(audio) or "principal"
    if audio not in ("principal", "mixar", "sobreposto"):
        raise ValueError("audio: 'principal', 'mixar' ou 'sobreposto'")
    ao_acabar = _chave(ao_acabar) or "sumir"
    if ao_acabar not in ("sumir", "congelar", "repetir", "preto"):
        raise ValueError("ao_acabar: 'sumir', 'congelar', 'repetir' ou 'preto'")
    vol = _num(volume, "volume", 0.0, 4.0, 1.0)
    entrada = doc.atual()
    info = core._midia(entrada)
    W, H, taxa, dur = info["w"], info["h"], info["taxa"], info["dur"]
    extras: list[str] = []
    grafo: list[str] = []
    sec: list[tuple[int, dict | None, float, float]] = []
    if modo == "pip":
        if len(paths) != 1:
            raise ValueError("modo 'pip' sobrepõe um arquivo só (para vários use 'grade' ou 'lado_a_lado')")
        p = paths[0]
        a, b, en = _janela(inicio, fim, estado, info)
        a0 = a if a is not None else 0.0
        b0 = b if b is not None else dur
        args, pr, img = _entrada_sec(p, info, ao_acabar == "repetir")
        extras += args
        e = float(escala)
        pw = core._par(e * W) if e <= 1 else core._par(e)
        if not 16 <= pw <= W:
            raise ValueError("escala: fração da largura do vídeo (0.1–1) ou pixels")
        bd = max(0, int(borda or 0))
        f = [f"scale={pw}:-2:flags=lanczos,setsar=1"]
        if not img:
            f.insert(0, f"fps={taxa}")
            f.append(f"setpts=PTS-STARTPTS+{a0:.6f}/TB")
            if ao_acabar == "congelar":
                f.append("tpad=stop_mode=clone:stop=-1")
            if ao_acabar in ("congelar", "repetir"):
                f.append(f"trim=end={b0:.6f}")
        if bd:
            f.append(f"pad=iw+{2 * bd}:ih+{2 * bd}:{bd}:{bd}:color={esc_filtro(cor_borda)}")
        if cantos:
            r = float(cantos)
            # raio: px, ou fração (< 1) da menor dimensão do pip
            ph = core._par(pw * (H / W))  # aproximado (a altura real depende do arquivo)
            rr = max(2, int(r * min(pw, ph))) if r < 1 else int(r)
            f.append(_cantos_alfa(rr))
        margem = max(8, round(H * 0.03))
        x, y = core._posicao_xy(posicao or "sup_dir", W, H, "w", "h", margem)
        eof = "repeat" if img or ao_acabar == "congelar" else "pass"
        grafo += [f"[1:v]{','.join(f)}[pip]",
                  f"[0:V:0][pip]overlay=x={esc_filtro(x)}:y={esc_filtro(y)}:eof_action={eof}:format=auto"
                  f"{en}[vo]"]
        if not img:
            sa = next((s for s in pr.get("streams", []) if s.get("codec_type") == "audio"), None)
            d_sec = core.duracao(pr)
            lim = (b0 - a0) if ao_acabar == "repetir" else min(b0 - a0, d_sec or (b0 - a0))
            sec.append((1, sa, a0, lim))
        desc = f"sobrepôs {p.name} ({posicao}, {pw}px){core._desc_janela(a, b)}"
    else:
        if inicio is not None or fim is not None:
            raise ValueError("inicio/fim só valem no modo 'pip' (a tela dividida vale para o vídeo todo)")
        if len(paths) > 8:
            raise ValueError("no máximo 8 arquivos na tela dividida")
        n = len(paths) + 1
        if modo == "grade":
            cols = math.ceil(math.sqrt(n))
            rows = math.ceil(n / cols)
            cw, ch = core._par(W / cols), core._par(H / rows)
        rot = []
        for j in range(n):
            if j == 0:
                ent = core._com_pre(info, "0:V:0")
                cad: list[str] = []
            else:
                p = paths[j - 1]
                args, pr, img = _entrada_sec(p, info, ao_acabar == "repetir")
                if img:
                    args = ["-loop", "1", "-framerate", taxa] + args
                extras += args
                ent = f"[{j}:v]"
                cad = [f"fps={taxa}", "setpts=PTS-STARTPTS"]
                if not img:
                    if ao_acabar == "congelar":
                        cad.append("tpad=stop_mode=clone:stop=-1")
                    elif ao_acabar in ("preto", "sumir"):
                        cad.append("tpad=stop_mode=add:stop=-1:color=black")
                    sa = next((s for s in pr.get("streams", []) if s.get("codec_type") == "audio"), None)
                    d_sec = core.duracao(pr)
                    sec.append((j, sa, 0.0, dur if ao_acabar == "repetir" else min(dur, d_sec or dur)))
                cad.append(f"trim=end={dur + 1:.3f}")
            if modo == "lado_a_lado":
                cad.append(f"scale=-2:{H}:flags=lanczos" if j else "null")
            elif modo == "empilhado":
                cad.append(f"scale={W}:-2:flags=lanczos" if j else "null")
            else:
                cad.append(f"scale={cw}:{ch}:force_original_aspect_ratio=decrease:flags=lanczos,"
                           f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black")
            cad.append(f"setsar=1,format={_fmt_trabalho(info)}")
            grafo.append(f"{ent}{','.join(cad)}[c{j}]")
            rot.append(f"[c{j}]")
        if modo == "lado_a_lado":
            grafo.append("".join(rot) + f"hstack=inputs={n}:shortest=1[pilha]")
        elif modo == "empilhado":
            grafo.append("".join(rot) + f"vstack=inputs={n}:shortest=1[pilha]")
        else:
            layout = "|".join(f"{(j % cols) * cw}_{(j // cols) * ch}" for j in range(n))
            grafo.append("".join(rot) + f"xstack=inputs={n}:layout={layout}:fill=black:shortest=1[pilha]")
        # soma de larguras/alturas pode dar ímpar (vídeos com proporções diferentes): força pares
        grafo.append(f"[pilha]{core._PAR_FILTRO}[vo]")
        a = b = None
        desc = f"tela dividida ({modo.replace('_', ' ')}) com {', '.join(p.name for p in paths)}"
    audios, novas, avisos = None, [], []
    if audio != "principal":
        ga, audios, novas, avisos = _mix_audio(info, sec, audio, a, b, vol)
        grafo += ga
        if not ga:
            audios = None
        else:
            desc += f" (áudio: {audio})"
    return _executar(doc, info, entrada, grafo, desc, progresso, extras=extras, audios=audios, novas=novas,
                     avisos=avisos)


# ---------------------------------------------------------------- 5. zoom animado
def _alvo(alvo, W: int, H: int) -> tuple[float, float, float | None]:
    """[x, y] (centro) ou [x, y, largura, altura] (retângulo), em px ou frações 0–1 → (cx, cy, fator do retângulo)."""
    if alvo is None:
        return W / 2, H / 2, None
    if isinstance(alvo, str):
        nome = _chave(alvo)
        if nome in core._ANCORAS:
            ax, ay = core._ANCORAS[nome]
            return {"l": W / 4, "c": W / 2, "r": 3 * W / 4}[ax], {"t": H / 4, "c": H / 2, "b": 3 * H / 4}[ay], None
        alvo = re.split(r"[,;\s]+", alvo.strip())
    v = [float(str(x).replace(",", ".")) for x in alvo]
    if len(v) == 2:
        x, y = v
        if x <= 1 and y <= 1:
            x, y = x * W, y * H
        return x, y, None
    if len(v) == 4:
        x, y, rw, rh = core._retangulo(v, W, H, "alvo")
        return x + rw / 2, y + rh / 2, min(W / rw, H / rh)
    raise ValueError("alvo: [x, y] (centro) ou [x, y, largura, altura], em pixels ou frações 0–1, ou nome "
                     "('centro', 'topo', 'sup_dir'...)")


def zoom(doc: Documento, modo: str = "aproximar", alvo=None, fator: float | None = None, inicio=None, fim=None,
         transicao_s: float | None = None, suave: bool = True, estado: dict | None = None, progresso=None) -> dict:
    """Zoom animado (perspective com expressões por quadro: subpixel, sem tremor, mesmo tamanho de quadro).
    modo 'aproximar': entra no zoom em transicao_s, segura e sai no fim da janela; 'punch': corte seco para o
    zoom (Shorts); 'ken_burns': aproxima devagar durante a janela toda; 'afastar': começa no zoom e abre."""
    _exigir_filtro("perspective", "zoom animado")
    modo = _chave(modo) or "aproximar"
    modo = {"zoom_in": "aproximar", "entrar": "aproximar", "punch_in": "punch", "corte": "punch",
            "kenburns": "ken_burns", "panoramica": "ken_burns", "zoom_out": "afastar", "abrir": "afastar"}.get(modo, modo)
    if modo not in ("aproximar", "punch", "ken_burns", "afastar"):
        raise ValueError("modo: 'aproximar', 'punch', 'ken_burns' ou 'afastar'")
    entrada = doc.atual()
    info = core._midia(entrada)
    W, H, fps, dur = info["w"], info["h"], info["fps"], info["dur"]
    cx, cy, f_ret = _alvo(alvo, W, H)
    padrao = {"punch": 1.3, "aproximar": 1.6, "ken_burns": 1.25, "afastar": 1.4}[modo]
    Z = _num(fator if fator is not None else (f_ret or padrao), "fator", 1.01, 6.0)
    est = dict(estado or {})
    est.setdefault("duracao", dur)
    a = max(0.0, parse_tempo(inicio, est)) if inicio is not None else 0.0
    b = min(dur, parse_tempo(fim, est)) if fim is not None else dur
    if b - a < 0.1:
        raise ValueError(f"intervalo curto demais: {fmt_tempo(a)} → {fmt_tempo(b)}")
    T = f"(in/{fps:.6f}+{info['atraso_video']:.6f})"

    def rampa(t0: float, d: float) -> str:
        if d <= 1e-3:
            return f"gte({T},{t0:.4f})"
        return f"clip(({T}-{t0:.4f})/{d:.4f},0,1)"

    def suavizar(reg: int) -> str:
        return f"st({reg},ld({reg})*ld({reg})*(3-2*ld({reg})));" if suave else ""

    if modo in ("aproximar", "punch"):
        ti = 0.0 if modo == "punch" else _num(transicao_s, "transicao_s", 0.0, 10.0, 0.6)
        ti = min(ti, (b - a) / 2)
        sai = b < dur - 0.05
        prog = f"st(0,{rampa(a, ti)});" + suavizar(0)
        if sai:
            prog += f"st(1,{rampa(b - ti, ti) if ti > 1e-3 else f'gte({T},{b:.4f})'});" + suavizar(1)
            prog += "st(1,min(ld(0),1-ld(1)));"
        else:
            prog += "st(1,ld(0));"
    else:
        prog = f"st(0,{rampa(a, b - a)});" + suavizar(0)
        prog += "st(1,ld(0));" if modo == "ken_burns" else "st(1,1-ld(0));"
    # ld(1) = quanto do zoom (0–1); ld(2) = fator atual; ld(3)/ld(4) = meia largura/altura do recorte
    base = prog + f"st(2,1+{Z - 1:.5f}*ld(1));st(3,W/(2*ld(2)));st(4,H/(2*ld(2)));"
    ex = f"clip({W / 2:.3f}+({cx - W / 2:.3f})*ld(1),ld(3),W-ld(3))"
    ey = f"clip({H / 2:.3f}+({cy - H / 2:.3f})*ld(1),ld(4),H-ld(4))"
    xs = {"-": f"{base}{ex}-ld(3)", "+": f"{base}{ex}+ld(3)"}
    ys = {"-": f"{base}{ey}-ld(4)", "+": f"{base}{ey}+ld(4)"}
    op = [f"x0={esc_filtro(xs['-'])}", f"y0={esc_filtro(ys['-'])}", f"x1={esc_filtro(xs['+'])}",
          f"y1={esc_filtro(ys['-'])}", f"x2={esc_filtro(xs['-'])}", f"y2={esc_filtro(ys['+'])}",
          f"x3={esc_filtro(xs['+'])}", f"y3={esc_filtro(ys['+'])}", "interpolation=cubic", "sense=source",
          "eval=frame"]
    fim_ef = b if modo in ("ken_burns", "afastar") or b < dur - 0.05 else dur + 1
    en = ":enable=" + esc_filtro(f"between(t,{a:.3f},{fim_ef:.3f})")
    grafo = [f"[0:V:0]perspective={':'.join(op)}{en}[vo]"]
    desc = f"zoom {modo.replace('_', ' ')} ×{Z:g} em ({cx:.0f},{cy:.0f}) {fmt_tempo(a)}→{fmt_tempo(b)}"
    return _executar(doc, info, entrada, grafo, desc, progresso)


# ---------------------------------------------------------------- 6. linha do tempo: inverter, repetir, lenta
def _total_quadros(info: dict) -> int:
    v = info["v"]
    try:
        n = int(v.get("nb_frames") or 0)
    except ValueError:
        n = 0
    dur_v = None
    try:
        dur_v = float(v.get("duration") or 0) or None
    except ValueError:
        pass
    est = round((dur_v or (info["dur"] - info["atraso_video"])) * info["fps"])
    return n if n and abs(n - est) <= 2 else est


def _quadro(t: float, info: dict) -> int:
    return max(0, min(_total_quadros(info), round((t - info["atraso_video"]) * info["fps"])))


def _orcamento_quadros(info: dict) -> int:
    """Quantos quadros decodificados cabem na memória de trabalho (reverse/loop guardam os quadros crus)."""
    # medido: `reverse` de 288 quadros 720p yuv420p ≈ 836 MB de RSS (≈ 2,1× o quadro cru: alinhamento/pool)
    bpp = 3.0 if core._10bits(info.get("pix_fmt", "")) else 1.5
    quadro = info["w"] * info["h"] * bpp * 2.1
    livre = 4e9
    try:
        for linha in Path("/proc/meminfo").read_text().splitlines():
            if linha.startswith("MemAvailable:"):
                livre = int(linha.split()[1]) * 1024
    except OSError:
        pass
    # na troca de pedaço o anterior ainda está na fila do encoder enquanto o próximo enche: até 2 na memória
    orc = max(48e6, min(300e6, livre / 8))
    return max(8, int(orc / quadro))


def _filtros_segmento(info: dict, entrada: Path, i: int, sg: dict, pre_aqui: bool) -> tuple[list[str], list[str], float | None, float]:
    """Entrada e cadeias de UM segmento (rótulos [v{i}] e [a{k}_{i}]). Devolve (args de entrada, partes do
    grafo, duração de saída ou None = até o fim, instante de início na origem).
    pre_aqui: aplica a normalização de SAR na própria cadeia (fora de _executar, que só cuida de [0:V:0])."""
    fps, taxa = info["fps"], info["taxa"]
    k0, n = int(sg["k0"]), sg.get("n")
    t0 = float(sg.get("atraso", info["atraso_video"])) + k0 / fps
    ss = t0 - 0.25 / fps
    args = ["-threads", "2"]
    if k0 > 0 and ss > 0.0005:
        args += ["-ss", f"{ss:.6f}"]
        off = t0 - ss
    else:
        off = t0
    if n is not None:
        args += ["-t", f"{off + n / fps + 0.5:.6f}"]
    args += sg.get("entrada_args") or info["dec"] + ["-i", str(entrada)]
    grafo: list[str] = []
    # vídeo
    v = [f"fps={taxa}"]
    if n is not None:
        v.append(f"trim=end_frame={n}")
    v.append(f"setpts=N/({taxa})/TB")
    n_out = n
    if sg.get("rev"):
        v.append("reverse")
    laco = int(sg.get("laco") or 1)
    if laco > 1:
        v.append(f"loop=loop={laco - 1}:size={n}:start=0")
        n_out = n * laco
    lento = sg.get("lento")
    if lento:
        n_out = round(n / lento)
        v.append(f"setpts=PTS/{lento:.6f}")
        interp = sg.get("interp") or "mci"
        if interp == "mci":
            v.append(f"minterpolate=fps={taxa}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
        elif interp == "mesclar":
            v.append(f"framerate=fps={taxa}")
        v.append(f"fps={taxa}")
        v.append(f"tpad=stop_mode=clone:stop=-1,trim=end_frame={n_out}")
    if sg.get("total") is not None:
        n_out = min(n_out, int(sg["total"]))
        v.append(f"trim=end_frame={n_out}")
    v.append(f"setpts=N/({taxa})/TB")
    if sg.get("sem_pre"):
        ent_v = f"[{i}:v:0]"  # intermediário já normalizado ('v' minúsculo: _aplicar_pre não mexe)
    elif i == 0 and not pre_aqui:
        ent_v = "[0:V:0]"  # _executar aplica a normalização
    else:
        ent_v = core._com_pre(info, f"{i}:V:0")
    grafo.append(f"{ent_v}{','.join(v)}[v{i}]")
    d_out = n_out / fps if n_out is not None else None
    # áudio
    for k, s in enumerate(info["auds"]):
        sr = int(s.get("sample_rate") or 48000)
        f = [f"{core._filtro_layout(s)}aresample=async=1:min_hard_comp=0.01:first_pts=0"]
        if n is not None:
            d = n / fps
            f += [f"atrim=start={off:.6f}:end={off + d:.6f}", "asetpts=PTS-STARTPTS",
                  f"apad=whole_dur={d:.6f}", f"atrim=end={d:.6f}"]
        else:
            f += [f"atrim=start={off:.6f}", "asetpts=PTS-STARTPTS"]
        if sg.get("rev"):
            f.append("areverse")
        if laco > 1:
            f.append(f"aloop=loop={laco - 1}:size={round(n / fps * sr)}:start=0")
        if lento:
            f.append(core._cadeia_atempo(lento))
        if sg.get("mudo"):
            f.append("volume=0")
        if d_out is not None and (laco > 1 or lento or sg.get("total") is not None):
            f += ["asetpts=N/SR/TB", f"apad=whole_dur={d_out:.6f}", f"atrim=end={d_out:.6f}"]
        grafo.append(f"[{i}:a:{k}]{','.join(f)}[a{k}_{i}]")
    return args, grafo, d_out, t0


def _segmentos(doc: Documento, info: dict, entrada: Path, segs: list[dict], descricao: str,
               progresso=None, avisos: list[str] | None = None) -> dict:
    """Reconstrói o vídeo por segmentos de QUADROS, cada um uma entrada própria (`-ss` ¼ de quadro antes do 1º
    quadro, como em objetos.py; nada de split/trim no vídeo inteiro).
    seg: {"k0": 1º quadro, "n": nº de quadros (None = até o fim), "rev": inverte, "laco": repetições,
          "total": nº de quadros de saída (corta a última repetição), "lento": fator < 1 com "interp"
          ('mci' minterpolate | 'mesclar' framerate | 'duplicar' fps), "mudo": silencia o trecho,
          "entrada_args"/"atraso"/"sem_pre": entrada alternativa (intermediário já normalizado)}.
    Vídeo: fps da origem, trim por nº de quadros, pts refeitos (N/fps). Áudio: exatamente n/fps segundos
    por segmento (aresample+atrim+apad), invertido com areverse, repetido com aloop, esticado com atempo.
    Vídeo e áudio têm concats separados (ver core._segmentos_av)."""
    auds = info["auds"]
    if len(segs) > _MAX_ENTRADAS:
        raise ValueError(f"operação precisaria de {len(segs)} trechos (máx. {_MAX_ENTRADAS}); faça em partes "
                         f"menores")
    entradas: list[str] = []
    grafo: list[str] = []
    cv = ""
    ca = [""] * len(auds)
    dur_saida = 0.0
    for i, sg in enumerate(segs):
        args, g, d_out, t0 = _filtros_segmento(info, entrada, i, sg, pre_aqui=False)
        entradas += args
        grafo += g
        cv += f"[v{i}]"
        for k in range(len(auds)):
            ca[k] += f"[a{k}_{i}]"
        dur_saida += d_out if d_out is not None else max(0.0, info["dur"] - t0)
    n = len(segs)
    grafo.append(f"{cv}concat=n={n}:v=1:a=0[vo]")
    for k in range(len(auds)):
        grafo.append(f"{ca[k]}concat=n={n}:v=0:a=1[ao{k}]")
    return _executar(doc, info, entrada, grafo, descricao, progresso, entradas=entradas,
                     audios={k: f"ao{k}" for k in range(len(auds))}, tempo_mudou=True, dur_saida=dur_saida,
                     avisos=avisos)


def _intermediarios(doc: Documento, info: dict, entrada: Path, pedacos: list[dict], pasta: Path,
                    progresso=None, peso: float = 0.5, vezes: int = 1) -> dict:
    """1ª passada das operações que guardam quadros crus (reverse/loop longos): renderiza cada pedaço, UM
    comando por vez (pico de memória = um pedaço), num intermediário sem perda (FFV1 + FLAC em .mkv) e devolve
    UM segmento que lê todos pelo concat demuxer (listados `vezes` vezes seguidas). Quem chama apaga `pasta`."""
    fps = info["fps"]
    arquivos: list[tuple[Path, int]] = []
    total = sum(int(p["n"]) for p in pedacos)
    feito = 0
    for j, sg in enumerate(pedacos):
        n = int(sg["n"])
        sg = {k: v for k, v in sg.items() if k != "mudo"}  # silêncio é aplicado na passada final
        args, g, d_out, _ = _filtros_segmento(info, entrada, 0, sg, pre_aqui=True)
        saida = pasta / f"p{j:03d}.mkv"
        cmd = FFMPEG[:] + args + ["-filter_complex", ";".join(g), "-map", "[v0]"]
        for k in range(len(info["auds"])):
            cmd += ["-map", f"[a{k}_0]", f"-c:a:{k}", "flac"]
        cmd += ["-c:v", "ffv1", "-level", "3", "-slices", "4", "-g", "1", "-threads", "2",
                "-pix_fmt", info.get("pix_fmt") or "yuv420p", "-map_metadata", "-1", str(saida)]
        core._rodar_com_progresso(cmd, d_out or n / fps, progresso, peso * feito / total, peso * n / total)
        feito += n
        arquivos.append((saida, n))
    lista = pasta / "lista.txt"
    linhas = ["ffconcat version 1.0"]
    for _ in range(vezes):
        for arq, n in arquivos:
            linhas += [f"file '{arq.name}'", f"duration {n / fps:.6f}"]
    lista.write_text("\n".join(linhas) + "\n")
    return {"entrada_args": ["-f", "concat", "-safe", "0", "-i", str(lista)], "atraso": 0.0, "sem_pre": True,
            "k0": 0, "n": total * vezes}


def _pasta_temp(doc: Documento, prefixo: str) -> Path:
    """Pasta temporária ao lado das versões (mesmo disco do vídeo; intermediários sem perda são grandes)."""
    doc.pasta.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefixo, dir=doc.pasta))


def _progresso_parcial(progresso, base: float, peso: float):
    if not progresso:
        return None
    return lambda f: progresso(base + peso * f)


def _trecho_quadros(info, inicio, fim, estado) -> tuple[int, int, int]:
    """(1º quadro, quadro depois do último, total de quadros) de [inicio, fim] (padrão: o vídeo todo)."""
    est = dict(estado or {})
    est.setdefault("duracao", info["dur"])
    N = _total_quadros(info)
    a = parse_tempo(inicio, est) if inicio is not None else 0.0
    b = parse_tempo(fim, est) if fim is not None else info["dur"]
    ka = _quadro(max(0.0, a), info)
    kb = N if fim is None else _quadro(min(b, info["dur"]), info)
    if kb - ka < 2:
        raise ValueError(f"trecho curto demais: {fmt_tempo(a)} → {fmt_tempo(b)}")
    return ka, kb, N


def _pedacos_invertidos(ka: int, kb: int, maximo: int) -> list[dict]:
    """[ka, kb) invertido em pedaços de até `maximo` quadros (do fim para o começo)."""
    out, fim_p = [], kb
    while fim_p > ka:
        ini = max(ka, fim_p - maximo)
        out.append({"k0": ini, "n": fim_p - ini, "rev": True})
        fim_p = ini
    return out


def inverter_video(doc: Documento, modo: str = "reverso", inicio=None, fim=None, audio: bool = True,
                   estado: dict | None = None, progresso=None) -> dict:
    """Toca [inicio, fim] (padrão: o vídeo todo) de trás para frente ('reverso') ou ida e volta ('boomerang').
    audio=True inverte o som junto; False deixa o trecho invertido mudo."""
    modo = _chave(modo) or "reverso"
    modo = {"inverter": "reverso", "tras_para_frente": "reverso", "ao_contrario": "reverso",
            "ida_e_volta": "boomerang", "vai_e_volta": "boomerang"}.get(modo, modo)
    if modo not in ("reverso", "boomerang"):
        raise ValueError("modo: 'reverso' ou 'boomerang'")
    entrada = doc.atual()
    info = core._midia(entrada)
    fps = info["fps"]
    ka, kb, N = _trecho_quadros(info, inicio, fim, estado)
    maximo = _orcamento_quadros(info)
    pedacos = _pedacos_invertidos(ka, kb, maximo)
    a, b = ka / fps, kb / fps
    desc = f"{'boomerang' if modo == 'boomerang' else 'inverteu'} {fmt_tempo(a)}→{fmt_tempo(b)}"
    if not audio:
        desc += " (trecho invertido mudo)"
    pasta = None
    try:
        prog_final = progresso
        if len(pedacos) > 1:
            # longo demais para um `reverse` só: 1ª passada pedaço a pedaço em intermediários sem perda
            pasta = _pasta_temp(doc, "inverter-")
            pedacos = [_intermediarios(doc, info, entrada, pedacos, pasta, progresso, 0.5)]
            prog_final = _progresso_parcial(progresso, 0.5, 0.5)
        segs: list[dict] = []
        if ka > 0:
            segs.append({"k0": 0, "n": ka})
        if modo == "boomerang":
            segs.append({"k0": ka, "n": kb - ka})
        for p in pedacos:
            p["mudo"] = not audio
        segs += pedacos
        if kb < N:
            segs.append({"k0": kb, "n": None})
        v = _segmentos(doc, info, entrada, segs, desc, prog_final)
    finally:
        if pasta:
            shutil.rmtree(pasta, ignore_errors=True)
    fim_novo = b + (b - a if modo == "boomerang" else 0)
    v["trecho_novo"] = [fmt_tempo(a), fmt_tempo(fim_novo)]
    return v


def repetir(doc: Documento, vezes: int | None = None, duracao_alvo_s: float | None = None, inicio=None, fim=None,
            estado: dict | None = None, progresso=None) -> dict:
    """Repete [inicio, fim] (padrão: o vídeo todo) `vezes` vezes seguidas (2 = toca duas vezes), ou até o
    trecho repetido somar `duracao_alvo_s` (a última repetição é cortada no ponto exato)."""
    if (vezes is None) == (duracao_alvo_s is None):
        raise ValueError("passe vezes OU duracao_alvo_s")
    entrada = doc.atual()
    info = core._midia(entrada)
    fps = info["fps"]
    ka, kb, N = _trecho_quadros(info, inicio, fim, estado)
    n = kb - ka
    if vezes is not None:
        R = int(_num(vezes, "vezes", 2, 1000))
        total = n * R
    else:
        alvo = _num(duracao_alvo_s, "duracao_alvo_s", 0.1, 4 * 3600)
        total = round(alvo * fps)
        if total <= n:
            raise ValueError(f"duracao_alvo_s ({alvo:g} s) precisa ser maior que o trecho ({n / fps:.2f} s)")
        R = math.ceil(total / n)
    if total / fps > 4 * 3600:
        raise ValueError("resultado passaria de 4 horas")
    a, b = ka / fps, kb / fps
    desc = f"repetiu {fmt_tempo(a)}→{fmt_tempo(b)} " + (f"{R}×" if vezes is not None else
                                                       f"até {total / fps:g} s")
    pasta = None
    try:
        prog_final = progresso
        if n <= _orcamento_quadros(info):
            meio = {"k0": ka, "n": n, "laco": R, "total": total}
        else:
            # trecho grande demais para o filtro loop (guarda os quadros crus): renderiza uma vez sem perda e
            # lista o intermediário R vezes no concat demuxer
            pasta = _pasta_temp(doc, "repetir-")
            meio = _intermediarios(doc, info, entrada, [{"k0": ka, "n": n}], pasta, progresso, 0.3, vezes=R)
            meio["total"] = total
            prog_final = _progresso_parcial(progresso, 0.3, 0.7)
        segs: list[dict] = []
        if ka > 0:
            segs.append({"k0": 0, "n": ka})
        segs.append(meio)
        if kb < N:
            segs.append({"k0": kb, "n": None})
        v = _segmentos(doc, info, entrada, segs, desc, prog_final)
    finally:
        if pasta:
            shutil.rmtree(pasta, ignore_errors=True)
    v["trecho_novo"] = [fmt_tempo(a), fmt_tempo(a + total / fps)]
    return v


def camera_lenta_suave(doc: Documento, fator: float = 0.5, inicio=None, fim=None, metodo: str = "interpolar",
                       audio: str = "esticar", estado: dict | None = None, progresso=None) -> dict:
    """Câmera lenta com quadros intermediários CRIADOS (minterpolate, 'interpolar') ou mesclados ('mesclar'),
    em vez de repetir quadros. fator 0.1–0.9 (0.5 = metade da velocidade). audio: 'esticar' (atempo, mantém
    o tom) ou 'mudo'."""
    lento = _num(fator, "fator", 0.1, 0.9)
    metodo = _chave(metodo) or "interpolar"
    interp = {"interpolar": "mci", "mci": "mci", "mesclar": "mesclar", "duplicar": "duplicar"}.get(metodo)
    if not interp:
        raise ValueError("metodo: 'interpolar' (melhor, lento), 'mesclar' (rápido, rastro) ou 'duplicar'")
    if interp == "mci":
        _exigir_filtro("minterpolate", "câmera lenta com interpolação")
    entrada = doc.atual()
    info = core._midia(entrada)
    fps = info["fps"]
    ka, kb, N = _trecho_quadros(info, inicio, fim, estado)
    segs: list[dict] = []
    if ka > 0:
        segs.append({"k0": 0, "n": ka})
    segs.append({"k0": ka, "n": kb - ka, "lento": lento, "interp": interp, "mudo": _chave(audio) == "mudo"})
    if kb < N:
        segs.append({"k0": kb, "n": None})
    a, b = ka / fps, kb / fps
    desc = f"câmera lenta suave ×{lento:g} ({metodo}) em {fmt_tempo(a)}→{fmt_tempo(b)}"
    av = []
    if interp == "mci":
        av.append("interpolação de movimento: bordas de objetos rápidos podem 'derreter'; se ficar estranho, "
                  "desfaça e use metodo='mesclar'")
    v = _segmentos(doc, info, entrada, segs, desc, progresso, avisos=av)
    v["trecho_novo"] = [fmt_tempo(a), fmt_tempo(a + (b - a) / lento)]
    return v


# ---------------------------------------------------------------- 7. fade de imagem
def fade_video(doc: Documento, entrada_s: float | None = None, saida_s: float | None = None, cor: str = "black",
               audio: bool = True, inicio=None, fim=None, estado: dict | None = None, progresso=None) -> dict:
    """Fade da IMAGEM: aparece a partir de `cor` em `inicio` (padrão: começo) durante entrada_s e some para
    `cor` terminando em `fim` (padrão: final) durante saida_s. Antes de inicio / depois de fim a tela fica na
    cor. audio=True faz o som acompanhar (afade nos mesmos tempos); False copia o áudio intacto."""
    if not entrada_s and not saida_s:
        raise ValueError("passe entrada_s (aparecer) e/ou saida_s (sumir), em segundos")
    entrada = doc.atual()
    info = core._midia(entrada)
    dur = info["dur"]
    a = _tempo(inicio, estado, info) if inicio is not None else 0.0
    b = _tempo(fim, estado, info) if fim is not None else dur
    a, b = max(0.0, a), min(dur, b)
    if b - a < 0.05:
        raise ValueError("intervalo vazio")
    di = _num(entrada_s, "entrada_s", 0.01, b - a) if entrada_s else 0.0
    do = _num(saida_s, "saida_s", 0.01, b - a) if saida_s else 0.0
    c = esc_filtro(cor)
    vi = max(a, info["atraso_video"])
    vf = []
    if di:
        vf.append(f"fade=t=in:st={vi:.3f}:d={di:.3f}:color={c}")
    if do:
        vf.append(f"fade=t=out:st={b - do:.3f}:d={do:.3f}:color={c}")
    grafo = [f"[0:V:0]{','.join(vf)}[vo]"]
    audios = None
    if audio and info["auds"]:
        audios = {}
        for k, s in enumerate(info["auds"]):
            ini, fim_f = core._extensao_faixa(info, s)
            ff = []
            if di:
                st = max(a, ini)
                ff.append(f"afade=t=in:st={st:.3f}:d={min(di, max(0.01, fim_f - st)):.3f}")
            if do:
                ff.append(f"afade=t=out:st={min(b, fim_f) - do:.3f}:d={do:.3f}")
            grafo.append(f"[0:a:{k}]{','.join(ff)}[af{k}]")
            audios[k] = f"af{k}"
    partes = []
    if di:
        partes.append(f"aparece em {di:g} s" + (f" a partir de {fmt_tempo(a)}" if a > 0 else ""))
    if do:
        partes.append(f"some em {do:g} s" + (f" até {fmt_tempo(b)}" if b < dur - 0.01 else ""))
    desc = f"fade de imagem ({cor}): {' e '.join(partes)}" + (" + som" if audios else "")
    return _executar(doc, info, entrada, grafo, desc, progresso, audios=audios)


# ---------------------------------------------------------------- 8. desenhar
def _px(valor, ref: int, padrao: float) -> int:
    v = padrao if valor is None else float(str(valor).replace(",", "."))
    return max(1, round(v * ref) if v < 1 else round(v))


def _texto_tempo(expr: str, formato: str, arred: str) -> str:
    """Texto do drawtext (com %{eif}) que mostra o valor (segundos) de `expr` no formato pedido."""
    V = f"{arred}({expr})" if arred else f"({expr})"
    if formato == "ss":
        return f"%{{eif:{V}:d}}"
    if formato == "ss.d":
        return f"%{{eif:trunc({expr}):d}}.%{{eif:mod(trunc(({expr})*10),10):d}}"
    if formato == "hh:mm:ss":
        return (f"%{{eif:trunc({V}/3600):d:2}}:%{{eif:mod(trunc({V}/60),60):d:2}}:"
                f"%{{eif:mod(trunc({V}),60):d:2}}")
    if formato == "mm:ss.cc":
        return (f"%{{eif:trunc(({expr})/60):d:2}}:%{{eif:mod(trunc({expr}),60):d:2}}."
                f"%{{eif:mod(trunc(({expr})*100),100):d:2}}")
    return f"%{{eif:trunc({V}/60):d:2}}:%{{eif:mod(trunc({V}),60):d:2}}"  # mm:ss


def desenhar(doc: Documento, elementos, estado: dict | None = None, progresso=None) -> dict:
    """Desenha elementos gráficos por cima do vídeo (numa única versão). Cada elemento é um dict com "tipo":
    - "caixa": retangulo [x,y,l,a] (px ou frações), cor, espessura (px; 'preenchida' ou preenchida=true)
    - "barra_progresso": posicao 'base'|'topo', cor, altura (px ou fração), fundo (cor ou null)
    - "cronometro": modo 'crescente'|'regressivo'|'relogio' (tempo do vídeo), formato 'mm:ss'|'hh:mm:ss'|'ss'|
      'ss.d'|'mm:ss.cc', de (valor inicial em s), posicao, tamanho, cor, caixa (bool), fonte
    Todos aceitam inicio/fim."""
    if isinstance(elementos, dict):
        elementos = [elementos]
    if not elementos:
        raise ValueError("passe ao menos um elemento")
    entrada = doc.atual()
    info = core._midia(entrada)
    W, H, dur, taxa = info["w"], info["h"], info["dur"], info["taxa"]
    grafo: list[str] = []
    atual = "0:V:0"
    descs = []
    for i, el in enumerate(elementos):
        if not isinstance(el, dict):
            raise ValueError("cada elemento é um objeto com 'tipo'")
        tipo = _chave(el.get("tipo"))
        tipo = {"retangulo": "caixa", "quadrado": "caixa", "moldura": "caixa", "barra": "barra_progresso",
                "progresso": "barra_progresso", "progress_bar": "barra_progresso", "timer": "cronometro",
                "contagem": "cronometro", "contagem_regressiva": "cronometro", "timecode": "cronometro",
                "relogio": "cronometro"}.get(tipo, tipo)
        a, b, en = _janela(el.get("inicio"), el.get("fim"), estado, info)
        a0, b0 = (a if a is not None else 0.0), (b if b is not None else dur)
        prox = "vo" if i == len(elementos) - 1 else f"d{i}"
        if tipo == "caixa":
            x, y, rw, rh = core._retangulo(el.get("retangulo") or el.get("area"), W, H, "retangulo")
            esp = el.get("espessura")
            cheia = el.get("preenchida") or _chave(esp) in ("preenchida", "cheia", "fill")
            t = "fill" if cheia else str(_px(esp, H, max(2, round(H / 180))))
            cor = esc_filtro(el.get("cor") or "red")
            grafo.append(f"[{atual}]drawbox=x={x}:y={y}:w={rw}:h={rh}:color={cor}:t={t}{en}[{prox}]")
            descs.append(f"caixa {rw}x{rh}")
        elif tipo == "barra_progresso":
            bh = _px(el.get("altura"), H, max(4, round(H * 0.012)))
            pos = _chave(el.get("posicao") or "base")
            if pos not in ("base", "topo", "baixo", "cima"):
                raise ValueError("barra_progresso.posicao: 'base' ou 'topo'")
            y = 0 if pos in ("topo", "cima") else H - bh
            cor = el.get("cor") or "#FF0033"
            fundo = el.get("fundo", "white@0.3")
            ent = f"[{atual}]"
            if fundo:
                grafo.append(f"{ent}drawbox=x=0:y={y}:w={W}:h={bh}:color={esc_filtro(fundo)}:t=fill{en}[db{i}]")
                ent = f"[db{i}]"
            grafo.append(f"color=c={esc_filtro(cor)}:s={W}x{bh}:r={taxa}:d={dur + 1:.3f}[bar{i}]")
            px = f"-w+w*clip((t-{a0:.4f})/{max(0.001, b0 - a0):.4f},0,1)"
            grafo.append(f"{ent}[bar{i}]overlay=x={esc_filtro(px)}:y={y}:shortest=1:eof_action=pass{en}[{prox}]")
            descs.append("barra de progresso")
        elif tipo == "cronometro":
            modo = _chave(el.get("modo") or ("regressivo" if _chave(el.get("tipo")).startswith("contagem")
                                             else "relogio" if _chave(el.get("tipo")) in ("timecode", "relogio")
                                             else "crescente"))
            formato = str(el.get("formato") or "mm:ss").lower()
            if formato not in ("mm:ss", "hh:mm:ss", "ss", "ss.d", "mm:ss.cc"):
                raise ValueError("cronometro.formato: 'mm:ss', 'hh:mm:ss', 'ss', 'ss.d' ou 'mm:ss.cc'")
            decimal = formato in ("ss.d", "mm:ss.cc")
            if modo == "regressivo":
                de = float(el.get("de") if el.get("de") is not None else b0 - a0)
                expr, arred = f"max(0,{de:.4f}-(t-{a0:.4f}))", ("" if decimal else "ceil")
            elif modo in ("relogio", "timecode", "video"):
                expr, arred = "t", ("" if decimal else "trunc")
            elif modo == "crescente":
                de = float(el.get("de") or 0)
                expr, arred = f"max(0,t-{a0:.4f}+{de:.4f})", ("" if decimal else "trunc")
            else:
                raise ValueError("cronometro.modo: 'crescente', 'regressivo' ou 'relogio'")
            texto = _texto_tempo(expr, formato, arred)
            px = _px(el.get("tamanho"), H, 1 / 12)
            arq_fonte = core.fonte_arquivo(el.get("fonte"))
            if el.get("fonte") and not arq_fonte:
                raise ValueError(f"fonte não encontrada: {el.get('fonte')}")
            margem = max(8, round(H * 0.04))
            x, y = core._posicao_xy(el.get("posicao") or "sup_dir", W, H, "text_w", "text_h", margem)
            op = [f"text={esc_filtro(texto)}", f"fontsize={px}", f"fontcolor={esc_filtro(el.get('cor') or 'white')}",
                  f"x={esc_filtro(x)}", f"y={esc_filtro(y)}"]
            if arq_fonte:
                op.insert(1, f"fontfile={esc_filtro(arq_fonte)}")
            if el.get("caixa"):
                op += ["box=1", f"boxcolor={esc_filtro(el.get('cor_caixa') or 'black@0.55')}",
                       f"boxborderw={max(4, round(px * 0.25))}"]
            else:
                op += [f"borderw={max(1, round(px / 20))}", "bordercolor=black"]
            grafo.append(f"[{atual}]drawtext={':'.join(op)}{en}[{prox}]")
            descs.append(f"cronômetro {modo} ({formato})")
        else:
            raise ValueError(f"tipo desconhecido: {el.get('tipo')} (use 'caixa', 'barra_progresso' ou 'cronometro'; "
                             f"texto livre: `sobrepor_texto`)")
        atual = prox
    return _executar(doc, info, entrada, grafo, "desenhou: " + ", ".join(descs), progresso)


# ---------------------------------------------------------------- 9. melhorar imagem
def _detectar_entrelacado(entrada: Path, info: dict) -> bool:
    if not _tem_filtro("idet"):
        return False
    dur = info["dur"]
    tff = bff = prog = 0
    for t in ([dur * f for f in (0.2, 0.5, 0.8)] if dur > 6 else [0.0]):
        try:
            err = core._ffmpeg_stderr([FFMPEG[0], "-hide_banner", "-nostdin", "-threads", "2", "-noaccurate_seek",
                                       "-ss", f"{t:.3f}"] + info["dec"] + ["-i", str(entrada), "-map", "0:V:0",
                                       "-frames:v", "60", "-vf", "idet", "-an", "-sn", "-f", "null", "-"], 120)
        except Exception:  # noqa: BLE001
            continue
        m = re.findall(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)", err)
        if m:
            a1, b1, c1 = max(m, key=lambda x: sum(map(int, x)))
            tff, bff, prog = tff + int(a1), bff + int(b1), prog + int(c1)
    fo = info["v"].get("field_order") or ""
    return fo in ("tt", "bb", "tb", "bt") or (tff + bff > 2 * prog and tff + bff >= 20)


def melhorar_video(doc: Documento, reduzir_ruido=None, nitidez: float | None = None, desentrelacar=None,
                   deflicker: bool = False, aumentar_resolucao: float | None = None, metodo_escala: str = "lanczos",
                   inicio=None, fim=None, estado: dict | None = None, progresso=None) -> dict:
    """Limpeza de imagem numa só versão, nesta ordem: desentrelaçar (bwdif) → deflicker → reduzir ruído
    (hqdn3d 'leve'/'media'; nlmeans 'forte', lento) → aumentar resolução (lanczos / 'pixel' vizinho mais
    próximo / 'xbr' pixel art) → nitidez (CAS 0–1)."""
    entrada = doc.atual()
    info = core._midia(entrada)
    W, H = info["w"], info["h"]
    f: list[str] = []
    desc: list[str] = []
    av: list[str] = []
    if desentrelacar:
        auto = _chave(desentrelacar) in ("auto", "automatico")
        if not auto or _detectar_entrelacado(entrada, info):
            _exigir_filtro("bwdif", "desentrelaçar")
            f.append("bwdif=mode=send_frame:deint=all")
            desc.append("desentrelaçou")
        else:
            av.append("não detectei entrelaçamento (idet): desentrelaçar foi pulado")
    if deflicker:
        f.append("deflicker=size=5:mode=pm")
        desc.append("deflicker")
    if reduzir_ruido:
        r = _chave(reduzir_ruido)
        r = {"true": "media", "medio": "media", "sim": "media", "1": "media"}.get(r, r)
        if r == "leve":
            f.append("hqdn3d=2:1.5:3:2.25")
        elif r == "media":
            f.append("hqdn3d=4:3:6:4.5")
        elif r == "forte":
            f.append("nlmeans=s=3.5:p=7:r=9")
            av.append("ruído 'forte' usa nlmeans: lento (pode levar várias vezes a duração do vídeo)")
        else:
            raise ValueError("reduzir_ruido: 'leve', 'media' ou 'forte'")
        desc.append(f"ruído {r}")
    escala_ok = None
    if aumentar_resolucao:
        k = _num(aumentar_resolucao, "aumentar_resolucao", 1.1, 4.0)
        if inicio is not None or fim is not None:
            raise ValueError("aumentar_resolucao vale para o vídeo todo (sem inicio/fim)")
        nw, nh = core._par(W * k), core._par(H * k)
        if nw * nh > 3840 * 2160 * 1.05:
            raise ValueError(f"resultado {nw}x{nh} passa de 4K; use um fator menor")
        m = _chave(metodo_escala) or "lanczos"
        if m == "xbr":
            _exigir_filtro("xbr", "aumentar pixel art")
            n = min(4, max(2, round(k)))
            f.append(f"xbr=n={n}" + (f",scale={nw}:{nh}:flags=lanczos" if abs(n - k) > 0.01 else ""))
        elif m in ("pixel", "vizinho", "neighbor", "nearest"):
            f.append(f"scale={nw}:{nh}:flags=neighbor")
        elif m in ("lanczos", "bicubic", "spline"):
            f.append(f"scale={nw}:{nh}:flags={'spline' if m == 'spline' else m}+accurate_rnd+full_chroma_int")
        else:
            raise ValueError("metodo_escala: 'lanczos', 'bicubic', 'spline', 'pixel' ou 'xbr'")
        f.append("setsar=1")
        desc.append(f"{nw}x{nh} ({m})")
        escala_ok = (nw, nh)
        av.append("aumentar a resolução não cria detalhes que não existem (sem IA); fica mais suave, não mais nítido")
    if nitidez:
        s = _num(nitidez, "nitidez", 0.0, 1.0)
        f.append(f"cas=strength={s:g}")
        desc.append(f"nitidez {s:g}")
    if not f:
        raise ValueError("nada a fazer: passe reduzir_ruido, nitidez, desentrelacar, deflicker ou aumentar_resolucao")
    a, b, en = _janela(inicio, fim, estado, info)
    grafo = _efeito_em_janela(info, _cadeia(",".join(f)), en) if not escala_ok else \
        [f"[0:V:0]{','.join(f)}[vo]"]
    return _executar(doc, info, entrada, grafo, "melhorou: " + ", ".join(desc) + core._desc_janela(a, b),
                     progresso, avisos=av)


# ---------------------------------------------------------------- 10. fps
def mudar_fps(doc: Documento, fps, modo: str = "duplicar", progresso=None) -> dict:
    """Muda a taxa de quadros sem mudar a duração: 'duplicar' (repete/descarta quadros), 'mesclar' (mistura
    quadros vizinhos) ou 'interpolar' (cria quadros intermediários; minterpolate, lento)."""
    s = str(fps).strip().replace(",", ".")
    nomes = {"ntsc": "30000/1001", "29.97": "30000/1001", "23.976": "24000/1001", "59.94": "60000/1001",
             "pal": "25", "cinema": "24"}
    s = nomes.get(s.lower(), s)
    if not re.fullmatch(r"\d+(\.\d+)?(/\d+)?", s):
        raise ValueError("fps: número (24, 25, 30, 60, 29.97...) ou fração (30000/1001)")
    alvo = core._fps_stream({"r_frame_rate": s}) if "/" in s else float(s)
    if not 1 <= alvo <= 240:
        raise ValueError("fps entre 1 e 240")
    modo = _chave(modo) or "duplicar"
    entrada = doc.atual()
    info = core._midia(entrada)
    if abs(alvo - info["fps"]) < 0.01:
        raise ValueError(f"o vídeo já está em {info['fps']:g} fps")
    if modo == "duplicar":
        f = f"fps={s}"
    elif modo == "mesclar":
        f = f"framerate=fps={s}"
    elif modo == "interpolar":
        _exigir_filtro("minterpolate", "interpolar quadros")
        f = f"minterpolate=fps={s}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
    else:
        raise ValueError("modo: 'duplicar', 'mesclar' ou 'interpolar'")
    av = ["interpolação de movimento é lenta (às vezes mais lenta que o tempo real) e pode deformar bordas em "
          "movimento rápido"] if modo == "interpolar" else []
    # framerate/minterpolate deixam o último quadro com duração errada e o modo CFR do ffmpeg 6 duplica um no
    # fim: `fps` refaz as durações e o trim corta no nº exato (duração igual à origem)
    # e o framerate do 8.1 pode soltar um a menos (tpad repete o último para completar)
    if modo != "duplicar":
        f += f",fps={s}"
    f += f",tpad=stop_mode=clone:stop_duration=1,trim=end_frame={max(1, round(_total_quadros(info) / info['fps'] * alvo))}"
    return _executar(doc, info, entrada, [f"[0:V:0]{f}[vo]"], f"fps {info['fps']:g} → {alvo:g} ({modo})",
                     progresso, avisos=av)


# ---------------------------------------------------------------- 11. bordas pretas
def detectar_bordas(entrada: Path, info: dict, limiar: float = 0.1, amostras: int = 8) -> tuple[int, int, int, int] | None:
    """Maior área não preta vista em `amostras` pontos do vídeo (cropdetect) → (x, y, l, a) ou None."""
    _exigir_filtro("cropdetect", "detectar bordas pretas")
    dur = info["dur"]
    caixas = []
    for j in range(amostras):
        t = dur * (j + 0.5) / amostras
        try:
            err = core._ffmpeg_stderr([FFMPEG[0], "-hide_banner", "-nostdin", "-threads", "2", "-ss", f"{t:.3f}"]
                                      + info["dec"] + ["-i", str(entrada), "-map", "0:V:0", "-frames:v", "8",
                                      "-vf", f"{info['pre'] + ',' if info['pre'] else ''}cropdetect=limit={limiar:g}:round=2:reset=0",
                                      "-an", "-sn", "-f", "null", "-"], 120)
        except Exception:  # noqa: BLE001
            continue
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", err)
        if m:
            w, h, x, y = map(int, m[-1])
            if w > 0 and h > 0:
                caixas.append((x, y, x + w, y + h))
    if not caixas:
        return None
    x0 = min(c[0] for c in caixas)
    y0 = min(c[1] for c in caixas)
    x1 = max(c[2] for c in caixas)
    y1 = max(c[3] for c in caixas)
    x0, y0 = x0 // 2 * 2, y0 // 2 * 2
    return x0, y0, (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2


def cortar_bordas_pretas(doc: Documento, limiar: float = 0.1, manter_tamanho: bool = False, progresso=None) -> dict:
    """Detecta tarjas pretas (letterbox/pillarbox) em vários pontos do vídeo e recorta. limiar 0–1: até
    quanto de brilho conta como 'preto' (0.1 ≈ 24/255). manter_tamanho=True amplia de volta ao tamanho original."""
    lim = _num(limiar, "limiar", 0.01, 0.5, 0.1)
    entrada = doc.atual()
    info = core._midia(entrada)
    W, H = info["w"], info["h"]
    caixa = detectar_bordas(entrada, info, lim)
    if not caixa:
        raise ValueError("não consegui analisar o vídeo (cropdetect)")
    x, y, cw, ch = caixa
    if cw * ch >= 0.985 * W * H or cw < 16 or ch < 16:
        raise ValueError(f"não encontrei bordas pretas (área útil {cw}x{ch} de {W}x{H})")
    f = f"crop={cw}:{ch}:{x}:{y}"
    if manter_tamanho:
        f += f",scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},setsar=1"
    else:
        f += ",setsar=1"
    desc = f"cortou bordas pretas: {W}x{H} → {cw}x{ch} em ({x},{y})" + (f" (ampliado para {W}x{H})"
                                                                       if manter_tamanho else "")
    v = _executar(doc, info, entrada, [f"[0:V:0]{f}[vo]"], desc, progresso)
    v["detalhes"] = {"recorte": [x, y, cw, ch]}
    return v


# ---------------------------------------------------------------- 12. logo
def remover_logo(doc: Documento, retangulo, inicio=None, fim=None, estado: dict | None = None,
                 progresso=None) -> dict:
    """Apaga um logo/marca d'água fixo interpolando os pixels em volta (delogo; rápido, sem IA)."""
    _exigir_filtro("delogo", "remover logo")
    entrada = doc.atual()
    info = core._midia(entrada)
    W, H = info["w"], info["h"]
    x, y, rw, rh = core._retangulo(retangulo, W, H, "retangulo")
    # o delogo interpola com a borda de 1 px em volta: a área não pode encostar na borda do quadro
    x, y = max(1, x), max(1, y)
    rw, rh = min(rw, W - 1 - x), min(rh, H - 1 - y)
    if rw < 2 or rh < 2:
        raise ValueError("área do logo pequena demais ou fora do quadro")
    a, b, en = _janela(inicio, fim, estado, info)
    grafo = [f"[0:V:0]delogo=x={x}:y={y}:w={rw}:h={rh}{en}[vo]"]
    return _executar(doc, info, entrada, grafo, f"removeu logo {rw}x{rh} em ({x},{y}){core._desc_janela(a, b)}",
                     progresso, filtros_8bits="delogo")


# ---------------------------------------------------------------- 13. perspectiva / 360
def perspectiva(doc: Documento, modo: str = "corrigir", cantos=None, yaw: float = 0, pitch: float = 0,
                roll: float = 0, fov: float = 90, largura: int | None = None, altura: int | None = None,
                progresso=None) -> dict:
    """'corrigir': endireita um quadrilátero (tela/quadro/documento filmado de lado): cantos = 4 pontos
    [sup_esq, sup_dir, inf_esq, inf_dir] em px ou frações → viram os cantos do quadro.
    '360': de vídeo 360° equirretangular para uma vista normal, olhando para yaw (graus, direita +),
    pitch (cima +), roll, com campo de visão fov (graus); tamanho de saída largura x altura (padrão 1280x720)."""
    modo = _chave(modo) or "corrigir"
    entrada = doc.atual()
    info = core._midia(entrada)
    W, H = info["w"], info["h"]
    if modo == "corrigir":
        _exigir_filtro("perspective", "corrigir perspectiva")
        if not cantos or len(cantos) != 4:
            raise ValueError("cantos: 4 pontos [[x,y] sup_esq, [x,y] sup_dir, [x,y] inf_esq, [x,y] inf_dir]")
        pts = []
        for c in cantos:
            px_, py_ = (float(str(v).replace(",", ".")) for v in c)
            if px_ <= 1 and py_ <= 1:
                px_, py_ = px_ * W, py_ * H
            pts.append((px_, py_))
        op = [f"x{i}={p[0]:.2f}:y{i}={p[1]:.2f}" for i, p in enumerate(pts)]
        grafo = [f"[0:V:0]perspective={':'.join(op)}:interpolation=cubic:sense=source[vo]"]
        desc = "corrigiu a perspectiva"
    elif modo in ("360", "vr", "equirretangular"):
        _exigir_filtro("v360", "vídeo 360")
        lw = core._par(int(largura or 1280))
        lh = core._par(int(altura or round(lw * 9 / 16)))
        fv = _num(fov, "fov", 10, 170)
        vf = math.degrees(2 * math.atan(math.tan(math.radians(fv) / 2) * lh / lw))
        grafo = [f"[0:V:0]v360=input=e:output=flat:yaw={float(yaw):g}:pitch={float(pitch):g}:roll={float(roll):g}:"
                 f"h_fov={fv:g}:v_fov={vf:.3f}:w={lw}:h={lh}:interp=cubic,setsar=1[vo]"]
        desc = f"vista 360 (yaw {float(yaw):g}°, pitch {float(pitch):g}°, fov {fv:g}°) {lw}x{lh}"
        if abs(W / H - 2) > 0.05:
            desc += " [origem não é 2:1]"
    else:
        raise ValueError("modo: 'corrigir' ou '360'")
    return _executar(doc, info, entrada, grafo, desc, progresso)
