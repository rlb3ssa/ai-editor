"""Formas de onda por faixa de áudio, extraídas com ffmpeg e guardadas em cache."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import numpy as np

from . import binarios
from .core import CACHE_DIR

PICOS_POR_SEG = 200      # resolução: 5 ms por pico
TAXA = 8000              # decodifica em 8 kHz mono (suficiente para desenhar)


def _chave(arquivo: Path, faixa: int) -> Path:
    st = arquivo.stat()
    h = hashlib.sha1(f"{arquivo}|{st.st_size}|{st.st_mtime_ns}|{faixa}|{PICOS_POR_SEG}".encode()).hexdigest()
    return CACHE_DIR / "ondas" / f"{h}.npy"


def picos(arquivo: Path | str, faixa: int) -> np.ndarray:
    """Retorna array float32 (0..1) com o pico absoluto a cada 1/PICOS_POR_SEG segundos."""
    arquivo = Path(arquivo)
    cache = _chave(arquivo, faixa)
    if cache.exists():
        return np.load(cache)
    cmd = [binarios.exe("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(arquivo),
           "-map", f"0:a:{faixa}", "-ac", "1", "-ar", str(TAXA), "-f", "s16le", "-"]
    bloco = TAXA // PICOS_POR_SEG
    out = []
    resto = np.empty(0, dtype=np.int16)
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        while True:
            dados = proc.stdout.read(TAXA * 2 * 10)  # 10 s por vez
            if not dados:
                break
            a = np.concatenate([resto, np.frombuffer(dados, dtype=np.int16)])
            n = len(a) // bloco * bloco
            if n:
                out.append(np.abs(a[:n].astype(np.int32)).reshape(-1, bloco).max(axis=1))
            resto = a[n:]
    if len(resto):
        out.append(np.array([np.abs(resto.astype(np.int32)).max()]))
    p = (np.concatenate(out) / 32768.0).astype(np.float32) if out else np.zeros(1, np.float32)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.save(cache, p)
    return p
