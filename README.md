# AI Editor

Player e editor de vídeo e áudio **leve** para Linux, feito para trabalhar junto com uma IA.
Você assiste ao vídeo, marca pontos na régua e pede em linguagem natural — "corta entre as marcas
amarelas", "tira o ruído do áudio", "exporta para WhatsApp" — e a IA (Claude, via
[MCP](https://modelcontextprotocol.io)) executa a edição com o ffmpeg **no seu computador**.

> Projeto em desenvolvimento. O roadmap e as decisões estão em [`plano.md`](plano.md).

## O que faz
- Reprodução fluida com aceleração por hardware (libmpv), quadro a quadro, zoom no vídeo e na régua.
- Ondas de áudio por faixa, volume/mudo/solo por faixa, mixagem.
- Marcas ilimitadas na régua; cada par de marcas forma um trecho colorido que a IA enxerga.
- Edição não destrutiva: cada edição vira uma versão temporária; o original só é substituído quando você manda salvar
  (e vai para a Lixeira, não é apagado).
- Metadados completos (ffprobe, MediaInfo, ExifTool, C2PA) com pistas automáticas de origem.

## Privacidade
Tudo roda localmente. O AI Editor **não envia seus vídeos para lugar nenhum** e não tem telemetria.
Quando você usa a IA, ela recebe apenas o que as ferramentas devolvem (textos, números e, quando você pede para
ela "ver" o vídeo, alguns quadros em imagem). Recursos de IA local (legendas, recorte de objetos) baixam modelos
uma única vez e depois funcionam offline.

## Instalação (AppImage)
Um único arquivo com tudo embutido (Python, Qt, libmpv, ffmpeg, MediaInfo, ExifTool, c2patool). Precisa de um Linux
com glibc 2.39 ou mais nova (Ubuntu 24.04, Fedora 40, Debian 13...) e do `perl` do sistema (para o ExifTool).

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
Requer Ubuntu 24.04 com `uv`, `libmpv2`, `mediainfo`, `perl` e `patchelf` (ou `uvx`). Releases: `./deploy.sh X.Y.Z`
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
