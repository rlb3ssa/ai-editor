# AI Editor

**Um player de vídeo que edita para você.** Abra um vídeo, marque pontos na régua e peça em português — "corta entre
as marcas amarelas", "tira o barulho da rua", "deixa vertical para o Reels", "bipa os palavrões", "separa a voz da
música" — e a IA (Claude ou outro assistente compatível com [MCP](https://modelcontextprotocol.io)) faz a edição
**no seu computador**, com o ffmpeg e IAs locais que já vêm dentro do programa.

> Projeto em desenvolvimento. O roadmap e as decisões estão em [`plano.md`](plano.md).

## Por que usar o AI Editor (e não outro editor livre)?
Kdenlive, Shotcut, OpenShot e DaVinci são **editores para quem sabe editar**: linha do tempo, keyframes, nós de cor.
O AI Editor é para quem **sabe o que quer**:

- **Você pede, a IA faz.** Sem procurar menus nem aprender editor: quem nunca editou consegue fazer edição de verdade.
- **Substitui os sites de edição.** Cortar, comprimir para o WhatsApp, tirar o fundo, separar a voz, legendar,
  remover ruído — o que hoje se faz no Clideo, Kapwing ou "removedor de ruído online", sem assinatura, sem marca
  d'água, sem limite de tamanho e **sem enviar seu vídeo para ninguém**.
- **IA de verdade, offline.** Legendas automáticas, separar voz e instrumentos, apagar objetos, limpar ruído de voz:
  tudo embutido, funciona sem internet desde a primeira vez.
- **É o seu player.** Abre com dois cliques, como o VLC; se precisar ajustar algo, peça ali mesmo — sem "importar
  para um projeto".
- **Nunca destrói o original.** Cada pedido vira uma versão que dá para desfazer; o original só é trocado quando você
  manda salvar, e ainda vai para a Lixeira.
- **Precisão técnica.** Corte exato no quadro, sincronia de áudio conferida amostra a amostra, metadados completos com
  pistas de origem e Content Credentials (C2PA).
- **Livre (GPL-3.0).** Qualquer pessoa pode usar, auditar, melhorar e redistribuir.

**Onde ele não é a melhor escolha:** montagens criativas com muitas trilhas, títulos animados e controle manual fino
— para isso, Kdenlive e DaVinci continuam melhores. O AI Editor depende de um assistente de IA com MCP (Claude Code,
Claude Desktop...); sem ele, é um bom player com exportação. Por enquanto, só Linux.

## O que faz
- Reprodução fluida com aceleração por hardware (libmpv), quadro a quadro, zoom no vídeo e na régua.
- Contador de quadros exato mesmo em vídeos de taxa variável (gravações de tela, celular); ao mudar a velocidade,
  mostra os quadros por segundo equivalentes.
- Ondas de áudio por faixa, volume/mudo/solo por faixa, mixagem.
- Marcas ilimitadas na régua; cada par de marcas forma um trecho colorido que a IA enxerga.
- Edição não destrutiva: cada edição vira uma versão temporária; o original só é substituído quando você manda salvar
  (e vai para a Lixeira, não é apagado).
- Metadados completos (ffprobe, MediaInfo, ExifTool, C2PA) com pistas automáticas de origem.
- Tudo o que a IA pode fazer está listado em [Ferramentas MCP](#ferramentas-mcp-para-assistentes-de-ia).

## Privacidade
Tudo roda localmente. O AI Editor **não envia seus vídeos para lugar nenhum** e não tem telemetria.
Quando você usa a IA, ela recebe apenas o que as ferramentas devolvem (textos, números e, quando você pede para
ela "ver" o vídeo, alguns quadros em imagem). As IAs locais (legendas, redução de ruído, separação de voz e
instrumentos, recorte e remoção de objetos) já vêm com os modelos dentro do AppImage: funcionam offline desde o
primeiro uso e o programa não baixa nada.

## Instalação (AppImage)
Um único arquivo (**~1,75 GB**) com tudo embutido: Python, Qt, libmpv, ffmpeg, MediaInfo, ExifTool, c2patool e as
IAs locais com seus modelos — whisper.cpp (modelo `small`, legendas/transcrição), DeepFilterNet (redução de ruído),
Demucs v4 (separar voz e instrumentos), SAM 2.1 e LaMa (objetos) sobre o ONNX Runtime. Nada é baixado depois: funciona
totalmente offline. Precisa de um Linux x86-64 com glibc 2.39 ou mais nova (Ubuntu 24.04, Fedora 40, Debian 13...) e
do `perl` do sistema (para o ExifTool).

1. Baixe `AI_Editor-X.Y.Z-x86_64.AppImage` na página de [Releases](https://github.com/rlb3ssa/ai-editor/releases)
   (confira com o `SHA256SUMS.txt`) e guarde num lugar fixo, por exemplo `~/Applications/`.
2. Dê permissão e instale a integração com o sistema e com o Claude:
   ```bash
   chmod +x AI_Editor-*-x86_64.AppImage
   ./AI_Editor-*-x86_64.AppImage --instalar
   ```
   O `--instalar` cria o atalho `~/.local/bin/ai-editor`, o item no menu de aplicativos, associa os tipos de
   vídeo/áudio ao AI Editor (`--sem-mime` para não associar) e registra o servidor MCP no **Claude Code**
   (`claude mcp add --scope user ai-editor`) e no **Claude Desktop**, se estiverem instalados (`--sem-claude` para pular).
   Tudo fica no seu diretório pessoal, sem `sudo`; antes de alterar um arquivo de configuração é guardada uma cópia do
   original (`*.ai-editor.bak`, apagada pelo `--desinstalar`). O menu e o MCP apontam para o atalho
   `~/.local/bin/ai-editor`: se mudar o AppImage de lugar ou de versão, rode `--instalar` de novo pelo arquivo novo.
3. No Claude Code, rode `/mcp` (ou abra uma sessão nova); no Claude Desktop, reinicie o aplicativo.

Outros comandos:
```bash
ai-editor video.mp4            # abre a janela (nome começando com "-": ai-editor -- -video.mp4)
ai-editor --diagnostico        # ferramentas encontradas (no AppImage, todas vêm do pacote)
ai-editor --desinstalar        # desfaz o --instalar (restaura associações e registros anteriores)
ai-editor --versao
```
**Atualizar:** o AppImage traz a informação de atualização (zsync); use o
[AppImageUpdate](https://github.com/AppImageCommunity/AppImageUpdate) ou simplesmente troque o arquivo
(e rode `--instalar` pelo arquivo novo, para o atalho apontar para ele).
**Claude Desktop:** além do `--instalar`, há o bundle `ai-editor-X.Y.Z.mcpb` na Release. Ele não leva o programa:
executa `~/.local/bin/ai-editor` (criado pelo `--instalar`), o AppImage indicado em `AI_EDITOR_APPIMAGE` ou o mais
recente de `~/Applications/`.
Licenças e código-fonte dos componentes embutidos: `LICENCAS_TERCEIROS.md` e `COMPONENTES.txt` na Release.

### Gerar o AppImage
```bash
packaging/build_appimage.sh     # → dist/ (AppImage, .zsync, .mcpb); downloads em ~/.cache/ai-editor-build
```
Requer Ubuntu 24.04 com `uv`, `libmpv2`, `mediainfo`, `perl`, `patchelf` (ou `uvx`), `build-essential` e `cmake` (ou
`uvx`; compilam o whisper.cpp portátil) e ~12 GB livres (os modelos, ~1,5 GB, ficam em cache). Releases: `./deploy.sh X.Y.Z`
cria a tag (`--push` envia) e o GitHub Actions publica.

## Requisitos (rodando do código-fonte)
- Linux (testado no Ubuntu 24.04, X11; Wayland em teste).
- `sudo apt install ffmpeg libmpv2 mediainfo libimage-exiftool-perl`
- [uv](https://docs.astral.sh/uv/) para o Python.

```bash
uv sync
uv run ai-editor video.mp4          # abre a janela
uv run ai-editor --diagnostico      # mostra as ferramentas encontradas
claude mcp add ai-editor -- "$(pwd)/.venv/bin/ai-editor-mcp"   # conecta ao Claude Code
```

No código-fonte, `uv run ai-editor --instalar` registra a versão de desenvolvimento (`ai-editor-dev`).
Do código-fonte as IAs são opcionais: `uv sync --extra ia` instala o ONNX Runtime; os modelos, o `deep-filter` e o
`whisper-cli` (compilado na máquina) são obtidos no primeiro uso, com versão e SHA-256 fixados.

## Ferramentas MCP (para assistentes de IA)
Esta seção é para a IA que controla o editor (e para quem quer saber exatamente o que ela consegue fazer).

**Convenções**
- **Tempos** aceitam segundos (`83.456`), `'1:23.456'`, `'1500ms'` ou palavras: `'atual'`/`'aqui'`, `'inicio'`,
  `'fim'`, marcas `'m1'`, `'m2'`... e expressões como `'atual-2'` ou `'m3+0.5'`. `'in'` = primeira marca,
  `'out'` = última.
- **Marcas e trechos:** o usuário marca pontos na régua; cada par consecutivo forma um trecho colorido
  (m1→m2 amarelo, m2→m3 ciano...). Consulte `estado` (campos `marcas` e `trechos_marcados`) quando ele falar de
  marcas, pontos ou de um trecho por cor.
- **Faixas de áudio** são numeradas a partir de 1 (A1, A2...), como na janela.
- **Versões:** toda edição cria uma versão temporária (v1, v2...) e a janela recarrega sozinha; `desfazer` volta uma.
  O original só é substituído por `salvar`, que deve ser chamado **apenas quando o usuário pedir**.
- **Arquivos novos** (`exportar`, `dividir`, `gerar_legenda`, extrações...) nunca sobrescrevem nada: o nome é
  automático, ao lado do original.
- **Enxergar:** `ver_quadros`/`ver_trecho` mostram imagens do vídeo; `ver_espectro` mostra o som como imagem
  (para conferir ruído ou vazamento entre instrumentos).
- **Tarefas longas de IA** (objetos, separação de áudio) rodam em segundo plano: acompanhe com `tarefas`.
- Se nada abaixo servir, `ffmpeg_livre` roda qualquer comando do ffmpeg embutido e cria uma versão.

<!-- ferramentas-mcp:inicio (gerado por packaging/gerar_ferramentas_readme.py; não edite à mão) -->

**90 ferramentas.** O resumo é a primeira frase da descrição que a IA recebe (a descrição completa, com parâmetros e exemplos, vem do próprio servidor).

#### Janela e reprodução

| Ferramenta | O que faz |
|---|---|
| `abrir` | Abre um vídeo (ou áudio) na janela do player, abrindo a janela se necessário. |
| `estado` | Estado atual do player: arquivo, posição (ms), duração, pausado, marcas (m1, m2...) e trechos coloridos, fps/quadro, faixas de áudio (mudo/solo/volume) e versões não salvas. |
| `controlar` | Controla a reprodução: tocar, pausar, pular para um tempo, andar quadro a quadro, velocidade, zoom na régua e mensagem na barra de status. |
| `marcar` | Marca pontos na régua, como o usuário faz com o botão de seta. |
| `mixar` | Ajusta como uma faixa de áudio TOCA na janela (não altera o arquivo). |

#### Enxergar e analisar

| Ferramenta | O que faz |
|---|---|
| `ver_quadros` | Extrai e mostra quadros do vídeo para o Claude enxergar. |
| `ver_trecho` | Mostra `quantidade` quadros espaçados igualmente entre inicio e fim (visão geral de um trecho). |
| `metadados` | TODOS os metadados do arquivo (padrão: o vídeo aberto), de várias fontes: sistema de arquivos (datas, atributos estendidos com URL de download), ffprobe (formato, faixas, capítulos, dados privados), mediainfo… |
| `analisar_audio` | Mede o áudio de uma faixa (padrão: arquivo inteiro): volume médio/máximo (dB), loudness integrada (LUFS), faixa dinâmica, pico real e lista de silêncios. |
| `diagnostico` | Ferramentas externas encontradas (ffmpeg, mediainfo, exiftool, libmpv, c2patool...), de onde vieram (pacote, sob demanda ou sistema), versões e recursos do ffmpeg disponíveis. |
| `detectar_cenas` | Detecta cortes de cena (mudanças bruscas de imagem) no vídeo aberto: sugestões de pontos de corte. |
| `relatorio_tecnico` | Relatório técnico do arquivo (padrão: o arquivo atual do vídeo aberto, incluindo edições não salvas): contêiner, codec/perfil/nível, resolução, fps, bitrate médio e por faixa (e pico em 1 s), GOP/intervalo de keyframes,… |
| `ver_espectro` | Mostra o ESPECTROGRAMA (frequência × tempo, escala log 40 Hz–16 kHz, cores em dBFS) de faixas de áudio do vídeo aberto, um embaixo do outro — para você "ver" o som, já que não o ouve. |

#### Cortar e montar

| Ferramenta | O que faz |
|---|---|
| `cortar` | Mantém SÓ o trecho [inicio, fim] (padrão: da primeira à última marca; ex.: inicio='m2', fim='m3'). |
| `remover_trechos` | Remove um ou mais trechos (corte exato no quadro) e junta o resto. |
| `montar_trechos` | Monta um novo vídeo com os trechos na ordem dada (pode reordenar/repetir). |
| `remover_silencios` | Corta as pausas/silêncios (vídeo e todas as faixas de áudio juntos, corte preciso; nova versão). |
| `juntar_arquivos` | Emenda outros arquivos (vídeos ou áudios do usuário) ao vídeo aberto, numa nova versão. |
| `dividir` | Divide o vídeo aberto em ARQUIVOS NOVOS (como exportar: não cria versão, não altera nada, nunca sobrescreve): nome.parte01.mp4, nome.parte02.mp4... |
| `capitulos` | Capítulos do vídeo aberto (aparecem na régua da janela como marcadores discretos, com o título no tooltip). |

#### Imagem

| Ferramenta | O que faz |
|---|---|
| `transformar_imagem` | Recorta, gira, espelha e/ou redimensiona o vídeo (nova versão). |
| `mudar_proporcao` | Muda a proporção da tela (nova versão): '9:16' (vertical/Reels/TikTok), '16:9', '1:1', '4:5', '4:3'... |
| `ajustar_cor` | Ajusta a cor da imagem (nova versão), no vídeo todo ou só em [inicio, fim]. |
| `estabilizar` | Estabiliza a câmera tremida (nova versão). |
| `velocidade_trecho` | Câmera lenta ou acelerado num trecho (nova versão; padrão: o vídeo todo). |
| `congelar_quadro` | Congela a imagem num quadro por `duracao_s` segundos (nova versão). |
| `sobrepor_texto` | Escreve um texto por cima do vídeo (nova versão), no vídeo todo ou só em [inicio, fim]. |
| `marca_dagua` | Coloca uma imagem (logo PNG com transparência, JPG...) por cima do vídeo (nova versão). |
| `borrar_area` | Borra ou pixela uma área (placa de carro, rosto, tela) em [inicio, fim] (nova versão; padrão: o vídeo todo). |

#### Efeitos de vídeo

| Ferramenta | O que faz |
|---|---|
| `efeito_visual` | Efeito visual pronto (nova versão), no vídeo todo ou só em [inicio, fim]. |
| `aplicar_lut` | Aplica uma LUT de cor / "filtro de cinema" (nova versão): arquivo .cube, .3dl, .dat, .m3d, .csp ou imagem Hald CLUT (.png quadrada). |
| `chroma_key` | Tira o fundo de TELA VERDE/AZUL e põe outro fundo (nova versão). |
| `sobrepor_video` | Vídeo/imagem por cima (picture-in-picture) ou TELA DIVIDIDA (nova versão; a duração não muda). |
| `zoom` | Zoom animado no vídeo (nova versão; mesmo tamanho de quadro, movimento suave sem tremor). |
| `fade_video` | Fade da IMAGEM (nova versão): aparece a partir de `cor` durante entrada_s segundos (a partir de inicio, padrão o começo) e/ou some para `cor` durante saida_s terminando em fim (padrão o final). |
| `desenhar` | Desenha gráficos por cima do vídeo, todos numa versão só. |
| `melhorar_video` | Limpa/melhora a imagem numa versão só (combine o que precisar): reduzir_ruido: 'leve', 'media' ou 'forte' (forte = nlmeans, lento); nitidez 0–1; desentrelacar: 'auto' (só se detectar linhas de TV entrelaçada) ou true;… |
| `mudar_fps` | Muda a taxa de quadros sem mudar a duração nem o áudio (nova versão). |
| `cortar_bordas_pretas` | Detecta e recorta TARJAS PRETAS (letterbox em cima/embaixo, pillarbox nas laterais) olhando vários pontos do vídeo (nova versão). |
| `remover_logo` | Apaga um logo/marca d'água FIXO interpolando os pixels em volta (delogo) (nova versão). |
| `perspectiva` | Corrige perspectiva ou converte vídeo 360° (nova versão). |
| `inverter_video` | Toca de TRÁS PARA FRENTE (nova versão): modo 'reverso' (o trecho volta ao contrário) ou 'boomerang' (o trecho toca normal e depois ao contrário, como no Instagram). |
| `repetir` | LOOP: repete um trecho (padrão: o vídeo todo) seguidas vezes, com som (nova versão). |
| `camera_lenta_suave` | Câmera lenta FLUIDA (nova versão): cria quadros intermediários em vez de repetir quadros (diferente de `velocidade_trecho`, que repete). |

#### Áudio

| Ferramenta | O que faz |
|---|---|
| `ajustar_audio` | Ajustes simples de áudio no ARQUIVO (nova versão): volume (ganho), normalizar, silenciar, fade e redução de ruído rápida. |
| `faixa_audio` | Gerencia faixas de áudio: remover uma faixa, adicionar um áudio externo como faixa nova (ex.: dublagem) ou extrair uma faixa para um arquivo de áudio. |
| `reduzir_ruido` | Reduz ruído de fundo constante (chiado, ventilador, ar-condicionado, hum largo) de uma faixa de áudio (nova versão; vídeo e outras faixas copiados sem perda). |
| `fade_audio` | Fade in no começo e/ou fade out no fim (nova versão). |
| `musica_fundo` | Adiciona ou troca a música de fundo a partir de um arquivo de áudio do usuário (mp3, wav, m4a...; nova versão). |

#### Efeitos de áudio

| Ferramenta | O que faz |
|---|---|
| `equalizar` | EQUALIZADOR de uma faixa de áudio (nova versão). |
| `efeito_voz` | EFEITOS DE VOZ criativos numa faixa (nova versão; duração e sincronia mantidas). |
| `mudar_tom` | Muda o TOM (afinação) de uma faixa sem mudar a duração nem a sincronia (nova versão). |
| `dinamica_audio` | DINÂMICA e restauração de uma faixa (nova versão). |
| `canais_audio` | CANAIS (esquerdo/direito) de uma faixa (nova versão). |
| `sincronizar_audio` | Corrige DESSINCRONIA entre som e imagem (lip sync) movendo uma faixa de áudio no tempo (nova versão). |
| `censurar_palavras` | CENSURA: troca palavrões/nomes/palavras por BIPE (1 kHz) ou SILÊNCIO (nova versão). |
| `normalizar_volume` | NORMALIZA O VOLUME para um alvo de loudness (LUFS, EBU R128) com precisão (nova versão): mede, aplica ganho linear (não muda a dinâmica) e só limita os picos se passariam do teto (limitador de pico verdadeiro); confere… |
| `inverter_audio` | Toca o ÁUDIO de trás para frente (efeito "fita ao contrário"; nova versão). |
| `mixar_faixas` | JUNTA (mixa) várias faixas de áudio DO ARQUIVO numa só (nova versão), ex. |

#### Áudio com IA

| Ferramenta | O que faz |
|---|---|
| `separar_audio` | IA local (Demucs v4, sem internet depois de instalado): separa VOZ, MÚSICA e INSTRUMENTOS de uma faixa de áudio. |

#### Legendas e fala

| Ferramenta | O que faz |
|---|---|
| `transcrever` | Transcreve a FALA do vídeo aberto (versão atual) ou de `arquivo`, 100% local (whisper.cpp na CPU; nada vai para a internet). |
| `gerar_legenda` | Gera arquivo de LEGENDA (.srt e/ou .vtt) a partir da fala, ARQUIVO NOVO ao lado do vídeo (`video.pt.srt`; players como o mpv/VLC carregam sozinhos). |
| `gravar_legenda` | QUEIMA a legenda na imagem do vídeo aberto (sempre visível, em qualquer player; nova versão; o áudio é copiado). |
| `embutir_legenda` | EMBUTE a legenda como FAIXA de legenda no arquivo (o espectador liga/desliga no player), sem re-encodar vídeo nem áudio (rápido, sem perda; nova versão). |
| `buscar_fala` | EDIÇÃO PELO TEXTO: acha os momentos em que a frase `texto` é dita (busca aproximada: ignora acentos, maiúsculas e pontuação e tolera pequenos erros do reconhecimento). |

#### Objetos com IA

| Ferramenta | O que faz |
|---|---|
| `segmentar_objeto` | IA local (SAM 2.1): encontra um objeto num quadro e o RASTREIA pelo trecho, gerando uma máscara (id 'obj1', 'obj2'...) usada por `remover_objeto`, `fundo_objeto`, `recortar_objeto` e `destacar_objeto`. |
| `ajustar_mascara` | Corrige uma máscara: refaz a segmentação com os prompts originais MAIS os novos no quadro em `tempo` (pontos = partes do objeto que faltaram; pontos_negativos = o que entrou e não devia; caixa = nova caixa; coordenadas… |
| `destacar_objeto` | Pré-visualização: cria uma versão com o objeto da máscara pintado (cor/opacidade) e contornado, para o usuário conferir no player. |
| `remover_objeto` | Remove o objeto da máscara (de `segmentar_objeto`) e reconstrói o fundo; cria uma versão. |
| `fundo_objeto` | Mantém o objeto da máscara e muda o FUNDO no trecho dela (cria uma versão). |
| `recortar_objeto` | EXPORTA (arquivo novo ao lado do original; não é versão) só o trecho da máscara com fundo transparente: formato 'webm' (VP9 com alfa; navegadores, OBS), 'mov'/'prores' (ProRes 4444 com alfa; editores de vídeo) ou… |
| `tarefas` | Tarefas longas de IA (segmentar/remover objeto...). |
| `objetos_ia` | Situação da IA local: ONNX Runtime instalado?, modelos baixados (tamanho e licença), memória livre e as máscaras do vídeo aberto. |

#### Arquivos, imagens e metadados

| Ferramenta | O que faz |
|---|---|
| `extrair_quadros` | Salva quadros do vídeo como IMAGENS (jpg/png/webp) numa PASTA NOVA ao lado do original (`video.quadros/`, `video.quadros.2/`...; ou dentro de `destino`). |
| `folha_contatos` | Folha de contatos (contact sheet): UMA imagem com colunas×linhas miniaturas espaçadas igualmente pelo vídeo, com o tempo no canto de cada uma e um cabeçalho (nome, duração, resolução, fps, codecs, tamanho). |
| `capa` | Capa embutida (thumbnail / arte do álbum) de MP4, M4A, MOV, MP3, MKV e FLAC — a imagem que o gerenciador de arquivos, o celular e os players mostram. |
| `video_de_imagens` | Cria um VÍDEO NOVO (.mp4 H.264, toca em qualquer lugar) a partir de fotos: slideshow / apresentação. |
| `audio_para_video` | Transforma ÁUDIO (podcast, música, gravação) em VÍDEO .mp4 NOVO para YouTube/Instagram/WhatsApp, que não aceitam só áudio. |
| `editar_metadados` | Edita ou APAGA metadados (tags) sem reencodar — nova versão, o original fica intacto até `salvar`. |
| `converter_sem_perda` | Troca o FORMATO (contêiner) SEM reencodar: instantâneo e sem perder qualidade (ex.: MKV → MP4, MOV → MP4, MP4 → MKV, extrair o áudio AAC para .m4a, Opus para .opus). |
| `reparar_arquivo` | Tenta CONSERTAR um vídeo/áudio que não abre direito, trava ao avançar, tem duração errada, áudio fora de sincronia por timestamps ruins ou não toca no navegador/celular (índice no fim). |
| `rotacao_sem_perda` | Gira o vídeo SEM reencodar (instantâneo, sem perda): muda só a marcação de rotação que os players respeitam (MP4/MOV/MKV). |
| `legenda_arquivo` | Legendas como ARQUIVOS e faixas embutidas (para CRIAR legenda a partir da fala use `gerar_legenda`; para embutir/queimar um .srt use `embutir_legenda`). |
| `comparar_qualidade` | Mede objetivamente QUANTO de qualidade se perdeu entre duas versões/arquivos e explica em português: VMAF (nota 0–100 da Netflix, a que melhor imita o olho humano; disponível no ffmpeg do pacote), SSIM e PSNR, com o… |

#### Exportar

| Ferramenta | O que faz |
|---|---|
| `exportar` | EXPORTA para um ARQUIVO NOVO (converter, compartilhar, extrair áudio, tirar foto de um quadro). |

#### Versões

| Ferramenta | O que faz |
|---|---|
| `versoes` | Lista as versões temporárias (com descrição e comandos ffmpeg usados) do vídeo aberto. |
| `desfazer` | Desfaz a última edição (apaga a última versão temporária) e recarrega a janela. |
| `descartar_versoes` | Apaga TODAS as versões temporárias e volta ao original. |
| `salvar` | SALVA: a última versão passa a ser o arquivo original; o original vai para a Lixeira e as versões temporárias são apagadas. |

#### Outros

| Ferramenta | O que faz |
|---|---|
| `ffmpeg_livre` | Qualquer outra edição: roda ffmpeg com seus argumentos sobre a versão atual e cria nova versão. |

<!-- ferramentas-mcp:fim -->

## Licença
**GPL-3.0-or-later** — veja [`LICENSE`](LICENSE).

Em termos simples: qualquer pessoa pode usar, estudar, modificar e redistribuir o AI Editor, inclusive para fins
comerciais, desde que versões distribuídas continuem livres sob a mesma licença e com o código-fonte disponível.
A GPL foi escolhida porque o AI Editor distribui componentes que já são GPL (ffmpeg com x264/x265, libmpv), e porque
garante que melhorias feitas por outros voltem para a comunidade.

### Sem garantia
O AI Editor é oferecido **"como está", sem garantia de qualquer tipo**, e os autores não se responsabilizam por danos
decorrentes do uso (seções 15 e 16 da GPL-3.0). Faça cópia dos arquivos importantes antes de editar.

### Uso responsável
- Você é responsável pelo conteúdo que edita: respeite direitos autorais, direitos de imagem e a privacidade de terceiros.
- As "pistas de origem" nos metadados são **heurísticas automáticas** (regras sobre o nome do arquivo e os metadados),
  não uma perícia; podem errar para mais ou para menos.
- Alguns formatos de vídeo/áudio (como H.264, H.265 e AAC) podem estar sujeitos a patentes em certos países.
  O AI Editor usa as implementações livres do projeto FFmpeg; verifique a legislação aplicável ao seu uso.
- "Claude" é marca da Anthropic. O AI Editor é um projeto independente, sem vínculo com a Anthropic, e pode ser usado com
  qualquer cliente compatível com MCP.

Copyright (C) 2026 Richardson Bessa e colaboradores.
