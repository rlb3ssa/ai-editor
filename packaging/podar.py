"""Poda o bundle do PyInstaller dentro do AppDir (chamado pelo build_appimage.sh).

1. Remove plugins do Qt que o AI Editor não usa e que puxam bibliotecas grandes
   (teclado virtual → QtQuick/QML; PDF; tema GTK3 → GTK inteiro; eglfs/vnc/linuxfb).
2. Remove as bibliotecas de `_internal/` que nada mais referencia (fecho transitivo de DT_NEEDED a partir
   do executável, dos módulos Python, dos plugins do Qt que sobraram e da libmpv em usr/lib).

3. Remove de `_internal/` as bibliotecas de sistema que o PyInstaller recolheu mas que devem vir da máquina
   (libstdc++, libgcc_s, X11/xcb, Wayland, som, fontconfig...): mesma lista EXCLUIR do build_appimage.sh,
   recebida como segundo argumento (expressão regular).

Uso: python podar.py <AppDir> [regex-de-exclusao]
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PLUGINS_FORA = [
    "platforminputcontexts/libqtvirtualkeyboardplugin.so",
    "imageformats/libqpdf.so",
    "platformthemes/libqgtk3.so",
    "platforms/libqeglfs.so", "platforms/libqvnc.so", "platforms/libqlinuxfb.so", "platforms/libqvkkhrdisplay.so",
    "egldeviceintegrations",
    "virtualkeyboard",
    "qmltooling",
]


def needed(arq: Path) -> list[str]:
    r = subprocess.run(["readelf", "-d", str(arq)], capture_output=True, text=True)
    return [l.split("[", 1)[1].rstrip("]") for l in r.stdout.splitlines() if "(NEEDED)" in l]


def eh_elf(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def main() -> int:
    appdir = Path(sys.argv[1])
    interno = appdir / "usr" / "bin" / "_internal"
    qt = interno / "PySide6" / "Qt"
    removidos = 0
    for rel in PLUGINS_FORA:
        p = qt / "plugins" / rel
        if p.is_dir():
            subprocess.run(["rm", "-rf", str(p)], check=True)
            removidos += 1
        elif p.exists():
            p.unlink()
            removidos += 1
    for d in ("qml", "translations"):
        if (qt / d).is_dir():
            subprocess.run(["rm", "-rf", str(qt / d)], check=True)

    # Índice nome → arquivo das bibliotecas que podem ser podadas (topo de _internal e Qt/lib).
    # (O PyInstaller põe links simbólicos no topo de _internal apontando para Qt/lib: o nome do link conta.)
    podaveis: dict[str, Path] = {}
    for pasta in (interno, qt / "lib"):
        for p in pasta.glob("*.so*"):
            if p.name.startswith("lib") and p.is_file() and eh_elf(p):
                podaveis[p.name] = p.resolve()
    # Raízes: tudo que é ELF fora dessas duas pastas (executável, módulos Python, plugins Qt) + usr/lib.
    raizes = [appdir / "usr" / "bin" / "ai-editor"]
    for p in interno.rglob("*"):
        if p.is_file() and not p.is_symlink() and p.parent not in (interno, qt / "lib") and eh_elf(p):
            raizes.append(p)
    raizes += [p for p in (appdir / "usr" / "lib").glob("*.so*") if p.is_file()]
    raizes += [p for p in interno.glob("libpython*.so*") if p.is_file()]

    usados: set[Path] = set()
    fila = list(raizes)
    while fila:
        arq = fila.pop()
        for n in needed(arq):
            alvo = podaveis.get(n)
            if alvo and alvo not in usados:
                usados.add(alvo)
                fila.append(alvo)
    lixo = {p for n, p in podaveis.items() if p not in usados and not n.startswith("libpython")}
    if len(sys.argv) > 2:  # bibliotecas que vêm do sistema
        rx = re.compile(sys.argv[2])
        sistema = [p for p in interno.glob("*.so*") if rx.match(p.name) and p.parent == interno]
        for p in sistema:
            if p.is_symlink():
                p.unlink()
            else:
                lixo.add(p)
    lixo = sorted(lixo)
    total = 0
    for p in lixo:
        total += p.stat().st_size
        print(f"    - {p.relative_to(appdir)}")
        p.unlink()
    for p in interno.rglob("*"):  # links que ficaram sem destino
        if p.is_symlink() and not p.exists():
            p.unlink()
    print(f"    plugins removidos: {removidos}; bibliotecas sem uso removidas: {len(lixo)} "
          f"({total / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
