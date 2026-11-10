# AI Editor — instruções para o Claude

Player/editor de vídeo e áudio leve para Linux, controlado pelo Claude via MCP. Open source, **GPL-3.0**.
O spec, o roadmap e as decisões ficam em **`plano.md`** — leia antes de começar e atualize ao concluir itens
(marcar `[x]`, registrar decisões com data em "Decisões").

## Idioma
- Conversa, comentários, docstrings, mensagens de commit, textos da interface e nomes do domínio: **português**.
- Identificadores seguem o estilo já existente (`abrir`, `marcar`, `Documento`, `fmt_tempo`...).

## Estrutura
| Arquivo | Papel |
|---|---|
| `src/ai_editor/app.py` | Janela PySide6 + libmpv: vídeo, controles, régua, ondas por faixa, mix, servidor de controle por socket |
| `src/ai_editor/core.py` | Núcleo sem Qt: caminhos/modo dev, tempos, ffmpeg, versões (`Documento`), cortes, áudio, metadados |
| `src/ai_editor/ondas.py` | Picos de áudio com cache |
| `src/ai_editor/binarios.py` | Localiza ferramentas externas (env → pacote → sob demanda → sistema), diagnóstico |
| `src/ai_editor/lixeira.py` | Lixeira freedesktop (usada no `salvar`) |
| `src/ai_editor/info.py` | Modal de metadados (glassmorphism) |
| `src/ai_editor/exportacao.py` | Painel de exportação (mesmo vidro); roda `core.exportar` em `QThreadPool` |
| `src/ai_editor/cliente.py` | Cliente do socket da janela |
| `src/ai_editor/mcp_server.py` | Servidor MCP (stdio); fala com a janela pelo socket |

Fluxo: Claude → MCP (`ai-editor-mcp`) → socket Unix JSON-lines (`{"cmd":..., ...}` → `{"ok", "dados"|"erro"}`) → janela.
Edições pesadas (ffmpeg) rodam no processo do MCP via `core.py`; depois o MCP manda `recarregar` para a janela.
`core.py` não pode importar Qt (o MCP precisa ficar leve).

## Ambiente de desenvolvimento
- Gerenciado com **uv** (Python 3.12): `uv sync`, `uv run ai-editor [arquivo]`, `uv add <pacote>`.
- Rodando do código-fonte = **modo dev** automático: nome/socket `ai-editor-dev`
  (`$XDG_RUNTIME_DIR/ai-editor-dev/controle.sock`), título "AI Editor (dev)". Forçar com `AI_EDITOR_DEV=0/1`.
- MCP de desenvolvimento registrado como `ai-editor-dev` → `.venv/bin/ai-editor-mcp`.
  Mudou `mcp_server.py` ou as assinaturas das ferramentas? Avise o usuário para rodar `/mcp` e reconectar.
- O player padrão do usuário (duplo clique em mídia) é a versão dev: `~/.local/share/applications/ai-editor-dev.desktop`.

### Recarga automática
Salvar qualquer `.py` (exceto só `mcp_server.py`) reinicia a janela sozinha, mantendo arquivo, posição, pausa,
marcas, zoom e geometria. **Não reinicie a janela manualmente** após editar. Erro de sintaxe cancela a recarga
e aparece na barra de status. Erros em tempo de execução: ver o log (stderr do processo).

### Controlar e inspecionar a janela
```bash
.venv/bin/python -c "from ai_editor import cliente as c; print(c.enviar('estado'))"
```
Comandos úteis: `estado`, `depurar` (props do mpv; `extras=[...]`), `capturar` (print da janela via Qt, inclusive o vídeo, mesmo com a janela atrás de outras →
`$XDG_RUNTIME_DIR/ai-editor-dev/captura.png`), `abrir`, `pausar`, `pular`,
`marcar`, `zoom_video`, `editar_tempo` (com `valor`, vai ao tempo), `exportar` (painel; `acao`=abrir/iniciar/
cancelar/fechar/estado, `predefinicao`, `trecho`=índice no combo, `limite_mb`, `destino`), `recarregar`, `sair`.

## Ferramentas externas
- **Nunca** chame `ffmpeg`/`ffprobe`/`mediainfo`/`exiftool` pelo nome solto: use `binarios.exe(nome)`,
  `binarios.caminho(nome)` (erro com dica de instalação) ou `core.FFMPEG`/`core.FFPROBE`.
- Ferramenta nova → registrar em `binarios.REGISTRO` (e recursos do ffmpeg em `RECURSOS_FFMPEG`) e na
  seção 5 do `plano.md`, com licença compatível com GPL-3.0.
- `ai-editor --diagnostico` (ou a ferramenta MCP `diagnostico`) mostra o que foi encontrado e o que falta.

## Regras importantes
- **Nunca** use `pkill -f ai-editor`/`claude-player` nem `kill` por padrão de nome: mata o servidor MCP e o
  próprio shell. Para fechar a janela: comando de socket `sair`.
- **Nunca simule mouse/teclado** (XTest/xdotool) no desktop: o usuário está usando a máquina. Teste a GUI via
  socket + `capturar`; interação real de mouse, peça para o usuário testar.
- Screenshot da tela inteira (`ffmpeg -f x11grab`) só captura o que está visível; prefira `capturar`.
- Os vídeos do usuário são dele: edite só por versões temporárias. **`salvar` só quando o usuário pedir**
  (substitui o original; o original vai para a Lixeira).
- Arquivos de teste ficam no scratchpad da sessão, nunca no repositório.
- Mantenha tudo leve: máquina alvo é Ubuntu 24.04, Intel UHD 620, 8 threads, 15 GB RAM, sem NVIDIA.
  Nada de dependência pesada no caminho principal; IA e afins são opcionais e carregados sob demanda.

## Detalhes técnicos que já custaram tempo
- python-mpv: `mpv["prop"]` lê **opções**, não propriedades. Use `self._get(prop)` / `self._set(prop, v)`
  (por baixo: `_get_property(prop, mpv.lazy_decoder)` / `_set_property`).
- libmpv exige `locale.setlocale(LC_NUMERIC, "C")` antes de criar o `MPV`.
- PySide6 6.12 removeu alguns enums curtos (ex.: `Qt.WA_DontCreateWindowContents`); confira com `hasattr`.
- Vídeo: render API do libmpv (`vo=libmpv`) num `QOpenGLWidget` (`TelaVideo`). Em X11 o Qt usa EGL
  (`QT_XCB_GL_INTEGRATION=xcb_egl`) e o display nativo é passado ao mpv → VAAPI com interop.
  `update_cb` vem de outra thread: só emitir sinal. `tela.liberar()` antes de `mpv.terminate()`.
- Mix de várias faixas de áudio: `lavfi-complex` com `amix=normalize=0`.
- Corte preciso: uma entrada por trecho (`-ss a -t d -i arq`) + `concat` no `filter_complex`, reaplicando as
  tags das faixas (idioma/título). O modo rápido (`-c copy`) não é exato no quadro.
- Ícones: QtAwesome, Phosphor (`ph.*`) por padrão; nomes no código são os do MDI e traduzidos em `_PHOSPHOR`.
- Exportar ≠ editar: `core.exportar` cria arquivo novo e não registra versão. Nunca sobrescreve e aguenta
  exportações simultâneas: `_reservar` cria o nome final com `O_EXCL`, o ffmpeg grava num `mkstemp` oculto
  (`.nome.ext.XXXXXXXX.parcial.ext`) e `_publicar` faz `os.replace` sobre a reserva. O ffmpeg roda com
  `PR_SET_PDEATHSIG` (morre junto com o processo/thread que o criou); `.parcial` com mais de 10 min na pasta de
  destino são órfãos e `_limpar_orfaos` os remove. Janela: `painel_exportar.encerrar()` antes do `execv` da
  recarga e no `closeEvent`. Predefinições são dados em `core.PREDEFINICOES`. Progresso: `-progress pipe:1` (`out_time_us`).
  Painéis sobre a janela devem ser widgets filhos (não `QDialog`), senão não aparecem no `capturar`.
- Edições de imagem (Fase 3, `core.py` seção "imagem"): `_editar_quadros` (só quadros; áudio/legendas copiados)
  e `_editar_linha_tempo` (velocidade/congelar: uma entrada `-ss/-t` por segmento + `concat`; **nunca** `split` +
  `trim` para segmentos, que guarda quadros decodificados na memória). Filtros por segmento em `_segmentos_av`
  (também usado pelo `_cmd_preciso` e pelo `exportar` com vários trechos): nada de `PTS-STARTPTS` por faixa, fim por `trim`/`atrim` (o `-t` da entrada
  conta do 1º quadro de cada faixa), concat de vídeo e de áudio separados, vídeo com nº exato de quadros na grade
  `fps` (o concat estima o fim do vídeo e cada trecho ganhava até 1 quadro). Layout de canais: `_filtro_layout`. Valores de texto/caminho/expressão no
  `-filter_complex` passam por `core.esc_filtro` (2 níveis de escape; `drawtext` com `expansion=none`).
  Quadro "exibido em t": desconte o `start_time` do vídeo em relação ao do contêiner (MKV costuma ter 0,021 s).
- WebM com alfa: o ffprobe (decodificador VP9 nativo) mostra `yuv420p`; o alfa está na tag `alpha_mode=1` e só
  o `libvpx-vp9` decodifica (`ffmpeg -c:v libvpx-vp9 -i arq.webm`). Em comandos novos use `core._dec_entrada(stream)`
  antes de **cada** `-i`.
- Áudio (Fase 4, `core.py` seção "áudio"): `_editar_audio` (vídeo/faixas não tocadas copiados). `afftdn` só reduz de
  verdade com `nf` ≈ ruído RMS + 18 dB (o default −50 quase não faz nada); perfil de ruído = trecho de pausa posto
  antes do áudio (`concat`) + `asendcmd` `sn start/stop` + `atrim`. `amix duration=first` com vídeo copiado perde
  ~85 ms do fim no ffmpeg 6.1: use `duration=longest` com as entradas já do tamanho certo. Cortes em tempos de quadro:
  `_cmd_preciso` escreve `-ss` em µs arredondados para baixo (antes, 3 casas: 8,16667 → 8,167 perdia o quadro). Muitos trechos no
  corte preciso = muitos decodificadores abertos: lotes de 8 com `-threads 2` (`_cortar_preciso`; partes .mkv já no
  codec de vídeo do contêiner final, senão o `-c:v copy` da junção falha em .webm).
  No `-filter_complex` o pts da faixa = start_time dela menos o do contêiner (áudio em 0,478 s tem pts 0,478; use
  `_extensao_faixa`): `concat` **não** desconta o pts inicial do 2º segmento (zere com `asetpts=PTS-STARTPTS` antes),
  `afade`/`atrim start=`/`enable` usam pts. `afftdn` atrasa 25 ms e `anlmdn` 8 ms sem mudar o nº de amostras
  (`latencia_filtro`: `apad` + `atrim`). `alimiter` tem `level=1` por padrão (normaliza para 0 dB): use `level=0`.
  `-ss` junto com `-stream_loop` corrompe os timestamps: entradas separadas + `concat` + `asetpts=N/SR/TB`.

## Git
- Trabalhe na branch **`dev`**. Commits pequenos, mensagem em português no imperativo
  ("Adiciona…", "Corrige…").
- **Push, merge na `main`, tags e releases só quando o usuário pedir.**
- Antes de commitar: `python -m py_compile` nos arquivos alterados e verificação da janela (`estado`/`capturar`).
- Não commitar mídia, modelos de IA, `.venv`, AppImage nem pastas `.versoes/`.

## Licenças
Toda dependência nova precisa ser compatível com GPL-3.0 (registre na tabela do `plano.md`).
Modelos/código com licença não comercial (ex.: ProPainter) não entram no pacote.
