"""Localização das ferramentas externas (ffmpeg, mediainfo, exiftool, libmpv...).

Ordem de busca de cada ferramenta:
  1. variável de ambiente AI_EDITOR_<NOME> (ex.: AI_EDITOR_FFMPEG=/opt/ffmpeg/bin/ffmpeg) — para testes;
  2. dentro do pacote (AppImage: $APPDIR/usr/bin e $APPDIR/usr/lib);
  3. ferramentas baixadas sob demanda (~/.local/share/ai-editor/bin);
  4. o sistema (PATH, ~/.local/bin, pastas de bibliotecas).

`ai-editor --diagnostico` mostra o que foi encontrado, de onde, a versão e os recursos que faltam.
Este módulo não importa Qt nem mpv (é usado também pelo servidor MCP).
"""
from __future__ import annotations

import ctypes.util
import functools
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DADOS_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ai-editor"
BIN_SOB_DEMANDA = DADOS_DIR / "bin"
MODELOS_DIR = DADOS_DIR / "modelos"


def _base_pacote() -> Path | None:
    """Raiz do pacote quando empacotado (AppImage/PyInstaller); None rodando do código-fonte."""
    if os.environ.get("APPDIR"):
        return Path(os.environ["APPDIR"]) / "usr"
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return None


class FerramentaAusente(RuntimeError):
    pass


@dataclass
class Ferramenta:
    nome: str                      # nome do executável (ou "libmpv")
    para_que: str
    licenca: str
    obrigatoria: bool = True       # False = recurso opcional (o app funciona sem)
    sob_demanda: bool = False      # baixada só quando alguém usa
    biblioteca: bool = False       # .so em vez de executável
    versao_cmd: list[str] = field(default_factory=list)
    versao_rx: str = r"(\d+(?:\.\d+)+)"
    dica: str = ""                 # como instalar no sistema, enquanto não está no pacote

    @property
    def env(self) -> str:
        return "AI_EDITOR_" + re.sub(r"\W", "_", self.nome).upper()


REGISTRO: dict[str, Ferramenta] = {f.nome: f for f in [
    Ferramenta("ffmpeg", "todas as edições, análise e exportação", "GPL",
               versao_cmd=["-hide_banner", "-version"], versao_rx=r"ffmpeg version n?([\w.\-]+)",
               dica="sudo apt install ffmpeg"),
    Ferramenta("ffprobe", "ler formato, faixas e capítulos", "GPL",
               versao_cmd=["-hide_banner", "-version"], versao_rx=r"ffprobe version n?([\w.\-]+)",
               dica="sudo apt install ffmpeg"),
    Ferramenta("libmpv", "reprodução com aceleração por hardware", "GPL/LGPL", biblioteca=True,
               dica="sudo apt install libmpv2"),
    Ferramenta("mediainfo", "metadados detalhados", "BSD-2-Clause",
               versao_cmd=["--Version"], versao_rx=r"v(\d+(?:\.\d+)+)",
               dica="sudo apt install mediainfo"),
    Ferramenta("exiftool", "metadados de câmera, GPS, XMP", "Artistic/GPL",
               versao_cmd=["-ver"], dica="sudo apt install libimage-exiftool-perl"),
    Ferramenta("c2patool", "Content Credentials (procedência, conteúdo gerado por IA)", "MIT/Apache-2.0",
               obrigatoria=False, versao_cmd=["--version"],
               dica="baixe em github.com/contentauth/c2pa-rs/releases e coloque em ~/.local/bin"),
    Ferramenta("whisper-cli", "legendas e transcrição (whisper.cpp)", "MIT",
               obrigatoria=False, sob_demanda=True, versao_cmd=["--version"],
               versao_rx=r"version:\s*([\w.\-]+)",
               dica="compile o whisper.cpp fixado (1–2 min, sem sudo): python -m ai_editor.fala --instalar-whisper"),
]}

# Recursos do ffmpeg usados (ou planejados) pelo app: (tipo, nome, obrigatório?, para quê)
RECURSOS_FFMPEG = [
    ("encoders", "libx264", True, "exportar MP4/H.264"),
    ("encoders", "libvpx-vp9", True, "exportar WebM/VP9 (inclui transparência)"),
    ("encoders", "libopus", True, "áudio de WebM"),
    ("encoders", "aac", True, "áudio de MP4"),
    ("encoders", "libmp3lame", False, "exportar MP3"),
    ("encoders", "libx265", False, "exportar H.265"),
    ("encoders", "libsvtav1", False, "exportar AV1"),
    ("encoders", "prores_ks", False, "exportar ProRes 4444 com transparência"),
    ("filters", "subtitles", False, "gravar legendas no vídeo (libass)"),
    ("filters", "rubberband", False, "mudar velocidade sem alterar o tom"),
    ("filters", "arnndn", False, "redução de ruído (RNNoise)"),
    ("filters", "whisper", False, "transcrição direto no ffmpeg (ffmpeg 8+)"),
    ("filters", "drawtext", False, "sobrepor texto (sobrepor_texto; precisa de libfreetype)"),
    ("filters", "vidstabdetect", False, "estabilizar em 2 passes (vid.stab; sem ele usa deshake)"),
    ("filters", "vidstabtransform", False, "estabilizar em 2 passes (vid.stab)"),
    ("filters", "deshake", False, "estabilizar (alternativa simples ao vid.stab)"),
    ("filters", "colortemperature", False, "temperatura de cor (ajustar_cor; sem ele usa colorbalance)"),
    ("filters", "xfade", False, "juntar_arquivos com transicao='crossfade' (ffmpeg 4.3+)"),
    ("filters", "acrossfade", False, "juntar_arquivos com transicao='crossfade'"),
    ("filters", "idet", False, "relatorio_tecnico: detectar entrelaçamento"),
    ("filters", "blockdetect", False, "relatorio_tecnico: blocagem estimada (ffmpeg 5.1+)"),
    ("filters", "ebur128", False, "loudness (LUFS) e pico verdadeiro"),
]


def _candidatos(f: Ferramenta) -> list[tuple[str, Path]]:
    c: list[tuple[str, Path]] = []
    if os.environ.get(f.env):
        c.append(("variável " + f.env, Path(os.environ[f.env])))
    base = _base_pacote()
    if f.biblioteca:
        if base:
            c += [("pacote", p) for p in sorted((base / "lib").glob("libmpv.so*"))]
        return c
    if base:
        c.append(("pacote", base / "bin" / f.nome))
    c.append(("sob demanda", BIN_SOB_DEMANDA / f.nome))
    if shutil.which(f.nome):
        c.append(("sistema", Path(shutil.which(f.nome))))
    c.append(("sistema", Path.home() / ".local" / "bin" / f.nome))
    return c


@functools.cache
def localizar_com_origem(nome: str) -> tuple[str, Path] | None:
    f = REGISTRO[nome]
    for origem, p in _candidatos(f):
        if p.is_file() and (f.biblioteca or os.access(p, os.X_OK)):
            return origem, p
    if f.biblioteca:
        so = ctypes.util.find_library("mpv")
        if so:
            return "sistema", Path(so)
    return None


def localizar(nome: str) -> str | None:
    r = localizar_com_origem(nome)
    return str(r[1]) if r else None


def caminho(nome: str) -> str:
    """Caminho da ferramenta ou FerramentaAusente com a dica de instalação."""
    p = localizar(nome)
    if p:
        return p
    f = REGISTRO[nome]
    raise FerramentaAusente(f"{nome} não encontrado ({f.para_que}). Instale: {f.dica}" if f.dica
                            else f"{nome} não encontrado ({f.para_que}).")


def exe(nome: str) -> str:
    """Para montar comandos: caminho encontrado ou o próprio nome (o erro aparece ao executar)."""
    return localizar(nome) or nome


def preparar_libmpv() -> None:
    """Faz o python-mpv carregar a libmpv localizada aqui (do pacote, se houver). Chamar antes de `import mpv`."""
    r = localizar_com_origem("libmpv")
    if not r or r[0] == "sistema":
        return
    original = ctypes.util.find_library

    def achar(nome):
        return str(r[1]) if nome == "mpv" else original(nome)

    ctypes.util.find_library = achar


# ---------------------------------------------------------------- diagnóstico
def versao(nome: str) -> str | None:
    f = REGISTRO[nome]
    p = localizar(nome)
    if not p:
        return None
    if f.biblioteca:
        m = re.search(r"\.so\.(\d+)", p)
        return f"so.{m.group(1)}" if m else "?"
    try:
        r = subprocess.run([p, *f.versao_cmd], capture_output=True, text=True, timeout=10)
        m = re.search(f.versao_rx, r.stdout + r.stderr)
        return m.group(1) if m else "?"
    except Exception:  # noqa: BLE001
        return "?"


@functools.cache
def _lista_ffmpeg(tipo: str) -> set[str]:
    p = localizar("ffmpeg")
    if not p:
        return set()
    r = subprocess.run([p, "-hide_banner", f"-{tipo}"], capture_output=True, text=True, timeout=15)
    nomes = set()
    for linha in r.stdout.splitlines():
        partes = linha.split()
        if len(partes) >= 2 and re.fullmatch(r"[A-Z.|]{2,}", partes[0].replace("-", ".")):
            nomes.add(partes[1])
    return nomes


def recursos_ffmpeg() -> list[dict]:
    return [{"recurso": n, "tipo": t, "obrigatorio": o, "para_que": q, "ok": n in _lista_ffmpeg(t)}
            for t, n, o, q in RECURSOS_FFMPEG]


def diagnostico() -> dict:
    itens = []
    for nome, f in REGISTRO.items():
        r = localizar_com_origem(nome)
        itens.append({
            "ferramenta": nome, "para_que": f.para_que, "licenca": f.licenca,
            "obrigatoria": f.obrigatoria, "sob_demanda": f.sob_demanda,
            "encontrada": bool(r), "origem": r[0] if r else None, "caminho": str(r[1]) if r else None,
            "versao": versao(nome) if r else None, "dica": None if r else f.dica,
        })
    rec = recursos_ffmpeg()
    faltando = [i["ferramenta"] for i in itens if i["obrigatoria"] and not i["encontrada"]]
    faltando += [f"ffmpeg:{r['recurso']}" for r in rec if r["obrigatorio"] and not r["ok"]]
    return {"pacote": str(_base_pacote()) if _base_pacote() else None, "ferramentas": itens,
            "recursos_ffmpeg": rec, "ok": not faltando, "faltando": faltando}


def imprimir_diagnostico() -> int:
    d = diagnostico()
    print(f"AI Editor — diagnóstico ({'pacote: ' + d['pacote'] if d['pacote'] else 'rodando do código-fonte'})\n")
    for i in d["ferramentas"]:
        if i["encontrada"]:
            s = f"  ✔ {i['ferramenta']:<12} {i['versao'] or '':<14} [{i['origem']}] {i['caminho']}"
        else:
            tipo = "obrigatória" if i["obrigatoria"] else ("sob demanda" if i["sob_demanda"] else "opcional")
            s = f"  {'✘' if i['obrigatoria'] else '·'} {i['ferramenta']:<12} ausente ({tipo})"
            if i["dica"]:
                s += f" — {i['dica']}"
        print(s)
    print("\n  recursos do ffmpeg:")
    for r in d["recursos_ffmpeg"]:
        marca = "✔" if r["ok"] else ("✘" if r["obrigatorio"] else "·")
        print(f"    {marca} {r['recurso']:<12} {r['para_que']}")
    print("\n" + ("Tudo pronto." if d["ok"] else "Faltando: " + ", ".join(d["faltando"])))
    return 0 if d["ok"] else 1
