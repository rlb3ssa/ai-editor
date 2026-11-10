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
        "'inicio', 'fim', marcas 'm1', 'm2'... e expressões como 'atual-2' ou 'm3+0.5'.\n"
        "- O usuário marca N pontos na régua (botão de seta ou tecla I). As marcas são numeradas por ordem de "
        "tempo (m1, m2...) e cada par consecutivo forma um trecho colorido (m1→m2 amarelo, m2→m3 ciano...). "
        "Quando ele falar de 'marcas', 'pontos' ou de um trecho por cor, consulte `estado` (campos `marcas` e "
        "`trechos_marcados`). 'in' = primeira marca, 'out' = última.\n"
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
    r = {"versao": versao["n"], "descricao": versao["descricao"], "arquivo": str(doc.atual()),
         "nova_duracao": fmt_tempo(core.duracao(probe))}
    vids = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")]
    if vids:
        r["resolucao"] = "{}x{}".format(*core._dimensoes(vids[0]))
    for k in ("avisos", "trecho_novo", "detalhes"):
        if versao.get(k):
            r[k] = versao[k]
    r["comandos"] = versao["comandos"]
    r["lembrete"] = "versão temporária; o original só muda com `salvar`"
    return r


def _progresso(rotulo: str):
    """Callback de progresso (fração 0..1) que mostra '<rotulo>… N%' na barra de status da janela."""
    ultimo = [0.0, -1]

    def cb(f: float):
        pct = int(f * 100)
        agora = time.time()
        if pct != ultimo[1] and (agora - ultimo[0] >= 1.0 or pct >= 100):
            ultimo[:] = [agora, pct]
            try:
                cliente.enviar("mensagem", texto=f"Claude: {rotulo}… {pct}%", timeout=2)
            except Exception:  # noqa: BLE001
                pass
    return cb


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
    """Estado atual do player: arquivo, posição (ms), duração, pausado, marcas (m1, m2...) e trechos coloridos,
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
async def marcar(adicionar: list[str] | None = None, remover: list[int] | None = None,
                 limpar: bool = False) -> dict:
    """Marca pontos na régua, como o usuário faz com o botão de seta.
    adicionar: tempos (ex.: ['atual', '1:23.456']); remover: números das marcas (1 = a mais cedo);
    limpar=True remove todas antes. Devolve as marcas e os trechos coloridos entre elas."""
    est = _estado()
    return cliente.enviar("marcar", adicionar=[_t(x, est) for x in adicionar or []],
                          remover=remover or [], limpar=limpar)


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


@ferramenta
async def diagnostico() -> dict:
    """Ferramentas externas encontradas (ffmpeg, mediainfo, exiftool, libmpv, c2patool...), de onde vieram
    (pacote, sob demanda ou sistema), versões e recursos do ffmpeg disponíveis. Use quando algo falhar."""
    from . import binarios
    return binarios.diagnostico()


# ---------------------------------------------------------------- edição
@ferramenta
async def cortar(inicio: str = "in", fim: str = "out", preciso: bool = True) -> dict:
    """Mantém SÓ o trecho [inicio, fim] (padrão: da primeira à última marca; ex.: inicio='m2', fim='m3').
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
    ex. [["m1","m2"],["m3","m4"]] ou [["0:10","0:12.5"],["1:00","1:03"]]."""
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


# ---------------------------------------------------------------- imagem
@ferramenta
async def transformar_imagem(recorte: list[float] | None = None, proporcao: str | None = None,
                             posicao: str | None = None, girar: float | None = None, modo_giro: str = "expandir",
                             cor: str = "black", espelhar: str | None = None, largura: int | None = None,
                             altura: int | None = None, escala: float | None = None) -> dict:
    """Recorta, gira, espelha e/ou redimensiona o vídeo (nova versão). Pode combinar tudo numa chamada;
    a ordem aplicada é: recorte → giro → espelho → tamanho. Coordenadas = vídeo como aparece na tela.
    recorte=[x, y, largura, altura] em pixels, ou frações 0–1 (ex. [0.25, 0.25, 0.5, 0.5] = meio central)
      OU proporcao='9:16' | '1:1' | '4:5' | '16:9' (maior área possível) com posicao='centro' | 'esquerda' |
      'direita' | 'topo' | 'base' (ou número 0–1). Para mudar a proporção SEM cortar, use `mudar_proporcao`.
    girar: graus no sentido horário (90, 180, 270 sem perda; -90 = anti-horário). Ângulo livre (ex. 3 para
      endireitar o horizonte): modo_giro='expandir' (tela aumenta, cantos com `cor`), 'manter' (mesmo tamanho,
      cantos com `cor`) ou 'recorte' (amplia para não sobrar canto).
    espelhar: 'horizontal' | 'vertical' | 'ambos'.
    largura/altura: novo tamanho mantendo a proporção (só uma = a outra é calculada; as duas = cabe dentro);
      escala=0.5 reduz pela metade. Dimensões sempre pares.
    Exemplos: "corta só o meio" → recorte=[0.25,0.25,0.5,0.5]; "deixa quadrado pegando a esquerda" →
    proporcao='1:1', posicao='esquerda'; "gira para a direita" → girar=90; "está de cabeça para baixo" →
    girar=180; "endireita, está torto uns 2 graus para a esquerda" → girar=2, modo_giro='recorte';
    "espelha" → espelhar='horizontal'; "reduz para 720p" → altura=720."""
    doc, _ = _doc()
    v = await _bg(core.transformar_imagem, doc, recorte=recorte, proporcao=proporcao, posicao=posicao,
                  girar=girar, modo_giro=modo_giro, cor=cor, espelhar=espelhar, largura=largura, altura=altura,
                  escala=escala, progresso=_progresso("transformando"))
    return _apos_edicao(v, doc)


@ferramenta
async def mudar_proporcao(proporcao: str, modo: str = "desfocado", cor: str = "black",
                          largura: int | None = None, altura: int | None = None,
                          posicao: str | None = None) -> dict:
    """Muda a proporção da tela (nova versão): '9:16' (vertical/Reels/TikTok), '16:9', '1:1', '4:5', '4:3'...
    modo='desfocado' (padrão): o vídeo inteiro no meio, com o próprio vídeo ampliado e desfocado no fundo;
    'barras': barras de `cor` (ex. 'black', 'white', '#202020'); 'recorte': corta as sobras (sem barras;
    posicao='centro' | 'esquerda' | 'direita' | 'topo' | 'base' escolhe o que fica).
    Tamanho: largura/altura se passados (ex. largura=1080 para 1080x1920); senão o lado maior do vídeo
    continua o mesmo (1280x720 → 720x1280).
    Exemplos: "deixa vertical para o Reels com fundo desfocado" → proporcao='9:16';
    "quadrado com barras brancas" → proporcao='1:1', modo='barras', cor='white';
    "vertical cortando, pega o lado direito" → proporcao='9:16', modo='recorte', posicao='direita'.
    (Para só EXPORTAR para o Instagram sem editar o vídeo, use `exportar` com predefinicao='instagram'.)"""
    doc, _ = _doc()
    v = await _bg(core.mudar_proporcao, doc, proporcao, modo=modo, cor=cor, largura=largura, altura=altura,
                  posicao=posicao, progresso=_progresso("mudando a proporção"))
    return _apos_edicao(v, doc)


@ferramenta
async def ajustar_cor(brilho: float | None = None, contraste: float | None = None,
                      saturacao: float | None = None, gama: float | None = None,
                      temperatura: float | None = None, inicio: str | None = None,
                      fim: str | None = None) -> dict:
    """Ajusta a cor da imagem (nova versão), no vídeo todo ou só em [inicio, fim].
    brilho: -1 a 1 (0 = igual; 0.05–0.15 já se nota); contraste: multiplicador (1 = igual, 1.2 = +20%,
    0.8 = menos); saturacao: multiplicador (1 = igual, 0 = preto e branco, 1.3 = cores mais vivas);
    gama: 1 = igual (1.2 clareia os tons médios sem estourar o branco; 0.8 escurece);
    temperatura: kelvin (6500 = neutro; 4500–5500 = mais quente/alaranjado; 8000+ = mais frio/azulado).
    Exemplos: "está escuro" → gama=1.3 (ou brilho=0.08); "deixa preto e branco do m1 ao m2" → saturacao=0,
    inicio='m1', fim='m2'; "cores mais vivas e um pouco mais de contraste" → saturacao=1.3, contraste=1.1;
    "esquenta a imagem" → temperatura=5000."""
    doc, est = _doc()
    v = await _bg(core.ajustar_cor, doc, brilho=brilho, contraste=contraste, saturacao=saturacao, gama=gama,
                  temperatura=temperatura, inicio=inicio, fim=fim, estado=est,
                  progresso=_progresso("ajustando a cor"))
    return _apos_edicao(v, doc)


@ferramenta
async def estabilizar(suavizacao: int = 15, bordas: str = "zoom", tremor: int = 5,
                      metodo: str | None = None) -> dict:
    """Estabiliza a câmera tremida (nova versão). Usa vid.stab em 2 passes (analisa e corrige; o melhor) ou,
    se o ffmpeg não tiver, `deshake` (metodo='deshake' força). Demora: cerca de 2–3× a duração do vídeo.
    suavizacao: quadros usados para suavizar (15 ≈ 0,5 s a 30 fps; 30–60 = câmera bem mais "parada");
    tremor: 1–10, o quanto treme (padrão 5; 8–10 para muito tremido);
    bordas: 'zoom' (amplia o mínimo para esconder as bordas que aparecem), 'preto' ou 'manter'.
    Exemplos: "estabiliza" → padrão; "ainda está tremendo, estabiliza mais" → suavizacao=40, tremor=8."""
    doc, _ = _doc()
    v = await _bg(core.estabilizar, doc, suavizacao=suavizacao, bordas=bordas, tremor=tremor, metodo=metodo,
                  progresso=_progresso("estabilizando"))
    return _apos_edicao(v, doc)


@ferramenta
async def velocidade_trecho(fator: float, inicio: str | None = None, fim: str | None = None,
                            manter_tom: bool = True, silenciar: bool = False) -> dict:
    """Câmera lenta ou acelerado num trecho (nova versão; padrão: o vídeo todo). O resto não muda.
    fator: 0.5 = metade da velocidade (câmera lenta), 0.25 = 4× mais lento, 2 = dobro, 4 = 4× (0.1 a 10).
    O áudio acompanha mantendo o tom (manter_tom=False muda o tom junto, efeito "fita");
    silenciar=True deixa o trecho alterado sem som. Câmera lenta repete quadros (sem interpolação).
    As marcas são limpas (os tempos depois do trecho mudam); a resposta traz `trecho_novo`.
    Exemplos: "câmera lenta no trecho amarelo" → fator=0.5, inicio='m1', fim='m2';
    "acelera 4x do 1:00 ao 2:00 sem som" → fator=4, inicio='1:00', fim='2:00', silenciar=True."""
    doc, est = _doc()
    v = await _bg(core.velocidade_trecho, doc, fator, inicio=inicio, fim=fim, manter_tom=manter_tom,
                  silenciar=silenciar, estado=est, progresso=_progresso("mudando a velocidade"))
    cliente.enviar("marcar", limpar=True)
    return _apos_edicao(v, doc)


@ferramenta
async def congelar_quadro(tempo: str = "atual", duracao_s: float = 2.0, modo: str = "inserir") -> dict:
    """Congela a imagem num quadro por `duracao_s` segundos (nova versão).
    tempo: o quadro a congelar (padrão: posição atual; aceita 'm1', '0:12.5'...).
    modo='inserir' (padrão): o vídeo para nesse quadro e depois continua de onde parou (fica mais longo);
      o áudio fica em silêncio durante a pausa.
    modo='substituir': o quadro parado cobre os próximos `duracao_s` segundos e o áudio continua normal
      (a duração não muda).
    Exemplos: "congela aqui por 3 segundos" → duracao_s=3; "congela a imagem no m1 mas deixa o som
    rolando por 2 s" → tempo='m1', duracao_s=2, modo='substituir'."""
    doc, est = _doc()
    v = await _bg(core.congelar_quadro, doc, tempo, duracao_s, modo=modo, estado=est,
                  progresso=_progresso("congelando o quadro"))
    if modo == "inserir":
        cliente.enviar("marcar", limpar=True)
    return _apos_edicao(v, doc)


@ferramenta
async def sobrepor_texto(texto: str, posicao: str | list[float] = "base", tamanho: float | None = None,
                         cor: str = "white", caixa: bool = False, cor_caixa: str = "black@0.55",
                         contorno: int | None = None, cor_contorno: str = "black", fonte: str | None = None,
                         inicio: str | None = None, fim: str | None = None) -> dict:
    """Escreve um texto por cima do vídeo (nova versão), no vídeo todo ou só em [inicio, fim].
    Qualquer caractere pode ser usado (aspas, dois-pontos, %, barras...); quebre linhas com \n.
    posicao: 'base' (padrão), 'topo', 'centro', 'esquerda', 'direita', 'sup_esq', 'sup_dir', 'inf_esq',
      'inf_dir', ou [x, y] (canto superior esquerdo do texto, em pixels ou frações 0–1).
    tamanho: altura da letra em pixels (ex. 48) ou fração da altura do vídeo (ex. 0.08); padrão ≈ 1/15 da
      altura, reduzido se o texto não couber na largura.
    cor / cor_caixa / cor_contorno: nome ('white', 'yellow'...) ou '#RRGGBB', com '@opacidade' (ex. 'black@0.5').
    caixa=True: faixa de fundo atrás do texto (legível sobre qualquer imagem); contorno: espessura em px
    (padrão: fino sem caixa, nenhum com caixa). fonte: nome (ex. 'Ubuntu', 'DejaVu Serif') ou arquivo .ttf.
    Exemplos: "escreve 'Parte 2' no topo nos primeiros 3 segundos" → texto='Parte 2', posicao='topo',
    inicio='0', fim='3'; "legenda 'Olá!' embaixo com fundo preto do m1 ao m2" → texto='Olá!', caixa=True,
    inicio='m1', fim='m2'."""
    doc, est = _doc()
    v = await _bg(core.sobrepor_texto, doc, texto, posicao=posicao, tamanho=tamanho, cor=cor, caixa=caixa,
                  cor_caixa=cor_caixa, contorno=contorno, cor_contorno=cor_contorno, fonte=fonte,
                  inicio=inicio, fim=fim, estado=est, progresso=_progresso("escrevendo o texto"))
    return _apos_edicao(v, doc)


@ferramenta
async def marca_dagua(imagem: str, posicao: str | list[float] = "inf_dir", escala: float = 0.15,
                      opacidade: float = 1.0, margem: float = 0.03, inicio: str | None = None,
                      fim: str | None = None) -> dict:
    """Coloca uma imagem (logo PNG com transparência, JPG...) por cima do vídeo (nova versão).
    posicao: 'inf_dir' (padrão), 'inf_esq', 'sup_dir', 'sup_esq', 'centro', 'topo', 'base'... ou [x, y].
    escala: largura do logo como fração da largura do vídeo (0.15 = 15%) ou em pixels (ex. 200).
    opacidade: 0–1 (0.5 = meio transparente); margem: distância da borda (fração da altura do vídeo).
    inicio/fim: só nesse intervalo (padrão: o vídeo todo).
    Exemplo: "põe meu logo ~/logo.png no canto superior direito, meio transparente" →
    imagem='~/logo.png', posicao='sup_dir', opacidade=0.5."""
    doc, est = _doc()
    v = await _bg(core.marca_dagua, doc, imagem, posicao=posicao, escala=escala, opacidade=opacidade,
                  margem=margem, inicio=inicio, fim=fim, estado=est, progresso=_progresso("aplicando a marca"))
    return _apos_edicao(v, doc)


@ferramenta
async def borrar_area(retangulos: list[float] | list[list[float]] | None = None,
                      trajetoria: list[dict] | None = None, modo: str = "borrar",
                      intensidade: float | None = None, inicio: str | None = None,
                      fim: str | None = None) -> dict:
    """Borra ou pixela uma área (placa de carro, rosto, tela) em [inicio, fim] (nova versão; padrão: o vídeo todo).
    Antes, use `ver_quadros` para achar a área. Coordenadas = vídeo como aparece, em pixels ou frações 0–1.
    retangulos: [x, y, largura, altura] (área parada) ou vários: [[x,y,l,a], [x,y,l,a]].
    trajetoria: área que se MOVE: [{"tempo": "0:02", "retangulo": [x,y,l,a]}, {"tempo": "0:04", "retangulo": [...]}];
      a posição é interpolada em linha reta entre os pontos (use mais pontos se o movimento for curvo); o tamanho
      é o maior dos pontos; sem inicio/fim, vale do primeiro ao último ponto.
    modo: 'borrar' (desfoque) ou 'pixelar' (blocos). intensidade: força do desfoque / tamanho do bloco em px
    (padrão proporcional ao tamanho da área).
    Exemplos: "borra a placa no canto inferior esquerdo do 0:05 ao 0:09" → retangulos=[0.05,0.75,0.2,0.1],
    inicio='0:05', fim='0:09'; "pixela o rosto que anda da esquerda para a direita" → trajetoria com 2+ pontos."""
    doc, est = _doc()
    v = await _bg(core.borrar_area, doc, retangulos=retangulos, trajetoria=trajetoria, modo=modo,
                  intensidade=intensidade, inicio=inicio, fim=fim, estado=est,
                  progresso=_progresso("borrando"))
    return _apos_edicao(v, doc)


# ---------------------------------------------------------------- áudio (Fase 4)
@ferramenta
async def reduzir_ruido(metodo: str = "afftdn", intensidade: str | float = "media", faixa: int = 1,
                        inicio: str | None = None, fim: str | None = None,
                        piso_ruido_db: float | None = None) -> dict:
    """Reduz ruído de fundo constante (chiado, ventilador, ar-condicionado, hum largo) de uma faixa de
    áudio (nova versão; vídeo e outras faixas copiados sem perda).
    metodo='afftdn' (padrão, rápido): mede o ruído sozinho — usa a pausa mais quieta da faixa como amostra
      do perfil de ruído e ajusta o piso a partir do nível medido (sem pausas, estima pelo nível mais baixo).
    metodo='anlmdn': alternativa mais suave (médias não locais), ~10× mais lenta, reduz menos.
    metodo='rnnoise': rede neural para voz (arnndn); só funciona se houver um modelo .rnnn em
      ~/.local/share/ai-editor/modelos/rnnoise/ (senão dá erro explicando).
    intensidade: 'leve' (10 dB), 'media' (20 dB, padrão), 'forte' (32 dB) ou dB de redução (3–60).
    faixa: 1 = A1. inicio/fim: só nesse trecho. piso_ruido_db: força o nível do ruído (dBFS) se a medição errar.
    A resposta traz `detalhes` (ruído medido, de onde veio a amostra, parâmetros).
    Exemplos: "tira o chiado" → padrão; "ainda tem ruído" → intensidade='forte';
    "limpa o ruído só do m1 ao m2" → inicio='m1', fim='m2'."""
    doc, est = _doc()
    v = await _bg(core.reduzir_ruido, doc, metodo=metodo, intensidade=intensidade, faixa=faixa, inicio=inicio,
                  fim=fim, piso_ruido_db=piso_ruido_db, estado=est, progresso=_progresso("reduzindo ruído"))
    return _apos_edicao(v, doc)


@ferramenta
async def remover_silencios(modo: str = "cortar", limiar_db: float | None = None, duracao_min_s: float = 0.6,
                            margem_s: float = 0.15, faixa: int = 1, inicio: str | None = None,
                            fim: str | None = None) -> dict:
    """Corta as pausas/silêncios (vídeo e todas as faixas de áudio juntos, corte preciso; nova versão).
    A detecção usa a faixa `faixa` (1 = A1; normalmente a da voz).
    modo='analisar': só lista os silêncios e o que seria removido (não edita) — bom para mostrar ao usuário
      antes; use `marcar` com os tempos de `a_remover` para vê-los na régua.
    limiar_db: abaixo disso é silêncio (padrão automático, entre o piso de ruído e o nível da fala; com
      ruído de fundo forte, tente -35 ou -30). duracao_min_s: só pausas pelo menos desse tamanho (0.6).
    margem_s: deixa esse tanto de pausa antes e depois de cada fala (0.15; respiração, evita "picotar").
    inicio/fim: procurar só nesse trecho. A linha do tempo muda: as marcas são limpas.
    Exemplos: "tira os silêncios" → padrão; "corta só as pausas longas" → duracao_min_s=1.5;
    "ficou picotado" → desfazer e margem_s=0.3; "mostra onde tem silêncio" → modo='analisar'."""
    doc, est = _doc()
    v = await _bg(core.remover_silencios, doc, modo=modo, limiar_db=limiar_db, duracao_min_s=duracao_min_s,
                  margem_s=margem_s, faixa=faixa, inicio=inicio, fim=fim, estado=est,
                  progresso=_progresso("removendo silêncios"))
    if (modo or "cortar").lower() == "analisar":
        return v
    cliente.enviar("marcar", limpar=True)
    return _apos_edicao(v, doc)


@ferramenta
async def fade_audio(entrada_s: float | None = None, saida_s: float | None = None,
                     faixas: list[int] | None = None, video: bool = False, cor: str = "black",
                     curva: str = "tri") -> dict:
    """Fade in no começo e/ou fade out no fim (nova versão).
    entrada_s: duração do fade in (s); saida_s: duração do fade out (s). faixas: [1], [1,2]... (padrão: todas).
    video=True: a imagem também aparece/some (fade para `cor`, padrão 'black'; re-encoda o vídeo).
    curva: 'tri' (linear, padrão), 'qsin' (suave), 'exp', 'log'...
    Para fade num trecho no meio do vídeo use `ajustar_audio` (fade_in_s/fade_out_s com inicio/fim).
    Exemplos: "fade out de 3 s no final" → saida_s=3; "abre e fecha com fade do preto" → entrada_s=1,
    saida_s=2, video=True."""
    doc, _ = _doc()
    v = await _bg(core.fade_audio, doc, entrada_s=entrada_s, saida_s=saida_s, faixas=faixas, video=video,
                  cor=cor, curva=curva, progresso=_progresso("aplicando fade"))
    return _apos_edicao(v, doc)


@ferramenta
async def musica_fundo(arquivo: str, modo: str = "mixar", faixa: int = 1, volume_db: float | None = None,
                       loop: bool = True, fade_in_s: float = 1.0, fade_out_s: float = 2.0,
                       inicio: str | None = None, fim: str | None = None, musica_inicio: str | None = None,
                       ducking: bool = False, ducking_db: float = 12.0, titulo: str | None = None) -> dict:
    """Adiciona ou troca a música de fundo a partir de um arquivo de áudio do usuário (mp3, wav, m4a...;
    nova versão).
    modo='mixar' (padrão): soma a música à faixa `faixa` (a voz continua igual);
    'trocar': a música SUBSTITUI a faixa `faixa` (som original sai);
    'nova_faixa': a música vira uma faixa separada (A2...), mantendo A1 — o usuário ajusta o mix na janela.
    volume_db: volume da música (padrão -15 em mixar/nova_faixa, 0 em trocar; mais negativo = mais baixa).
    loop: repete a música se ela for menor que o trecho (padrão True). fade_in_s/fade_out_s: fades da música.
    inicio/fim: onde a música toca no vídeo (padrão: tudo). musica_inicio: começa a música desse ponto
    dela (ex. '0:30' pula a introdução).
    ducking=True: abaixa a música automaticamente enquanto alguém fala na faixa `faixa`, em ~ducking_db dB.
    Exemplos: "põe essa música de fundo baixinha" → arquivo=..., volume_db=-20; "música de fundo que abaixa
    quando eu falo" → ducking=True; "troca o áudio por essa música" → modo='trocar'."""
    doc, est = _doc()
    v = await _bg(core.musica_fundo, doc, arquivo, modo=modo, faixa=faixa, volume_db=volume_db, loop=loop,
                  fade_in_s=fade_in_s, fade_out_s=fade_out_s, inicio=inicio, fim=fim,
                  musica_inicio=musica_inicio, ducking=ducking, ducking_db=ducking_db, titulo=titulo,
                  estado=est, progresso=_progresso("pondo música de fundo"))
    return _apos_edicao(v, doc)


# ---------------------------------------------------------------- exportação
@ferramenta
async def exportar(predefinicao: str | None = None, formato: str | None = None,
                   inicio: str | float | None = None, fim: str | float | None = None,
                   trechos: list[list[str | float]] | None = None, tempo: str | float | None = None,
                   destino: str | None = None, alvo_mb: float | None = None,
                   largura: int | None = None, altura: int | None = None, qualidade: str | int | None = None,
                   fps: float | None = None, sem_audio: bool = False, faixa: int | None = None,
                   enquadramento: str | None = None, arquivo: str | None = None) -> dict:
    """EXPORTA para um ARQUIVO NOVO (converter, compartilhar, extrair áudio, tirar foto de um quadro).
    Diferente de editar: não cria versão, não altera o original nem recarrega a janela. Parte do arquivo
    atual do documento (a última versão, se houver edições não salvas). Nunca sobrescreve: o nome é
    automático ao lado do original (`video.whatsapp.mp4`, `video.whatsapp.2.mp4`, `video.mp3`...).

    predefinicao:
      whatsapp — MP4 H.264+AAC leve, até 720p, cabe em ~16 MB quando possível
      youtube — MP4 H.264 alta qualidade, mesma resolução, AAC 192k
      instagram (ou reels) — 1080x1920 vertical; vídeo horizontal ganha fundo desfocado
                             (enquadramento='desfocado' | 'recorte' (centro) | 'barras')
      mp4 — H.264+AAC boa qualidade, todas as faixas de áudio
      menor — H.265 com CRF alto (arquivo bem menor); menor_vp9 — WebM VP9
      webm — VP9+Opus;  gif — GIF animado 480 px, 12,5 fps, sem som
      transparente — WebM VP9 com alfa (yuva420p); transparente_mov (ou prores) — ProRes 4444 .mov com alfa
      mp3 | m4a | opus | ogg | wav | flac — só o áudio
      png | jpg — foto do quadro exato em `tempo` (padrão: posição atual; 'fim' = último quadro).
                  Não há ferramenta separada de foto.
    formato: só a extensão ('mp3', 'mkv', 'webm'...), alternativa à predefinição; com predefinição, troca o
      contêiner se for compatível (ex.: youtube + mkv); combinação impossível (youtube + webm) dá erro.
    inicio/fim: exporta só esse trecho; trechos=[[ini, fim], ...] exporta vários trechos emendados (corte exato).
      Tempos em segundos (número) ou texto como nas outras ferramentas: '1:23.5', 'atual', 'm1', 'in', 'out',
      'm2+0.5', 'fim'...
    destino: pasta ou caminho de arquivo (padrão: pasta do original). A extensão do arquivo tem de combinar
      com a predefinição.
    alvo_mb: tamanho máximo em MB → no vídeo calcula o bitrate e codifica em 2 passes; no áudio comprimido
      usa um bitrate calculado.
    largura/altura: redimensiona (só uma = mantém proporção; as duas = cabe dentro da caixa); ímpar vira par.
    qualidade: 'alta' | 'normal' | 'baixa' ou número CRF (menor = melhor). fps: muda a taxa de quadros.
    sem_audio=True tira o som; faixa=N escolhe a faixa de áudio (padrão: A1, ou todas em mp4/webm/menor).
    arquivo: exporta outro arquivo em vez do aberto na janela. Nesse caso marcas e posição da janela
      ('m1', 'atual', 'in'...) não valem: use segundos.

    Exemplos de pedidos:
      "converte para mp4 leve para WhatsApp" → predefinicao='whatsapp'
      "gera um GIF do trecho amarelo" → predefinicao='gif', inicio='m1', fim='m2' (veja `estado`.trechos_marcados)
      "exporta só o áudio em mp3" → predefinicao='mp3'
      "tira um print deste quadro" → predefinicao='png' (tempo='atual')
      "manda para o Instagram só do 0:10 ao 0:40" → predefinicao='instagram', inicio='0:10', fim='0:40'
      "preciso que fique com menos de 8 MB" → predefinicao='mp4', alvo_mb=8
    Devolve caminho, tamanho, duração, codecs, resolução e avisos. Pode demorar em vídeos longos
    (a barra de status da janela mostra o progresso)."""
    import re
    import threading

    est_janela = None
    if cliente.vivo():
        est_janela = _estado()
    doc_janela = (est_janela or {}).get("documento")
    if arquivo is None:
        if not doc_janela:
            raise RuntimeError("nenhum vídeo aberto no player; use `abrir` ou passe `arquivo`")
        arquivo = doc_janela["original"]
        est = est_janela
    elif doc_janela and Documento(arquivo).original == Path(doc_janela["original"]):
        est = est_janela
    else:
        # outro arquivo: marcas/posição da janela não se aplicam a ele
        est = None
        valores = [inicio, fim, tempo] + [x for par in (trechos or []) for x in par]
        rx = re.compile(r"^\s*(atual|agora|aqui|pos|in|entrada|out|sa[ií]da|m\d+)\b", re.I)
        ruins = [str(v) for v in valores if isinstance(v, str) and rx.match(v)]
        if ruins:
            raise ValueError(f"{', '.join(ruins)} se refere(m) à janela, mas `arquivo` não é o vídeo aberto nela; "
                             f"use tempos em segundos ou '1:23.5'")
    rotulo = predefinicao or formato or "arquivo"
    ultimo = [0.0, -1]

    def progresso(f: float):
        pct = int(f * 100)
        agora = time.time()
        if est_janela and pct != ultimo[1] and (agora - ultimo[0] >= 1.0 or pct >= 100):
            ultimo[:] = [agora, pct]
            try:
                cliente.enviar("mensagem", texto=f"Claude: exportando {rotulo}… {pct}%", timeout=2)
            except Exception:  # noqa: BLE001
                pass

    cancelar = threading.Event()
    try:
        r = await _bg(core.exportar, arquivo, predefinicao=predefinicao, formato=formato, inicio=inicio,
                      fim=fim, trechos=trechos, tempo=tempo, destino=destino, alvo_mb=alvo_mb, largura=largura,
                      altura=altura, qualidade=qualidade, fps=fps, sem_audio=sem_audio, faixa=faixa,
                      enquadramento=enquadramento, estado=est, progresso=progresso, cancelar=cancelar.is_set)
    except asyncio.CancelledError:
        cancelar.set()  # pedido cancelado pelo cliente: interrompe o ffmpeg (a thread segue sozinha)
        raise
    if est_janela:
        try:
            cliente.enviar("mensagem", texto=f"Claude: exportado → {r['caminho']} ({r['tamanho_mb']} MB)")
        except Exception:  # noqa: BLE001
            pass
    r["lembrete"] = "arquivo novo; o original e as versões não foram alterados"
    return r


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
