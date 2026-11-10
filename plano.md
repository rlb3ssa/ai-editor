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
| `app.py` | Janela PySide6 + libmpv: vídeo, controles, régua com zoom, uma onda por faixa de áudio, mix (mudo/solo/volume), marcas IN/OUT, zoom/arraste no vídeo, servidor de controle por socket |
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
- Barra de progresso com precisão de milissegundos, zoom/pan na régua, marcação IN/OUT pelo botão direito.
- Roda do mouse no vídeo = zoom no ponto; arrastar = mover; duplo clique = 100%.
- Arquivos só de áudio escondem a área de vídeo e aumentam as ondas.

### 2.5 Limitações conhecidas
- Depende de Python, libmpv, ffmpeg, mediainfo e exiftool instalados no sistema.
- Zoom/arraste do vídeo ainda não validado com mouse real.
- Wayland ainda não testado numa sessão Wayland real.

## 3. Roadmap

### Fase 1 — Base portátil (Linux)
- [x] Renomear pacote/comandos para `ai-editor` / `ai-editor-mcp`.
- [x] Modo dev: instância separada (`ai-editor-dev`) com recarga automática ao salvar código.
- [x] Trocar o embutimento por `wid` pela **render API do libmpv** num `QOpenGLWidget` (X11 + Wayland, mouse nativo do Qt). VAAPI com interop confirmado em X11.
- [ ] Localizar binários primeiro dentro do pacote e depois no sistema (`ffmpeg`, `ffprobe`, `mediainfo`, `exiftool`, `libmpv`).
- [ ] Comando `ai-editor --instalar`: registra o MCP no Claude Code e no Claude Desktop, cria o `.desktop` e associa tipos MIME (opcional).
- [ ] `--desinstalar` simétrico.

### Fase 2 — Empacotamento
- [ ] AppImage com Python embutido (PyInstaller ou Nuitka), Qt, libmpv, ffmpeg estático, mediainfo, exiftool.
- [ ] Bundle `.mcpb` para o Claude Desktop apontando para o AppImage.
- [ ] GitHub Actions: build do AppImage a cada tag e publicação na Release.
- [ ] Atualização via zsync/AppImageUpdate.
- [ ] Conformidade GPL: listar componentes, versões e link para o código-fonte de cada binário distribuído.

### Fase 3 — Objetos: destacar e remover
Fluxo: o Claude localiza o objeto nos quadros → segmenta e rastreia → mostra o contorno para confirmação → remove (transparente ou reconstruído).

- [ ] Ambiente de IA separado, carregado sob demanda; modelos baixados no primeiro uso para `~/.local/share/ai-editor/modelos`.
- [ ] Inferência com **ONNX Runtime** (CPU; OpenVINO opcional para GPU Intel).
- [ ] `segmentar_objeto(pontos|caixa, tempo)`: SAM 2.1 tiny, rastreamento por todos os quadros, máscara salva junto das versões.
- [ ] `ajustar_mascara`: pontos positivos/negativos para refinar.
- [ ] `destacar_objeto`: versão de pré-visualização com contorno e realce.
- [ ] `remover_objeto(modo="transparente")`: exporta com alfa (WebM VP9 `yuva420p` ou ProRes 4444).
- [ ] `remover_objeto(modo="consertar")`:
  - câmera parada: LaMa em um quadro e o mesmo remendo aplicado em todos (estável, rápido);
  - câmera em movimento: propagação por fluxo óptico + preenchimento; avaliar alternativa de licença livre ao ProPainter (licença não comercial).
- [ ] Tarefas longas em segundo plano: a ferramenta devolve um id, `tarefas` consulta o progresso, a janela mostra a barra.

### Fase 4 — Depois
- [ ] Legendas (transcrição local com Whisper) e edição por texto.
- [ ] Redução de ruído no áudio.
- [ ] Exportar com predefinições (WhatsApp, YouTube, GIF).
- [ ] Outras plataformas (Windows/macOS) — depende da render API da Fase 1.

## 4. Fluxo de desenvolvimento

- **Modo dev** = rodando do código-fonte (`uv run ai-editor`). Detectado automaticamente (não empacotado);
  forçar com `AI_EDITOR_DEV=0/1`. Usa nome/socket próprios (`ai-editor-dev`, `$XDG_RUNTIME_DIR/ai-editor-dev/`),
  então convive com a versão instalada (`ai-editor`).
- **Recarga automática**: ao salvar um `.py` da janela, ela se reinicia mantendo arquivo, posição, pausa,
  marcas IN/OUT, zoom do vídeo e geometria. Se o arquivo tiver erro de sintaxe, a recarga é cancelada e o
  erro aparece na barra de status. Mudanças só em `mcp_server.py` exigem `/mcp` → reconectar.
- **MCP dev**: registrado como `ai-editor-dev` (`claude mcp add --scope user ai-editor-dev -- <repo>/.venv/bin/ai-editor-mcp`).
- **Player padrão** durante o desenvolvimento: `~/.local/share/applications/ai-editor-dev.desktop`.
- **AppImage** só para testar o empacotamento e nas releases (`make appimage`); é um arquivo único, atualizar = trocar o arquivo.
- **Git**: trabalho na `dev`; conjunto estável → merge na `main` + tag → GitHub Actions publica o AppImage.

## 5. Licença e dependências

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

## 6. Decisões
- 2026-11-09: foco inicial só em Linux; distribuição por AppImage.
- 2026-11-09: licença GPL-3.0.
- 2026-11-09: nome do pacote/comando `ai-editor`; app "AI Editor".
- 2026-11-09: IAs rodam localmente em CPU (sem LLM extra); o Claude faz o papel de "cérebro" via MCP.

## 7. Questões em aberto
- Manter o idioma da interface em português ou adicionar i18n desde já?
- Associar o app como player padrão de mídia na instalação ou deixar opcional?
