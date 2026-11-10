# AI Editor — plano e especificação

> Documento vivo. Aqui fica o spec do projeto: o que é, como funciona, o que já existe e o que vem a seguir.

## 1. Visão

Player e editor de vídeo/áudio **leve**, para Linux, controlado em linguagem natural pelo Claude via **MCP**.
A pessoa assiste ao vídeo numa janela simples e pede no chat: *"corta do 12,5 s ao 18 s e baixa o volume da faixa 2"*.
O Claude executa as edições (ffmpeg + IAs locais), e o player recarrega o resultado na hora.

Princípios:
- **Leve**: nada de editor pesado; a janela só mostra, o trabalho pesado roda sob demanda.
- **Local e offline**: tudo roda na máquina. Internet só para o Claude e para baixar modelos de IA uma vez.
- **Não destrutivo**: toda edição gera uma versão temporária; só "Salvar" substitui o original.
- **Distribuível**: um único AppImage, sem exigir Python, ffmpeg ou bibliotecas instaladas.
- **Open source**: GPL-3.0.

## 2. Estado atual (v0 — importado de `~/claude-player`, renomeado para `ai_editor`)

### 2.1 Arquitetura

```
Claude (Code/Desktop) ──stdio/MCP──> claude-player-mcp ──socket Unix (JSON lines)──> janela (Qt + libmpv)
                                          │
                                          └──> ffmpeg / ffprobe / mediainfo / exiftool
```

| Módulo | Papel |
|---|---|
| `app.py` | Janela PySide6 + libmpv: vídeo, controles, régua com zoom, uma onda por faixa de áudio, mix (mudo/solo/volume), N marcas com trechos coloridos, zoom/arraste no vídeo, servidor de controle por socket |
| `core.py` | Núcleo: tempos, chamadas ao ffmpeg, versões (`Documento`), cortes precisos, filtros de áudio, faixas, metadados completos |
| `ondas.py` | Picos de áudio (200/s) com cache em `~/.cache/claude-player/ondas` |
| `cliente.py` | Cliente do socket (`$XDG_RUNTIME_DIR/claude-player/controle.sock`) |
| `mcp_server.py` | Servidor MCP com 21 ferramentas; abre a janela se ela não estiver aberta |
| `exportacao.py` | Painel de exportação da janela (vidro, como o de metadados): predefinição, trecho, limite em MB, destino, progresso |

### 2.2 Ferramentas MCP existentes
`abrir`, `estado`, `controlar`, `marcar`, `mixar`, `ver_quadros`, `ver_trecho`, `metadados`, `analisar_audio`,
`cortar`, `remover_trechos`, `montar_trechos`, `ajustar_audio`, `faixa_audio`, `ffmpeg_livre`,
`versoes`, `desfazer`, `descartar_versoes`, `salvar`, `diagnostico`, `exportar`.

`exportar(predefinicao | formato, inicio/fim | trechos, tempo, destino, alvo_mb, largura, altura, qualidade, fps,
sem_audio, faixa, enquadramento, arquivo)`: cria um arquivo novo (não é versão; não recarrega a janela) a partir da
última versão do documento. Predefinições em `core.PREDEFINICOES` (tabela de dados): `whatsapp`, `youtube`,
`instagram`/`reels`, `mp4`, `menor` (H.265; `menor_vp9` se faltar libx265), `webm`, `transparente` (VP9 yuva420p),
`transparente_mov`/`prores` (ProRes 4444), `gif`, `mp3`, `m4a`, `opus`, `ogg`, `wav`, `flac`, `png`, `jpg`.
A foto de quadro é a predefinição `png`/`jpg` com `tempo` (sem ferramenta separada). A barra de status da janela
mostra o progresso e o caminho final.

Edições de imagem (Fase 3; cada uma cria uma versão e recarrega a janela; tempos no formato de `parse_tempo`):
- `transformar_imagem(recorte | proporcao+posicao, girar, modo_giro, cor, espelhar, largura, altura, escala)`:
  recorte → giro → espelho → tamanho numa versão só.
- `mudar_proporcao(proporcao, modo=desfocado|barras|recorte, cor, largura, altura, posicao)`.
- `ajustar_cor(brilho, contraste, saturacao, gama, temperatura, inicio, fim)`.
- `estabilizar(suavizacao, bordas=zoom|preto|manter, tremor, metodo=vidstab|deshake)`.
- `velocidade_trecho(fator, inicio, fim, manter_tom, silenciar)` e `congelar_quadro(tempo, duracao_s,
  modo=inserir|substituir)`: mudam a linha do tempo (limpam as marcas).
- `sobrepor_texto(texto, posicao, tamanho, cor, caixa, cor_caixa, contorno, cor_contorno, fonte, inicio, fim)` e
  `marca_dagua(imagem, posicao, escala, opacidade, margem, inicio, fim)`.
- `borrar_area(retangulos | trajetoria, modo=borrar|pixelar, intensidade, inicio, fim)`.

Edições de áudio (Fase 4; cada uma cria uma versão; faixas numeradas a partir de 1; resposta traz `detalhes`
com o que foi medido):
- `reduzir_ruido(metodo=afftdn|anlmdn|rnnoise, intensidade=leve|media|forte|dB, faixa, inicio, fim, piso_ruido_db)`.
- `remover_silencios(modo=cortar|analisar, limiar_db (padrão automático), duracao_min_s, margem_s, faixa, inicio, fim)`:
  corta vídeo e todas as faixas juntos; `analisar` só lista (para `marcar` na régua).
- `fade_audio(entrada_s, saida_s, faixas, video, cor, curva)`: fade no começo/fim; `video=True` escurece a imagem junto.
- `musica_fundo(arquivo, modo=mixar|trocar|nova_faixa, faixa, volume_db, loop, fade_in_s, fade_out_s, inicio, fim,
  musica_inicio, ducking, ducking_db, titulo)`.
- Extrair só o áudio: `exportar` com predefinição `mp3`/`m4a`/`opus`/`ogg`/`wav`/`flac` (ou `faixa_audio(acao='extrair')`).

### 2.3 Regras de versões
- Cada edição grava `vNNN.ext` em `.<arquivo>.versoes/` ao lado do original, com `historico.json`.
- **Salvar**: a última versão vira o original; o original vai para a Lixeira e as versões são apagadas.
- **Desfazer** volta uma versão; **Descartar** apaga todas.

### 2.4 Interface
- Ícones Phosphor (QtAwesome); variável `CLAUDE_PLAYER_ICONES=mdi6` volta ao Material.
- Barra de progresso com precisão de milissegundos, zoom/pan na régua.
- Marcas: botão de seta (ou tecla I) marca N pontos; trechos entre marcas consecutivas ganham cores em ciclo (amarelo, ciano, rosa, verde, laranja, violeta). O Claude lê as marcas pelo `estado` e usa `m1`, `m2`... como tempos. Botão direito na régua adiciona/remove.
- Roda do mouse no vídeo = zoom no ponto; arrastar = mover; duplo clique = 100%.
- Arquivos só de áudio escondem a área de vídeo e aumentam as ondas.

### 2.5 Limitações conhecidas
- Rodando do código-fonte, depende de Python, libmpv, ffmpeg, mediainfo e exiftool do sistema (o pacote vai levar tudo; ver seção 5).
- Zoom/arraste do vídeo ainda não validado com mouse real.
- Wayland ainda não testado numa sessão Wayland real.

## 3. Roadmap

Ordem aprovada em 2026-11-10: primeiro o que o usuário mais usa no dia a dia, depois IA, por último a distribuição.
Convenção: **editar** cria versões temporárias do mesmo arquivo (o original só muda com `salvar`);
**exportar** cria um arquivo novo ao lado do original e não mexe em nada.

### Fase 1 — Base (Linux)
- [x] Renomear pacote/comandos para `ai-editor` / `ai-editor-mcp`.
- [x] Modo dev: instância separada (`ai-editor-dev`) com recarga automática ao salvar código.
- [x] Trocar o embutimento por `wid` pela **render API do libmpv** num `QOpenGLWidget` (X11 + Wayland, mouse nativo do Qt). VAAPI com interop confirmado em X11.
- [x] Localizar binários: variável `AI_EDITOR_<NOME>` → pacote → sob demanda → sistema (`binarios.py`), com `ai-editor --diagnostico` e ferramenta MCP `diagnostico`.
- [x] Lixeira própria no padrão freedesktop (`lixeira.py`), sem depender do `gio`.
- [x] Modal de metadados (botão ⓘ / Ctrl+I) com copiar tudo; C2PA via c2patool.
- [x] Marcas N na régua (m1, m2…) com trechos coloridos visíveis para o Claude.
- [x] Corrigir `INVALID_ENUM` do OpenGL (religar o FBO do widget após o render do mpv).

### Fase 2 — Exportar / converter
- [x] `exportar(formato, predefinicao, trecho?)`: arquivo novo ao lado do original (`video.whatsapp.mp4`,
  `video.whatsapp.2.mp4`...; quando a predefinição é a própria extensão fica `video.mp3`, `video.gif`).
  Nunca sobrescreve (grava `.nome.parcial.ext` e publica com `link` atômico). Parte da última versão do documento.
- [x] Predefinições: WhatsApp (720p, CRF 26 com teto VBV para caber em 16 MB quando o bitrate necessário ≥ 400 kbps),
  YouTube, Instagram/Reels (1080x1920; horizontal → fundo desfocado por padrão, ou `recorte`/`barras`), GIF
  (palettegen/paletteuse, 480 px, 12,5 fps = atraso exato de 8 cs), MP3/M4A/Opus/OGG/WAV/FLAC, WebM, "menor tamanho"
  (H.265 CRF 28), "até X MB" (`alvo_mb`: 2 passes no vídeo, logs em `~/.cache/ai-editor/2passes-*` apagados ao fim;
  CBR no áudio com taxa padrão do LAME).
- [x] Exportar só um trecho ou vários trechos emendados (corte exato, mesmo esquema de `_cmd_preciso`).
- [x] Exportar com transparência (WebM VP9 `yuva420p` / ProRes 4444 `yuva444p10le`). Testado com fonte com alfa 40%:
  alfa preservado nos dois. Fonte sem alfa sai opaca (o alfa de verdade virá da Fase 7).
- [x] Foto de um quadro (PNG/JPG): quadro exato (busca precisa meio quadro antes do quadro n; conferido por md5
  contra `select=eq(n,N)`).
- [x] Botão de exportar na janela (Ctrl+E), painel com predefinição, trecho (inteiro / m1→mN / cada trecho colorido),
  tamanho máximo, destino e progresso; roda em `QThreadPool` com cancelar. Comando de socket `exportar`
  (`acao`: abrir | iniciar | cancelar | fechar | estado).
- Limitações: progresso do GIF fica parado até o fim (o `palettegen` precisa ver o vídeo todo); `alvo_mb` costuma
  ficar entre 93% e 100% do alvo (x265/VP9 com margem 0,97; trechos curtos em VP9 ficam mais abaixo); áudio de várias faixas: whatsapp/youtube/instagram exportam
  só a A1 (`faixa=N` escolhe), mp4/webm/menor levam todas; sem mixagem das faixas na exportação.

### Fase 3 — Imagem
Tudo como **versões** (mesmo mecanismo das edições existentes). Vídeo re-encodado com `codec_video` (x264 CRF 18
`veryfast`, ou VP9 em `.webm`); origem de 10 bits ou HEVC sai em x265 (10 bits mantidos, `hvc1` em mp4) com as
tags de cor (primárias/transferência/matriz/faixa: HDR continua HDR) e aviso na resposta. Dimensões sempre pares
(`setsar=1`), coordenadas no vídeo como é exibido (rotação de metadado aplicada; pixels não quadrados — SAR ≠ 1,
ex. DVD 720x576 64:45 — normalizados para 1024x576 no início do grafo). Edições que não mudam o tempo copiam
áudio, legendas e anexos do MKV (fontes das legendas ASS) sem perda (tags mantidas); mp4 com `+faststart`.
Código em `core.py`, seção "imagem (Fase 3)".
- [x] Recortar área (crop), girar, espelhar, mudar resolução → `transformar_imagem`. Recorte em px ou frações 0–1,
  ou por proporção com posição; giro 90/180/270 por `transpose`, ângulo livre com `rotate` em 3 modos
  (`expandir`/`manter`/`recorte`, este com zoom mínimo calculado para não sobrar canto).
- [x] Mudar proporção → `mudar_proporcao` (`desfocado`/`barras`/`recorte`), reaproveita `_cadeia_video` da
  exportação (ganhou o parâmetro `cor` para as barras). Sem tamanho dado, mantém o lado maior (1280x720 → 720x1280);
  `recorte` sem tamanho mantém os pixels originais.
- [x] Brilho, contraste, saturação, gama, temperatura → `ajustar_cor` (`eq` + `colortemperature`, com fallback
  `colorbalance`), opcionalmente só num trecho (`enable=between(t,a,b)`).
- [x] Estabilizar → `estabilizar`: vid.stab em 2 passes (`vidstabdetect` → `.trf` temporário → `vidstabtransform`
  + `unsharp`); sem vid.stab, `deshake` (com `bordas='zoom'`: `crop` de 16 px + `scale` de volta). Teste com vídeo tremido sintético: diferença média entre quadros
  8,2 → 1,5 (vid.stab) e 6,1 (deshake).
- [x] Câmera lenta / acelerar num trecho → `velocidade_trecho` (uma entrada `-ss/-t` por segmento, filtros de
  `core._segmentos_av`, os mesmos do corte preciso — sem `split`, que acumularia quadros na memória; `setpts` +
  `atempo` encadeado em estágios 0,5–2; `manter_tom=False` usa `asetrate`). Congelar quadro →
  `congelar_quadro` (`inserir`: pausa com áudio em silêncio — a própria faixa com `volume=0,apad`, não `anullsrc`,
  que quebra com layouts "unknown"; `substituir`: quadro parado por cima, áudio/legendas/anexos copiados).
  Sincronia (2026-11-10, revisão): sem `PTS-STARTPTS` por faixa (preserva vídeo que começa depois do áudio); fim de
  cada segmento por `trim`/`atrim` e não pelo `-t` (que conta do 1º quadro de cada faixa); concat de vídeo e de
  áudio separados — áudio com duração exata (`aresample first_pts=0` + `apad`), vídeo na grade `fps` com
  exatamente round(T_fim·fps) − round(T_ini·fps) quadros (o concat estimava o fim do vídeo como
  último pts × n/(n−1) e cada trecho ganhava até 1 quadro; em 29,97 fps 20 trechos de 24 quadros davam 16,6 s em
  vez de 16,02). Erro ≤ ½ quadro, sem acumular. Layouts 5.1(side)/"unknown" renomeados para o padrão
  (`channelmap`) antes do concat. Pega o quadro exibido no tempo pedido, descontando o atraso do vídeo no contêiner (MKV com
  vídeo em 0,021 s). Conferido pelo contador do testsrc e pela frequência do tom (330 Hz mantido / 165 Hz sem tom).
- [x] Texto → `sobrepor_texto` (`drawtext`, `expansion=none`, escape em 2 níveis em `core.esc_filtro`: aspas,
  dois-pontos, vírgula, colchetes, `;`, `\` e `%` testados na imagem; fonte DejaVu Sans via `fc-match`; reduz o
  tamanho padrão se o texto não couber; `text_align` em várias linhas quando o ffmpeg tem, 6.1+; `\n` literal vira
  quebra de linha; fonte pedida que não existe → erro, em vez de cair no padrão do fontconfig).
  Marca d'água → `marca_dagua` (PNG com alfa, escala, opacidade via `colorchannelmixer`, intervalo; nome com `%`
  é copiado para um temporário, senão o demuxer de imagem o leria como sequência).
- [x] Borrar/pixelar área → `borrar_area` (`crop` + `gblur`/`pixelize` + `overlay` com `enable`); vários retângulos
  parados ou uma `trajetoria` com pontos (centro interpolado linearmente por expressão em `t` no `crop` e no
  `overlay`; tamanho = o maior dos pontos).
- Limitações: câmera lenta duplica quadros (sem interpolação de movimento, `minterpolate` seria pesado demais);
  velocidade e congelar `inserir` descartam legendas embutidas e capítulos (avisam) e re-encodam todo o áudio; a trajetória
  do borrão é linear por partes e de tamanho fixo (sem rastreamento automático — fica para a Fase 7);
  marca d'água só imagem estática (GIF/vídeo como marca não); `colortemperature` é lento (~0,5× tempo real em
  720p); `eq` e `deshake` trabalham em 8 bits (em origem de 10 bits avisam de possível banding); segmentos de
  linha do tempo com menos de ~1,5 quadro perdem esse quadro (estimativa do concat); `vidstab` leva ~2,5× a duração do vídeo. Filtros usados registrados em `binarios.RECURSOS_FFMPEG`
  (`drawtext`, `vidstabdetect/transform`, `deshake`, `colortemperature`); vid.stab é GPL-2.0+ (ok).

### Fase 4 — Áudio prático
Só com o ffmpeg (nada baixado). Código em `core.py`, seção "áudio (Fase 4)"; helper `_editar_audio` (vídeo e faixas
não tocadas copiados sem perda, faixa editada re-encodada com `codec_audio` e tags reaplicadas). Medições por janelas
de 50 ms do `astats` (`niveis_audio`, p10 = piso, p80 = fala).
- [x] Redução de ruído → `reduzir_ruido`. `afftdn` (padrão): acha a pausa mais quieta (≥ 0,3 s, ≤ p10+3 dB, quando há
  ≥ 10 dB de contraste fala/pausa), coloca esse trecho **antes** do áudio (`concat` com uma 2ª entrada `-ss/-t`) para o
  filtro aprender o perfil (`asendcmd` → `sn start/stop`) e corta de volta (`atrim`); `nf` = ruído medido + 18 dB
  (calibrado: com o nf default −50 o afftdn quase não reduz, nem com perfil). Sem pausas, estima pelo p10.
  Intensidade = `nr` (leve 10, media 20, forte 32 dB). `anlmdn` como alternativa (força 1/10/158). `rnnoise`:
  ponto de extensão (`arnndn`, 48 kHz) que usa o primeiro `~/.local/share/ai-editor/modelos/rnnoise/*.rnnn`; sem
  modelo, erro claro (nada é baixado). Trecho opcional por `enable=between(t,…)`.
  Testes: tom + ruído rosa −48 dB com pausa → pausa −48,1 → **−68,1 dB** (−20 dB, tom intacto −21,1); video_720p
  (senoide 440 Hz + rosa contínuo, sem pausas): ruído fora de 440 Hz −40,6 → −48,0 (leve) / −50,5 (media) /
  −50,8 (forte); anlmdn −43,8/−44,6; senoide sempre −21,1/−21,2. Só no trecho 5–10 s: −50,8 dentro, −40,6 fora.
- [x] Tirar silêncios → `remover_silencios`: `silencedetect` (limiar automático = p10 + máx(6, 30% de p80−p10),
  entre −60 e −20 dB), margem antes/depois da fala, cortes alinhados aos quadros (0,4 ms antes do quadro, porque o
  `_cmd_preciso` escreve `-ss` com 3 casas e 8,16667 → 8,167 perderia o quadro: vídeo 1 quadro adiantado por trecho).
  Reaproveita `_cmd_preciso`; com > 8 trechos corta em lotes de 8 (`-threads 2` por entrada; partes .mkv com áudio
  FLAC) e junta com o demuxer concat (vídeo copiado, áudio codificado uma vez). Memória: 12 entradas 720p = 800 MB;
  lote de 8 com 2 threads ≈ 410 MB de pico. Testes: fala_com_silencio 8 s → **6,30 s**; video com 3 s mudos 20 → 17,30 s
  (vídeo 17,300/áudio 17,343); 30 pausas em 45 s → 29,867 s (4 lotes, 16 s); sincronia medida com flash+bipe em
  cada trecho: desvio áudio−vídeo ≤ 21 ms (igual ao original, granularidade da medição), sem acúmulo.
- [x] Fade in/out → `fade_audio` (`afade` com curva; `video=True` → `fade` para `cor`). Teste: áudio −51,9 dB em
  0–0,1 s, −21 no meio, −55 no fim; luminância 16 (preto) no 1º quadro, 126 no meio, 17 no último.
- [x] Música de fundo → `musica_fundo` (`-stream_loop -1` para loop, `atrim` + fades + `adelay` + `apad`;
  `mixar` com `amix normalize=0`, `trocar`, `nova_faixa`; em arquivo só de áudio a nova faixa vira `.mka`).
  Ducking: `sidechaincompress` com a voz (filtrada 120 Hz–6 kHz) no sidechain; limiar e razão calculados do
  nível da voz (p80) e do piso para dar ~`ducking_db`. Testes: música −47 dB (banda 220 Hz) sem voz → −58,2 dB com
  voz (pedido 12, obtido ~11 dB); loop de música de 3 s em 2–15 s presente só nesse trecho, com fades; duração
  áudio = vídeo (20,010/20,000).
- [x] Extrair só o áudio: coberto por `exportar` (predefinições `mp3`/`m4a`/`opus`/`ogg`/`wav`/`flac`) e por
  `faixa_audio(acao='extrair')` — sem ferramenta nova.
- [ ] DeepFilterNet (redução de ruído com IA) — **pendente, não baixado**. Análise: DeepFilterNet 3 (MIT/Apache-2.0,
  ok com GPL-3.0). O caminho leve é o executável `deep-filter` (Rust/libDF, ~10–20 MB com o modelo, binário x86_64 nas
  releases do GitHub) ou os 3 modelos ONNX (~8 MB) no ONNX Runtime da Fase 7; **não** usar o pacote Python (PyTorch,
  ~700 MB). Roda mais rápido que o tempo real em 1 núcleo (RTF ~0,05), memória < 200 MB: viável na máquina alvo.
  Trabalha em 48 kHz: ffmpeg extrai a faixa em WAV 48 kHz → `deep-filter` → remonta como versão (mesmo
  `_editar_audio`). Recomendação: entrar como `metodo='deepfilter'` em `reduzir_ruido`, baixado sob demanda
  (com confirmação e tamanho) para `~/.local/share/ai-editor/bin`.
- [ ] Separar voz da música — **pendente, não baixado**. Demucs v4 (`htdemucs`, código e pesos MIT): exige PyTorch
  (~700 MB–1 GB instalado) + modelo ~80 MB (`htdemucs_ft` ~320 MB); em CPU de notebook ~0,5–1× o tempo real e
  2–4 GB de RAM em trechos longos (precisa `--segment`): pesado demais para o caminho principal e arriscado com
  ~4 GB livres. Alternativa: modelos ONNX (htdemucs exportado, ou MDX-Net de voz ~30–60 MB; conferir a licença de
  cada peso — os da comunidade UVR variam) no ONNX Runtime (MIT, ~50 MB) já previsto na Fase 7, processando em
  blocos com sobreposição e como tarefa em segundo plano. Recomendação: fazer junto com a Fase 7 (mesmo ambiente de
  IA sob demanda), saída como faixas novas A2 = voz, A3 = música (`nova_faixa`), reaproveitando o ducking.
- Limitações: o ducking é aproximado (detector RMS do compressor; ~1 dB a menos que o pedido) e não distingue voz
  de outros sons na faixa; a redução com `afftdn` só pega ruído estacionário (não tira cliques, vozes de fundo ou
  ruído que muda); `enable` num trecho liga/desliga o filtro sem transição; `remover_silencios` descarta legendas
  embutidas e capítulos e recodifica vídeo e áudio; cada edição de áudio recodifica a faixa (AAC 192k em MP4),
  somando alguns ms de preenchimento no fim; sem DeepFilterNet/RNNoise embutidos, voz com ruído não estacionário
  fica a cargo do usuário trazer um modelo `.rnnn`. Bug do ffmpeg 6.1 evitado: `amix duration=first` com o vídeo
  copiado perdia ~85 ms do fim do áudio → usa `duration=longest` (a música já tem a duração exata).

### Fase 5 — Montagem e análise
- [ ] Juntar outro arquivo (emendar vídeos diferentes, normalizando resolução/fps/áudio).
- [ ] Dividir em partes (por marcas ou a cada N minutos).
- [ ] Detectar cortes de cena (sugestões de pontos de corte).
- [ ] Capítulos: mostrar na régua, criar e editar.
- [ ] Relatório técnico: bitrate, qualidade, sinais de recompressão.

### Fase 6 — Legendas e fala (IA local)
- [ ] Transcrição local com Whisper (whisper.cpp sob demanda, ou o filtro `whisper` do ffmpeg 8).
- [ ] Gerar `.srt`/`.vtt` e gravar legenda no vídeo (libass).
- [ ] Edição pelo texto ("corta onde ele fala X").

### Fase 7 — Objetos: destacar e remover (IA local)
Fluxo: o Claude localiza o objeto nos quadros → segmenta e rastreia → mostra o contorno para confirmação → remove (transparente ou reconstruído).

- [ ] Ambiente de IA separado, carregado sob demanda; modelos baixados no primeiro uso para `~/.local/share/ai-editor/modelos`.
- [ ] Inferência com **ONNX Runtime** (CPU; OpenVINO opcional para GPU Intel).
- [ ] `segmentar_objeto(pontos|caixa, tempo)`: SAM 2.1 tiny, rastreamento por todos os quadros, máscara salva junto das versões.
- [ ] `ajustar_mascara`: pontos positivos/negativos para refinar.
- [ ] `destacar_objeto`: versão de pré-visualização com contorno e realce.
- [ ] `remover_objeto(modo="transparente")`: exporta com alfa.
- [ ] `remover_objeto(modo="consertar")`:
  - câmera parada: LaMa em um quadro e o mesmo remendo aplicado em todos (estável, rápido);
  - câmera em movimento: propagação por fluxo óptico + preenchimento; avaliar alternativa de licença livre ao ProPainter (licença não comercial).
- [ ] Tarefas longas em segundo plano: a ferramenta devolve um id, `tarefas` consulta o progresso, a janela mostra a barra.

### Fase 8 — Distribuição
- [x] Ponto de entrada único `ai-editor` (`cli.py`): `[arquivo]` abre a janela, `--mcp`, `--diagnostico`,
  `--instalar [--sem-mime] [--sem-claude]`, `--desinstalar`, `--versao`. Só importa Qt quando abre a janela.
- [x] AppImage com Python embutido (PyInstaller one-dir), Qt (só módulos usados; plugins de teclado virtual/PDF/GTK3/eglfs
  podados por `packaging/podar.py`), libmpv + dependências, ffmpeg/ffprobe, mediainfo, exiftool, c2patool.
  `packaging/build_appimage.sh` (cache em `~/.cache/ai-editor-build`, saída em `dist/`). Build local: ~2,5 min,
  pico de RAM ok (disponível nunca abaixo de 4,1 GB).
  - **Tamanho: 233 MB** (zstd 19, blocos de 1 MiB). `COMP=xz` ≈ 213 MB (abre mais devagar).
    `FFMPEG_ORIGEM=sistema` (ffmpeg 6.1 do Ubuntu, reaproveitando as libav* da libmpv): **164 MB**, com todos os recursos
    do diagnóstico ✔ — alternativa se o tamanho importar mais que ter o ffmpeg 8.
  - Downloads com versão fixa e conferidos com `packaging/SHA256SUMS` (`sha256sum -c`; hash errado/download truncado
    apaga o arquivo do cache e para o build): FFmpeg do release datado `autobuild-2026-11-09-12-42` (receita BtbN
    `c9fe511`), exiftool 13.62, c2patool 0.27.22, appimagetool 1.9.1 + runtime type2 `20251108`. Trocar versão:
    editar o script e rodar `build_appimage.sh --atualizar-sums`. O BtbN apaga autobuilds antigos: quando o release
    sumir, atualizar para um novo. `uv sync --frozen` sem fallback.
  - ffmpeg = build GPL **"shared"** do FFmpeg-Builds (BtbN) n8.1.3, isolado em `usr/lib/ffmpeg` (RPATH próprio): 2 executáveis
    estáticos ocupariam ~3x mais. Tem x264, x265, libvpx, opus, SVT-AV1, LAME, ProRes, libass, rubberband, arnndn;
    **não** tem o filtro `whisper` (`--disable-whisper` nos builds do BtbN).
  - libmpv/mediainfo vêm do Ubuntu 24.04 com as dependências (ldd), RPATH via patchelf; ficam de fora libc, libstdc++,
    GL/EGL/DRM/GBM, VA-API/VDPAU, X11/libxcb e extensões de protocolo, Wayland, ALSA/Pulse/PipeWire, fontconfig/freetype/harfbuzz, zlib,
    D-Bus/systemd/udev (precisam casar com o sistema). Requer glibc ≥ 2.39 (build no Ubuntu 24.04).
    A família xcb-util (libxcb-cursor0 etc.) vai no pacote: o plugin xcb do Qt ≥ 6.5 não abre sem ela.
  - exiftool: código Perl em `usr/lib/exiftool` + wrapper que usa o `perl` do sistema.
  - Testado: `--versao` (0,6 s), `--diagnostico` (tudo `[pacote]`), janela com vídeo (VAAPI ativo, ícones e ondas ok),
    `estado`/`capturar`/`sair` pelo socket `ai-editor`, `--mcp` (initialize, 21 ferramentas, `metadados` com
    ffprobe/mediainfo/exiftool/c2pa do pacote, `abrir` abre a janela e ela sobrevive ao fim do MCP).
  - Empacotado, o MCP abre a janela com `sys.executable -m ai_editor.app`; o `cli.py` reconhece isso e, dentro de
    AppImage, reexecuta pelo `$APPIMAGE` (a janela ganha a própria montagem e não morre quando o MCP termina).
- [x] `ai-editor --instalar` (`instalador.py`, só stdlib): atalho `~/.local/bin/ai-editor` (AppImage), `.desktop` + ícone SVG,
  tipos MIME de vídeo/áudio em `~/.config/mimeapps.list` (lista explícita; `--sem-mime`), MCP no Claude Code
  (`claude mcp add --scope user`, ou `~/.claude.json` se não houver o comando; pula se não houver Claude Code) e no
  Claude Desktop (`~/.config/Claude/claude_desktop_config.json`, se a pasta existir). Respeita o modo dev
  (`ai-editor-dev`, `instalacao-dev.json`).
  - Transacional (revisão de 2026-11-10): o manifesto `~/.local/share/ai-editor/instalacao.json` é gravado **antes**
    de cada alteração, já com o valor anterior; falha no meio (permissão, timeout do `claude`, Ctrl+C) deixa o
    manifesto coerente e o comando sai com código ≠ 0. Reinstalar com o manifesto perdido não perde o original: se o
    valor atual já é nosso, o anterior vem do backup `*.ai-editor.bak`, que guarda só o **primeiro** original
    (nunca é sobrescrito) e é apagado no `--desinstalar` bem-sucedido. Manifesto ilegível → renomeado para
    `.corrompido-<data>`, com aviso.
  - Arquivos de configuração que são links simbólicos (dotfiles) são gravados no destino real (o link continua link);
    gravação atômica com `mkstemp` no mesmo diretório, mesmas permissões do original (0600 continua 0600), `fsync`.
  - `claude mcp remove` + `add`: se o `add` falha, o registro anterior volta com `add-json`. Procura o `claude` no PATH e
    em `~/.claude/local/claude`; sem ele, edita `~/.claude.json` e avisa para fechar as sessões do Claude Code.
  - Com AppImage, o `.desktop` (`Exec`/`TryExec`) e o MCP usam o atalho estável `~/.local/bin/ai-editor` (trocar o
    AppImage de lugar/versão = refazer só o link). `Exec` escapado conforme a especificação Desktop Entry.
  - Testado em HOMEs falsos com um `claude` simulado: dotfiles por link, falha no meio + manifesto perdido,
    falha do `mcp add`, manifesto corrompido, seção `[Default Applications]` criada por nós, caminho com espaço/`$`.
- [x] `--desinstalar` simétrico (restaura MIME/registros anteriores, só onde ninguém mudou depois; o que não deu certo
  fica no manifesto para a próxima tentativa; remove a seção `[Default Applications]` vazia criada por nós; volta o
  arquivo byte a byte quando o resultado equivale ao original). `ai-editor -- -arquivo.mp4` abre nomes que começam com `-`.
- [x] Bundle `.mcpb` (MCPB 0.3, tipo `binary`, validado contra o schema oficial): lançador que executa
  `$AI_EDITOR_APPIMAGE`, `~/.local/bin/ai-editor` ou o AppImage mais recente de `~/Applications` com `--mcp` (nunca
  ~/Downloads nem o PATH). `packaging/mcpb/empacotar_mcpb.py`
  lê a versão e a lista de ferramentas do próprio servidor.
- [x] GitHub Actions: `release.yml` (tag `v*` → build, testes do AppImage, SHA256SUMS, Release com AppImage, .zsync,
  .mcpb, COMPONENTES.txt, LICENCAS_TERCEIROS.md) e `ci.yml` (push/PR em dev/main: compileall, testes, instalador em
  HOME falso, initialize do MCP). Ainda não rodaram no GitHub. Endurecidos: actions fixadas por SHA (comentário com a
  versão), `persist-credentials: false`, permissões só de leitura; a publicação é um job separado (o único com
  `contents: write`) que recebe `dist/` como artefato e confere o SHA256SUMS.
- [x] `deploy.sh X.Y.Z [--dry-run] [--push]`: confere main limpa, sintaxe, sobe a versão, commit + tag; push só com `--push`.
  `--dry-run` não altera nada (sem `uv`, sintaxe conferida com `compile()` em memória, sem `__pycache__`). Aceita só
  `X.Y.Z` e pré-lançamentos PEP 440 normalizados (`X.Y.ZaN/bN/rcN`), comparados na ordem PEP 440 (`1.0.0rc1 < 1.0.0`).
- [x] Atualização via zsync: informação `gh-releases-zsync|rlb3ssa|ai-editor|latest|AI_Editor-*x86_64.AppImage.zsync`
  embutida e `.zsync` gerado (zsyncmake extraído do .deb do Ubuntu quando não instalado).
- [x] Conformidade GPL: `packaging/LICENCAS_TERCEIROS.md` (licenças, links de fonte, oferta escrita) e `COMPONENTES.txt`
  gerado no build (versões exatas, pacote Debian de origem e link Launchpad de cada `.so`), ambos em
  `usr/share/doc/ai-editor/` dentro do AppImage, junto com os textos das licenças: `licencas-comuns/` (GPL/LGPL/
  Apache/BSD/MPL/Artistic), `python/<pacote>/` (dist-info; PySide6/shiboken6 com a LGPL-3), `ubuntu/<pacote>/copyright`
  de cada pacote do Ubuntu e `ffmpeg/BIBLIOTECAS.tsv` (repositório + commit de cada biblioteca da receita BtbN).
  Releases devem sair do CI: na máquina local alguns pacotes são
  versões ESM (Ubuntu Pro), cujo fonte não é público no Launchpad.
- [ ] Testar numa sessão Wayland real.
- [ ] Reduzir mais: compilar a libmpv contra as libav* do build do BtbN (eliminaria a segunda cópia do ffmpeg, ~70 MB).

### Depois
- [ ] Outras plataformas (Windows/macOS) — a render API já ajuda.

## 4. Fluxo de desenvolvimento

- **Modo dev** = rodando do código-fonte (`uv run ai-editor`). Detectado automaticamente (não empacotado);
  forçar com `AI_EDITOR_DEV=0/1`. Usa nome/socket próprios (`ai-editor-dev`, `$XDG_RUNTIME_DIR/ai-editor-dev/`),
  então convive com a versão instalada (`ai-editor`).
- **Recarga automática**: ao salvar um `.py` da janela, ela se reinicia mantendo arquivo, posição, pausa,
  marcas, zoom do vídeo e geometria. Se o arquivo tiver erro de sintaxe, a recarga é cancelada e o
  erro aparece na barra de status. Mudanças só em `mcp_server.py` exigem `/mcp` → reconectar.
- **MCP dev**: registrado como `ai-editor-dev` (`claude mcp add --scope user ai-editor-dev -- <repo>/.venv/bin/ai-editor-mcp`).
- **Player padrão** durante o desenvolvimento: `~/.local/share/applications/ai-editor-dev.desktop`.
- **AppImage** só para testar o empacotamento e nas releases (`make appimage`); é um arquivo único, atualizar = trocar o arquivo.
- **Git**: trabalho na `dev`; conjunto estável → merge na `main` + tag → GitHub Actions publica o AppImage.

## 5. Inventário de ferramentas

Decidido em 2026-11-09. Registro em `src/ai_editor/binarios.py` (`REGISTRO` e `RECURSOS_FFMPEG`).

### 5.1 Embutido no pacote (offline desde o primeiro uso)
| Ferramenta | Para quê | Licença | Tamanho aprox. |
|---|---|---|---|
| ffmpeg + ffprobe (build GPL "shared" do FFmpeg-Builds n8.1.3, release `autobuild-2026-11-09-12-42`, isolado em `usr/lib/ffmpeg`) | edições, análise, exportação. Precisa de: libx264, libvpx-vp9, libopus, aac. Desejável: libx265, libsvtav1, libmp3lame, prores_ks, libass (`subtitles`), rubberband, arnndn | GPL-3.0 | ~220 MB descomp. |
| libmpv + dependências | reprodução com aceleração por hardware | GPL/LGPL | ~40 MB |
| mediainfo | metadados detalhados | BSD-2 | ~10 MB |
| exiftool | metadados de câmera/GPS/XMP; usa o Perl do sistema (o pacote não leva Perl) | Artistic/GPL | ~21 MB |
| c2patool | Content Credentials (procedência, "gerado por IA") | MIT/Apache-2.0 | ~20 MB |
| modelo RNNoise | redução de ruído básica (`arnndn`) — ainda não incluído (entra com a Fase 4) | BSD | < 1 MB |
| Python + PySide6 (só módulos usados), python-mpv, numpy, qtawesome, mcp | o app | LGPL/MIT/BSD | ~120 MB |

Meta: AppImage comprimido com ~150–250 MB. Obtido: **233 MB** (164 MB com `FFMPEG_ORIGEM=sistema`).

### 5.2 Sob demanda (baixado no primeiro uso, com confirmação e tamanho)
Destino: `~/.local/share/ai-editor/bin` e `~/.local/share/ai-editor/modelos`.
| Ferramenta | Para quê | Licença | Tamanho |
|---|---|---|---|
| ONNX Runtime (+ OpenVINO opcional) | motor das IAs no processador | MIT | ~50 MB |
| SAM 2.1 tiny | recortar/rastrear objetos (Fase 3) | Apache-2.0 | ~150 MB |
| LaMa | preencher fundo removido (Fase 3) | Apache-2.0 | ~200 MB |
| whisper.cpp (`whisper-cli`) + modelo base/small | legendas/transcrição (Fase 4) — ou o filtro `whisper` do ffmpeg 8, se o build tiver | MIT | 5 MB + 150–470 MB |
| DeepFilterNet | redução de ruído de alta qualidade | MIT/Apache-2.0 | ~10 MB |
| Demucs (opcional) | separar voz e música | MIT | ~300 MB |

### 5.3 Eliminados
- `gio trash` → `lixeira.py` (padrão freedesktop, inclusive lixeira de outros discos).
- `xdg-mime` → `--instalar` escreve `mimeapps.list` direto (usa `xdg-mime` só se existir).

### 5.4 Só para construir
PyInstaller (dev; GPL com exceção do bootloader), appimagetool (MIT), patchelf (GPL-3.0), zsyncmake (Artistic-2.0),
GitHub Actions. Não usamos linuxdeploy: as dependências são resolvidas com `ldd` no `build_appimage.sh`.

### 5.5 A confirmar
- ~~Se os builds estáticos do ffmpeg 8 trazem o filtro `whisper`~~ — **não**: o FFmpeg-Builds (BtbN) usa `--disable-whisper`
  (verificado em 2026-11-10 no n8.1). Transcrição continua via whisper.cpp sob demanda.

## 6. Licença e dependências

Projeto: **GPL-3.0**.

| Componente | Licença | Observação |
|---|---|---|
| PySide6 / Qt | LGPL-3.0 | ok |
| libmpv | GPL-2.0+ (build padrão) | ok com GPL-3.0 |
| ffmpeg + x264 | GPL | ok; distribuir com link para o fonte |
| python-mpv | GPL-2.0+ / LGPL | ok |
| QtAwesome (+ Phosphor, MDI) | MIT / fontes MIT, Apache | ok |
| mcp SDK, numpy | MIT / BSD | ok |
| SAM 2.1 | Apache-2.0 | ok com GPL-3.0 |
| LaMa | Apache-2.0 | ok com GPL-3.0 |
| ProPainter | NTU S-Lab 1.0 (não comercial) | **evitar no pacote**; no máximo opcional |
| mediainfo (+ libmediainfo, libzen) | BSD-2-Clause | no AppImage, do Ubuntu |
| exiftool | Artistic / GPL-1.0+ | no AppImage (código Perl) |
| c2patool | MIT / Apache-2.0 | no AppImage (binário oficial) |
| PyInstaller (bootloader) | GPL-2.0 com exceção | ok; só o bootloader vai no pacote |
| runtime do AppImage, appimagetool | MIT | ok |

Lista completa e links de código-fonte: `packaging/LICENCAS_TERCEIROS.md` e `COMPONENTES.txt` (gerado no build).

## 7. Decisões
- 2026-11-09: foco inicial só em Linux; distribuição por AppImage.
- 2026-11-09: licença GPL-3.0.
- 2026-11-09: nome do pacote/comando `ai-editor`; app "AI Editor".
- 2026-11-09: inventário de ferramentas aprovado (seção 5).
- 2026-11-09: IAs rodam localmente em CPU (sem LLM extra); o Claude faz o papel de "cérebro" via MCP.
- 2026-11-10: roadmap reordenado (exportar → imagem → áudio → montagem → legendas → objetos → distribuição); `--instalar` e AppImage ficam por último.
- 2026-11-10: "pistas de origem" são regras locais (`core._PISTAS_*`), sem IA: funcionam offline e sem o Claude conectado.
- 2026-11-10: exportação = ferramenta única `exportar` com predefinições em tabela; foto de quadro é predefinição (`png`/`jpg`), não ferramenta separada. Instagram horizontal usa fundo desfocado por padrão. O painel da janela é uma sobreposição (como o de metadados), para aparecer no `capturar`.
- 2026-11-10: combinação inválida de predefinição e extensão (ex.: youtube → `.webm`, whatsapp → `.gif`) dá erro claro em vez de trocar a extensão: o destino pedido nunca é alterado em silêncio. Largura/altura ímpares são arredondadas para par. Foto em 'fim' ou além do fim = último quadro (com aviso se passou), descontando o atraso do vídeo no contêiner como `congelar_quadro`. Trecho com fim ≤ início é erro; trecho cortado pelo fim do vídeo gera aviso. Pelo MCP, se `arquivo` não é o vídeo da janela, tempos relativos à janela ('m1', 'atual', 'in'...) são erro.

## 8. Questões em aberto
- Manter o idioma da interface em português ou adicionar i18n desde já?
- Associar o app como player padrão de mídia na instalação ou deixar opcional?
