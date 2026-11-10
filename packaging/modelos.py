"""Baixa (com cache) e copia para o AppDir os modelos de IA que vão DENTRO do AppImage (chamado pelo build_appimage.sh).

    python packaging/modelos.py <cache> <destino> whisper:small objetos:sam2.1-tiny objetos:lama separar:embutidos

- <cache>: vira o XDG_DATA_HOME deste processo, então os downloads usam o próprio código do app (URLs com revisão
  fixa, tamanho e SHA-256 de `fala.MODELOS` / `objetos.MODELOS` / `separar.MODELOS`) e ficam em
  <cache>/ai-editor/modelos/ — rebuilds não baixam de novo.
- <destino>: AppDir/usr/share/ai-editor/modelos. Mesma estrutura de ~/.local/share/ai-editor/modelos
  (whisper/ggml-*.bin, <modelo>/<arquivos>). Cada arquivo é conferido de novo (SHA-256 completo) ao copiar: o app
  confia nos arquivos embutidos (só confere o tamanho).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while bloco := f.read(1 << 22):
            h.update(bloco)
    return h.hexdigest()


def copiar(orig: Path, dest: Path, sha: str, tam: int) -> int:
    if orig.stat().st_size != tam or sha256(orig) != sha:
        raise SystemExit(f"modelo corrompido no cache: {orig} (apague e rode de novo)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(orig, dest)
    os.chmod(dest, 0o644)
    return tam


def main() -> int:
    cache, destino, specs = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3:]
    os.environ["XDG_DATA_HOME"] = str(cache)  # antes de importar o app (binarios.MODELOS_DIR)
    os.environ.pop("APPDIR", None)
    from ai_editor import fala, objetos
    if any(s.startswith("separar:") for s in specs):
        from ai_editor import separar  # noqa: F401 — registra os modelos de separação em objetos.MODELOS
    expandidos = []
    for spec in specs:  # separar:embutidos = os modelos marcados "embutir": True em separar.MODELOS
        if spec == "separar:embutidos":
            from ai_editor import separar
            expandidos += [f"separar:{n}" for n, m in separar.MODELOS.items() if m.get("embutir")]
        else:
            expandidos.append(spec)
    total = 0
    for spec in expandidos:
        tipo, nome = spec.split(":", 1)
        if tipo == "whisper":
            fala.baixar_modelo(nome)
            arq, tam, sha, _ = fala.MODELOS[nome]
            total += copiar(fala.caminho_modelo(nome), destino / "whisper" / arq, sha, tam)
            with open(destino / "whisper" / "ORIGEM.txt", "a") as f:
                f.write(f"{arq}  {sha}  {fala.HF_BASE}{arq}  (MIT)\n")
            print(f"    whisper/{arq}: {tam / 1e6:.0f} MB")
        elif tipo in ("objetos", "separar"):
            pasta = objetos.baixar_modelo(nome)
            m = objetos.MODELOS[nome]
            for arq, tam, sha in m["arquivos"]:
                total += copiar(pasta / Path(arq).name, destino / nome / Path(arq).name, sha, tam)
            if (pasta / "ORIGEM.txt").is_file():
                shutil.copyfile(pasta / "ORIGEM.txt", destino / nome / "ORIGEM.txt")
            print(f"    {nome}: {objetos.tamanho_modelo(nome) / 1e6:.0f} MB — {m['licenca']}")
        else:
            raise SystemExit(f"tipo de modelo desconhecido: {spec}")
    print(f"    total dos modelos embutidos: {total / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
