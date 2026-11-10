"""Ferramentas MCP: efeitos e tratamento de áudio (equalizador, efeitos de voz, tom, dinâmica, canais, sincronia, censura de palavras, loudness).

Registradas no mesmo servidor (`mcp_server.mcp`) ao serem importadas no fim de `mcp_server.py`.
A lógica (ffmpeg, sem Qt) fica em módulos próprios; aqui só a interface MCP.
"""
from __future__ import annotations

from . import efeitos_audio as ea
from .mcp_server import _apos_edicao, _bg, _doc, _progresso, ferramenta  # noqa: F401

# Todas as edições abaixo criam uma VERSÃO nova (desfazer volta), mexem só na faixa escolhida (as outras faixas e o
# vídeo são copiados sem perda) e mantêm a sincronia com o vídeo: o atraso de cada filtro é medido e compensado.


@ferramenta
async def equalizar(preset: str | None = None, graves_db: float | None = None, medios_db: float | None = None,
                    agudos_db: float | None = None, bandas: list[dict] | None = None, faixa: int = 1,
                    inicio: str | float | None = None, fim: str | float | None = None) -> dict:
    """EQUALIZADOR de uma faixa de áudio (nova versão). Combine à vontade preset + ajustes + bandas.
    preset: 'voz' (voz mais clara), 'podcast' (encorpado e claro), 'mais_graves', 'menos_graves', 'mais_agudos',
      'menos_agudos', 'mais_medios', 'menos_medios', 'clareza', 'abafado' (atrás da parede), 'telefone'
      (300–3400 Hz), 'sem_ronco' (corta < 100 Hz: vento, batidas, ronco), 'sem_chiado' (abaixa agudos),
      'sem_zumbido_60' / 'sem_zumbido_50' (zumbido da rede elétrica 60 Hz no Brasil/EUA, 50 Hz na Europa, com
      harmônicos).
    graves_db (prateleira em 100 Hz), medios_db (1 kHz, 2 oitavas), agudos_db (prateleira em 4 kHz): −24 a +24 dB.
    bandas: ajuste fino [{"freq": 250, "ganho": -4, "largura": 1}] (Hz, dB, largura em oitavas).
    Reforços ganham um limitador no fim (não estoura). faixa: 1 = A1. inicio/fim: só nesse trecho (rampas de 30 ms).
    Exemplos: "deixa a voz mais clara" → preset='voz'; "mais grave" → graves_db=6; "tira esse zumbido" →
    preset='sem_zumbido_60'; "som abafado do m1 ao m2" → preset='abafado', inicio='m1', fim='m2';
    "corta 250 Hz" → bandas=[{"freq":250,"ganho":-6}]."""
    doc, est = _doc()
    v = await _bg(ea.equalizar, doc, preset=preset, graves_db=graves_db, medios_db=medios_db, agudos_db=agudos_db,
                  bandas=bandas, faixa=faixa, inicio=inicio, fim=fim, estado=est, progresso=_progresso("equalizando"))
    return _apos_edicao(v, doc)


@ferramenta
async def efeito_voz(efeito: str, intensidade: str | float | None = None, faixa: int = 1,
                     inicio: str | float | None = None, fim: str | float | None = None) -> dict:
    """EFEITOS DE VOZ criativos numa faixa (nova versão; duração e sincronia mantidas).
    efeito: 'eco' (repetições, montanha), 'reverb' (sala), 'banheiro' (sala pequena), 'caverna' (caverna/igreja),
      'estadio' (estádio/ginásio), 'robo' (voz robótica), 'telefone' (ligação), 'radio' (rádio antigo/walkie-talkie),
      'megafone' (alto-falante estridente), 'esquilo' (voz fina de desenho), 'monstro' (voz grave de vilão),
      'sussurro' (só o ar, sem tom), 'subaquatico' (debaixo d'água).
    intensidade: 'leve', 'media' (padrão), 'forte' ou 0–1. faixa: 1 = A1.
    inicio/fim: só nesse trecho — o eco/reverb continua soando um pouco depois do fim do trecho, como num estúdio.
    Para mudar só a afinação em semitons use `mudar_tom`; para equalização fina, `equalizar`.
    Exemplos: "põe um eco na voz" → efeito='eco'; "voz de robô do m1 ao m2" → efeito='robo', inicio='m1', fim='m2';
    "parece que está numa igreja" → 'caverna'; "voz de telefone mais forte" → 'telefone', intensidade='forte'."""
    doc, est = _doc()
    v = await _bg(ea.efeito_voz, doc, efeito, intensidade=intensidade, faixa=faixa, inicio=inicio, fim=fim,
                  estado=est, progresso=_progresso(f"efeito de voz {efeito}"))
    return _apos_edicao(v, doc)


@ferramenta
async def mudar_tom(semitons: float, manter_velocidade: bool = True, preservar_formantes: bool = False,
                    faixa: int = 1, inicio: str | float | None = None, fim: str | float | None = None) -> dict:
    """Muda o TOM (afinação) de uma faixa sem mudar a duração nem a sincronia (nova versão).
    semitons: −24 a +24 (12 = uma oitava; +2 = um tom acima; −3 = voz mais grave). Usa o rubberband (alta
    qualidade); sem ele no ffmpeg, cai para asetrate + atempo (avisa nos detalhes).
    preservar_formantes=True: muda a nota mas mantém o timbre (voz cantada/falada soa natural, sem "esquilo").
    manter_velocidade=False: efeito de fita (tom e velocidade juntos; muda a duração) — só em arquivo de ÁUDIO
      sem vídeo; em vídeo use `velocidade_trecho(manter_tom=False)`.
    faixa: 1 = A1. inicio/fim: só nesse trecho.
    Exemplos: "sobe a música meio tom" → semitons=1; "deixa a voz mais grave" → semitons=-3,
    preservar_formantes=True; "transpõe 2 tons abaixo" → semitons=-4; "disfarça a voz" → semitons=-5."""
    doc, est = _doc()
    v = await _bg(ea.mudar_tom, doc, semitons, manter_velocidade=manter_velocidade,
                  preservar_formantes=preservar_formantes, faixa=faixa, inicio=inicio, fim=fim, estado=est,
                  progresso=_progresso("mudando o tom"))
    return _apos_edicao(v, doc)


@ferramenta
async def dinamica_audio(modo: str, intensidade: str | float | None = None, faixa: int = 1,
                         inicio: str | float | None = None, fim: str | float | None = None) -> dict:
    """DINÂMICA e restauração de uma faixa (nova versão).
    modo: 'nivelar_fala' (iguala o volume da fala: partes baixas sobem, gritos descem — speechnorm),
      'nivelar_suave' (nivelamento lento para música/ambiente/entrevista — dynaudnorm),
      'compressor' (som mais cheio e constante), 'limitador' (segura picos abaixo de −1 dBFS; intensidade
      'media'/'forte' também sobe o volume +3/+6 dB antes), 'deesser' (suaviza "S" sibilantes),
      'gate' (silencia o ruído de fundo nas pausas; mede sozinho o nível da voz e do ruído),
      'remover_cliques' (estalos, cliques de boca, crepitação de vinil), 'remover_distorcao' (áudio
      estourado/clipado; lento).
    intensidade: 'leve', 'media' (padrão), 'forte' ou 0–1. faixa: 1 = A1. inicio/fim: só nesse trecho.
    Para volume-alvo de plataforma (LUFS) use `normalizar_volume`; ruído constante de fundo: `reduzir_ruido`.
    Exemplos: "tem partes que eu falo baixo e outras alto" → 'nivelar_fala'; "o S está chiando" → 'deesser';
    "tira o barulho entre as falas" → 'gate'; "o áudio estourou" → 'remover_distorcao';
    "tem uns estalos" → 'remover_cliques'."""
    doc, est = _doc()
    v = await _bg(ea.dinamica_audio, doc, modo, intensidade=intensidade, faixa=faixa, inicio=inicio, fim=fim,
                  estado=est, progresso=_progresso(f"dinâmica: {modo}"))
    return _apos_edicao(v, doc)


@ferramenta
async def canais_audio(acao: str, intensidade: str | float | None = None, faixa: int = 1,
                       inicio: str | float | None = None, fim: str | float | None = None) -> dict:
    """CANAIS (esquerdo/direito) de uma faixa (nova versão).
    acao: 'mono' (junta em um canal), 'estereo' (mono → estéreo), 'trocar' (esquerdo ↔ direito),
      'so_esquerdo' / 'so_direito' (usa um canal só, nos dois lados — microfone de lapela gravado num lado só),
      'separar' (esquerdo e direito viram DUAS faixas mono: A_n = esquerdo, faixa nova = direito; ex. entrevista
      com um microfone em cada canal), 'ampliar_estereo' / 'estreitar_estereo' (largura), 'realcar_centro'
      (realça a voz/diálogo do centro do estéreo), 'remover_centro' (karaokê: tira o que está no centro, em geral
      a voz principal), 'inverter_fase' (polaridade invertida).
    intensidade: para ampliar/estreitar/realçar ('leve'/'media'/'forte' ou 0–1). faixa: 1 = A1.
    inicio/fim: só em ações que não mudam o nº de canais (trocar, so_esquerdo, ampliar...).
    Exemplos: "o som só sai de um lado" → acao='so_esquerdo' (ou 'so_direito', veja em analisar_audio);
    "deixa mono" → 'mono'; "separa os dois microfones" → 'separar'; "quero a música sem voz" → 'remover_centro'."""
    doc, est = _doc()
    v = await _bg(ea.canais_audio, doc, acao, intensidade=intensidade, faixa=faixa, inicio=inicio, fim=fim,
                  estado=est, progresso=_progresso(f"canais: {acao}"))
    return _apos_edicao(v, doc)


@ferramenta
async def sincronizar_audio(deslocamento_ms: float, faixa: int = 1) -> dict:
    """Corrige DESSINCRONIA entre som e imagem (lip sync) movendo uma faixa de áudio no tempo (nova versão).
    deslocamento_ms > 0: ATRASA o áudio (use quando o som vem ANTES da boca mexer);
    deslocamento_ms < 0: ADIANTA o áudio (use quando o som vem DEPOIS da boca/do impacto).
    Exato na amostra; o vídeo e as outras faixas não mudam; a faixa mantém início e duração (silêncio onde o som
    saiu, corte do que passou do fim). faixa: 1 = A1.
    Dica: para achar o valor, `ver_trecho` num momento de batida/palma/fala e compare; valores típicos 40–300 ms.
    Exemplos: "o áudio está adiantado uns 100 ms" → deslocamento_ms=100; "o som está atrasado, chega depois da
    boca" → deslocamento_ms=-150."""
    doc, _ = _doc()
    v = await _bg(ea.sincronizar_audio, doc, deslocamento_ms, faixa=faixa,
                  progresso=_progresso("sincronizando o áudio"))
    return _apos_edicao(v, doc)


@ferramenta
async def censurar_palavras(palavras: list[str] | None = None, trechos: list[list[str | float]] | None = None,
                            som: str = "bip", faixa: int = 1, idioma: str = "auto", modelo: str = "base",
                            margem_ms: float = 60, limiar: float = 0.8, volume_bip_db: float = -12) -> dict:
    """CENSURA: troca palavrões/nomes/palavras por BIPE (1 kHz) ou SILÊNCIO (nova versão).
    palavras: lista de palavras ou frases a esconder; elas são achadas na fala com o tempo de cada palavra
      (transcrição 100% local com whisper, em cache — a 1ª vez transcreve e pode baixar o modelo). Busca aproximada:
      ignora acento/maiúscula; limiar 0.8 (baixe para ~0.7 se não achar).
    trechos: [[inicio, fim], ...] manuais (segundos, '1:23', 'm1'...), além ou no lugar das palavras.
    som: 'bip' (padrão) ou 'silencio'. margem_ms: folga antes/depois de cada palavra (60 ms).
    volume_bip_db: pico do bipe (−12 dBFS). idioma: 'pt', 'en'... ('auto' detecta). faixa: 1 = A1.
    A resposta lista `ocorrencias` (palavra, o que foi ouvido, início, fim) — mostre ao usuário. Se nada for
    achado, não cria versão (`sem_edicao`). Sem o whisper instalado, o erro explica como instalar.
    Exemplos: "bipa os palavrões 'porra' e 'caralho'" → palavras=['porra','caralho']; "tira o nome 'João'" →
    palavras=['João'], som='silencio'; "põe um bipe de 2:10 a 2:11" → trechos=[['2:10','2:11']]."""
    doc, est = _doc()
    v = await _bg(ea.censurar_palavras, doc, palavras=palavras, trechos=trechos, som=som, faixa=faixa, idioma=idioma,
                  modelo=modelo, margem_ms=margem_ms, limiar=limiar, volume_bip_db=volume_bip_db, estado=est,
                  progresso=_progresso("censurando"))
    if v.get("sem_edicao"):
        return v
    r = _apos_edicao(v, doc)
    r["ocorrencias"] = v.get("ocorrencias", [])
    return r


@ferramenta
async def normalizar_volume(alvo: str | float = "youtube", pico_db: float | None = None, faixa: int = 1) -> dict:
    """NORMALIZA O VOLUME para um alvo de loudness (LUFS, EBU R128) com precisão (nova versão): mede, aplica
    ganho linear (não muda a dinâmica) e só limita os picos se passariam do teto (limitador de pico verdadeiro);
    confere o resultado. A resposta traz `detalhes.antes` e `detalhes.depois` (I = LUFS integrado, TP = pico
    verdadeiro dBTP, LRA = faixa de loudness).
    alvo: 'youtube'/'spotify'/'tiktok'/'instagram' (−14), 'podcast'/'apple_podcasts'/'apple_music' (−16),
      'audiolivro' (−18), 'tv'/'ebu' (−23, EBU R128), 'tv_eua'/'atsc' (−24), 'cinema'/'netflix' (−27) ou número
      em LUFS (ex. -16). pico_db: teto do pico verdadeiro (padrão do alvo: −1; −2 em tv_eua/cinema; −3 audiolivro).
    faixa: 1 = A1. Vale para a faixa inteira (para subir/baixar um trecho use `ajustar_audio`).
    Exemplos: "normaliza para o YouTube" → alvo='youtube'; "volume padrão de podcast" → 'podcast';
    "deixa em -16 LUFS" → alvo=-16; "está muito baixo" → alvo='youtube'."""
    doc, _ = _doc()
    v = await _bg(ea.normalizar_volume, doc, alvo=alvo, pico_db=pico_db, faixa=faixa,
                  progresso=_progresso("normalizando o volume"))
    return _apos_edicao(v, doc)


@ferramenta
async def inverter_audio(faixa: int = 1, inicio: str | float | None = None, fim: str | float | None = None) -> dict:
    """Toca o ÁUDIO de trás para frente (efeito "fita ao contrário"; nova versão). Só o áudio: a imagem continua
    igual. Para inverter imagem e som juntos, use `inverter_video`.
    faixa: 1 = A1. inicio/fim: só nesse trecho (recomendado; o trecho invertido fica no mesmo lugar).
    Faixa inteira só até ~15–30 min (o trecho invertido fica na memória).
    Exemplos: "inverte o áudio do m1 ao m2" → inicio='m1', fim='m2'; "mensagem ao contrário em 0:05–0:08"."""
    doc, est = _doc()
    v = await _bg(ea.inverter_audio, doc, faixa=faixa, inicio=inicio, fim=fim, estado=est,
                  progresso=_progresso("invertendo o áudio"))
    return _apos_edicao(v, doc)


@ferramenta
async def mixar_faixas(faixas: list[int] | None = None, volumes_db: list[float] | None = None,
                       destino: str = "nova", titulo: str | None = None) -> dict:
    """JUNTA (mixa) várias faixas de áudio DO ARQUIVO numa só (nova versão), ex. voz em A1 + música em A2 → uma
    faixa para publicar (redes sociais e muitos players só tocam a primeira faixa).
    faixas: [1, 2] (padrão: todas). volumes_db: ganho de cada uma na mistura, na mesma ordem (ex. [0, -8] = música
    8 dB mais baixa). Soma sem normalizar + limitador (não estoura); cada faixa entra no seu tempo.
    destino: 'nova' (padrão: a mistura vira uma faixa nova no fim; as originais ficam) ou 'substituir' (a mistura
    entra no lugar da 1ª faixa citada e as outras citadas saem do arquivo). titulo: nome da faixa resultante.
    Não confundir com `mixar` (só muda como as faixas TOCAM na janela, sem alterar o arquivo) nem com
    `musica_fundo` (traz uma música de um arquivo externo).
    Exemplos: "junta as duas faixas numa só" → mixar_faixas(destino='substituir'); "mixa a música mais baixa" →
    volumes_db=[0, -10]."""
    doc, _ = _doc()
    v = await _bg(ea.mixar_faixas, doc, faixas=faixas, volumes_db=volumes_db, destino=destino, titulo=titulo,
                  progresso=_progresso("mixando as faixas"))
    return _apos_edicao(v, doc)
