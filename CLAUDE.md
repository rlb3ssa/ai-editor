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
`marcar`, `zoom_video`, `recarregar`, `sair`.

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

## Git
- Trabalhe na branch **`dev`**. Commits pequenos, mensagem em português no imperativo
  ("Adiciona…", "Corrige…").
- **Push, merge na `main`, tags e releases só quando o usuário pedir.**
- Antes de commitar: `python -m py_compile` nos arquivos alterados e verificação da janela (`estado`/`capturar`).
- Não commitar mídia, modelos de IA, `.venv`, AppImage nem pastas `.versoes/`.

## Licenças
Toda dependência nova precisa ser compatível com GPL-3.0 (registre na tabela do `plano.md`).
Modelos/código com licença não comercial (ex.: ProPainter) não entram no pacote.
