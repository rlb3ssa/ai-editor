"""Gera a lista de ferramentas MCP do README a partir do próprio servidor (nunca fica desatualizada).

Uso:
  uv run python packaging/gerar_ferramentas_readme.py             # reescreve o bloco no README.md
  uv run python packaging/gerar_ferramentas_readme.py --verificar # sai com erro se o README estiver desatualizado

O bloco fica entre os marcadores INICIO/FIM abaixo. O resumo de cada ferramenta é a 1ª frase da sua docstring
(o mesmo texto que a IA lê); a categoria vem de CATEGORIAS ou, para os módulos temáticos, do nome do módulo.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
README = RAIZ / "README.md"
INICIO = "<!-- ferramentas-mcp:inicio (gerado por packaging/gerar_ferramentas_readme.py; não edite à mão) -->"
FIM = "<!-- ferramentas-mcp:fim -->"

ORDEM = ["Janela e reprodução", "Enxergar e analisar", "Cortar e montar", "Imagem", "Efeitos de vídeo",
         "Áudio", "Efeitos de áudio", "Áudio com IA", "Legendas e fala", "Objetos com IA",
         "Arquivos, imagens e metadados", "Exportar", "Versões", "Outros"]

CATEGORIAS = {
    "Janela e reprodução": ["abrir", "estado", "controlar", "marcar", "mixar"],
    "Enxergar e analisar": ["ver_quadros", "ver_trecho", "metadados", "analisar_audio", "relatorio_tecnico",
                            "detectar_cenas", "diagnostico", "ver_espectro"],
    "Cortar e montar": ["cortar", "remover_trechos", "montar_trechos", "juntar_arquivos", "dividir", "capitulos",
                        "remover_silencios"],
    "Imagem": ["transformar_imagem", "mudar_proporcao", "ajustar_cor", "estabilizar", "velocidade_trecho",
               "congelar_quadro", "sobrepor_texto", "marca_dagua", "borrar_area"],
    "Áudio": ["ajustar_audio", "faixa_audio", "reduzir_ruido", "fade_audio", "musica_fundo"],
    "Áudio com IA": ["separar_audio"],
    "Legendas e fala": ["transcrever", "gerar_legenda", "gravar_legenda", "embutir_legenda", "buscar_fala"],
    "Objetos com IA": ["segmentar_objeto", "ajustar_mascara", "destacar_objeto", "remover_objeto", "fundo_objeto",
                       "recortar_objeto", "objetos_ia", "tarefas"],
    "Exportar": ["exportar"],
    "Versões": ["versoes", "desfazer", "descartar_versoes", "salvar"],
    "Outros": ["ffmpeg_livre"],
}
POR_MODULO = {"mcp_video": "Efeitos de vídeo", "mcp_audio": "Efeitos de áudio",
              "mcp_arquivos": "Arquivos, imagens e metadados"}


def _resumo(doc: str | None, limite: int = 220) -> str:
    """1ª frase da docstring, numa linha só, sem quebrar a tabela."""
    texto = " ".join((doc or "").split())
    m = re.search(r"(.+?[.!?])(\s|$)", texto)
    frase = m.group(1) if m else texto
    if len(frase) > limite:
        frase = frase[:limite].rsplit(" ", 1)[0] + "…"
    return frase.replace("|", "\\|")


def _ferramentas() -> list[tuple[str, str, str]]:
    sys.path.insert(0, str(RAIZ / "src"))
    from ai_editor import mcp_server

    lista = asyncio.run(mcp_server.mcp.list_tools())
    inverso = {n: c for c, nomes in CATEGORIAS.items() for n in nomes}
    res = []
    for t in lista:
        fn = getattr(mcp_server, t.name, None)
        modulo = ""
        for nome_mod in POR_MODULO:
            mod = sys.modules.get(f"ai_editor.{nome_mod}")
            if mod is not None and hasattr(mod, t.name):
                modulo = nome_mod
        cat = inverso.get(t.name) or POR_MODULO.get(modulo) or "Outros"
        res.append((cat, t.name, _resumo(t.description or (fn.__doc__ if fn else ""))))
    return res


def gerar() -> str:
    itens = _ferramentas()
    linhas = [INICIO, "", f"**{len(itens)} ferramentas.** O resumo é a primeira frase da descrição que a IA recebe "
              "(a descrição completa, com parâmetros e exemplos, vem do próprio servidor).", ""]
    for cat in ORDEM:
        grupo = [(n, r) for c, n, r in itens if c == cat]
        if not grupo:
            continue
        linhas += [f"#### {cat}", "", "| Ferramenta | O que faz |", "|---|---|"]
        linhas += [f"| `{n}` | {r} |" for n, r in grupo]
        linhas.append("")
    linhas.append(FIM)
    return "\n".join(linhas)


def main() -> int:
    texto = README.read_text(encoding="utf-8")
    if INICIO not in texto or FIM not in texto:
        print(f"marcadores não encontrados no {README.name}", file=sys.stderr)
        return 2
    novo = texto[:texto.index(INICIO)] + gerar() + texto[texto.index(FIM) + len(FIM):]
    if "--verificar" in sys.argv:
        if novo != texto:
            print("README desatualizado: rode packaging/gerar_ferramentas_readme.py", file=sys.stderr)
            return 1
        print("README em dia")
        return 0
    README.write_text(novo, encoding="utf-8")
    print(f"README atualizado ({novo.count('| `')} ferramentas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
