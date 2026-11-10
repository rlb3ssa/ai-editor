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
| `mcp_server.py` | Servidor MCP com 19 ferramentas; abre a janela se ela não estiver aberta |

### 2.2 Ferramentas MCP existentes
`abrir`, `estado`, `controlar`, `marcar`, `mixar`, `ver_quadros`, `ver_trecho`, `metadados`, `analisar_audio`,
`cortar`, `remover_trechos`, `montar_trechos`, `ajustar_audio`, `faixa_audio`, `ffmpeg_livre`,
`versoes`, `desfazer`, `descartar_versoes`, `salvar`.

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
- [ ] `exportar(formato, predefinicao, trecho?)`: arquivo novo ao lado do original (`video.whatsapp.mp4`).
- [ ] Predefinições: WhatsApp, YouTube, Instagram/Reels, GIF, MP3, "menor tamanho", "até X MB" (2 passes).
- [ ] Exportar só um trecho ou só os trechos marcados.
- [ ] Exportar com transparência (WebM VP9 `yuva420p` / ProRes 4444).
- [ ] Foto de um quadro (PNG).
- [ ] Botão de exportar na janela, com progresso.

### Fase 3 — Imagem
- [ ] Recortar área (crop), girar, espelhar, mudar resolução.
- [ ] Mudar proporção (16:9 → 9:16, com fundo desfocado ou recorte).
- [ ] Brilho, contraste, saturação; estabilizar (`vidstab`/`deshake`).
- [ ] Câmera lenta / acelerar num trecho; congelar quadro.
- [ ] Texto e marca d'água por cima; borrar uma área num trecho (placa, rosto).

### Fase 4 — Áudio prático
- [ ] Redução de ruído: RNNoise (`arnndn`, embutido); DeepFilterNet sob demanda.
- [ ] Tirar silêncios automaticamente (`silencedetect`).
- [ ] Fade in/out; trocar ou adicionar música de fundo (com ducking).
- [ ] Extrair só o áudio (MP3/WAV/Opus).
- [ ] Separar voz da música (Demucs, sob demanda).

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
- [ ] AppImage com Python embutido (PyInstaller ou Nuitka), Qt, libmpv, ffmpeg estático, mediainfo, exiftool, c2patool.
- [ ] `ai-editor --instalar`: registra o MCP no Claude Code e no Claude Desktop, cria o `.desktop`, atalho em `~/.local/bin` e associa tipos MIME (opcional).
- [ ] `--desinstalar` simétrico.
- [ ] Bundle `.mcpb` para o Claude Desktop apontando para o AppImage.
- [ ] GitHub Actions: build do AppImage a cada tag e publicação na Release.
- [ ] Atualização via zsync/AppImageUpdate.
- [ ] Conformidade GPL: listar componentes, versões e link para o código-fonte de cada binário distribuído.
- [ ] Testar numa sessão Wayland real.

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
| ffmpeg + ffprobe (estático) | edições, análise, exportação. Precisa de: libx264, libvpx-vp9, libopus, aac. Desejável: libx265, libsvtav1, libmp3lame, prores_ks, libass (`subtitles`), rubberband, arnndn | GPL | ~80 MB |
| libmpv + dependências | reprodução com aceleração por hardware | GPL/LGPL | ~40 MB |
| mediainfo | metadados detalhados | BSD-2 | ~10 MB |
| exiftool | metadados de câmera/GPS/XMP; usa o Perl do sistema e leva um Perl mínimo se faltar | Artistic/GPL | ~25 MB |
| c2patool | Content Credentials (procedência, "gerado por IA") | MIT/Apache-2.0 | ~20 MB |
| modelo RNNoise | redução de ruído básica (`arnndn`) | BSD | < 1 MB |
| Python + PySide6 (só módulos usados), python-mpv, numpy, qtawesome, mcp | o app | LGPL/MIT/BSD | ~120 MB |

Meta: AppImage comprimido com ~150–200 MB.

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
PyInstaller ou Nuitka, linuxdeploy, appimagetool, GitHub Actions.

### 5.5 A confirmar
- Se os builds estáticos do ffmpeg 8 trazem o filtro `whisper` (dispensaria o whisper.cpp).

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

## 7. Decisões
- 2026-11-09: foco inicial só em Linux; distribuição por AppImage.
- 2026-11-09: licença GPL-3.0.
- 2026-11-09: nome do pacote/comando `ai-editor`; app "AI Editor".
- 2026-11-09: inventário de ferramentas aprovado (seção 5).
- 2026-11-09: IAs rodam localmente em CPU (sem LLM extra); o Claude faz o papel de "cérebro" via MCP.
- 2026-11-10: roadmap reordenado (exportar → imagem → áudio → montagem → legendas → objetos → distribuição); `--instalar` e AppImage ficam por último.
- 2026-11-10: "pistas de origem" são regras locais (`core._PISTAS_*`), sem IA: funcionam offline e sem o Claude conectado.

## 8. Questões em aberto
- Manter o idioma da interface em português ou adicionar i18n desde já?
- Associar o app como player padrão de mídia na instalação ou deixar opcional?
