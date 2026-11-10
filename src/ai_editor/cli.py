"""Ponto de entrada único do AI Editor (comando `ai-editor` e executável do AppImage).

    ai-editor [arquivo]                abre a janela (ou encaminha o arquivo para a janela já aberta)
    ai-editor -- -arquivo.mp4          `--` encerra as opções (arquivos cujo nome começa com "-")
    ai-editor --mcp                    servidor MCP (stdio) — é o que o Claude executa
    ai-editor --diagnostico            ferramentas externas encontradas e o que falta
    ai-editor --instalar [--sem-mime] [--sem-claude]
                                       atalho, menu, tipos MIME e registro do MCP no Claude
    ai-editor --desinstalar            desfaz o --instalar
    ai-editor --versao                 mostra a versão

Despacha sem importar Qt antes da hora: --mcp, --diagnostico e --instalar não carregam a interface.
"""
from __future__ import annotations

import os
import sys

AJUDA = __doc__.split("\n\n", 1)[1].split("\n\nDespacha", 1)[0]


def versao() -> str:
    """Versão instalada (metadados do pacote; no AppImage, copiados pelo PyInstaller)."""
    try:
        from importlib.metadata import version
        return version("ai-editor")
    except Exception:  # noqa: BLE001
        return "desconhecida"


def _limpar_ambiente_empacotado() -> None:
    """Empacotado (PyInstaller), o carregador põe as bibliotecas do pacote no LD_LIBRARY_PATH. Este processo
    já foi carregado com elas; os programas que ele chama (ffmpeg, perl do exiftool, navegador...) devem ver
    o ambiente original do sistema."""
    if not getattr(sys, "frozen", False):
        return
    if "LD_LIBRARY_PATH_ORIG" in os.environ:
        os.environ["LD_LIBRARY_PATH"] = os.environ.pop("LD_LIBRARY_PATH_ORIG")
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


def main() -> None:
    _limpar_ambiente_empacotado()
    args = sys.argv[1:]
    # Compatibilidade: o servidor MCP abre a janela com `<python> -m ai_editor.app`; empacotado,
    # sys.executable é este mesmo executável, então essas duas palavras chegam aqui como argumentos.
    if args[:2] == ["-m", "ai_editor.app"]:
        args = args[2:]
        sys.argv = [sys.argv[0], *args]
        appimage = os.environ.get("APPIMAGE")
        if appimage and os.path.isfile(appimage) and getattr(sys, "frozen", False):
            # Dentro de um AppImage, este executável mora no ponto de montagem do processo do MCP, que some
            # quando o MCP termina. A janela precisa da própria montagem: reexecuta pelo arquivo .AppImage.
            os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            for var in ("APPDIR", "ARGV0", "OWD"):
                os.environ.pop(var, None)
            os.execv(appimage, [appimage, *args])

    # `--` separa opções de arquivos: o que vem depois é sempre arquivo, mesmo começando com "-".
    # A janela ignora argumentos que começam com "-", então esses viram caminho absoluto (/pasta/-video.mp4).
    if "--" in args:
        i = args.index("--")
        arquivos = [os.path.abspath(a) if a.startswith("-") else a for a in args[i + 1:]]
        args = args[:i]
        sys.argv = [sys.argv[0], *args, *arquivos]

    if "--mcp" in args:
        sys.argv = [sys.argv[0]]
        from .mcp_server import main as mcp_main
        mcp_main()
        return
    if "--versao" in args or "--version" in args:
        print(f"AI Editor {versao()}")
        return
    if "--ajuda" in args or "--help" in args or "-h" in args:
        print(AJUDA)
        return
    if "--diagnostico" in args:
        from .binarios import imprimir_diagnostico
        sys.exit(imprimir_diagnostico())
    if "--instalar" in args or "--desinstalar" in args:
        from . import instalador
        if "--desinstalar" in args:
            sys.exit(instalador.desinstalar())
        sys.exit(instalador.instalar(mime="--sem-mime" not in args, claude="--sem-claude" not in args))

    from .app import main as app_main
    app_main()


if __name__ == "__main__":
    main()
