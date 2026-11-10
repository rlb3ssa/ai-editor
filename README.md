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

Um AppImage com tudo embutido (sem precisar instalar nada) está no roadmap.

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
