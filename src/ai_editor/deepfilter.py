"""Redução de ruído com rede neural: DeepFilterNet 3 (Hendrik Schröter, MIT OR Apache-2.0).

Usa o executável `deep-filter` oficial (Rust/tract, estático/musl, com o modelo DeepFilterNet3 embutido):
não precisa de PyTorch, nem de onnxruntime, nem de GPU. Baixado no primeiro uso (~36 MB, versão e SHA-256
fixos) para ~/.local/share/ai-editor/bin/deep-filter; o binário funciona igual no AppImage.

Como o áudio passa por ele:
  - a faixa (ou só o trecho pedido, com 2 s de folga antes para a rede "aquecer") é extraída em 48 kHz
    float32 (a taxa nativa do modelo) e processada em blocos de 30 s a 5 min, cada um com folga antes e depois,
    vários ao mesmo tempo (o deep-filter usa 1 núcleo; `_trabalhadores` olha núcleos e memória livre) — a
    memória fica limitada mesmo em arquivos de horas;
  - cada bloco roda com `-D` (compensa o atraso da STFT + lookahead, ~30 ms) e silêncio extra no fim
    (o atraso compensado encurta a saída); do resultado se aproveita exatamente o miolo do bloco,
    amostra por amostra, então a sincronia com o vídeo é a original;
  - a saída é PCM 16 bits 48 kHz (o formato que o deep-filter grava), que o core volta para a taxa
    original da faixa e encaixa na linha do tempo.

Uso direto: `python -m ai_editor.deepfilter --instalar` (só baixa) ou `--status`.
"""
from __future__ import annotations

import os
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import binarios, core

VERSAO = "0.5.6"
URL = (f"https://github.com/Rikorose/DeepFilterNet/releases/download/v{VERSAO}/"
       f"deep-filter-{VERSAO}-x86_64-unknown-linux-musl")
SHA256 = "70775e251eee44c0f2451a1e833326cf8bcbbe304d3e7cd12851e6fce72ef7da"
TAMANHO = 36417296
LICENCA = "MIT OR Apache-2.0 (DeepFilterNet, Hendrik Schröter; modelo DeepFilterNet3 embutido)"
SR = 48000            # taxa do modelo
_BLOCO_S = 300.0      # miolo de cada bloco
_ANTES_S = 2.0        # folga antes do bloco (estado recorrente da rede)
_DEPOIS_S = 1.0       # folga depois (lookahead + atraso compensado pelo -D)


class DeepFilterIndisponivel(RuntimeError):
    pass


def executavel(baixar: bool = True, progresso=None) -> str:
    """Caminho do deep-filter; baixa (versão fixa, SHA-256 conferido) se não houver e `baixar`."""
    p = binarios.localizar("deep-filter")
    if p:
        return p
    if not baixar:
        raise DeepFilterIndisponivel(
            f"o DeepFilterNet não está instalado. Ele é baixado sozinho no primeiro uso ({TAMANHO >> 20} MB), "
            f"ou rode: {shlex.quote(sys.executable)} -m ai_editor.deepfilter --instalar")
    from . import fala  # downloads com trava entre processos e parcial atômico
    final = binarios.BIN_SOB_DEMANDA / "deep-filter"
    with fala._trava("deep-filter"):
        if not (final.is_file() and os.access(final, os.X_OK)):
            try:
                fala._baixar(URL, final, SHA256, TAMANHO, progresso, rotulo=f"deep-filter {VERSAO}")
            except OSError as e:
                raise DeepFilterIndisponivel(
                    f"não consegui baixar o DeepFilterNet ({TAMANHO >> 20} MB) de {URL}: {e}. Sem internet? "
                    f"Baixe esse arquivo, confira o SHA-256 {SHA256} e salve como {final} (chmod +x)") from e
            os.chmod(final, 0o755)
    binarios.localizar_com_origem.cache_clear()
    return str(final)


def status() -> dict:
    p = binarios.localizar_com_origem("deep-filter")
    return {"instalado": bool(p), "caminho": str(p[1]) if p else None, "origem": p[0] if p else None,
            "versao_fixada": VERSAO, "tamanho_mb": round(TAMANHO / 2**20, 1), "licenca": LICENCA}


def _cabecalho_wav_f32(n_quadros: int, canais: int) -> bytes:
    """Cabeçalho WAVE_FORMAT_IEEE_FLOAT (3), 32 bits, 48 kHz (o `wave` do Python não grava float)."""
    dados = n_quadros * canais * 4
    fmt = struct.pack("<HHIIHH", 3, canais, SR, SR * canais * 4, canais * 4, 32)
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + dados) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", dados))


def _trabalhadores(canais: int) -> int:
    """Quantos deep-filter rodar ao mesmo tempo: metade dos núcleos (máx. 4), limitado pela memória livre
    (~150 MB do processo + até ~250 MB do bloco por canal, deixando 1,5 GB de folga para o resto do sistema)."""
    n = max(1, min(4, (os.cpu_count() or 2) // 2))
    try:
        with open("/proc/meminfo") as f:
            livre = next(int(l.split()[1]) * 1024 for l in f if l.startswith("MemAvailable:"))
        por = (150 + 250 * canais) << 20
        n = max(1, min(n, (livre - (1536 << 20)) // por))
    except (OSError, StopIteration, ValueError):
        pass
    return int(n)


def _rodar(cmd: list[str], rotulo: str) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True,
                       preexec_fn=core._morrer_com_pai if core._prctl is not None else None)
    if r.returncode != 0:
        raise core.ErroFFmpeg(f"{rotulo} falhou ({r.returncode}): {shlex.join(cmd)}\n{(r.stderr or '')[-3000:]}")


def processar(entrada: Path, faixa: int, inicio: float, fim: float | None, canais: int, atenuacao_db: float,
              pasta: Path, progresso=None) -> tuple[Path, int]:
    """Passa a faixa `faixa` (0 = A1) de `entrada`, de `inicio` a `fim` (s, contados do começo da FAIXA;
    fim=None = até o fim) pelo DeepFilterNet. Devolve (arquivo PCM s16le 48 kHz com `canais` canais, nº de
    quadros) cobrindo exatamente [inicio, fim) — ou a faixa inteira. `pasta` recebe os temporários."""
    exe = executavel(progresso=None)
    i0 = round(max(0.0, inicio - _ANTES_S) * SR)          # 1ª amostra extraída
    a = round(inicio * SR)                                 # 1ª amostra pedida
    trim = f"atrim=start_sample={i0}" + (f":end_sample={round((fim + _DEPOIS_S) * SR)}" if fim is not None else "")
    bruto = pasta / "entrada.f32"
    _rodar(core.FFMPEG + ["-i", str(entrada), "-map", f"0:a:{faixa}", "-vn", "-sn", "-dn",
                          "-af", f"asetpts=PTS-STARTPTS,aresample={SR},{trim}",
                          "-ac", str(canais), "-f", "f32le", "-c:a", "pcm_f32le", str(bruto)], "extração do áudio")
    quadro = canais * 4
    n_total = bruto.stat().st_size // quadro              # quadros extraídos (a partir de i0)
    b = (round(fim * SR) if fim is not None else i0 + n_total)
    b = min(b, i0 + n_total)
    if b <= a:
        raise ValueError("o trecho não tem áudio nessa faixa")
    saida = pasta / "saida.s16"
    antes, depois = round(_ANTES_S * SR), round(_DEPOIS_S * SR)
    # o deep-filter usa 1 núcleo: blocos em paralelo (até metade dos núcleos, no máx. 4, e só com memória
    # sobrando — cada processo usa ~150 MB + o bloco). Blocos de 30 s a 5 min, ~1 por processo.
    trabalhadores = _trabalhadores(canais)
    bloco = round(min(_BLOCO_S, max(30.0, (b - a) / SR / trabalhadores)) * SR)
    blocos = [(c0, min(b, c0 + bloco)) for c0 in range(a, b, bloco)]
    feitos = [0]
    trava = threading.Lock()

    def um(i_c):
        i, (c0, c1) = i_c
        e0, e1 = max(i0, c0 - antes), min(i0 + n_total, c1 + depois)
        d = pasta / f"b{i:05d}"
        (d / "out").mkdir(parents=True, exist_ok=True)
        wav_in = d / "bloco.wav"
        with open(bruto, "rb") as fin, open(wav_in, "wb") as w:
            fin.seek((e0 - i0) * quadro)
            w.write(_cabecalho_wav_f32((e1 - e0) + depois, canais))
            w.write(fin.read((e1 - e0) * quadro))
            w.write(b"\0" * (depois * quadro))  # silêncio no fim: o -D tira o atraso do começo e a saída encurta
        _rodar([exe, "-D", "-a", f"{atenuacao_db:g}", "-o", str(d / "out"), str(wav_in)], "deep-filter")
        wav_in.unlink()
        with wave.open(str(d / "out" / "bloco.wav"), "rb") as w:
            if w.getnchannels() != canais or w.getframerate() != SR or w.getsampwidth() != 2:
                raise RuntimeError(f"deep-filter devolveu formato inesperado: {w.getnchannels()} canais, "
                                   f"{w.getframerate()} Hz, {8 * w.getsampwidth()} bits")
            ini, n = c0 - e0, c1 - c0
            if w.getnframes() < ini + n:
                raise RuntimeError(f"deep-filter devolveu {w.getnframes()} amostras, esperava ≥ {ini + n}")
            w.setpos(ini)
            (d / "miolo.s16").write_bytes(w.readframes(n))
        (d / "out" / "bloco.wav").unlink()
        with trava:
            feitos[0] += n
            if progresso:
                progresso(feitos[0] / (b - a))
        return d / "miolo.s16"

    with ThreadPoolExecutor(max_workers=trabalhadores) as ex:
        partes = list(ex.map(um, enumerate(blocos)))
    with open(saida, "wb") as fout:
        for p in partes:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, fout, 1 << 22)
            p.unlink()
    bruto.unlink(missing_ok=True)
    return saida, b - a


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m ai_editor.deepfilter", description=__doc__.split("\n")[0])
    ap.add_argument("--instalar", action="store_true", help=f"baixa o deep-filter {VERSAO} (~36 MB)")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    if args.instalar:
        print(executavel(progresso=lambda f: print(f"\r{f:5.0%}", end="", flush=True)))
        return 0
    import json
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
