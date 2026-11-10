"""Efeitos e tratamento de áudio — o "canivete suíço" de áudio (sem Qt; só o ffmpeg).

Equalizador, efeitos de voz, tom, dinâmica, canais, sincronia, censura de palavras, loudness, inverter e
mixar faixas. Cada edição cria uma VERSÃO com `core._editar_audio` (vídeo e faixas não tocadas copiados sem
perda; a faixa editada é re-encodada com `core.codec_audio` e as tags reaplicadas).

Sincronia (regra do módulo): todo efeito roda numa "linha do tempo da faixa" que começa em 0
(`asetpts=PTS-STARTPTS`) e volta ao início original da faixa no fim (`+st/TB`, `core._extensao_faixa`). O atraso
de cada efeito é MEDIDO (`latencia`: clique sintético, ou envelope de surtos de ruído para quem muda o tom) e
compensado: atraso positivo → `apad` desse tanto antes + `atrim=start` depois; negativo (o rubberband adianta o
som ao subir o tom) → `adelay` depois. Efeitos com cauda (eco, reverb) são cortados no fim da faixa: o
comprimento da saída é o da entrada.

Trecho (inicio/fim): saída = original − G·original + Efeito(G·original), com G = 1 dentro do trecho e rampas de
30 ms (`afade`, exatas na amostra). Assim o eco/reverb do fim do trecho continua soando depois dele (como num
editor de áudio), e fora do trecho o som é bit a bit o original (antes da re-codificação).
"""
from __future__ import annotations

import functools
import json
import math
import re
import subprocess
from pathlib import Path

from . import core
from .core import Documento, esc_filtro, fmt_tempo, parse_tempo

_RAMPA = 0.03  # s: rampa nas bordas de um trecho (inicio/fim)
_INTENSIDADE = {"leve": 0.35, "suave": 0.35, "fraco": 0.35, "fraca": 0.35, "media": 0.6, "média": 0.6,
                "medio": 0.6, "médio": 0.6, "normal": 0.6, "forte": 0.9, "muito_forte": 1.0, "maximo": 1.0,
                "máximo": 1.0}
_TETO = 0.97  # teto do limitador (≈ −0,26 dBFS), igual ao `core._LIMITADOR`


# ---------------------------------------------------------------- utilidades
def _nivel(intensidade, padrao: float = 0.6) -> float:
    """'leve'/'media'/'forte' ou número 0–1 (ou 0–100 %) → fração 0–1."""
    if intensidade is None or intensidade == "":
        return padrao
    if isinstance(intensidade, str):
        k = intensidade.strip().lower().replace(" ", "_")
        if k in _INTENSIDADE:
            return _INTENSIDADE[k]
        try:
            intensidade = float(k.replace(",", ".").rstrip("%"))
        except ValueError:
            raise ValueError("intensidade: 'leve', 'media', 'forte' ou número de 0 a 1 (ou 0 a 100 %)") from None
    x = float(intensidade)
    if x > 1:
        x /= 100
    if not 0 <= x <= 1:
        raise ValueError("intensidade: 'leve', 'media', 'forte' ou número de 0 a 1 (ou 0 a 100 %)")
    return x


@functools.lru_cache(maxsize=1)
def _filtros_disponiveis() -> frozenset[str]:
    r = subprocess.run([core.FFMPEG[0], "-hide_banner", "-filters"], capture_output=True, text=True)
    nomes = set()
    for ln in r.stdout.splitlines():
        p = ln.split()
        if len(p) >= 3 and re.fullmatch(r"[A-Z.|]{2,4}", p[0]):
            nomes.add(p[1])
    return frozenset(nomes)


def tem_filtro(nome: str) -> bool:
    return nome in _filtros_disponiveis()


def _exigir(nome: str, para: str) -> None:
    if not tem_filtro(nome):
        raise RuntimeError(f"o ffmpeg em uso ({core.FFMPEG[0]}) não tem o filtro '{nome}', necessário para {para}. "
                           "O AppImage do AI Editor já traz um ffmpeg completo; do código-fonte, instale um ffmpeg "
                           "com esse filtro ou aponte AI_EDITOR_FFMPEG para ele (veja `ai-editor --diagnostico`).")


@functools.lru_cache(maxsize=32)
def _tem_opcao(filtro: str, opcao: str) -> bool:
    r = subprocess.run([core.FFMPEG[0], "-hide_banner", "-h", f"filter={filtro}"], capture_output=True, text=True)
    return re.search(rf"^\s+{re.escape(opcao)}\s", r.stdout, re.M) is not None


def _db(x: float) -> str:
    return f"{x:+.2f}dB"


def _lin(db: float) -> float:
    return 10 ** (db / 20)


def _faixa(info: dict, faixa) -> tuple[int, dict, float, float, int, str, str]:
    """(k, stream, st, fim, sr, layout, prefixo de layout) da faixa 1-based."""
    k = core._faixa_valida(faixa, len(info["auds"]))
    s = info["auds"][k]
    st, fim_f = core._extensao_faixa(info, s)
    sr = int(s.get("sample_rate") or 48000)
    return k, s, st, fim_f, sr, core._layout(s), core._filtro_layout(s)


def _janela(inicio, fim, estado, info, st, fim_f) -> tuple[float | None, float | None]:
    """Trecho em tempo do arquivo, limitado à extensão da faixa; (None, None) = faixa inteira."""
    if inicio is None and fim is None:
        return None, None
    est = dict(estado or {})
    est.setdefault("duracao", info["dur"])
    a, b, _ = core._janela_tempo(inicio, fim, est, info["dur"])
    a, b = max(a, st), min(b, fim_f)
    if b - a < 0.01:
        raise ValueError(f"o trecho {fmt_tempo(a)} → {fmt_tempo(b)} fica fora da faixa "
                         f"(ela vai de {fmt_tempo(st)} a {fmt_tempo(fim_f)})")
    if a <= st + 0.001 and b >= fim_f - 0.001:
        return None, None
    return a, b


# ---------------------------------------------------------------- latência
def _ruido_teste(sr: int, lay: str, dur: float) -> str:
    # surtos de ruído (0,3 s ligados, 0,2 s desligados): envelope com bordas nítidas
    return (f"aevalsrc={esc_filtro('(random(0)-0.5)*0.5*between(mod(t,0.5),0.1,0.4)')}"
            f":s={sr}:c={lay}:d={dur}")


@functools.lru_cache(maxsize=128)
def latencia(fragmento: str, sr: int, lay: str = "mono", metodo: str = "clique") -> float:
    """Atraso (s) que um fragmento de filtro introduz (`fragmento` usa os rótulos {e} → {s}), medido uma vez por
    (fragmento, taxa, layout, método):
    - 'clique': pico de 0,9 em 0,5 s sobre ruído baixo (exato na amostra; como `core.latencia_filtro`);
    - 'envelope': correlação do envelope (RMS de 2 ms) de surtos de ruído antes/depois — para quem muda o tom
      (rubberband), em que o clique vira um borrão. Pode dar negativo (o efeito ADIANTA o som).
    Se o clique some (o filtro o remove), cai para o envelope."""
    import numpy as np
    if metodo == "clique":
        n0 = sr // 2
        src = (f"aevalsrc={esc_filtro(f'if(eq(n,{n0}),0.9,0)+0.001*(random(0)-0.5)')}:s={sr}:c={lay}:d=1.5")
    else:
        src = _ruido_teste(sr, lay, 4.0)
    grafo = "[0:a]anull[e0];" + fragmento.replace("{e}", "e0").replace("{s}", "s0")
    cmd = [core.FFMPEG[0], "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", src,
           "-filter_complex", grafo, "-map", "[s0]", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise core.ErroFFmpeg(f"medição de latência falhou: {r.stderr.decode(errors='replace')[-1500:]}")
    y = np.frombuffer(r.stdout[: len(r.stdout) // 4 * 4], np.float32).astype(np.float64)
    if metodo == "clique":
        if len(y) <= sr // 2:
            return 0.0
        p = int(np.argmax(np.abs(y)))
        if abs(y[p]) < 0.02:  # o filtro engoliu o clique: mede pelo envelope
            return latencia(fragmento, sr, lay, "envelope")
        return max(0, p - sr // 2) / sr
    r0 = subprocess.run([core.FFMPEG[0], "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", src,
                         "-ac", "1", "-f", "f32le", "-"], capture_output=True, timeout=60)
    x = np.frombuffer(r0.stdout[: len(r0.stdout) // 4 * 4], np.float32).astype(np.float64)
    n = min(len(x), len(y))
    if n < sr:
        return 0.0
    w = np.ones(max(8, sr // 500)) / max(8, sr // 500)
    ex = np.sqrt(np.convolve(x[:n] ** 2, w, "same"))
    ey = np.sqrt(np.convolve(y[:n] ** 2, w, "same"))
    ex -= ex.mean()
    ey -= ey.mean()
    cc = np.fft.irfft(np.fft.rfft(ey, 2 * n) * np.conj(np.fft.rfft(ex, 2 * n)))
    L = int(0.1 * sr)
    lags = np.concatenate([cc[-L:], cc[:L]])
    return (int(np.argmax(lags)) - L) / sr


# ---------------------------------------------------------------- montagem do grafo de uma faixa
def _frag(cadeia_ou_frag: str) -> str:
    """Cadeia simples ('highpass=f=80,volume=2dB') → fragmento com rótulos {e} → {s}."""
    if "{e}" in cadeia_ou_frag:
        return cadeia_ou_frag
    return "[{e}]" + cadeia_ou_frag + "[{s}]"


def _envolver(frag: str, ent: str, sai: str, lat: float, sr: int, lay_saida: str, D: float, exato: bool,
              pref: str) -> str:
    """Aplica o fragmento ao rótulo `ent` (pts começando em 0) com a latência compensada; `sai` sai com pts
    desde 0 e o mesmo comprimento da entrada (exato=True: exatamente D s — corta caudas e completa com
    silêncio quem encurta)."""
    antes = f"apad=pad_dur={lat:.6f}" if lat > 0 else "anull"
    depois = [f"aresample={sr}", f"aformat=sample_rates={sr}:channel_layouts={lay_saida}", "asetpts=N/SR/TB"]
    ini = max(lat, 0.0)
    if lat < 0:
        depois.append(f"adelay=delays={int(round(-lat * sr))}S:all=1")
        exato = True
    if exato:
        depois.append(f"apad=whole_dur={ini + D:.6f}")
        depois.append(f"atrim=start={ini:.6f}:end={ini + D:.6f}")
    elif ini > 0:
        depois.append(f"atrim=start={ini:.6f}")
    depois.append("asetpts=PTS-STARTPTS")
    corpo = frag.replace("{e}", f"{pref}p").replace("{s}", f"{pref}q")
    return f"[{ent}]{antes}[{pref}p];{corpo};[{pref}q]{','.join(depois)}[{sai}]"


def _grafo_efeito(info: dict, k: int, frag: str, lat: float, *, a=None, b=None, exato=False,
                  lay_saida: str | None = None, sai: str = "ar") -> list[str]:
    """Partes do -filter_complex que aplicam `frag` à faixa k (inteira ou só em [a, b], tempos do arquivo)."""
    s = info["auds"][k]
    st, fim_f = core._extensao_faixa(info, s)
    sr = int(s.get("sample_rate") or 48000)
    lay = core._layout(s)
    pre = core._filtro_layout(s)
    D = fim_f - st
    origem = f"[0:a:{k}]{pre}asetpts=PTS-STARTPTS"
    if a is None:
        return [f"{origem}[x{k}]",
                _envolver(frag, f"x{k}", f"y{k}", lat, sr, lay_saida or lay, D, exato, f"f{k}"),
                f"[y{k}]asetpts=PTS+{st:.6f}/TB[{sai}]"]
    if lay_saida and lay_saida != lay:
        raise ValueError("este efeito muda os canais da faixa: não dá para aplicar só num trecho")
    # G·original: rampas de _RAMPA centradas nas bordas do trecho (tempo da faixa = arquivo − st)
    A, B = a - st, b - st
    fades = []
    if A > 0.001:
        fades.append(f"afade=t=in:st={max(0.0, A - _RAMPA / 2):.6f}:d={_RAMPA:.3f}")
    if B < D - 0.001:
        fades.append(f"afade=t=out:st={max(0.0, B - _RAMPA / 2):.6f}:d={_RAMPA:.3f}")
    fmt = f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={lay}"
    return [f"{origem},{fmt},asplit=2[d{k}][x{k}]",
            f"[x{k}]{','.join(fades) or 'anull'},asplit=2[g{k}][h{k}]",
            _envolver(frag, f"g{k}", f"w{k}", lat, sr, lay, D, exato, f"f{k}"),
            f"[h{k}]volume=-1[n{k}]",
            # dry − G·dry + Efeito(G·dry); duration=longest: as três têm o comprimento da faixa
            f"[d{k}][n{k}][w{k}]amix=inputs=3:normalize=0:duration=longest,{fmt},"
            f"asetpts=PTS-STARTPTS+{st:.6f}/TB[{sai}]"]


def _aplicar(doc: Documento, faixa, frag: str, descricao: str, *, inicio=None, fim=None, estado=None,
             progresso=None, medir: str | None = None, metodo: str = "clique", exato: bool = False,
             lay_saida: str | None = None, detalhes: dict | None = None, avisos: list[str] | None = None,
             permitir_trecho: bool = True) -> dict:
    """Aplica um efeito (cadeia ou fragmento) a uma faixa e registra a versão. medir: fragmento neutro
    equivalente para medir o atraso (ex.: afftfilt identidade com a mesma janela); metodo: 'clique' ou
    'envelope'."""
    entrada = doc.atual()
    info = core._info_audio(entrada)
    k, s, st, fim_f, sr, lay, _ = _faixa(info, faixa)
    a, b = _janela(inicio, fim, estado, info, st, fim_f)
    if a is not None and not permitir_trecho:
        raise ValueError("esta operação vale para a faixa inteira (sem inicio/fim)")
    frag = _frag(frag)
    lat = latencia(_frag(medir) if medir else frag, sr, lay, metodo)
    grafo = _grafo_efeito(info, k, frag, lat, a=a, b=b, exato=exato, lay_saida=lay_saida)
    det = {"faixa": f"A{k + 1}", **(detalhes or {}), "latencia_compensada_ms": round(lat * 1000, 2)}
    if a is not None:
        det["trecho"] = f"{fmt_tempo(a)}→{fmt_tempo(b)}"
        descricao += core._desc_janela(a, b)
    extra = {"detalhes": det}
    if avisos:
        extra["avisos"] = list(avisos)
    return core._editar_audio(doc, grafo, {k: "ar"}, descricao, progresso, info=info, extra=extra)


def _editar_faixas(doc: Documento, grafo: list[str], saidas: dict[int, str], descricao: str, progresso=None,
                   novas: list[tuple[str, dict]] = (), remover: set[int] = frozenset(),
                   tags: dict[int, dict] | None = None, info: dict | None = None, extra: dict | None = None) -> dict:
    """Como `core._editar_audio`, mas também REMOVE faixas e troca tags (título) de faixas substituídas.
    (Usado por `canais_audio('separar')` e `mixar_faixas(destino='substituir')`.)"""
    entrada = doc.atual()
    info = info or core._info_audio(entrada)
    auds = info["auds"]
    ext = entrada.suffix.lower()
    ficam = [i for i in range(len(auds)) if i not in remover]
    total = len(ficam) + len(novas)
    if total > 1 and ext in (".wav", ".mp3", ".flac", ".ogg", ".opus", ".aac") and not info["v"]:
        ext = ".mka"
        extra = {**(extra or {}), "avisos": (extra or {}).get("avisos", []) +
                 ["o arquivo agora é .mka (o formato original só guarda uma faixa de áudio)"]}
    saida = doc.proxima_saida(ext)
    trocou_ext = saida.suffix.lower() != entrada.suffix.lower()
    cmd = core.FFMPEG + ["-i", str(entrada), "-filter_complex", ";".join(grafo), "-map", "0:V?"]
    for i in ficam:
        cmd += ["-map", f"[{saidas[i]}]" if i in saidas else f"0:a:{i}"]
    for rot, _ in novas:
        cmd += ["-map", f"[{rot}]"]
    if not trocou_ext:
        cmd += ["-map", "0:s?"]
    cmd += ["-c", "copy"]
    for j, i in enumerate(ficam):
        s = auds[i]
        if i in saidas:
            cmd += core.codec_audio(saida.suffix, j)
            t = {kk: (s.get("tags") or {}).get(kk) for kk in ("language", "title", "handler_name")}
            t.update((tags or {}).get(i, {}))
            for chave, valor in t.items():
                if valor:
                    cmd += [f"-metadata:s:a:{j}", f"{chave}={valor}"]
    for jn, (_, t) in enumerate(novas):
        j = len(ficam) + jn
        cmd += core.codec_audio(saida.suffix, j)
        for chave, valor in t.items():
            if valor:
                cmd += [f"-metadata:s:a:{j}", f"{chave}={valor}"]
    cmd += ["-map_metadata", "0"]
    if saida.suffix in (".mp4", ".mov", ".m4v", ".m4a"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(saida))
    return core._rodar_versao(doc, saida, [cmd], [info["dur"]], descricao, progresso, extra)


# ---------------------------------------------------------------- 1. equalizador
# (tipo, freq Hz, ganho dB, largura em oitavas) — tipos: 'pico' (equalizer), 'graves'/'agudos' (prateleira),
# 'passa_altas'/'passa_baixas' (corte; ganho ignorado), 'corta' (notch estreito, Q = largura)
PRESETS_EQ: dict[str, tuple[str, list[tuple]]] = {
    "voz": ("voz mais clara", [("passa_altas", 80, 0, 0), ("pico", 300, -3, 1.0), ("pico", 3000, 3, 1.5),
                               ("agudos", 8000, 1.5, 0)]),
    "podcast": ("som de podcast (encorpado e claro)", [("passa_altas", 70, 0, 0), ("pico", 120, 2, 1.0),
                                                        ("pico", 350, -3, 1.0), ("pico", 3500, 3, 1.5),
                                                        ("pico", 7500, -1.5, 1.0)]),
    "mais_graves": ("mais graves", [("graves", 100, 6, 0)]),
    "menos_graves": ("menos graves", [("graves", 150, -8, 0), ("passa_altas", 50, 0, 0)]),
    "mais_agudos": ("mais agudos (brilho)", [("agudos", 4000, 6, 0)]),
    "menos_agudos": ("menos agudos", [("agudos", 4000, -6, 0)]),
    "mais_medios": ("mais médios (presença)", [("pico", 1500, 4, 1.5)]),
    "menos_medios": ("menos médios (menos 'nasal'/'de caixa')", [("pico", 800, -4, 1.5)]),
    "clareza": ("mais clareza/inteligibilidade", [("pico", 300, -2, 1.0), ("pico", 4500, 4, 2.0)]),
    "abafado": ("abafado (som atrás da parede)", [("passa_baixas", 900, 0, 0), ("passa_baixas", 900, 0, 0)]),
    "telefone": ("faixa de telefone (300–3400 Hz)", [("passa_altas", 300, 0, 0), ("passa_altas", 300, 0, 0),
                                                       ("passa_baixas", 3400, 0, 0), ("passa_baixas", 3400, 0, 0)]),
    "sem_ronco": ("sem ronco/vento/batidas graves (corte abaixo de 100 Hz)",
                  [("passa_altas", 100, 0, 0), ("passa_altas", 100, 0, 0)]),
    "sem_chiado": ("menos chiado agudo", [("agudos", 7000, -8, 0)]),
    "sem_zumbido_60": ("sem zumbido de rede elétrica 60 Hz (e harmônicos)",
                       [("corta", 60 * i, -30, 12) for i in range(1, 6)]),
    "sem_zumbido_50": ("sem zumbido de rede elétrica 50 Hz (e harmônicos)",
                       [("corta", 50 * i, -30, 12) for i in range(1, 6)]),
}
_SINONIMOS_EQ = {"graves": "mais_graves", "grave": "mais_graves", "agudos": "mais_agudos", "brilho": "mais_agudos",
                 "zumbido": "sem_zumbido_60", "sem_zumbido": "sem_zumbido_60", "hum": "sem_zumbido_60",
                 "radio": "telefone", "voz_clara": "voz", "presenca": "mais_medios", "presença": "mais_medios"}


def _filtro_banda(tipo: str, f: float, g: float, w: float) -> str:
    if tipo == "pico":
        return f"equalizer=f={f:g}:t=o:w={w or 1:g}:g={g:g}"
    if tipo == "graves":
        return f"bass=f={f:g}:g={g:g}"
    if tipo == "agudos":
        return f"treble=f={f:g}:g={g:g}"
    if tipo == "passa_altas":
        return f"highpass=f={f:g}"
    if tipo == "passa_baixas":
        return f"lowpass=f={f:g}"
    if tipo == "corta":
        return f"equalizer=f={f:g}:t=q:w={w or 10:g}:g={g:g}"
    raise ValueError(tipo)


def equalizar(doc: Documento, preset: str | None = None, graves_db: float | None = None,
              medios_db: float | None = None, agudos_db: float | None = None, bandas: list[dict] | None = None,
              faixa: int = 1, inicio=None, fim=None, estado: dict | None = None, progresso=None) -> dict:
    """Equalizador: preset e/ou graves/médios/agudos (dB, −24 a +24) e/ou bandas livres
    [{freq, ganho, largura (oitavas, padrão 1)}]. Com reforço (> 0 dB) põe um limitador no fim (teto −0,26 dBFS)."""
    filtros: list[str] = []
    partes: list[str] = []
    reforco = 0.0
    if preset:
        p = preset.strip().lower().replace(" ", "_").replace("-", "_")
        p = _SINONIMOS_EQ.get(p, p)
        if p not in PRESETS_EQ:
            raise ValueError(f"preset: um de {', '.join(PRESETS_EQ)}")
        nome, bandas_p = PRESETS_EQ[p]
        for t, f, g, w in bandas_p:
            filtros.append(_filtro_banda(t, f, g, w))
            if t in ("pico", "graves", "agudos"):
                reforco = max(reforco, g)
        partes.append(f"preset '{p}' ({nome})")
    for nome, val, tipo, f, w in (("graves", graves_db, "graves", 100, 0), ("médios", medios_db, "pico", 1000, 2),
                                  ("agudos", agudos_db, "agudos", 4000, 0)):
        if val is None or float(val) == 0:
            continue
        v = float(val)
        if not -24 <= v <= 24:
            raise ValueError(f"{nome}: entre −24 e +24 dB")
        filtros.append(_filtro_banda(tipo, f, v, w))
        reforco = max(reforco, v)
        partes.append(f"{nome} {v:+g} dB")
    for bd in bandas or []:
        try:
            f = float(bd.get("freq") or bd.get("frequencia") or bd["f"])
            g = float(bd.get("ganho", bd.get("ganho_db", bd.get("g", 0))))
            w = float(bd.get("largura", bd.get("w", 1)) or 1)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("bandas: lista de {freq: Hz, ganho: dB, largura: oitavas (opcional, padrão 1)}") from None
        if not 20 <= f <= 20000 or not -30 <= g <= 24 or not 0.05 <= w <= 5:
            raise ValueError("banda: freq 20–20000 Hz, ganho −30 a +24 dB, largura 0,05–5 oitavas")
        filtros.append(_filtro_banda("pico", f, g, w))
        reforco = max(reforco, g)
        partes.append(f"{f:g} Hz {g:+g} dB")
    if not filtros:
        raise ValueError("passe preset, graves_db/medios_db/agudos_db ou bandas")
    det = {"filtros": filtros}
    if reforco > 0:
        filtros.append(core._LIMITADOR)
        det["limitador"] = "teto −0,26 dBFS (há reforço de até " + f"{reforco:g} dB)"
    return _aplicar(doc, faixa, ",".join(filtros), f"equalizou A{int(faixa or 1)}: " + ", ".join(partes),
                    inicio=inicio, fim=fim, estado=estado, progresso=progresso, detalhes=det)


# ---------------------------------------------------------------- 2. efeitos de voz
def _ir_reverb(pref: str, sr: int, canais: int, t60: float, pre_ms: float) -> str:
    """Resposta ao impulso sintética (ruído com decaimento exponencial, energia 1) como fonte no grafo.
    Ruído por hash do índice da amostra (determinístico; um por canal, para dar largura estéreo)."""
    kdec = math.log(1000) / t60
    c = math.sqrt(12 * 2 * kdec / sr)  # Σ (c·u)²·e^(−2kt) ≈ 1 com u uniforme em ±0,5
    pd = pre_ms / 1000
    exprs = []
    for i in range(min(canais, 2)):
        ruido = f"(mod(abs(sin(n*{12.9898 + 7.233 * i:.4f}+{i * 3.1:.1f}))*43758.5453,1)-0.5)"
        exprs.append(f"{c:.6f}*{ruido}*gte(t,{pd:.4f})*exp(-{kdec:.4f}*(t-{pd:.4f}))")
    return f"aevalsrc={esc_filtro('|'.join(exprs))}:s={sr}:d={t60 + pd:.3f}[{pref}ir]"


def _frag_reverb(sr: int, canais: int, t60: float, pre_ms: float, molhado: float, seco: float = 1.0,
                 corte_agudos: float = 6000, extra_molhado: str = "") -> str:
    """{e} → seco + reverb (afir com IR sintética) → {s}. O afir não tem atraso (IR com início em 0) e devolve o
    mesmo nº de amostras (a cauda depois do fim se perde, como queremos)."""
    norma = "irnorm=-1" if _tem_opcao("afir", "irnorm") else "gtype=none"
    irfmt = "input" if canais <= 2 else "mono"
    ir = _ir_reverb("rv", sr, canais, t60, pre_ms)
    return (f"{ir};[{{e}}]asplit=2[rvs][rvm];"
            f"[rvm][rvir]afir=dry=1:wet=1:{norma}:irfmt={irfmt},lowpass=f={corte_agudos:g},"
            f"{extra_molhado}volume={molhado:.3f}[rvw];"
            f"[rvs]volume={seco:.3f}[rvd];[rvd][rvw]amix=inputs=2:normalize=0:duration=longest,{core._LIMITADOR}[{{s}}]")


EFEITOS_VOZ = {
    "eco": "repetições nítidas da voz (eco de montanha)",
    "reverb": "ambiente de sala (reverberação)",
    "banheiro": "sala pequena e reflexiva",
    "caverna": "caverna/igreja: reverberação longa e escura com eco",
    "estadio": "estádio/ginásio: reverberação grande + ecos do sistema de som",
    "robo": "voz robótica (sem entonação, metálica)",
    "telefone": "voz de telefone/ligação",
    "radio": "rádio antigo/walkie-talkie (banda estreita, comprimido, saturado)",
    "megafone": "megafone/alto-falante (estridente e distorcido)",
    "esquilo": "voz de esquilo/desenho (aguda, rápida no timbre)",
    "monstro": "voz de monstro/vilão (grave)",
    "sussurro": "sussurro (sem tom de voz, só o 'ar')",
    "subaquatico": "debaixo d'água (abafado e ondulante)",
}
_SINONIMOS_VOZ = {"reverberacao": "reverb", "reverberação": "reverb", "sala": "reverb", "igreja": "caverna",
                  "estádio": "estadio", "ginasio": "estadio", "robô": "robo", "robotica": "robo", "rádio": "radio",
                  "walkie_talkie": "radio", "chipmunk": "esquilo", "fino": "esquilo", "grave": "monstro",
                  "demonio": "monstro", "sussurrar": "sussurro", "agua": "subaquatico", "debaixo_dagua": "subaquatico",
                  "subaquático": "subaquatico", "alto_falante": "megafone"}


def efeito_voz(doc: Documento, efeito: str, intensidade=None, faixa: int = 1, inicio=None, fim=None,
               estado: dict | None = None, progresso=None) -> dict:
    """Efeitos de voz criativos (ver EFEITOS_VOZ). intensidade 'leve'/'media'/'forte' ou 0–1."""
    e = (efeito or "").strip().lower().replace(" ", "_").replace("-", "_")
    e = _SINONIMOS_VOZ.get(e, e)
    if e not in EFEITOS_VOZ:
        raise ValueError(f"efeito: um de {', '.join(EFEITOS_VOZ)}")
    x = _nivel(intensidade)
    info = core._info_audio(doc.atual())
    k = core._faixa_valida(faixa, len(info["auds"]))
    s = info["auds"][k]
    sr = int(s.get("sample_rate") or 48000)
    canais = int(s.get("channels") or 2)
    medir, metodo, det = None, "clique", {"efeito": e, "intensidade": round(x, 2)}
    if e == "eco":
        d = 180 + 320 * x
        dec = 0.25 + 0.35 * x
        frag = (f"aecho=in_gain=1:out_gain=1:delays={d:.0f}|{2 * d:.0f}|{3 * d:.0f}:"
                f"decays={dec:.2f}|{dec ** 2:.3f}|{dec ** 3:.3f},{core._LIMITADOR}")
        det.update(atraso_ms=round(d), decaimento=round(dec, 2))
    elif e in ("reverb", "banheiro", "caverna", "estadio"):
        t60, pre, molh, seco, corte, extra = {
            "reverb": (0.5 + 1.3 * x, 12, 0.25 + 0.45 * x, 1.0, 6500, ""),
            "banheiro": (0.6 + 0.6 * x, 3, 0.35 + 0.4 * x, 1.0, 9000, ""),
            "caverna": (2.5 + 2.5 * x, 25, 0.45 + 0.4 * x, 0.85, 3000,
                        f"aecho=1:1:{260 + 200 * x:.0f}:{0.25 + 0.2 * x:.2f},"),
            "estadio": (2.0 + 2.0 * x, 40, 0.4 + 0.4 * x, 0.85, 5000,
                        f"aecho=1:1:{110 + 60 * x:.0f}|{240 + 120 * x:.0f}:0.35|0.2,"),
        }[e]
        frag = _frag_reverb(sr, canais, t60, pre, molh, seco, corte, extra)
        det.update(t60_s=round(t60, 2), molhado=round(molh, 2))
    elif e == "robo":
        # fase zerada em janelas de 512 amostras (~10 ms): tom fixo e metálico; 'forte' soma modulação em anel.
        # (1−2·mod(b,2)) desloca o pulso de fase zero para o MEIO da janela (senão ele cai nas bordas, onde a
        # janela de síntese zera, e o atraso real nos cliques ficava ~4 ms diferente do medido). Assim o atraso é
        # o da identidade com a mesma janela (384 amostras), medido no `medir` — conferido numa trilha de cliques.
        rob = (f"afftfilt=real={esc_filtro('hypot(re,im)*(1-2*mod(b,2))')}:imag=0:win_size=512:overlap=0.75")
        medir = "afftfilt=real=re:imag=im:win_size=512:overlap=0.75"
        anel = f",aeval={esc_filtro('val(ch)*(0.6+0.4*sin(2*PI*30*t))')}:c=same" if x >= 0.85 else ""
        if x < 0.5:  # leve: metade robô, metade voz (a voz seca atrasada igual ao afftfilt para não dar filtro pente)
            lat_r = latencia(_frag(medir), sr, core._layout(s))
            frag = (f"[{{e}}]asplit=2[rb1][rb2];[rb1]{rob},volume=0.6[rbw];"
                    f"[rb2]adelay=delays={int(round(lat_r * sr))}S:all=1,volume=0.5[rbd];"
                    f"[rbd][rbw]amix=inputs=2:normalize=0:duration=longest,{core._LIMITADOR}[{{s}}]")
        else:
            frag = f"{rob},volume=1.5{anel},{core._LIMITADOR}"
    elif e == "telefone":
        frag = ("highpass=f=300,highpass=f=300,lowpass=f=3400,lowpass=f=3400,"
                f"equalizer=f=1700:t=o:w=1.5:g={3 + 4 * x:.1f},"
                f"acompressor=threshold=0.1:ratio=4:attack=5:release=80:makeup=2,"
                + (f"acrusher=bits={12 - 4 * x:.0f}:mix={0.2 + 0.4 * x:.2f}:mode=log," if x >= 0.5 else "")
                + core._LIMITADOR)
    elif e == "radio":
        frag = ("highpass=f=250,highpass=f=250,lowpass=f=4500,lowpass=f=4500,"
                f"equalizer=f=2000:t=o:w=1:g={2 + 4 * x:.1f},"
                "acompressor=threshold=0.08:ratio=8:attack=3:release=60:makeup=4,"
                f"asoftclip=type=tanh:threshold={0.9 - 0.5 * x:.2f},{core._LIMITADOR}")
    elif e == "megafone":
        frag = ("highpass=f=600,highpass=f=600,lowpass=f=3800,lowpass=f=3800,"
                f"equalizer=f=1800:t=o:w=1:g={6 + 6 * x:.1f},volume={4 + 6 * x:.1f}dB,"
                f"asoftclip=type=hard:threshold={0.7 - 0.4 * x:.2f},{core._LIMITADOR}")
    elif e in ("esquilo", "monstro"):
        _exigir("rubberband", f"o efeito '{e}'")
        r = 1.45 + 0.6 * x if e == "esquilo" else 0.78 - 0.25 * x
        frag = f"rubberband=pitch={r:.4f}:pitchq=quality"
        if e == "monstro":
            frag += f",bass=f=120:g={3 + 3 * x:.1f},{core._LIMITADOR}"
        metodo = "envelope"
        medir = f"rubberband=pitch={r:.4f}:pitchq=quality"
        det["semitons"] = round(12 * math.log2(r), 1)
    elif e == "sussurro":
        frag = (f"afftfilt=real={esc_filtro('hypot(re,im)*cos((random(0)*2-1)*2*PI)')}:"
                f"imag={esc_filtro('hypot(re,im)*sin((random(1)*2-1)*2*PI)')}:win_size=256:overlap=0.75,"
                f"highpass=f=400,volume={2 + 2 * x:.1f}dB,{core._LIMITADOR}")
        medir = "afftfilt=real=re:imag=im:win_size=256:overlap=0.75"
    else:  # subaquatico
        frag = (f"lowpass=f={900 - 400 * x:.0f},lowpass=f={900 - 400 * x:.0f},"
                f"vibrato=f=3:d={0.15 + 0.35 * x:.2f},chorus=0.6:0.8:40:0.4:0.6:1.2,{core._LIMITADOR}")
        medir = None
        metodo = "envelope"  # o vibrato/chorus deformam o clique
    cauda = e in ("eco", "reverb", "banheiro", "caverna", "estadio", "subaquatico")
    return _aplicar(doc, faixa, frag, f"efeito de voz '{e}' em A{k + 1}", inicio=inicio, fim=fim, estado=estado,
                    progresso=progresso, medir=medir, metodo=metodo, exato=cauda, detalhes=det)


# ---------------------------------------------------------------- 3. tom
def mudar_tom(doc: Documento, semitons: float, manter_velocidade: bool = True, preservar_formantes: bool = False,
              faixa: int = 1, inicio=None, fim=None, estado: dict | None = None, progresso=None) -> dict:
    """Muda o tom (afinação) em semitons (−24 a +24), sem mudar a duração (rubberband, alta qualidade; sem ele,
    asetrate + atempo). preservar_formantes=True: muda a nota mantendo o timbre (voz menos 'de desenho').
    manter_velocidade=False (efeito de fita: tom e velocidade juntos) só em arquivo de áudio sem vídeo."""
    st_ = float(semitons)
    if not -24 <= st_ <= 24 or st_ == 0:
        raise ValueError("semitons entre −24 e +24 (diferente de 0); 12 = uma oitava")
    r = 2 ** (st_ / 12)
    info = core._info_audio(doc.atual())
    k, s, st, fim_f, sr, lay, pre = _faixa(info, faixa)
    det = {"semitons": st_, "fator": round(r, 5)}
    if not manter_velocidade:
        if info["v"]:
            raise ValueError("manter_velocidade=False mudaria a duração do áudio e o tiraria de sincronia com o "
                             "vídeo. Para acelerar/desacelerar vídeo e áudio juntos use `velocidade_trecho` "
                             "(manter_tom=False dá o mesmo efeito de fita)")
        if inicio is not None or fim is not None:
            raise ValueError("manter_velocidade=False vale para a faixa inteira (sem inicio/fim)")
        if len(info["auds"]) > 1:
            raise ValueError("manter_velocidade=False muda a duração: só em arquivo com uma faixa de áudio")
        nova = (fim_f - st) / r
        grafo = [f"[0:a:{k}]{pre}asetpts=PTS-STARTPTS,asetrate={sr * r:.6f},aresample={sr},"
                 f"asetpts=PTS+{st:.6f}/TB[ar]"]
        det["nova_duracao"] = fmt_tempo(st + nova)
        return core._editar_audio(doc, grafo, {k: "ar"}, f"tom {st_:+g} semitons em A{k + 1} (tom e velocidade, "
                                  f"efeito de fita)", progresso, info=info, extra={"detalhes": det},
                                  dur_saida=st + nova)
    if tem_filtro("rubberband"):
        frag = f"rubberband=pitch={r:.6f}:pitchq=quality" + (":formant=preserved" if preservar_formantes else "")
        det["motor"] = "rubberband"
    else:
        # sem rubberband: reamostra (muda tom e velocidade) e devolve a velocidade com atempo (0,5–2 por etapa)
        t = 1 / r
        etapas = []
        while t > 2:
            etapas.append("atempo=2")
            t /= 2
        while t < 0.5:
            etapas.append("atempo=0.5")
            t /= 0.5
        etapas.append(f"atempo={t:.6f}")
        frag = f"asetrate={sr * r:.6f},aresample={sr}," + ",".join(etapas)
        det["motor"] = "asetrate+atempo (sem rubberband no ffmpeg: qualidade menor)"
        if preservar_formantes:
            det["aviso"] = "preservar_formantes precisa do rubberband; ignorado"
    return _aplicar(doc, faixa, frag, f"tom {st_:+g} semitons em A{k + 1}", inicio=inicio, fim=fim,
                    estado=estado, progresso=progresso, metodo="envelope", exato=True, detalhes=det)


# ---------------------------------------------------------------- 4. dinâmica
MODOS_DINAMICA = {
    "nivelar_fala": "iguala o volume da fala (partes baixas sobem, altas descem) — speechnorm",
    "nivelar_suave": "nivelamento lento e geral (música, ambiente, entrevistas) — dynaudnorm",
    "compressor": "comprime picos (som mais 'cheio' e constante) — acompressor + limitador",
    "limitador": "segura os picos abaixo de −1 dBFS; intensidade sobe o volume antes (+0/+3/+6 dB)",
    "deesser": "suaviza 'S' e 'X' sibilantes",
    "gate": "silencia o ruído de fundo nas pausas (abre só quando há voz)",
    "remover_cliques": "remove estalos, cliques e crepitação (disco de vinil, boca) — adeclick",
    "remover_distorcao": "recupera áudio estourado/clipado — adeclip (lento)",
}
_SINONIMOS_DIN = {"nivelar": "nivelar_fala", "normalizar_fala": "nivelar_fala", "compressao": "compressor",
                  "compressão": "compressor", "limitar": "limitador", "de_esser": "deesser", "sibilancia": "deesser",
                  "noise_gate": "gate", "portao": "gate", "cliques": "remover_cliques", "estalos": "remover_cliques",
                  "declick": "remover_cliques", "clipping": "remover_distorcao", "distorcao": "remover_distorcao",
                  "estourado": "remover_distorcao", "declip": "remover_distorcao"}


def dinamica_audio(doc: Documento, modo: str, intensidade=None, faixa: int = 1, inicio=None, fim=None,
                   estado: dict | None = None, progresso=None) -> dict:
    m = (modo or "").strip().lower().replace(" ", "_").replace("-", "_")
    m = _SINONIMOS_DIN.get(m, m)
    if m not in MODOS_DINAMICA:
        raise ValueError(f"modo: um de {', '.join(MODOS_DINAMICA)}")
    x = _nivel(intensidade)
    det: dict = {"modo": m, "intensidade": round(x, 2)}
    avisos: list[str] = []
    if m == "nivelar_fala":
        e = 2 + 10.5 * x  # leve ≈ 5,7×, media ≈ 8,3×, forte ≈ 11,5× de expansão máxima
        frag = f"speechnorm=e={e:.2f}:r=0.0001:l=1:p=0.9"
        det["expansao_max"] = round(e, 2)
        avisos.append("o nivelamento também sobe o ruído das pausas; se aparecer chiado, use reduzir_ruido antes "
                      "ou dinamica_audio(modo='gate')")
    elif m == "nivelar_suave":
        g = int(31 - 20 * x) | 1  # janela gaussiana (ímpar): menor = mais rápido/forte
        frag = f"dynaudnorm=f=200:g={g}:p=0.9:m={5 + 15 * x:.1f}"
        det["janela"] = g
    elif m == "compressor":
        thr = -14 - 14 * x
        ratio = 2 + 6 * x
        makeup = (-thr) * (1 - 1 / ratio) * 0.45
        frag = (f"acompressor=threshold={_lin(thr):.5f}:ratio={ratio:.2f}:attack=10:release=200:knee=4:"
                f"makeup={_lin(makeup):.4f},{core._LIMITADOR}")
        det.update(limiar_db=round(thr, 1), razao=round(ratio, 2), ganho_db=round(makeup, 1))
    elif m == "limitador":
        ganho = 0 if x < 0.5 else (3 if x < 0.85 else 6)
        frag = (f"volume={ganho}dB," if ganho else "") + f"alimiter=limit={_lin(-1):.5f}:level=0:latency=1"
        det.update(teto_db=-1, ganho_entrada_db=ganho)
    elif m == "deesser":
        frag = f"deesser=i={0.25 + 0.6 * x:.2f}:m=0.5:f=0.5:s=o"
    elif m == "gate":
        info = core._info_audio(doc.atual())
        k = core._faixa_valida(faixa, len(info["auds"]))
        dbs = [d for _, d in core.niveis_audio(doc.atual(), k)]
        piso, fala = core._niveis_fala(dbs)
        if fala - piso < 8:
            raise ValueError(f"pouca diferença entre voz ({fala:.0f} dB) e pausas ({piso:.0f} dB): o gate cortaria a "
                             "voz junto. Tente reduzir_ruido")
        thr = piso + max(6.0, 0.35 * (fala - piso))
        rng = -(12 + 30 * x)
        frag = (f"agate=threshold={_lin(thr):.6f}:range={_lin(rng):.6f}:ratio=10:attack=5:release=200:"
                f"knee=2:detection=rms")
        det.update(piso_db=round(piso, 1), voz_db=round(fala, 1), limiar_db=round(thr, 1), atenuacao_db=round(rng))
    elif m == "remover_cliques":
        t = 6 - 5 * x  # limiar de detecção (menor = mais agressivo); padrão do adeclick = 2
        frag = f"adeclick=w=55:o=75:t={t:.2f}"
        medir = "adeclick=w=55:o=75:t=100"
        det["limiar"] = round(t, 2)
        return _aplicar(doc, faixa, frag, f"removeu cliques de A{int(faixa or 1)}", inicio=inicio, fim=fim,
                        estado=estado, progresso=progresso, medir=medir, detalhes=det)
    else:  # remover_distorcao
        t = 20 - 15 * x
        frag = f"adeclip=w=55:o=75:t={t:.2f}"
        det["limiar"] = round(t, 2)
        avisos.append("o adeclip é lento (~1–3× o tempo real)")
        return _aplicar(doc, faixa, frag, f"recuperou distorção (clipping) de A{int(faixa or 1)}", inicio=inicio,
                        fim=fim, estado=estado, progresso=progresso, medir="adeclip=w=55:o=75:t=100",
                        detalhes=det, avisos=avisos)
    return _aplicar(doc, faixa, frag, f"dinâmica '{m}' em A{int(faixa or 1)}", inicio=inicio, fim=fim, estado=estado,
                    progresso=progresso, detalhes=det, avisos=avisos)


# ---------------------------------------------------------------- 5. canais
ACOES_CANAIS = {
    "mono": "junta os canais num só (mono)",
    "estereo": "mono → estéreo (o mesmo som nos dois lados)",
    "trocar": "troca esquerdo ↔ direito",
    "so_esquerdo": "usa só o canal esquerdo, nos dois lados (microfone de lapela só num canal)",
    "so_direito": "usa só o canal direito, nos dois lados",
    "separar": "esquerdo e direito viram duas faixas mono (A_n = esquerdo, nova faixa = direito)",
    "ampliar_estereo": "abre o estéreo (mais largo)",
    "estreitar_estereo": "fecha o estéreo (mais perto do mono)",
    "realcar_centro": "realça o que está no centro do estéreo (voz/diálogo) — dialoguenhance",
    "remover_centro": "tira o que está no centro (karaokê: voz principal some, se estiver no centro)",
    "inverter_fase": "inverte a polaridade (corrige microfones em contrafase)",
}
_SINONIMOS_CANAIS = {"estéreo": "estereo", "stereo": "estereo", "inverter": "trocar", "trocar_lados": "trocar",
                     "esquerdo": "so_esquerdo", "direito": "so_direito", "so_esquerda": "so_esquerdo",
                     "so_direita": "so_direito", "dividir": "separar", "largura": "ampliar_estereo",
                     "alargar": "ampliar_estereo", "karaoke": "remover_centro", "karaokê": "remover_centro",
                     "remover_voz": "remover_centro", "centralizar_voz": "realcar_centro", "dialogo": "realcar_centro",
                     "polaridade": "inverter_fase"}


def canais_audio(doc: Documento, acao: str, intensidade=None, faixa: int = 1, inicio=None, fim=None,
                 estado: dict | None = None, progresso=None) -> dict:
    a_ = (acao or "").strip().lower().replace(" ", "_").replace("-", "_")
    a_ = _SINONIMOS_CANAIS.get(a_, a_)
    if a_ not in ACOES_CANAIS:
        raise ValueError(f"acao: uma de {', '.join(ACOES_CANAIS)}")
    info = core._info_audio(doc.atual())
    k, s, st, fim_f, sr, lay, pre = _faixa(info, faixa)
    n = int(s.get("channels") or 2)
    x = _nivel(intensidade)
    estereo_req = a_ in ("trocar", "so_esquerdo", "so_direito", "separar", "ampliar_estereo", "estreitar_estereo",
                         "realcar_centro", "remover_centro")
    if estereo_req and n != 2:
        raise ValueError(f"'{a_}' precisa de faixa estéreo (A{k + 1} tem {n} canal(is), layout {lay})"
                         + ("; para mono → estéreo use acao='estereo'" if n == 1 else ""))
    det = {"acao": a_, "canais_antes": n, "layout_antes": lay}
    desc = f"canais de A{k + 1}: {a_.replace('_', ' ')}"
    if a_ == "separar":
        if inicio is not None or fim is not None:
            raise ValueError("'separar' vale para a faixa inteira")
        # pan para mono (o channelsplit dá layout "1 channels (FL)", que o encoder AAC recusa)
        grafo = [f"[0:a:{k}]{pre}asplit=2[sl][sr];[sl]pan=mono|c0=c0[esq];[sr]pan=mono|c0=c1[dir]"]
        titulo = (s.get("tags") or {}).get("title")
        base = f"{titulo} — " if titulo else ""
        det["faixas"] = {f"A{k + 1}": "esquerdo", f"A{len(info['auds']) + 1}": "direito"}
        return _editar_faixas(doc, grafo, {k: "esq"}, desc, progresso,
                              novas=[("dir", {"title": f"{base}direito",
                                              "language": (s.get("tags") or {}).get("language")})],
                              tags={k: {"title": f"{base}esquerdo"}}, info=info, extra={"detalhes": det})
    lay_saida = None
    medir = "anull"  # pan/volume não atrasam (e o clique se cancela no 'remover_centro')
    if a_ == "mono":
        if n == 1:
            raise ValueError(f"A{k + 1} já é mono")
        frag = ("pan=mono|c0=0.5*c0+0.5*c1" if n == 2 else "aformat=channel_layouts=mono")
        lay_saida = "mono"
    elif a_ == "estereo":
        if n != 1:
            raise ValueError(f"A{k + 1} já tem {n} canais")
        frag = "pan=stereo|c0=c0|c1=c0"
        lay_saida = "stereo"
    elif a_ == "trocar":
        frag = "pan=stereo|c0=c1|c1=c0"
    elif a_ == "so_esquerdo":
        frag = "pan=stereo|c0=c0|c1=c0"
    elif a_ == "so_direito":
        frag = "pan=stereo|c0=c1|c1=c1"
    elif a_ == "ampliar_estereo":
        frag = f"extrastereo=m={1.3 + 1.2 * x:.2f}:c=false,{core._LIMITADOR}"
        det["fator"] = round(1.3 + 1.2 * x, 2)
    elif a_ == "estreitar_estereo":
        frag = f"extrastereo=m={max(0.0, 0.8 - 0.8 * x):.2f}:c=false"
        det["fator"] = round(max(0.0, 0.8 - 0.8 * x), 2)
    elif a_ == "realcar_centro":
        _exigir("dialoguenhance", "realçar o centro/diálogo")
        # dialoguenhance devolve 3.0 (FL FR FC): o centro realçado volta para os dois lados
        frag = (f"dialoguenhance=original={1 - 0.5 * x:.2f}:enhance={1 + 2 * x:.2f}:voice=2,"
                f"pan=stereo|c0=c0+c2|c1=c1+c2,{core._LIMITADOR}")
        medir = frag
    elif a_ == "remover_centro":
        frag = "pan=stereo|c0=c0-c1|c1=c1-c0"
        det["aviso"] = "só some o que está igual nos dois canais (voz no centro); graves centrais também caem"
    else:  # inverter_fase
        frag = "volume=-1"
    return _aplicar(doc, faixa, frag, desc, inicio=inicio, fim=fim, estado=estado, progresso=progresso,
                    medir=medir, lay_saida=lay_saida, detalhes=det)


# ---------------------------------------------------------------- 6. sincronia
def sincronizar_audio(doc: Documento, deslocamento_ms: float, faixa: int = 1, progresso=None) -> dict:
    """Move a faixa no tempo: deslocamento_ms > 0 ATRASA o áudio (som chega depois), < 0 ADIANTA.
    Exato na amostra: re-encoda só essa faixa (silêncio onde o som saiu, corte do que passou do fim);
    a faixa mantém o início e a duração (o vídeo e o contêiner não mudam).
    Decisão: sem modo "sem perda" por deslocamento do contêiner — testado no ffmpeg 6.1 e 8.1: no MP4 o atraso
    sai errado pelo priming do AAC (0,178 s em vez de 0,2) e no MKV um deslocamento negativo empurra o VÍDEO."""
    d = float(deslocamento_ms) / 1000
    if d == 0:
        raise ValueError("deslocamento_ms diferente de 0 (positivo atrasa o áudio, negativo adianta)")
    info = core._info_audio(doc.atual())
    k, s, st, fim_f, sr, lay, pre = _faixa(info, faixa)
    D = fim_f - st
    if abs(d) >= D:
        raise ValueError(f"deslocamento maior que a faixa ({D:.2f} s)")
    n = int(round(abs(d) * sr))
    if d > 0:
        corpo = f"adelay=delays={n}S:all=1,atrim=end={D:.6f}"
        desc = f"atrasou A{k + 1} em {abs(d) * 1000:g} ms"
    else:
        corpo = f"atrim=start_sample={n},asetpts=PTS-STARTPTS,apad=pad_dur={abs(d):.6f}"
        desc = f"adiantou A{k + 1} em {abs(d) * 1000:g} ms"
    grafo = [f"[0:a:{k}]{pre}asetpts=PTS-STARTPTS,aresample={sr},asetpts=N/SR/TB,{corpo},"
             f"asetpts=PTS-STARTPTS+{st:.6f}/TB[ar]"]
    det = {"faixa": f"A{k + 1}", "deslocamento_ms": round(d * 1000, 3), "amostras": n if d > 0 else -n,
           "dica": "lábio antes do som → atrase o vídeo... ou seja, adiante o áudio (negativo); som antes do lábio "
                   "→ atrase o áudio (positivo)"}
    return core._editar_audio(doc, grafo, {k: "ar"}, desc, progresso, info=info, extra={"detalhes": det})


# ---------------------------------------------------------------- 7. censura
def _juntar_trechos(tr: list[tuple[float, float]], folga: float = 0.0) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(tr):
        if out and a <= out[-1][1] + folga:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def censurar_palavras(doc: Documento, palavras: list[str] | None = None, trechos: list | None = None,
                      som: str = "bip", faixa: int = 1, idioma: str = "auto", modelo: str = "base",
                      margem_ms: float = 60, limiar: float = 0.8, volume_bip_db: float = -12,
                      frequencia_bip: float = 1000, estado: dict | None = None, progresso=None) -> dict:
    """Troca palavras (achadas na transcrição local, com o tempo de cada palavra) e/ou trechos manuais por um
    bipe ou silêncio. Devolve a versão + `ocorrencias`; sem nada achado, não cria versão (`sem_edicao`)."""
    som = (som or "bip").strip().lower()
    som = {"bipe": "bip", "beep": "bip", "apito": "bip", "mudo": "silencio", "silêncio": "silencio",
           "silenciar": "silencio"}.get(som, som)
    if som not in ("bip", "silencio"):
        raise ValueError("som: 'bip' ou 'silencio'")
    if not palavras and not trechos:
        raise ValueError("passe palavras=[...] (achadas pela fala) e/ou trechos=[[inicio, fim], ...]")
    entrada = doc.atual()
    info = core._info_audio(entrada)
    k, s, st, fim_f, sr, lay, pre = _faixa(info, faixa)
    est = dict(estado or {})
    est.setdefault("duracao", info["dur"])
    folga = max(0.0, float(margem_ms)) / 1000
    spans: list[tuple[float, float]] = []
    ocorrencias: list[dict] = []
    avisos: list[str] = []
    det: dict = {"faixa": f"A{k + 1}", "som": som}
    if palavras:
        from . import fala
        prog_tr = (lambda f: progresso(0.6 * f)) if progresso else None
        tr = fala.transcrever(entrada, idioma=idioma, modelo=modelo, faixa=k + 1, palavras=True, progresso=prog_tr)
        if tr.get("modelo_baixado"):
            mb = tr["modelo_baixado"]
            avisos.append(f"primeiro uso: baixei o modelo de fala '{mb['modelo']}' ({mb['tamanho']})")
        det["transcricao"] = {"idioma": tr.get("idioma"), "modelo": tr.get("modelo"), "do_cache": tr.get("cache")}
        nao_achadas = []
        for p in palavras:
            p = str(p).strip()
            if not p:
                continue
            r = fala.buscar_fala(tr, p, limiar=limiar, margem_s=0.0, max_resultados=1000)
            if not r["encontrados"]:
                nao_achadas.append(p)
            for x in r["encontrados"]:
                a, b = x["inicio"] - folga, x["fim"] + folga
                spans.append((a, b))
                ocorrencias.append({"palavra": p, "ouvido": x["texto"], "inicio": round(max(st, a), 3),
                                    "fim": round(min(fim_f, b), 3), "similaridade": x["similaridade"]})
        if nao_achadas:
            avisos.append(f"não achei na fala: {', '.join(nao_achadas)} (limiar {limiar}; confira com "
                          "`transcrever` ou baixe o limiar para ~0.7)")
    for t in trechos or []:
        try:
            a, b = parse_tempo(t[0], est), parse_tempo(t[1], est)
        except (TypeError, IndexError, KeyError):
            raise ValueError("trechos: [[inicio, fim], ...]") from None
        if b <= a:
            raise ValueError(f"trecho vazio: {t}")
        spans.append((a, b))
        ocorrencias.append({"trecho_manual": True, "inicio": round(a, 3), "fim": round(b, 3)})
    spans = [(max(st, a), min(fim_f, b)) for a, b in spans]
    spans = _juntar_trechos([(a, b) for a, b in spans if b - a > 0.005])
    ocorrencias.sort(key=lambda o: o["inicio"])
    if not spans:
        return {"sem_edicao": True, "ocorrencias": [], "avisos": avisos or ["nada a censurar"], "detalhes": det}
    # máscara m(t) ∈ [0, 1] (1 = censurado) com rampas de 5 ms, avaliada a cada 64 amostras (≤ 1,3 ms)
    rp = 0.005
    termos = [f"clip(min((t-{a - st:.4f})/{rp},({b - st:.4f}-t)/{rp}),0,1)" for a, b in spans]
    mascara = "+".join(termos)
    D = fim_f - st
    fmt = f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={lay}"
    voz = (f"[0:a:{k}]{pre}asetpts=PTS-STARTPTS,{fmt},asetnsamples=n=64:p=0,"
           f"volume={esc_filtro(f'1-({mascara})')}:eval=frame")
    volta = f"asetpts=PTS-STARTPTS+{st:.6f}/TB[ar]"
    if som == "bip":
        grafo = [voz + "[cv]"]
        # sine tem amplitude 1/8 (−18 dBFS): volume leva o pico a volume_bip_db
        grafo.append(f"sine=f={float(frequencia_bip):g}:sample_rate={sr}:duration={D:.6f},{fmt},"
                     f"volume={float(volume_bip_db) + 18.06:.2f}dB,asetnsamples=n=64:p=0,"
                     f"volume={esc_filtro(mascara)}:eval=frame[cb]")
        # duration=longest: o bipe tem a duração da faixa (com first o ffmpeg 6.1 perdia o fim, ver core)
        grafo.append(f"[cv][cb]amix=inputs=2:normalize=0:duration=longest,atrim=end={D:.6f},{volta}")
    else:
        grafo = [f"{voz},{volta}"]
    det["trechos"] = [[round(a, 3), round(b, 3)] for a, b in spans]
    desc = (f"censurou {len(spans)} trecho(s) de A{k + 1} com " + ("bipe" if som == "bip" else "silêncio"))
    extra = {"detalhes": det, "ocorrencias": ocorrencias}
    if avisos:
        extra["avisos"] = avisos
    prog_ed = (lambda f: progresso(0.6 + 0.4 * f)) if (progresso and palavras) else progresso
    return core._editar_audio(doc, grafo, {k: "ar"}, desc, prog_ed, info=info, extra=extra)


# ---------------------------------------------------------------- 8. loudness
ALVOS_LOUDNESS = {  # (LUFS integrado, pico verdadeiro dBTP)
    "youtube": (-14.0, -1.0), "spotify": (-14.0, -1.0), "tiktok": (-14.0, -1.0), "instagram": (-14.0, -1.0),
    "apple_music": (-16.0, -1.0), "podcast": (-16.0, -1.0), "apple_podcasts": (-16.0, -1.0),
    "audiolivro": (-18.0, -3.0), "tv": (-23.0, -1.0), "ebu": (-23.0, -1.0), "tv_eua": (-24.0, -2.0),
    "atsc": (-24.0, -2.0), "cinema": (-27.0, -2.0), "netflix": (-27.0, -2.0),
}


def medir_loudness(arquivo: Path | str, faixa: int = 1, filtro: str = "") -> dict:
    """{I (LUFS), TP (dBTP), LRA (LU), limiar} da faixa (1 = A1), pela 1ª passada do loudnorm (EBU R128)."""
    k = int(faixa) - 1
    af = (filtro + "," if filtro else "") + "loudnorm=I=-23:TP=-1:LRA=11:print_format=json"
    r = subprocess.run([core.FFMPEG[0], "-hide_banner", "-nostdin", "-i", str(arquivo), "-map", f"0:a:{k}",
                        "-af", af, "-f", "null", "-"], capture_output=True, text=True,
                       preexec_fn=core._morrer_com_pai if core._prctl is not None else None)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
    if r.returncode != 0 or not m:
        raise core.ErroFFmpeg(f"medição de loudness falhou: {r.stderr[-1500:]}")
    j = json.loads(m.group(0))

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return float("-inf")
    return {"I": num(j["input_i"]), "TP": num(j["input_tp"]), "LRA": num(j["input_lra"]),
            "limiar": num(j["input_thresh"])}


def _cadeia_loudness(ganho: float, limitar: bool, teto: float, sr: int) -> str:
    c = f"volume={ganho:.3f}dB"
    if limitar:
        # limitador com 4× sobreamostragem ≈ limitador de pico verdadeiro (TP do EBU R128)
        c += (f",aresample={4 * sr},alimiter=limit={_lin(teto):.6f}:level=0:latency=1:attack=1:release=60,"
              f"aresample={sr}")
    return c


def normalizar_volume(doc: Documento, alvo="youtube", pico_db: float | None = None, faixa: int = 1,
                      progresso=None) -> dict:
    """Loudness exato em LUFS (EBU R128): mede, aplica ganho linear e, só se o pico passaria do teto, um
    limitador de pico verdadeiro; confere a previsão e mede o resultado final."""
    if isinstance(alvo, str):
        chave = alvo.strip().lower().replace(" ", "_").replace("-", "_")
        if chave in ALVOS_LOUDNESS:
            I_alvo, tp_padrao = ALVOS_LOUDNESS[chave]
            nome = chave
        else:
            try:
                I_alvo, tp_padrao, nome = float(chave.replace(",", ".").removesuffix("lufs").strip("_ ")), -1.0, None
            except ValueError:
                raise ValueError(f"alvo: um de {', '.join(ALVOS_LOUDNESS)} ou número em LUFS (ex. -16)") from None
    else:
        I_alvo, tp_padrao, nome = float(alvo), -1.0, None
    if not -40 <= I_alvo <= -5:
        raise ValueError("alvo entre −40 e −5 LUFS")
    teto = float(pico_db) if pico_db is not None else tp_padrao
    if not -9 <= teto <= 0:
        raise ValueError("pico_db entre −9 e 0 dBTP")
    entrada = doc.atual()
    info = core._info_audio(entrada)
    k, s, st, fim_f, sr, lay, pre = _faixa(info, faixa)
    if progresso:
        progresso(0.02)
    antes = medir_loudness(entrada, k + 1)
    if antes["I"] < -70 or math.isinf(antes["I"]):
        raise ValueError(f"A{k + 1} está praticamente em silêncio (loudness {antes['I']} LUFS): nada a normalizar")
    ganho = I_alvo - antes["I"]
    if ganho > 40:
        raise ValueError(f"precisaria de +{ganho:.0f} dB: o áudio é baixo demais (só ruído?)")
    margem = 0.5  # o AAC/Opus sobe o pico verdadeiro alguns décimos: limitador mira meio dB abaixo
    limitar = antes["TP"] + ganho > teto - margem
    det: dict = {"faixa": f"A{k + 1}", "alvo_lufs": I_alvo, "pico_max_dbtp": teto, "ganho_db": round(ganho, 2),
                 "antes": {kk: round(v, 2) for kk, v in antes.items()}}
    if limitar:
        # o limitador tira um pouco de loudness: mede a previsão e corrige o ganho (até 2 vezes)
        for _ in range(2):
            prev = medir_loudness(entrada, k + 1, f"{pre}{_cadeia_loudness(ganho, True, teto - margem, sr)}")
            erro = I_alvo - prev["I"]
            if abs(erro) < 0.2:
                break
            ganho += erro
        det["limitador"] = f"pico verdadeiro limitado a {teto - margem:g} dBTP (o ganho levaria a " \
                           f"{antes['TP'] + I_alvo - antes['I']:+.1f} dBTP)"
        det["ganho_db"] = round(ganho, 2)
    if progresso:
        progresso(0.3)
    cadeia = _cadeia_loudness(ganho, limitar, teto - margem, sr)
    lat = latencia(_frag(cadeia), sr, lay)
    grafo = _grafo_efeito(info, k, _frag(cadeia), lat)
    desc = f"normalizou A{k + 1} para {I_alvo:g} LUFS" + (f" ({nome})" if nome else "")
    prog = (lambda f: progresso(0.3 + 0.6 * f)) if progresso else None
    v = core._editar_audio(doc, grafo, {k: "ar"}, desc, prog, info=info, extra={"detalhes": det})
    depois = medir_loudness(doc.atual(), k + 1)
    det["depois"] = {kk: round(vv, 2) for kk, vv in depois.items()}
    det["latencia_compensada_ms"] = round(lat * 1000, 2)
    if abs(depois["I"] - I_alvo) > 0.5:
        v.setdefault("avisos", []).append(f"loudness final {depois['I']:.1f} LUFS (alvo {I_alvo:g})")
    if depois["TP"] > teto + 0.3:
        # típico de ganho enorme + codificador com perdas (AAC/Opus refazem os picos depois do limitador)
        v.setdefault("avisos", []).append(
            f"pico verdadeiro final {depois['TP']:+.1f} dBTP passou do teto {teto:g} (o codificador "
            f"{s.get('codec_name') or 'com perdas'} recria picos depois do limitador; ganho de {ganho:+.0f} dB): "
            "se importar, rode de novo com pico_db mais baixo ou exporte em WAV/FLAC")
    if progresso:
        progresso(1.0)
    return v


# ---------------------------------------------------------------- 9. inverter
_MAX_REVERSO_BYTES = 400 << 20  # o areverse guarda o trecho decodificado (float) na memória


def inverter_audio(doc: Documento, faixa: int = 1, inicio=None, fim=None, estado: dict | None = None,
                   progresso=None) -> dict:
    """Toca o áudio de trás para frente (só o áudio; a imagem fica igual)."""
    entrada = doc.atual()
    info = core._info_audio(entrada)
    k, s, st, fim_f, sr, lay, pre = _faixa(info, faixa)
    a, b = _janela(inicio, fim, estado, info, st, fim_f)
    a0, b0 = (st, fim_f) if a is None else (a, b)
    n = int(s.get("channels") or 2)
    if (b0 - a0) * sr * n * 4 > _MAX_REVERSO_BYTES:
        lim = _MAX_REVERSO_BYTES / (sr * n * 4)
        raise ValueError(f"trecho longo demais para inverter de uma vez ({fmt_tempo(b0 - a0)}; máximo "
                         f"~{fmt_tempo(lim)} nesta faixa): passe inicio/fim")
    fmt = f"aresample={sr},aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={lay}"
    # cada pedaço numa entrada própria (sem asplit, que guardaria o resto do arquivo esperando o concat):
    # 0 = antes do trecho, 1 = trecho (invertido), 2 = depois
    extra_in = ["-ss", f"{a0:.6f}", "-t", f"{b0 - a0:.6f}", "-i", str(entrada)]
    partes, rots = [], []
    if a0 > st + 0.0005:
        partes.append(f"[0:a:{k}]{pre}{fmt},asetpts=PTS-STARTPTS,atrim=end={a0 - st:.6f},asetpts=PTS-STARTPTS[p0]")
        rots.append("[p0]")
    # faixa inteira: sem atrim (a duração anunciada pode ser alguns ms menor que o áudio real e cortaria o fim)
    corte = f"atrim=end={b0 - a0:.6f}," if a is not None else ""
    partes.append(f"[1:a:{k}]{pre}{fmt},asetpts=PTS-STARTPTS,{corte}areverse,asetpts=PTS-STARTPTS[p1]")
    rots.append("[p1]")
    if b0 < fim_f - 0.0005:
        extra_in += ["-ss", f"{b0:.6f}", "-i", str(entrada)]
        partes.append(f"[2:a:{k}]{pre}{fmt},asetpts=PTS-STARTPTS[p2]")
        rots.append("[p2]")
    if len(rots) > 1:
        partes.append(f"{''.join(rots)}concat=n={len(rots)}:v=0:a=1,asetpts=PTS-STARTPTS+{st:.6f}/TB[ar]")
    else:
        partes[-1] = partes[-1].replace("[p1]", f",asetpts=PTS+{st:.6f}/TB[ar]")
    det = {"faixa": f"A{k + 1}", "trecho": f"{fmt_tempo(a0)}→{fmt_tempo(b0)}"}
    desc = f"inverteu o áudio de A{k + 1}" + core._desc_janela(a, b)
    return core._editar_audio(doc, partes, {k: "ar"}, desc, progresso, extra_in, info=info,
                              extra={"detalhes": det})


# ---------------------------------------------------------------- 10. mixar faixas
def mixar_faixas(doc: Documento, faixas: list[int] | None = None, volumes_db: list[float] | None = None,
                 destino: str = "nova", titulo: str | None = None, progresso=None) -> dict:
    """Soma várias faixas do ARQUIVO numa só (amix normalize=0 + limitador). destino 'nova': a mistura vira uma
    faixa nova no fim (as originais ficam); 'substituir': a mistura entra no lugar da 1ª faixa citada e as outras
    citadas são removidas. Cada faixa entra no seu tempo (start_time) — começos diferentes ficam alinhados."""
    entrada = doc.atual()
    info = core._info_audio(entrada)
    auds = info["auds"]
    if len(auds) < 2:
        raise ValueError("o arquivo tem uma faixa de áudio só: nada a mixar")
    idx = [core._faixa_valida(f, len(auds)) for f in (faixas or range(1, len(auds) + 1))]
    if len(set(idx)) != len(idx) or len(idx) < 2:
        raise ValueError("faixas: pelo menos duas faixas diferentes, ex. [1, 2]")
    vols = list(volumes_db or [])
    if vols and len(vols) != len(idx):
        raise ValueError(f"volumes_db: um valor por faixa ({len(idx)})")
    vols = vols or [0.0] * len(idx)
    destino = (destino or "nova").strip().lower()
    if destino not in ("nova", "substituir"):
        raise ValueError("destino: 'nova' (faixa nova com a mistura) ou 'substituir'")
    ref = max((auds[i] for i in idx), key=lambda s: (int(s.get("channels") or 0), int(s.get("sample_rate") or 0)))
    sr = int(ref.get("sample_rate") or 48000)
    nc = int(ref.get("channels") or 2)
    lay = core._layout(ref)
    exts = [core._extensao_faixa(info, auds[i]) for i in idx]
    st0 = min(e[0] for e in exts)
    fim0 = max(e[1] for e in exts)
    grafo, rots = [], []
    for j, (i, v) in enumerate(zip(idx, vols)):
        s = auds[i]
        st_i = exts[j][0]
        up = "pan=stereo|c0=c0|c1=c0," if int(s.get("channels") or 0) == 1 and nc == 2 else ""
        grafo.append(f"[0:a:{i}]{core._filtro_layout(s)}asetpts=PTS-STARTPTS,{up}aresample={sr},"
                     f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts={lay},asetpts=N/SR/TB,"
                     f"adelay=delays={int(round((st_i - st0) * sr))}S:all=1,volume={float(v):g}dB[m{j}]")
        rots.append(f"[m{j}]")
    grafo.append(f"{''.join(rots)}amix=inputs={len(rots)}:normalize=0:duration=longest,{core._LIMITADOR},"
                 f"atrim=end={fim0 - st0:.6f},asetpts=PTS-STARTPTS+{st0:.6f}/TB[mix]")
    nomes = "+".join(f"A{i + 1}" for i in idx)
    det = {"faixas": nomes, "volumes_db": vols, "layout": lay, "limitador": "teto −0,26 dBFS"}
    if destino == "nova":
        n_nova = len(auds) + 1
        desc = f"mixou {nomes} numa faixa nova A{n_nova}"
        return core._editar_audio(doc, grafo, {}, desc, progresso, novas=[("mix", {"title": titulo or f"Mix {nomes}"})],
                                  info=info, extra={"detalhes": det})
    alvo = idx[0]
    remover = set(idx[1:])
    desc = f"mixou {nomes} em A{alvo + 1} (as outras saíram)"
    det["faixas_removidas"] = [f"A{i + 1}" for i in sorted(remover)]
    tags = {alvo: {"title": titulo}} if titulo else None
    return _editar_faixas(doc, grafo, {alvo: "mix"}, desc, progresso, remover=remover, tags=tags, info=info,
                          extra={"detalhes": det})
