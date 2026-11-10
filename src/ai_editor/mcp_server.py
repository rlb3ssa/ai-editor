"""Servidor MCP do AI Editor: o Claude controla a janela e edita com ffmpeg."""
from __future__ import annotations

import asyncio
import functools
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import cliente, core
from .core import NOME, Documento, fmt_tempo, parse_tempo

mcp = MCPServer(
    NOME,
    log_level="WARNING",
    instructions=(
        "Controla a janela de vídeo do usuário (AI Editor) e edita com ffmpeg.\n"
        "- Tempos aceitam segundos (83.456), '1:23.456', '1500ms' ou palavras: 'atual'/'aqui', "
        "'in', 'out', 'inicio', 'fim' e expressões como 'atual-2' ou 'out+0.5'.\n"
        "- Faixas de áudio são numeradas a partir de 1 (A1, A2...), como na janela.\n"
        "- Toda edição cria uma versão temporária (v1, v2...) e a janela recarrega sozinha. "
        "O original só é substituído com `salvar`, que deve ser chamado APENAS quando o usuário pedir.\n"
        "- Use `estado` para saber onde o usuário está e o que marcou; `ver_quadros` para enxergar o vídeo."
    ),
)

APP_CMD = [sys.executable, "-m", "ai_editor.app"]


def ferramenta(fn):
    """Registra a ferramenta e transforma erros em mensagens legíveis para o Claude."""
    @functools.wraps(fn)
    async def envolvida(*a, **k):
        try:
            return await fn(*a, **k)
        except ToolError:
            raise
        except cliente.JanelaFechada as e:
            raise ToolError("o player não está aberto; use a ferramenta `abrir` com o caminho do vídeo") from e
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"{type(e).__name__}: {e}") from e
    return mcp.tool()(envolvida)


# ---------------------------------------------------------------- utilidades
def _garantir_janela(timeout: float = 15.0):
    if cliente.vivo():
        return
    log = core.RUNTIME_DIR / "janela.log"
    core.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(APP_CMD, stdout=open(log, "ab"), stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())
    fim = time.time() + timeout
    while time.time() < fim:
        if cliente.vivo():
            return
        time.sleep(0.2)
    raise RuntimeError(f"a janela não abriu; veja {log}")


def _estado() -> dict:
    return cliente.enviar("estado")


def _doc() -> tuple[Documento, dict]:
    est = _estado()
    if not est.get("documento"):
        raise RuntimeError("nenhum vídeo aberto no player; use `abrir` primeiro")
    return Documento(est["documento"]["original"]), est


def _t(valor, est):
    return parse_tempo(valor, est)


def _apos_edicao(versao: dict, doc: Documento) -> dict:
    msg = f"Claude: v{versao['n']} — {versao['descricao']}"
    try:
        cliente.enviar("recarregar", mensagem=msg)
    except Exception:  # noqa: BLE001
        pass
    probe = core.ffprobe(doc.atual())
    return {"versao": versao["n"], "descricao": versao["descricao"], "arquivo": str(doc.atual()),
            "nova_duracao": fmt_tempo(core.duracao(probe)), "comandos": versao["comandos"],
            "lembrete": "versão temporária; o original só muda com `salvar`"}


async def _bg(fn, *a, **k):
    return await asyncio.to_thread(fn, *a, **k)


# ---------------------------------------------------------------- janela / reprodução
@ferramenta
async def abrir(arquivo: str) -> dict:
    """Abre um vídeo (ou áudio) na janela do player, abrindo a janela se necessário.
    Se o arquivo tiver versões não salvas de uma sessão anterior, a última é carregada."""
    p = Path(arquivo).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"arquivo não encontrado: {p}")
    await _bg(_garantir_janela)
    cliente.enviar("abrir", arquivo=str(p))
    await asyncio.sleep(0.8)
    return _estado()


@ferramenta
async def estado() -> dict:
    """Estado atual do player: arquivo, posição (ms), duração, pausado, marcas IN/OUT,
    fps/quadro, faixas de áudio (mudo/solo/volume) e versões não salvas."""
    return _estado()


@ferramenta
async def controlar(acao: str, valor: str | None = None) -> dict:
    """Controla a reprodução.
    acao: 'tocar' | 'pausar' | 'alternar' | 'pular' (valor=tempo) | 'quadros' (valor=n, pode ser negativo)
          | 'velocidade' (valor=0.25..4) | 'zoom' (valor='inicio-fim', ex. '1:00-1:05'; mostra esse trecho na régua)
          | 'mensagem' (valor=texto na barra de status) | 'mostrar' (traz a janela para frente)."""
    est = _estado()
    if acao in ("tocar", "pausar", "alternar", "mostrar"):
        return cliente.enviar(acao)
    if acao == "pular":
        return cliente.enviar("pular", t=_t(valor, est))
    if acao == "quadros":
        return cliente.enviar("quadros", n=int(valor or 1))
    if acao == "velocidade":
        return cliente.enviar("velocidade", v=float(valor))
    if acao == "zoom":
        a, b = str(valor).split("-", 1) if "-" in str(valor)[1:] else (valor, None)
        return cliente.enviar("zoom", ini=_t(a, est), fim=_t(b, est))
    if acao == "mensagem":
        return cliente.enviar("mensagem", texto=valor or "")
    raise ValueError(f"ação desconhecida: {acao}")


@ferramenta
async def marcar(entrada: str | None = None, saida: str | None = None, limpar: bool = False) -> dict:
    """Define as marcas IN/OUT na régua (ex.: entrada='atual', saida='1:23.456'). limpar=True remove ambas."""
    est = _estado()
    return cliente.enviar("marcar",
                          entrada=_t(entrada, est) if entrada is not None else None,
                          saida=_t(saida, est) if saida is not None else None, limpar=limpar)


@ferramenta
async def mixar(faixa: int, mudo: bool | None = None, solo: bool | None = None,
                volume_pct: int | None = None) -> dict:
    """Ajusta como uma faixa de áudio TOCA na janela (não altera o arquivo). faixa começa em 1."""
    a = {k: v for k, v in {"mudo": mudo, "solo": solo, "volume": volume_pct}.items() if v is not None}
    return cliente.enviar("mix", faixa=faixa, **a)


@ferramenta
async def ver_quadros(tempos: list[str] | None = None, largura: int = 960) -> list:
    """Extrai e mostra quadros do vídeo para o Claude enxergar.
    tempos: lista de tempos (padrão: posição atual). Até 12 quadros; com vários, a largura cai para 480."""
    est = _estado()
    arq = Path(est["arquivo_tocando"])
    tempos = tempos or ["atual"]
    if len(tempos) > 12:
        raise ValueError("no máximo 12 quadros por vez")
    if len(tempos) > 2:
        largura = min(largura, 480)
    tmp = Path(tempfile.mkdtemp(prefix="quadros-", dir=core.RUNTIME_DIR))
    saida: list = []
    for i, tv in enumerate(tempos):
        t = _t(tv, est)
        dest = await _bg(core.extrair_quadro, arq, t, largura, tmp / f"q{i:02d}.jpg")
        saida.append(f"quadro em {fmt_tempo(t)}")
        saida.append(Image(path=dest))
    return saida


@ferramenta
async def ver_trecho(inicio: str, fim: str, quantidade: int = 8) -> list:
    """Mostra `quantidade` quadros espaçados igualmente entre inicio e fim (visão geral de um trecho)."""
    est = _estado()
    a, b = _t(inicio, est), _t(fim, est)
    q = max(2, min(quantidade, 12))
    tempos = [a + (b - a) * i / (q - 1) for i in range(q)]
    return await ver_quadros([str(t) for t in tempos])


# ---------------------------------------------------------------- análise
@ferramenta
async def metadados(arquivo: str | None = None, profundo: bool = False, hash_sha256: bool = False) -> dict:
    """TODOS os metadados do arquivo (padrão: o vídeo aberto), de várias fontes: sistema de arquivos
    (datas, atributos estendidos com URL de download), ffprobe (formato, faixas, capítulos, dados privados),
    mediainfo (completo), exiftool (todas as tags, inclusive desconhecidas, por grupo: QuickTime, XMP,
    GPS, fabricante...), C2PA/Content Credentials (procedência e IA) e pistas de origem.
    profundo=True extrai também dados embutidos (telemetria GPS de GoPro/DJI, etc.; pode ser grande).
    O relatório completo também é salvo em JSON; o caminho vem em 'relatorio_completo'."""
    if arquivo is None:
        arquivo = _estado()["documento"]["original"]
    rel = await _bg(core.metadados, arquivo, profundo, hash_sha256)
    caminho = core.salvar_relatorio(rel)
    texto = json.dumps(rel, ensure_ascii=False, default=str)
    if len(texto) <= 90_000:
        rel["relatorio_completo"] = str(caminho)
        return rel
    return {"relatorio_completo": str(caminho), "resumo": rel["resumo"],
            "pistas_de_origem": rel["pistas_de_origem"],
            "sistema_de_arquivos": rel["sistema_de_arquivos"], "ffprobe": rel["ffprobe"],
            "aviso": f"relatório muito grande ({len(texto) // 1000} kB); exiftool/mediainfo completos "
                     f"estão no arquivo acima — leia-o para os detalhes."}


@ferramenta
async def analisar_audio(faixa: int = 1, inicio: str | None = None, fim: str | None = None,
                         limiar_silencio_db: float = -40, silencio_min_s: float = 0.5) -> dict:
    """Mede o áudio de uma faixa (padrão: arquivo inteiro): volume médio/máximo (dB), loudness
    integrada (LUFS), faixa dinâmica, pico real e lista de silêncios."""
    est = _estado()
    a = _t(inicio, est) if inicio else None
    b = _t(fim, est) if fim else None
    return await _bg(core.analisar_audio, Path(est["arquivo_tocando"]), faixa - 1, a, b,
                     limiar_silencio_db, silencio_min_s)


# ---------------------------------------------------------------- edição
@ferramenta
async def cortar(inicio: str = "in", fim: str = "out", preciso: bool = True) -> dict:
    """Mantém SÓ o trecho [inicio, fim] (padrão: entre as marcas IN e OUT).
    preciso=True re-encoda (corte exato ao milissegundo/quadro); False é instantâneo e sem perda,
    mas o corte cai no keyframe mais próximo (pode incluir alguns quadros a mais)."""
    doc, est = _doc()
    a, b = _t(inicio, est), _t(fim, est)
    v = await _bg(core.manter_trechos, doc, [(a, b)], preciso,
                  f"manteve {fmt_tempo(a)} → {fmt_tempo(b)}" + ("" if preciso else " (rápido)"))
    cliente.enviar("marcar", limpar=True)
    return _apos_edicao(v, doc)


@ferramenta
async def remover_trechos(trechos: list[list[str]], preciso: bool = True) -> dict:
    """Remove um ou mais trechos e junta o resto. trechos: [[inicio, fim], ...],
    ex. [["in","out"]] ou [["0:10","0:12.5"],["1:00","1:03"]]."""
    doc, est = _doc()
    dur = core.duracao(core.ffprobe(doc.atual()))
    rem = [(_t(a, est), _t(b, est)) for a, b in trechos]
    manter = core.complemento(rem, dur)
    desc = "removeu " + ", ".join(f"{fmt_tempo(a)}→{fmt_tempo(b)}" for a, b in rem)
    v = await _bg(core.manter_trechos, doc, manter, preciso, desc)
    cliente.enviar("marcar", limpar=True)
    return _apos_edicao(v, doc)


@ferramenta
async def montar_trechos(trechos: list[list[str]], preciso: bool = True) -> dict:
    """Monta um novo vídeo com os trechos na ordem dada (pode reordenar/repetir).
    trechos: [[inicio, fim], ...]."""
    doc, est = _doc()
    tr = [(_t(a, est), _t(b, est)) for a, b in trechos]
    desc = "montou " + " + ".join(f"{fmt_tempo(a)}→{fmt_tempo(b)}" for a, b in tr)
    v = await _bg(core.manter_trechos, doc, tr, preciso, desc)
    return _apos_edicao(v, doc)


@ferramenta
async def ajustar_audio(faixas: list[int] | None = None, ganho_db: float | None = None,
                        normalizar: bool = False, reduzir_ruido: bool = False, silenciar: bool = False,
                        fade_in_s: float | None = None, fade_out_s: float | None = None,
                        inicio: str | None = None, fim: str | None = None) -> dict:
    """Altera o áudio no ARQUIVO (cria nova versão). faixas: [1], [2], [1,2]... (padrão: todas).
    ganho_db (+/-), silenciar e fades podem ser limitados a [inicio, fim];
    normalizar (EBU R128, -16 LUFS) e reduzir_ruido valem para a faixa inteira.
    Vídeo e faixas não citadas são copiados sem perda."""
    doc, est = _doc()
    probe = core.ffprobe(doc.atual())
    n = len(core.faixas_audio(probe))
    idx = [f - 1 for f in (faixas or range(1, n + 1))]
    a = _t(inicio, est) if inicio else None
    b = _t(fim, est) if fim else None
    cadeia = core.cadeia_audio(ganho_db, normalizar, reduzir_ruido, silenciar, fade_in_s, fade_out_s,
                               a, b, core.duracao(probe))
    partes = []
    if ganho_db:
        partes.append(f"{ganho_db:+g} dB")
    if normalizar:
        partes.append("normalizou")
    if reduzir_ruido:
        partes.append("reduziu ruído")
    if silenciar:
        partes.append("silenciou")
    if fade_in_s:
        partes.append(f"fade-in {fade_in_s}s")
    if fade_out_s:
        partes.append(f"fade-out {fade_out_s}s")
    janela = f" em {fmt_tempo(a or 0)}→{fmt_tempo(b) if b is not None else 'fim'}" if (a or b) else ""
    desc = f"áudio A{','.join(str(i + 1) for i in idx)}: {', '.join(partes)}{janela}"
    v = await _bg(core.aplicar_filtro_audio, doc, idx, cadeia, desc)
    return _apos_edicao(v, doc)


@ferramenta
async def faixa_audio(acao: str, faixa: int | None = None, arquivo: str | None = None,
                      idioma: str | None = None, titulo: str | None = None, atraso_s: float = 0.0,
                      formato: str = "wav", inicio: str | None = None, fim: str | None = None) -> dict:
    """Gerencia faixas de áudio.
    acao='remover' (faixa) — tira a faixa do vídeo (nova versão).
    acao='adicionar' (arquivo, idioma, titulo, atraso_s) — adiciona áudio externo como nova faixa (nova versão).
    acao='extrair' (faixa, formato wav/mp3/flac/m4a/opus, inicio/fim opcionais) — salva a faixa como
    arquivo separado ao lado do original (não cria versão)."""
    doc, est = _doc()
    if acao == "remover":
        v = await _bg(core.remover_faixa_audio, doc, int(faixa) - 1)
        return _apos_edicao(v, doc)
    if acao == "adicionar":
        v = await _bg(core.adicionar_faixa_audio, doc, Path(arquivo).expanduser(), idioma, titulo, atraso_s)
        return _apos_edicao(v, doc)
    if acao == "extrair":
        f = int(faixa or 1)
        ext = "." + formato.lstrip(".")
        dest = doc.original.with_name(f"{doc.original.stem}_A{f}{ext}")
        a = _t(inicio, est) if inicio else 0.0
        cmd = core.FFMPEG + ["-ss", f"{a:.3f}", "-i", str(doc.atual())]
        if fim:
            cmd += ["-t", f"{_t(fim, est) - a:.3f}"]
        cmd += ["-map", f"0:a:{f - 1}", "-vn"] + core.codec_audio(ext) + [str(dest)]
        await _bg(core.rodar, cmd)
        return {"arquivo": str(dest)}
    raise ValueError("acao deve ser 'remover', 'adicionar' ou 'extrair'")


@ferramenta
async def ffmpeg_livre(argumentos: str, descricao: str, extensao: str | None = None) -> dict:
    """Qualquer outra edição: roda ffmpeg com seus argumentos sobre a versão atual e cria nova versão.
    Use {entrada} e {saida} nos argumentos, ex.:
    '-i {entrada} -vf "crop=1080:1080,eq=brightness=0.05" -c:a copy {saida}'.
    extensao muda o container da versão (ex. '.mp4'). Não precisa de -y/-hide_banner."""
    doc, _ = _doc()
    v = await _bg(core.ffmpeg_livre, doc, argumentos, descricao, extensao)
    return _apos_edicao(v, doc)


# ---------------------------------------------------------------- versões
@ferramenta
async def versoes() -> dict:
    """Lista as versões temporárias (com descrição e comandos ffmpeg usados) do vídeo aberto."""
    doc, _ = _doc()
    return doc.resumo()


@ferramenta
async def desfazer() -> dict:
    """Desfaz a última edição (apaga a última versão temporária) e recarrega a janela."""
    doc, _ = _doc()
    v = doc.desfazer()
    if not v:
        return {"aviso": "nada para desfazer"}
    cliente.enviar("recarregar", mensagem=f"Claude: desfez v{v['n']} ({v['descricao']})")
    return {"desfeito": v["descricao"], **doc.resumo()}


@ferramenta
async def descartar_versoes() -> dict:
    """Apaga TODAS as versões temporárias e volta ao original. Só use se o usuário pedir."""
    doc, _ = _doc()
    n = doc.descartar()
    cliente.enviar("recarregar", mensagem=f"Claude: {n} versões descartadas")
    return {"descartadas": n}


@ferramenta
async def salvar() -> dict:
    """SALVA: a última versão passa a ser o arquivo original; o original vai para a Lixeira e as
    versões temporárias são apagadas. Chame SOMENTE quando o usuário pedir explicitamente para salvar."""
    doc, _ = _doc()
    novo = await _bg(doc.salvar)
    cliente.enviar("recarregar", arquivo=str(novo), mensagem=f"Claude: salvo em {novo}")
    return {"salvo_em": str(novo), "original_anterior": "movido para a Lixeira"}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
