"""Gera o bundle `.mcpb` (Claude Desktop) do AI Editor: dist/ai-editor-<versão>.mcpb

    uv run python packaging/mcpb/empacotar_mcpb.py

O .mcpb é um zip com `manifest.json` (especificação MCPB 0.3, github.com/modelcontextprotocol/mcpb), o ícone e um
lançador (`server/ai-editor-mcp`) que executa o AppImage instalado com `--mcp`. A versão vem do pyproject.toml e a
lista de ferramentas é lida do próprio servidor MCP (nome + primeira linha da docstring).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import zipfile
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent.parent


def versao() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (RAIZ / "pyproject.toml").read_text(), re.M)
    return m.group(1)


def ferramentas() -> list[dict]:
    sys.path.insert(0, str(RAIZ / "src"))
    from ai_editor import mcp_server
    lista = asyncio.run(mcp_server.mcp.list_tools())
    return [{"name": t.name, "description": (t.description or "").strip().splitlines()[0]} for t in lista]


def main() -> int:
    manifesto = json.loads((AQUI / "manifest.json").read_text(encoding="utf-8"))
    manifesto["version"] = versao()
    manifesto["tools"] = ferramentas()
    dist = Path(os.environ.get("AI_EDITOR_DIST") or RAIZ / "dist")
    dist.mkdir(exist_ok=True)
    saida = dist / f"ai-editor-{manifesto['version']}.mcpb"

    def adicionar(z: zipfile.ZipFile, nome: str, dados: bytes, executavel: bool = False) -> None:
        info = zipfile.ZipInfo(nome, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (0o100755 if executavel else 0o100644) << 16
        z.writestr(info, dados)

    with zipfile.ZipFile(saida, "w") as z:
        adicionar(z, "manifest.json", (json.dumps(manifesto, indent=2, ensure_ascii=False) + "\n").encode())
        adicionar(z, "server/ai-editor-mcp", (AQUI / "server" / "ai-editor-mcp").read_bytes(), executavel=True)
        adicionar(z, "icon.png", (RAIZ / "src" / "ai_editor" / "recursos" / "ai-editor.png").read_bytes())
        adicionar(z, "LICENSE", (RAIZ / "LICENSE").read_bytes())
    print(f"{saida} ({saida.stat().st_size // 1024} KB, {len(manifesto['tools'])} ferramentas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
