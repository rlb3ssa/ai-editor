"""Ferramentas MCP: arquivos e imagens (quadros em arquivo, folha de contatos, capa, slideshow, áudio→vídeo, metadados, remux/reparo, legendas em arquivo, comparar qualidade).

Registradas no mesmo servidor (`mcp_server.mcp`) ao serem importadas no fim de `mcp_server.py`.
A lógica (ffmpeg, sem Qt) fica em `arquivos.py`; aqui só a interface MCP.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import arquivos, cliente
from .core import Documento
from .mcp_server import Image, _apos_edicao, _bg, _doc, _estado, _progresso, abrir, ferramenta  # noqa: F401

_RX_JANELA = re.compile(r"^\s*(atual|agora|aqui|pos|in|entrada|out|sa[ií]da|m\d+)\b", re.I)


def _janela() -> dict | None:
    try:
        return _estado() if cliente.vivo() else None
    except Exception:  # noqa: BLE001
        return None


def _fonte(arquivo: str | None, *tempos) -> tuple[Path, Path, dict | None]:
    """(arquivo a ler, original para nomear a saída, estado p/ tempos). Sem `arquivo`: o documento aberto
    (a última versão, com o nome do original). Com `arquivo` de fora da janela, marcas/'atual' não valem."""
    est = _janela()
    doc_j = (est or {}).get("documento")
    if arquivo is None:
        if not doc_j:
            raise RuntimeError("nenhum vídeo aberto no player; use `abrir` ou passe `arquivo`")
        d = Documento(doc_j["original"])
        return d.atual(), d.original, est
    p = Path(str(arquivo)).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"arquivo não encontrado: {p}")
    if doc_j and Documento(p).original == Path(doc_j["original"]):
        d = Documento(doc_j["original"])
        return d.atual(), d.original, est
    ruins = [str(v) for v in tempos if isinstance(v, str) and _RX_JANELA.match(v)]
    if ruins:
        raise ValueError(f"{', '.join(ruins)} se refere(m) à janela, mas `arquivo` não é o vídeo aberto nela; "
                         f"use segundos ou '1:23.5'")
    return p, p, None


def _documento(arquivo: str | None) -> tuple[Documento, bool]:
    """Documento a editar por versão: o da janela, ou o de `arquivo` (True = é o aberto na janela)."""
    est = _janela()
    doc_j = (est or {}).get("documento")
    if arquivo is None:
        if not doc_j:
            raise RuntimeError("nenhum vídeo aberto no player; use `abrir` ou passe `arquivo`")
        return Documento(doc_j["original"]), True
    p = Path(str(arquivo)).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"arquivo não encontrado: {p}")
    d = Documento(p)
    return d, bool(doc_j and d.original == Path(doc_j["original"]))


def _versao(v: dict, doc: Documento, na_janela: bool) -> dict:
    if na_janela:
        return _apos_edicao(v, doc)
    r = {"versao": v["n"], "descricao": v["descricao"], "arquivo": str(doc.atual())}
    for k in ("avisos", "detalhes"):
        if v.get(k):
            r[k] = v[k]
    r["lembrete"] = ("versão temporária de um arquivo que NÃO está aberto na janela: o original continua igual. "
                     "Para substituí-lo, abra-o (`abrir`) e use `salvar` quando o usuário pedir")
    return r


# ---------------------------------------------------------------- quadros e imagens
@ferramenta
async def extrair_quadros(modo: str = "intervalo", intervalo_s: float | None = None,
                          tempos: list[str | float] | None = None, formato: str = "jpg", largura: int | None = None,
                          inicio: str | float | None = None, fim: str | float | None = None,
                          destino: str | None = None, maximo: int = 300, limiar_cenas: float = 0.3,
                          arquivo: str | None = None) -> dict:
    """Salva quadros do vídeo como IMAGENS (jpg/png/webp) numa PASTA NOVA ao lado do original
    (`video.quadros/`, `video.quadros.2/`...; ou dentro de `destino`). Não altera o vídeo.
    Para UMA foto de um quadro use `exportar(predefinicao='png'|'jpg', tempo=...)`.

    modo:
      intervalo — uma imagem a cada `intervalo_s` segundos (obrigatório)
      cenas     — uma imagem por cena (detecta os cortes; `limiar_cenas` 0,1–0,6, menor = mais sensível)
      tempos    — nos `tempos` da lista (segundos, '1:23.5', 'm1', 'atual'...)
      todos     — TODOS os quadros (cuidado: 30 por segundo; use inicio/fim)
    formato: 'jpg' (padrão, qualidade alta), 'png' (sem perda, maior) ou 'webp'.
    largura: reduz (mantém proporção; nunca amplia). inicio/fim: só esse trecho.
    maximo: limite de imagens (padrão 300, até 10000); se o pedido passar disso dá erro com a estimativa.
    Nomes: `quadro_0001_12.345s.jpg` (número + tempo no vídeo).
    arquivo: outro vídeo em vez do aberto (aí use tempos em segundos).

    Exemplos de pedidos:
      "salva um frame a cada 2 segundos" → modo='intervalo', intervalo_s=2
      "quero uma imagem de cada cena" → modo='cenas'
      "exporta todos os quadros do trecho amarelo em png" → modo='todos', inicio='m1', fim='m2', formato='png'
      "tira fotos em 0:05, 0:12 e 1:30" → modo='tempos', tempos=['0:05','0:12','1:30']
    Devolve pasta, quantidade, tamanho total e os primeiros nomes."""
    arq, orig, est = _fonte(arquivo, inicio, fim, *(tempos or []))
    return await _bg(arquivos.extrair_quadros, arq, orig, modo=modo, intervalo_s=intervalo_s, tempos=tempos,
                     formato=formato, largura=largura, inicio=inicio, fim=fim, destino=destino,
                     limiar_cenas=limiar_cenas, maximo=maximo, estado=est, progresso=_progresso("quadros"))


@ferramenta
async def folha_contatos(colunas: int = 4, linhas: int = 4, largura: int = 1920, com_tempos: bool = True,
                         cabecalho: bool = True, inicio: str | float | None = None, fim: str | float | None = None,
                         formato: str = "jpg", destino: str | None = None, arquivo: str | None = None) -> list:
    """Folha de contatos (contact sheet): UMA imagem com colunas×linhas miniaturas espaçadas igualmente pelo
    vídeo, com o tempo no canto de cada uma e um cabeçalho (nome, duração, resolução, fps, codecs, tamanho).
    Arquivo novo `video.folha.jpg` ao lado do original. Ótima para ver o vídeo todo de relance, catalogar ou
    mandar uma prévia. Também mostra a imagem para o Claude.

    colunas 1–12, linhas 1–20 (até 120 miniaturas); largura total da imagem em px (padrão 1920).
    com_tempos=False tira os tempos; cabecalho=False tira o cabeçalho; inicio/fim: só um trecho.
    formato 'jpg' | 'png' | 'webp'. arquivo: outro vídeo em vez do aberto.

    Exemplos: "faz uma folha de contatos do vídeo" → padrão; "um mosaico 6x5 do vídeo" → colunas=6, linhas=5;
    "uma prévia em grade só do primeiro minuto" → fim='1:00'."""
    arq, orig, est = _fonte(arquivo, inicio, fim)
    r = await _bg(arquivos.folha_contatos, arq, orig, colunas=colunas, linhas=linhas, largura=largura,
                  com_tempos=com_tempos, cabecalho=cabecalho, inicio=inicio, fim=fim, formato=formato,
                  destino=destino, estado=est, progresso=_progresso("folha de contatos"))
    import json
    saida: list = [json.dumps(r, ensure_ascii=False)]
    if Path(r["arquivo"]).suffix.lower() in (".jpg", ".png"):
        saida.append(Image(path=r["arquivo"]))
    return saida


@ferramenta
async def capa(acao: str = "extrair", tempo: str | float | None = None, arquivo_imagem: str | None = None,
               destino: str | None = None, arquivo: str | None = None) -> dict:
    """Capa embutida (thumbnail / arte do álbum) de MP4, M4A, MOV, MP3, MKV e FLAC — a imagem que o
    gerenciador de arquivos, o celular e os players mostram. Sem reencodar o áudio/vídeo.

    acao:
      extrair — salva a capa embutida num ARQUIVO NOVO (`video.capa.jpg`); não altera o vídeo
      definir — põe (ou troca) a capa: `tempo` = um quadro do próprio vídeo ('atual', '0:12'...) OU
                `arquivo_imagem` = uma foto (jpg/png; outros formatos/maiores que 1920 px viram JPEG). Nova versão.
      remover — tira a capa. Nova versão.
    arquivo: outro arquivo em vez do aberto na janela (a versão fica temporária até abrir e `salvar`).

    Exemplos: "usa este quadro como capa" → acao='definir', tempo='atual';
    "coloca a foto ~/capa.png como capa do mp3" → acao='definir', arquivo_imagem='~/capa.png';
    "salva a capa do álbum" → acao='extrair'; "tira a capa" → acao='remover'."""
    doc, na_janela = _documento(arquivo)
    est = _janela() if na_janela else None
    if not na_janela and isinstance(tempo, str) and _RX_JANELA.match(tempo):
        raise ValueError(f"'{tempo}' se refere à janela; use segundos")
    r = await _bg(arquivos.capa, doc, acao=acao, tempo=tempo, arquivo_imagem=arquivo_imagem, destino=destino,
                  estado=est, progresso=_progresso("capa"))
    if "n" in r and "descricao" in r:
        return _versao(r, doc, na_janela)
    return r


@ferramenta
async def video_de_imagens(imagens: list[str] | None = None, pasta: str | None = None,
                           duracao_cada_s: float | None = None, transicao: str = "crossfade",
                           duracao_transicao: float = 0.8, audio: str | None = None, resolucao: str | None = None,
                           fundo: str = "desfocado", ken_burns: bool = False, fps: int = 30,
                           destino: str | None = None, abrir_no_player: bool = False) -> dict:
    """Cria um VÍDEO NOVO (.mp4 H.264, toca em qualquer lugar) a partir de fotos: slideshow / apresentação.
    Não precisa de vídeo aberto. Saída `apresentacao.mp4` na pasta das imagens (ou `destino`); nunca sobrescreve.

    imagens: lista de arquivos NA ORDEM; ou pasta: todas as imagens da pasta em ordem natural do nome
      (foto2 antes de foto10). jpg, png, webp, bmp, tif, gif (1º quadro), heic/avif se o ffmpeg ler. A orientação
      EXIF de fotos de celular é respeitada. Até 500.
    duracao_cada_s: segundos por imagem (padrão 3; com `audio` e sem duracao_cada_s, as imagens se distribuem
      pela duração da música).
    transicao: 'crossfade' (padrão: uma se mistura na outra), 'fade' (escurece e clareia), 'corte' (seco) ou um
      efeito do xfade: wipeleft, slideleft, circleopen, radial, pixelize, zoomin, fadeblack...
    duracao_transicao: segundos (padrão 0,8).
    audio: música/narração de fundo (corta no fim das imagens com fade de saída).
    resolucao: '1920x1080' (padrão), '720p', '4k', ou proporção: 'vertical'/'9:16' (1080x1920), 'quadrado',
      '4:5'...
    fundo: fotos com outro formato que o vídeo: 'desfocado' (a própria foto borrada atrás, padrão),
      'recorte' (preenche cortando as bordas) ou uma cor ('black', 'white', '#202020') para barras.
    ken_burns=True: zoom lento em cada foto (alternando aproximar/afastar).
    abrir_no_player=True abre o vídeo pronto na janela.

    Exemplos: "faz um vídeo com as fotos da pasta ~/Fotos/viagem, 4 s cada, com a música x.mp3" →
      pasta='~/Fotos/viagem', duracao_cada_s=4, audio='x.mp3';
      "slideshow vertical para stories com zoom suave" → resolucao='vertical', ken_burns=True.
    Pode levar alguns minutos com muitas fotos (progresso na barra da janela, se aberta)."""
    r = await _bg(arquivos.video_de_imagens, imagens=imagens, pasta=pasta, duracao_cada_s=duracao_cada_s,
                  transicao=transicao, duracao_transicao=duracao_transicao, audio=audio, resolucao=resolucao,
                  fundo=fundo, ken_burns=ken_burns, fps=fps, destino=destino, progresso=_progresso("slideshow"))
    if abrir_no_player:
        await abrir(r["caminho"])
        r["aberto_no_player"] = True
    return r


@ferramenta
async def audio_para_video(estilo: str = "ondas", imagem: str | None = None, cor: str = "#4FC3F7",
                           fundo: str = "#101418", resolucao: str | None = None, titulo_texto: str | None = None,
                           faixa: int = 1, destino: str | None = None, arquivo: str | None = None,
                           abrir_no_player: bool = False) -> dict:
    """Transforma ÁUDIO (podcast, música, gravação) em VÍDEO .mp4 NOVO para YouTube/Instagram/WhatsApp, que
    não aceitam só áudio. Arquivo `nome.<estilo>.mp4` ao lado do original. Áudio AAC é copiado sem perda.

    estilo:
      ondas    — forma de onda animada no centro (padrão)
      espectro — espectrograma rolando (frequências × tempo)
      barras   — barras de frequência (equalizador) animadas
      cqt      — visualizador musical colorido (notas)
      imagem   — imagem parada (a de `imagem` ou a capa embutida do áudio), vídeo bem leve
    imagem: foto de fundo (fica centralizada com o fundo desfocado; nos estilos animados fica atrás do
      visualizador). Sem ela, fundo de cor sólida `fundo`.
    cor: cor das ondas/barras ('white', 'amarelo', '#FF5722'...). resolucao: '1280x720' (padrão), '1080p',
      'vertical' (1080x1920), 'quadrado'...
    titulo_texto: texto no topo (nome do episódio; '\\n' quebra linha). faixa: qual faixa de áudio (A1...).
    arquivo: outro áudio em vez do aberto. abrir_no_player=True abre o vídeo pronto.

    Exemplos: "transforma o podcast em vídeo pro YouTube com ondinhas e o título 'Ep. 12'" →
      estilo='ondas', titulo_texto='Ep. 12'; "vídeo da música com a capa do álbum" → estilo='imagem';
      "versão vertical para o Instagram com a foto ~/logo.png" → resolucao='vertical', imagem='~/logo.png'."""
    arq, orig, _est = _fonte(arquivo)
    r = await _bg(arquivos.audio_para_video, arq, orig, estilo=estilo, imagem=imagem, cor=cor, fundo=fundo,
                  resolucao=resolucao, titulo_texto=titulo_texto, faixa=faixa, destino=destino,
                  progresso=_progresso("áudio → vídeo"))
    if abrir_no_player:
        await abrir(r["caminho"])
        r["aberto_no_player"] = True
    return r


# ---------------------------------------------------------------- metadados e privacidade
@ferramenta
async def editar_metadados(titulo: str | None = None, artista: str | None = None, album: str | None = None,
                           data: str | None = None, comentario: str | None = None, descricao: str | None = None,
                           genero: str | None = None, idiomas: dict[str, str] | None = None,
                           nomes_faixas: dict[str, str] | None = None, remover: str | list[str] | None = None,
                           arquivo: str | None = None) -> dict:
    """Edita ou APAGA metadados (tags) sem reencodar — nova versão, o original fica intacto até `salvar`.
    Funciona com vídeo, áudio e FOTOS (jpg, png, heic, webp, tif). Para só VER os metadados use `metadados`.

    titulo, artista, album, data ('2024-05-17' ou '2024'), comentario, descricao, genero: definem tags
      ("" apaga a tag).
    idiomas: idioma por faixa, ex.: {"A1": "pt", "A2": "en", "L1": "pt"} (A = áudio, L = legenda, V = vídeo).
    nomes_faixas: nome exibido de cada faixa, ex.: {"A1": "Português", "A2": "Comentários do diretor"}.
    remover (privacidade, antes de compartilhar):
      'localizacao' — GPS/local da gravação (QuickTime, XMP, EXIF)
      'camera'      — marca/modelo/nº de série do aparelho, lente, software, dados do fabricante
      'c2pa'        — Content Credentials (manifesto de procedência/IA)
      'tudo'        — todos os metadados (mantém só idioma/nome das faixas; na foto mantém perfil de cor e
                      orientação)
      pode combinar: ['localizacao', 'camera'].
    No fim confere com o exiftool e informa o que havia e se ainda sobrou algo.
    arquivo: outro arquivo em vez do aberto (útil para fotos; a versão fica temporária até abrir e `salvar`).

    Exemplos: "tira a localização do vídeo antes de eu mandar" → remover='localizacao';
    "limpa tudo que identifica meu celular" → remover=['localizacao','camera'];
    "o áudio 2 é inglês" → idiomas={'A2':'en'}; "põe o título 'Aniversário da Ana'" → titulo='Aniversário da Ana'."""
    doc, na_janela = _documento(arquivo)
    v = await _bg(arquivos.editar_metadados, doc, titulo=titulo, artista=artista, album=album, data=data,
                  comentario=comentario, descricao=descricao, genero=genero, idiomas=idiomas,
                  nomes_faixas=nomes_faixas, remover=remover, progresso=_progresso("metadados"))
    return _versao(v, doc, na_janela)


# ---------------------------------------------------------------- remux, reparo, rotação
@ferramenta
async def converter_sem_perda(formato: str, audio_incompativel: str = "erro", destino: str | None = None,
                              arquivo: str | None = None) -> dict:
    """Troca o FORMATO (contêiner) SEM reencodar: instantâneo e sem perder qualidade (ex.: MKV → MP4,
    MOV → MP4, MP4 → MKV, extrair o áudio AAC para .m4a, Opus para .opus). ARQUIVO NOVO ao lado do original.
    Só funciona se os codecs couberem no formato (ex.: H.264/H.265 + AAC → mp4; VP9/AV1 + Opus → webm).
    Se não couber, explica e sugere `exportar` (reencoda). Legendas de texto são convertidas para o tipo que o
    formato aceita (mov_text no mp4, webvtt no webm); legendas de imagem/anexos incompatíveis ficam de fora
    com aviso.

    formato: mp4, mkv, mov, m4v, webm, ts, avi, m4a, mp3, ogg, opus, flac, wav, mka, aac.
    audio_incompativel='converter': se SÓ o áudio não couber (ex.: MKV com FLAC/DTS → mp4), reencoda apenas o
      áudio e copia o vídeo sem perda.
    arquivo: outro arquivo em vez do aberto.

    Exemplos: "passa esse mkv para mp4 sem perder qualidade" → formato='mp4';
    "o iPhone não abre, converte rápido para mov" → formato='mov'; "tira só o áudio sem converter" → formato='m4a'
    (se o áudio for AAC) ou 'mka'."""
    arq, orig, _est = _fonte(arquivo)
    return await _bg(arquivos.converter_sem_perda, arq, orig, formato=formato, destino=destino,
                     audio_incompativel=audio_incompativel, progresso=_progresso(f"convertendo para {formato}"))


@ferramenta
async def reparar_arquivo(formato: str | None = None, verificar: bool = True, destino: str | None = None,
                          arquivo: str | None = None) -> dict:
    """Tenta CONSERTAR um vídeo/áudio que não abre direito, trava ao avançar, tem duração errada, áudio fora de
    sincronia por timestamps ruins ou não toca no navegador/celular (índice no fim). Regrava sem reencodar
    (regenera timestamps, ignora pacotes corrompidos, índice no início): ARQUIVO NOVO `nome.reparado.ext`.
    Se o formato original não aceitar, sai em .mkv. verificar=True decodifica antes e depois e conta os erros.
    MP4 de gravação interrompida SEM o índice ('moov atom not found') não tem conserto aqui: a ferramenta
    explica (precisa do 'untrunc' com um vídeo bom do mesmo aparelho).
    arquivo: o arquivo com problema (padrão: o aberto). formato: força a extensão da saída (ex.: 'mkv').

    Exemplos: "esse vídeo trava quando avanço" / "a duração aparece errada" / "o arquivo está corrompido" →
    reparar_arquivo(arquivo=...)."""
    arq, orig, _est = _fonte(arquivo)
    return await _bg(arquivos.reparar_arquivo, arq, orig, formato=formato, destino=destino, verificar=verificar,
                     progresso=_progresso("reparando"))


@ferramenta
async def rotacao_sem_perda(graus: int, absoluto: bool = False, arquivo: str | None = None) -> dict:
    """Gira o vídeo SEM reencodar (instantâneo, sem perda): muda só a marcação de rotação que os players
    respeitam (MP4/MOV/MKV). Resolve o clássico "vídeo do celular deitado". Nova versão.
    graus: no sentido HORÁRIO, múltiplo de 90 (90, 180, 270 ou -90). absoluto=True define a rotação final em
    vez de somar (0 = tira a rotação).
    Limite: alguns editores/sites antigos ignoram essa marcação; aí use `transformar_imagem(girar=...)`, que
    reencoda e grava os pixels já girados. Outros ângulos (ex.: 3°) também só com `transformar_imagem`.
    arquivo: outro arquivo em vez do aberto.

    Exemplos: "o vídeo está deitado, gira para a direita" → graus=90; "está de cabeça para baixo" → graus=180;
    "tira a rotação" → graus=0, absoluto=True."""
    doc, na_janela = _documento(arquivo)
    v = await _bg(arquivos.rotacao_sem_perda, doc, graus, absoluto=absoluto, progresso=_progresso("rotação"))
    return _versao(v, doc, na_janela)


# ---------------------------------------------------------------- legendas em arquivo
@ferramenta
async def legenda_arquivo(acao: str = "listar", faixa_legenda: int | str | None = None, formato: str | None = None,
                          deslocamento_s: float | None = None, arquivo: str | None = None,
                          destino: str | None = None) -> dict:
    """Legendas como ARQUIVOS e faixas embutidas (para CRIAR legenda a partir da fala use `gerar_legenda`;
    para embutir/queimar um .srt use `embutir_legenda`).

    acao:
      listar    — faixas de legenda do vídeo aberto (idioma, título, texto/imagem, padrão/forçada)
      extrair   — salva a faixa `faixa_legenda` (1, 2...; padrão: todas) num ARQUIVO NOVO ao lado do vídeo
                  (`video.por.srt`); formato 'srt' | 'vtt' | 'ass' (padrão: o natural da faixa). Legenda de
                  imagem (PGS de Blu-ray) sai em .sup; DVD/DVB em .mks.
      converter — converte o arquivo `arquivo` (.srt/.vtt/.ass) para `formato` → arquivo novo
                  (detecta legenda antiga em Windows-1252 e grava UTF-8)
      deslocar  — atrasa/adianta a legenda `deslocamento_s` segundos (positivo = aparece DEPOIS; negativo =
                  ANTES). Com `arquivo` = .srt/.vtt/.ass externo → arquivo novo `nome.deslocado.srt`; sem
                  `arquivo` → a faixa embutida `faixa_legenda` do vídeo aberto, numa nova versão.
      remover   — tira a faixa `faixa_legenda` (ou 'todas') do vídeo aberto, nova versão sem reencodar.

    Exemplos: "a legenda está 2 segundos atrasada" → acao='deslocar', deslocamento_s=-2;
    "salva a legenda em português como srt" → acao='listar' (ver o número) e depois acao='extrair',
    faixa_legenda=N, formato='srt'; "converte esse .ass para .srt" → acao='converter', arquivo=..., formato='srt';
    "tira as legendas embutidas" → acao='remover', faixa_legenda='todas'."""
    precisa_doc = acao not in ("converter",) and not (acao == "deslocar" and arquivo is not None)
    doc = None
    if precisa_doc:
        doc, _est = _doc()
    r = await _bg(arquivos.legenda_arquivo, doc, acao=acao, faixa_legenda=faixa_legenda, formato=formato,
                  deslocamento_s=deslocamento_s, arquivo=arquivo, destino=destino, progresso=_progresso("legenda"))
    if doc is not None and "n" in r and "descricao" in r:
        return _apos_edicao(r, doc)
    return r


# ---------------------------------------------------------------- qualidade
@ferramenta
async def comparar_qualidade(versao_a: str | int = "original", versao_b: str | int = "atual",
                             inicio: float | None = None, duracao_s: float | None = None,
                             metricas: str = "auto") -> dict:
    """Mede objetivamente QUANTO de qualidade se perdeu entre duas versões/arquivos e explica em português:
    VMAF (nota 0–100 da Netflix, a que melhor imita o olho humano; disponível no ffmpeg do pacote), SSIM e PSNR,
    com o pior momento. versao_a = REFERÊNCIA (a de melhor qualidade, ex.: o original); versao_b = a comparada.

    versao_a / versao_b: 'original' (ou 0), 'atual', número da versão (3 ou 'v3') ou o caminho de um arquivo
      (ex.: o arquivo que `exportar` gerou). Padrão: original × versão atual.
    inicio/duracao_s: mede só um trecho (segundos) — mais rápido em vídeos longos (VMAF roda ~tempo real em 720p).
    metricas: 'auto' (VMAF se houver + SSIM + PSNR), 'ssim' (só SSIM/PSNR, mais rápido), 'vmaf'.
    Resoluções diferentes: o comparado é redimensionado para a da referência. Os dois devem ter o MESMO
    conteúdo desde o início (comparar um vídeo cortado com o original não faz sentido).

    Exemplos: "a versão para WhatsApp perdeu muita qualidade?" → versao_b='~/video.whatsapp.mp4';
    "compara a v2 com a v3" → versao_a=2, versao_b=3; "a compressão estragou?" → padrão."""
    est = _janela()
    doc = Documento(est["documento"]["original"]) if est and est.get("documento") else None
    ref = arquivos.resolver_lado(doc, versao_a)
    dist = arquivos.resolver_lado(doc, versao_b)
    return await _bg(arquivos.comparar_qualidade, ref, dist, inicio=inicio, duracao_s=duracao_s,
                     metricas=metricas, progresso=_progresso("medindo qualidade"))
