"""Ferramentas MCP: efeitos de vídeo (filtros visuais, LUT, chroma key, sobreposição/PiP/tela dividida, zoom, inverter/loop, fade, gráficos, melhorar imagem, fps).

Registradas no mesmo servidor (`mcp_server.mcp`) ao serem importadas no fim de `mcp_server.py`.
A lógica (ffmpeg, sem Qt) fica em módulos próprios; aqui só a interface MCP.
"""
from __future__ import annotations

from . import cliente
from . import efeitos_video as ev
from .mcp_server import _apos_edicao, _bg, _doc, _progresso, ferramenta  # noqa: F401

# Todas as edições abaixo criam uma VERSÃO nova (desfazer volta) mantendo resolução, pix_fmt (8/10 bits) e tags de
# cor. Quando só a imagem muda, o áudio, as legendas e os anexos são copiados sem perda. Tempos aceitam segundos,
# '1:23.5', 'atual', 'm1', 'm2+0.5'... Janela [inicio, fim] = só aquele trecho (o resto fica intacto).

_T = str | float | None


def _limpar_marcas() -> None:
    """A linha do tempo mudou: as marcas antigas apontariam para o lugar errado."""
    try:
        cliente.enviar("marcar", limpar=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- imagem
@ferramenta
async def efeito_visual(efeito: str, intensidade: float | None = None, inicio: _T = None, fim: _T = None) -> dict:
    """Efeito visual pronto (nova versão), no vídeo todo ou só em [inicio, fim].
    efeito: 'preto_e_branco', 'sepia', 'vinheta', 'negativo', 'desfoque', 'nitidez', 'pixelar' (8 bits),
    'desenho' (esboço a lápis), 'contorno' (cartum), 'granulado' (grão de filme), 'filme_antigo' (sépia+grão+
    cintilação), 'vhs' (retrô), 'espelho' (metade espelhada), 'brilho_suave' (glow/sonho).
    intensidade 0–1 (cada efeito tem um padrão; em preto_e_branco < 1 = cores desbotadas).
    Exemplos: "deixa preto e branco" → efeito='preto_e_branco'; "efeito VHS só no trecho amarelo" →
    efeito='vhs', inicio='m1', fim='m2'; "um granulado bem leve" → efeito='granulado', intensidade=0.2."""
    doc, est = _doc()
    v = await _bg(ev.efeito_visual, doc, efeito, intensidade=intensidade, inicio=inicio, fim=fim, estado=est,
                  progresso=_progresso("aplicando efeito"))
    return _apos_edicao(v, doc)


@ferramenta
async def aplicar_lut(arquivo: str, intensidade: float = 1.0, inicio: _T = None, fim: _T = None) -> dict:
    """Aplica uma LUT de cor / "filtro de cinema" (nova versão): arquivo .cube, .3dl, .dat, .m3d, .csp ou imagem
    Hald CLUT (.png quadrada). intensidade 0–1 mistura com o original (0.5 = meio a meio).
    Exemplos: "aplica a LUT ~/luts/teal_orange.cube" → arquivo=...; "a LUT ficou forte, deixa 60%" → desfazer
    e intensidade=0.6."""
    doc, est = _doc()
    v = await _bg(ev.aplicar_lut, doc, arquivo, intensidade=intensidade, inicio=inicio, fim=fim, estado=est,
                  progresso=_progresso("aplicando LUT"))
    return _apos_edicao(v, doc)


@ferramenta
async def chroma_key(cor: str = "verde", fundo: str = "transparente", similaridade: float = 0.15,
                     suavidade: float = 0.08, remover_reflexo: bool = True) -> dict:
    """Tira o fundo de TELA VERDE/AZUL e põe outro fundo (nova versão).
    cor: 'verde' ou 'azul' (o tom exato da tela é medido nas bordas do vídeo), '#RRGGBB' ou 'auto'.
    fundo: caminho de imagem ou vídeo (vídeo curto repete; é ampliado para cobrir o quadro), uma cor
    ('black', '#203040'...) ou 'transparente' (gera versão .webm VP9 com canal alfa, para usar em outro editor).
    similaridade 0.01–0.5: quanto da cor vira fundo (aumente se sobrar verde; diminua se comer a pessoa);
    suavidade 0–0.5: borda suave; remover_reflexo: tira o verde refletido na pessoa (despill).
    Exemplos: "tira o fundo verde e põe a praia.jpg" → fundo='~/praia.jpg'; "ainda sobrou verde no cabelo" →
    desfazer e similaridade=0.22."""
    doc, _ = _doc()
    v = await _bg(ev.chroma_key, doc, cor=cor, similaridade=similaridade, suavidade=suavidade, fundo=fundo,
                  remover_reflexo=remover_reflexo, progresso=_progresso("recortando o fundo"))
    return _apos_edicao(v, doc)


@ferramenta
async def sobrepor_video(arquivos: list[str] | str, modo: str = "pip", posicao: str | list = "sup_dir",
                         escala: float = 0.3, inicio: _T = None, fim: _T = None, audio: str = "principal",
                         volume: float = 1.0, borda: int = 0, cor_borda: str = "white", cantos: float = 0,
                         ao_acabar: str = "sumir") -> dict:
    """Vídeo/imagem por cima (picture-in-picture) ou TELA DIVIDIDA (nova versão; a duração não muda).
    modo: 'pip' (janelinha), 'lado_a_lado', 'empilhado' (um em cima do outro) ou 'grade' (até 4+ vídeos).
    arquivos: um caminho ou lista (grade/lado a lado aceitam vários). pip: posicao ('sup_dir', 'inf_esq',
    'centro'... ou [x, y]), escala (fração da largura do quadro, 0.3), borda (px) com cor_borda, cantos
    (arredondamento 0–0.5), inicio/fim (quando aparece). ao_acabar (sobreposto mais curto): 'sumir', 'congelar',
    'repetir' ou 'preto'. audio: 'principal' (só o som do vídeo atual, intacto), 'mixar' (os dois) ou
    'sobreposto' (só o do sobreposto enquanto ele aparece); volume do sobreposto (1 = igual).
    Exemplos: "põe minha câmera.mp4 no canto inferior direito com bordas arredondadas" → arquivos='camera.mp4',
    posicao='inf_dir', cantos=0.15; "deixa os dois vídeos lado a lado com o som dos dois" →
    modo='lado_a_lado', audio='mixar'."""
    doc, est = _doc()
    v = await _bg(ev.sobrepor_video, doc, arquivos, modo=modo, posicao=posicao, escala=escala, inicio=inicio,
                  fim=fim, audio=audio, volume=volume, borda=borda, cor_borda=cor_borda, cantos=cantos,
                  ao_acabar=ao_acabar, estado=est, progresso=_progresso("sobrepondo"))
    return _apos_edicao(v, doc)


@ferramenta
async def zoom(modo: str = "aproximar", alvo: str | list | None = None, fator: float | None = None,
               inicio: _T = None, fim: _T = None, transicao_s: float | None = None, suave: bool = True) -> dict:
    """Zoom animado no vídeo (nova versão; mesmo tamanho de quadro, movimento suave sem tremor).
    modo: 'aproximar' (entra no zoom em transicao_s, segura e volta no fim da janela), 'punch' (corte seco para
    o zoom, estilo Shorts/Reels), 'ken_burns' (aproxima devagar durante a janela toda) ou 'afastar' (começa
    aproximado e abre). alvo: onde focar — nome ('centro', 'sup_esq', 'topo'...), [x, y] (centro) ou
    [x, y, largura, altura] (retângulo que deve encher o quadro; define o fator), em px ou frações 0–1.
    fator: 1.3 = 30% maior (padrão por modo). suave=False: rampa linear em vez de acelerar/desacelerar.
    Exemplos: "dá um zoom no rosto dele entre 0:10 e 0:15" → ver_quadros para achar o rosto, depois
    alvo=[x,y,l,a], inicio='0:10', fim='0:15'; "punch-in a cada marca" → modo='punch' em cada trecho;
    "efeito Ken Burns na foto" → modo='ken_burns'."""
    doc, est = _doc()
    v = await _bg(ev.zoom, doc, modo=modo, alvo=alvo, fator=fator, inicio=inicio, fim=fim, transicao_s=transicao_s,
                  suave=suave, estado=est, progresso=_progresso("aplicando zoom"))
    return _apos_edicao(v, doc)


@ferramenta
async def fade_video(entrada_s: float | None = None, saida_s: float | None = None, cor: str = "black",
                     audio: bool = True, inicio: _T = None, fim: _T = None) -> dict:
    """Fade da IMAGEM (nova versão): aparece a partir de `cor` durante entrada_s segundos (a partir de inicio,
    padrão o começo) e/ou some para `cor` durante saida_s terminando em fim (padrão o final). Antes de inicio e
    depois de fim a tela fica na cor. audio=True: o som acompanha o fade; False: áudio intacto.
    Exemplos: "fade de entrada e saída de 1 segundo" → entrada_s=1, saida_s=1; "termina com fade para branco
    de 2 s" → saida_s=2, cor='white'."""
    doc, est = _doc()
    v = await _bg(ev.fade_video, doc, entrada_s=entrada_s, saida_s=saida_s, cor=cor, audio=audio, inicio=inicio,
                  fim=fim, estado=est, progresso=_progresso("aplicando fade"))
    return _apos_edicao(v, doc)


@ferramenta
async def desenhar(elementos: list[dict] | dict) -> dict:
    """Desenha gráficos por cima do vídeo, todos numa versão só. Cada elemento é um objeto com "tipo":
    - "caixa": retangulo [x, y, largura, altura] (px ou frações 0–1), cor, espessura (px) ou preenchida=true
      (cor com transparência: 'black@0.5');
    - "barra_progresso": barra que enche ao longo do vídeo (ou de inicio a fim); posicao 'base'|'topo', cor,
      altura (px ou fração), fundo (cor do trilho ou null);
    - "cronometro": modo 'crescente' | 'regressivo' (contagem regressiva a partir de `de` segundos) |
      'relogio' (tempo do vídeo), formato 'mm:ss' | 'hh:mm:ss' | 'ss' | 'ss.d' | 'mm:ss.cc', posicao, tamanho,
      cor, caixa (fundo escuro), fonte.
    Todos aceitam inicio/fim. Exemplos: "contagem regressiva de 10 s no canto a partir do 0:05" →
    {"tipo":"cronometro","modo":"regressivo","de":10,"inicio":"0:05","fim":"0:15","posicao":"sup_dir"};
    "barra de progresso vermelha embaixo" → {"tipo":"barra_progresso","cor":"red"}."""
    doc, est = _doc()
    v = await _bg(ev.desenhar, doc, elementos, estado=est, progresso=_progresso("desenhando"))
    return _apos_edicao(v, doc)


@ferramenta
async def melhorar_video(reduzir_ruido: str | bool | None = None, nitidez: float | None = None,
                         desentrelacar: str | bool | None = None, deflicker: bool = False,
                         aumentar_resolucao: float | None = None, metodo_escala: str = "lanczos",
                         inicio: _T = None, fim: _T = None) -> dict:
    """Limpa/melhora a imagem numa versão só (combine o que precisar):
    reduzir_ruido: 'leve', 'media' ou 'forte' (forte = nlmeans, lento); nitidez 0–1;
    desentrelacar: 'auto' (só se detectar linhas de TV entrelaçada) ou true; deflicker: tira a cintilação de
    brilho (time-lapse, lâmpadas); aumentar_resolucao: fator 1.5–4 com metodo_escala 'lanczos' (vídeo comum),
    'bicubic', 'spline', 'pixel' (vizinho mais próximo, pixel art) ou 'xbr' (pixel art suavizado, 2–4×).
    Não inventa detalhe (não é IA): aumentar a resolução só deixa maior e mais nítido.
    Exemplos: "o vídeo está granulado/com ruído" → reduzir_ruido='media'; "vídeo de VHS com tracinhos" →
    desentrelacar='auto', reduzir_ruido='media'; "dobra a resolução desse jogo retrô" → aumentar_resolucao=2,
    metodo_escala='xbr'."""
    doc, est = _doc()
    v = await _bg(ev.melhorar_video, doc, reduzir_ruido=reduzir_ruido, nitidez=nitidez, desentrelacar=desentrelacar,
                  deflicker=deflicker, aumentar_resolucao=aumentar_resolucao, metodo_escala=metodo_escala,
                  inicio=inicio, fim=fim, estado=est, progresso=_progresso("melhorando a imagem"))
    return _apos_edicao(v, doc)


@ferramenta
async def mudar_fps(fps: str | float, modo: str = "duplicar") -> dict:
    """Muda a taxa de quadros sem mudar a duração nem o áudio (nova versão).
    fps: 24, 25, 30, 60, '29.97', '23.976', 'ntsc', 'pal', '30000/1001'...
    modo: 'duplicar' (repete/descarta quadros; rápido), 'mesclar' (mistura quadros vizinhos) ou 'interpolar'
    (cria quadros intermediários por movimento; muito mais fluido para subir o fps, mas lento).
    Exemplos: "converte para 24 fps" → fps=24; "deixa esse vídeo de 30 fps fluido em 60" → fps=60,
    modo='interpolar'."""
    doc, _ = _doc()
    v = await _bg(ev.mudar_fps, doc, fps, modo=modo, progresso=_progresso("mudando o fps"))
    return _apos_edicao(v, doc)


@ferramenta
async def cortar_bordas_pretas(limiar: float = 0.1, manter_tamanho: bool = False) -> dict:
    """Detecta e recorta TARJAS PRETAS (letterbox em cima/embaixo, pillarbox nas laterais) olhando vários pontos
    do vídeo (nova versão). limiar 0.01–0.5: até quanto de brilho conta como preto (suba para tarjas cinza-escuro).
    manter_tamanho=True amplia de volta ao tamanho original (corta um pouco das bordas da imagem).
    Exemplos: "tira essas faixas pretas" → padrão; "tira as faixas mas mantém 1920x1080" → manter_tamanho=True."""
    doc, _ = _doc()
    v = await _bg(ev.cortar_bordas_pretas, doc, limiar=limiar, manter_tamanho=manter_tamanho,
                  progresso=_progresso("recortando as bordas"))
    return _apos_edicao(v, doc)


@ferramenta
async def remover_logo(retangulo: list | str, inicio: _T = None, fim: _T = None) -> dict:
    """Apaga um logo/marca d'água FIXO interpolando os pixels em volta (delogo) (nova versão).
    retangulo: [x, y, largura, altura] em px ou frações 0–1 — use `ver_quadros` para localizar e deixe uma
    pequena folga em volta. Funciona bem em logos pequenos sobre fundo simples; em logo grande fica uma mancha
    borrada (para objetos que se mexem use `remover_objeto`).
    Exemplo: "tira o logo da emissora no canto superior direito" → retangulo=[0.85, 0.03, 0.12, 0.08]."""
    doc, est = _doc()
    v = await _bg(ev.remover_logo, doc, retangulo, inicio=inicio, fim=fim, estado=est,
                  progresso=_progresso("removendo o logo"))
    return _apos_edicao(v, doc)


@ferramenta
async def perspectiva(modo: str = "corrigir", cantos: list | None = None, yaw: float = 0, pitch: float = 0,
                      roll: float = 0, fov: float = 90, largura: int | None = None, altura: int | None = None) -> dict:
    """Corrige perspectiva ou converte vídeo 360° (nova versão).
    modo='corrigir': endireita algo filmado de lado (tela, quadro branco, documento): cantos = 4 pontos
    [[x,y] sup_esq, [x,y] sup_dir, [x,y] inf_esq, [x,y] inf_dir] em px ou frações → viram os cantos do quadro.
    modo='360': vídeo 360° equirretangular → vista normal olhando para yaw (graus, + = direita), pitch
    (+ = cima), roll, campo de visão fov; tamanho de saída largura x altura (padrão 1280x720).
    Exemplos: "endireita o slide filmado torto" → ver_quadros, achar os 4 cantos e passar cantos;
    "mostra o vídeo 360 olhando para trás" → modo='360', yaw=180."""
    doc, _ = _doc()
    v = await _bg(ev.perspectiva, doc, modo=modo, cantos=cantos, yaw=yaw, pitch=pitch, roll=roll, fov=fov,
                  largura=largura, altura=altura, progresso=_progresso("corrigindo a perspectiva"))
    return _apos_edicao(v, doc)


# ---------------------------------------------------------------- linha do tempo (as marcas são limpas)
@ferramenta
async def inverter_video(modo: str = "reverso", inicio: _T = None, fim: _T = None, audio: bool = True) -> dict:
    """Toca de TRÁS PARA FRENTE (nova versão): modo 'reverso' (o trecho volta ao contrário) ou 'boomerang'
    (o trecho toca normal e depois ao contrário, como no Instagram). Padrão: o vídeo todo; inicio/fim = só o
    trecho. audio=True inverte o som junto; False deixa o trecho invertido mudo. Trechos longos são feitos em
    pedaços (sem estourar a memória). As marcas são limpas; a resposta traz `trecho_novo`.
    Exemplos: "faz esse pulo voltar ao contrário" → inicio='m1', fim='m2'; "boomerang dos 2 primeiros
    segundos" → modo='boomerang', inicio=0, fim=2."""
    doc, est = _doc()
    v = await _bg(ev.inverter_video, doc, modo=modo, inicio=inicio, fim=fim, audio=audio, estado=est,
                  progresso=_progresso("invertendo"))
    _limpar_marcas()
    return _apos_edicao(v, doc)


@ferramenta
async def repetir(vezes: int | None = None, duracao_alvo_s: float | None = None, inicio: _T = None,
                  fim: _T = None) -> dict:
    """LOOP: repete um trecho (padrão: o vídeo todo) seguidas vezes, com som (nova versão).
    vezes: total de vezes que o trecho toca (2 = duas vezes); OU duracao_alvo_s: repete até o trecho repetido
    somar esse tempo (a última repetição é cortada no quadro exato). As marcas são limpas; a resposta traz
    `trecho_novo`.
    Exemplos: "repete essa parte 3 vezes" → vezes=3, inicio='m1', fim='m2'; "faz um loop desse GIF até dar
    30 segundos" → duracao_alvo_s=30."""
    doc, est = _doc()
    v = await _bg(ev.repetir, doc, vezes=vezes, duracao_alvo_s=duracao_alvo_s, inicio=inicio, fim=fim, estado=est,
                  progresso=_progresso("repetindo"))
    _limpar_marcas()
    return _apos_edicao(v, doc)


@ferramenta
async def camera_lenta_suave(fator: float = 0.5, inicio: _T = None, fim: _T = None, metodo: str = "interpolar",
                             audio: str = "esticar") -> dict:
    """Câmera lenta FLUIDA (nova versão): cria quadros intermediários em vez de repetir quadros (diferente de
    `velocidade_trecho`, que repete). fator 0.1–0.9 (0.5 = metade da velocidade). metodo: 'interpolar'
    (por movimento; o mais fluido, mas lento — ~10–20× o tempo do trecho em 720p — e pode deformar bordas de
    objetos rápidos), 'mesclar' (rápido, rastro suave) ou 'duplicar'. audio: 'esticar' (mais lento, mesmo tom)
    ou 'mudo'. As marcas são limpas; a resposta traz `trecho_novo`.
    Exemplos: "câmera lenta bem suave no gol" → inicio='m1', fim='m2', fator=0.4; "ficou deformado" → desfazer
    e metodo='mesclar'."""
    doc, est = _doc()
    v = await _bg(ev.camera_lenta_suave, doc, fator=fator, inicio=inicio, fim=fim, metodo=metodo, audio=audio,
                  estado=est, progresso=_progresso("câmera lenta"))
    _limpar_marcas()
    return _apos_edicao(v, doc)
