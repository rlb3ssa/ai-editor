# AI Editor — componentes de terceiros e licenças

O AI Editor é software livre sob a **GPL-3.0-or-later** (arquivo `LICENSE`). O AppImage distribui, junto com o
programa, os componentes abaixo. Cada um continua sob a própria licença; todas são compatíveis com a GPL-3.0.
As versões exatas de cada build estão em `COMPONENTES.txt` (gerado pelo `packaging/build_appimage.sh` e
incluído no AppImage em `usr/share/doc/ai-editor/` e na página da Release).

### Textos das licenças dentro do AppImage (`usr/share/doc/ai-editor/`, veja com `--appimage-extract`)
- `LICENSE` — GPL-3.0 do AI Editor; `licencas-comuns/` — GPL-2, GPL-3, LGPL-2.1, LGPL-3, Apache-2.0, BSD, MPL-2.0, Artistic.
- `python/<pacote>/` — arquivos de licença do `dist-info` de cada pacote Python embutido (+ o campo `License`).
  PySide6/shiboken6 não trazem o texto nas wheels: a pasta deles recebe a LGPL-3.0 (e a GPL-3.0 que ela incorpora).
- `ubuntu/<pacote>/copyright` — o arquivo de copyright Debian de cada pacote do Ubuntu listado em `COMPONENTES.txt`.
- `ffmpeg-LICENSE.txt`, `ffmpeg/BIBLIOTECAS.tsv` (repositório e commit de cada biblioteca da receita),
  `ffmpeg/VERSAO.txt` (`ffmpeg -version`, com a configuração do build), `ffmpeg/FFmpeg-Builds-LICENSE` (MIT da receita).
- `exiftool-README`, `c2patool-README.md`.
- `whisper.cpp/LICENSE` (MIT; cobre whisper.cpp e ggml), `whisper.cpp/VERSAO.txt`.
- `deepfilternet/LICENSE-MIT` e `deepfilternet/LICENSE-APACHE` (DeepFilterNet, MIT OU Apache-2.0).
- `modelos/<modelo>-ORIGEM.txt` — repositório, revisão (commit), licença e SHA-256 de cada modelo de IA embutido;
  `modelos/Apache-2.0` e `modelos/whisper-MIT` com os textos das licenças.
- `python/onnxruntime/` — licença MIT e `ThirdPartyNotices.txt` do ONNX Runtime (e as de flatbuffers, protobuf...).

### Versões fixadas no build (conferidas por SHA-256 com `packaging/SHA256SUMS`)
| Componente | Versão / origem |
|---|---|
| FFmpeg (BtbN) | release datado `autobuild-2026-11-09-12-42`, arquivo `ffmpeg-n8.1.3-34-ga35157abb9-linux64-gpl-shared-8.1.tar.xz` (FFmpeg n8.1.3-34-ga35157abb9) |
| Receita do FFmpeg-Builds | commit `c9fe5114060e507409f7ee07edfd82efaf25d767` — https://github.com/BtbN/FFmpeg-Builds/tree/c9fe5114060e507409f7ee07edfd82efaf25d767 |
| ExifTool | 13.62 |
| c2patool | 0.27.22 |
| DeepFilterNet (`deep-filter`) | 0.5.6 — binário oficial `deep-filter-0.5.6-x86_64-unknown-linux-musl` |
| Modelos de IA | Hugging Face com revisão (commit) fixa; tamanho e SHA-256 fixos em `fala.MODELOS`, `objetos.MODELOS`, `separar.MODELOS` |
| whisper.cpp (código-fonte compilado no build) | v1.9.5 — `whisper.cpp-v1.9.5.tar.gz` (mesmo SHA-256 de `WHISPER_TARBALL_SHA256` em `src/ai_editor/fala.py`) |
| ONNX Runtime e demais pacotes Python | versões e hashes do `uv.lock` (extra `ia`) |
| appimagetool / runtime type2 | 1.9.1 / `20251108` |

## Oferta de código-fonte (GPL, seção 6)
- **AI Editor**: https://github.com/rlb3ssa/ai-editor — a tag `vX.Y.Z` corresponde exatamente ao AppImage `X.Y.Z`,
  e o script de empacotamento (`packaging/`) faz parte do código-fonte.
- **Binários de terceiros**: os links abaixo apontam para o código-fonte correspondente. Se algum link deixar de
  funcionar, abra uma issue no repositório que fornecemos uma cópia do código-fonte correspondente por no mínimo
  três anos a partir da data da release.

## Programas embutidos
| Componente | Onde fica no AppImage | Licença | Código-fonte |
|---|---|---|---|
| FFmpeg (ffmpeg, ffprobe, libav*) — build GPL "shared" do FFmpeg-Builds, com x264, x265, libvpx, opus, SVT-AV1, LAME, libass, rubberband, entre outras | `usr/lib/ffmpeg/` | GPL-3.0 (build com `--enable-gpl --enable-version3`); ver `ffmpeg-LICENSE.txt` | https://github.com/FFmpeg/FFmpeg (commit `a35157abb9`); receita no commit fixado acima, com o repositório e o commit de cada biblioteca em `ffmpeg/BIBLIOTECAS.tsv` |
| libmpv (mpv) e dependências, do Ubuntu 24.04 | `usr/lib/` | GPL-2.0-or-later (libmpv), demais LGPL/MIT/BSD etc. | https://github.com/mpv-player/mpv ; pacote e versão de cada `.so` em `COMPONENTES.txt`, fonte em https://launchpad.net/ubuntu/+source/ |
| MediaInfo (CLI) e dependências, do Ubuntu 24.04 | `usr/lib/mediainfo/` | BSD-2-Clause (MediaInfo/MediaInfoLib/ZenLib); dependências em `COMPONENTES.txt` | https://github.com/MediaArea/MediaInfo |
| ExifTool (Image::ExifTool) | `usr/lib/exiftool/` (o próprio código Perl) | Artistic License ou GPL-1.0-or-later (mesmos termos do Perl) | https://exiftool.org/ — o AppImage já contém o código-fonte completo |
| c2patool | `usr/bin/c2patool` | MIT ou Apache-2.0 | https://github.com/contentauth/c2pa-rs |
| deep-filter (DeepFilterNet 0.5.6, binário estático musl oficial, com o modelo DeepFilterNet3 dentro) | `usr/bin/deep-filter` | MIT ou Apache-2.0 | https://github.com/Rikorose/DeepFilterNet/tree/v0.5.6 |
| whisper-cli (whisper.cpp + ggml), compilado portátil (x86-64 genérico + variantes de CPU `libggml-cpu-*.so` escolhidas ao rodar) | `usr/lib/whisper/` (link em `usr/bin/whisper-cli`) | MIT | https://github.com/ggml-org/whisper.cpp/tree/v1.9.5 |

## Python e bibliotecas (embutidos pelo PyInstaller em `usr/bin/_internal/`)
| Componente | Licença | Código-fonte |
|---|---|---|
| CPython 3.12 | PSF-2.0 | https://github.com/python/cpython |
| PySide6 / Qt 6 (só os módulos usados) | LGPL-3.0 | https://code.qt.io/cgit/pyside/pyside-setup.git , https://code.qt.io/cgit/qt/ |
| python-mpv | GPL-2.0-or-later ou LGPL-2.1-or-later | https://github.com/jaseg/python-mpv |
| QtAwesome (+ fontes Phosphor, Material Design Icons, Font Awesome...) | MIT; fontes MIT/Apache-2.0/OFL-1.1 | https://github.com/spyder-ide/qtawesome |
| mcp (SDK Python do Model Context Protocol) e dependências (pydantic, anyio, starlette, httpx...) | MIT/BSD/Apache-2.0 | https://github.com/modelcontextprotocol/python-sdk |
| NumPy | BSD-3-Clause | https://github.com/numpy/numpy |
| ONNX Runtime (motor dos objetos com IA: SAM 2.1, LaMa) | MIT (componentes de terceiros em `ThirdPartyNotices.txt`) | https://github.com/microsoft/onnxruntime |
| FlatBuffers, Protocol Buffers (dependências do ONNX Runtime) | Apache-2.0; BSD-3-Clause | https://github.com/google/flatbuffers , https://github.com/protocolbuffers/protobuf |
| Bootloader do PyInstaller | GPL-2.0 com exceção para o bootloader | https://github.com/pyinstaller/pyinstaller |

A lista completa de pacotes Python, com versões, está em `COMPONENTES.txt`.

## Não incluído (vem do sistema)
Para funcionar com o driver de vídeo e o servidor gráfico/de som da máquina, o AppImage **usa as bibliotecas do
sistema** para: glibc, libstdc++, OpenGL/EGL/GBM/DRM, VA-API/VDPAU, X11/libxcb e suas extensões de protocolo, Wayland, ALSA/PulseAudio/PipeWire,
fontconfig/freetype/harfbuzz, zlib, D-Bus/systemd/udev. O **ExifTool** usa o `perl` do sistema.
A família **xcb-util** (libxcb-cursor, -icccm, -image, -keysyms, -render-util, -util; MIT) **vai** no AppImage: o
plugin xcb do Qt ≥ 6.5 exige a libxcb-cursor0, que falta em muitas instalações.

## Modelos de IA embutidos (`usr/share/ai-editor/modelos/`)
O AppImage não baixa nada ao rodar: os modelos usados vêm dentro dele, conferidos por SHA-256 no build.
| Modelo | Uso | Licença | Origem |
|---|---|---|---|
| Whisper `small` (ggml) | legendas e transcrição | MIT | https://huggingface.co/ggerganov/whisper.cpp (revisão em `fala.HF_REVISAO`) |
| SAM 2.1 hiera-tiny (ONNX) | segmentar/rastrear objetos | Apache-2.0 (pesos da Meta) | https://huggingface.co/onnx-community/sam2.1-hiera-tiny-ONNX |
| LaMa big-lama (ONNX) | preencher o fundo de objetos removidos | Apache-2.0 | https://huggingface.co/opencv/inpainting_lama |
| Demucs v4 htdemucs, htdemucs_ft (voz, bateria, baixo, outros) e htdemucs_6s (ONNX, pesos fp16) | separar voz e instrumentos | MIT (Meta; export ONNX do StemSplit) | https://huggingface.co/StemSplitio , código: https://github.com/facebookresearch/demucs |

Ficam de fora (só rodando do código-fonte, baixados no primeiro uso): Whisper tiny/base e quantizados e SAM 2.1 small.

## Ferramentas usadas só para construir (não distribuídas)
PyInstaller (o bootloader acima é distribuído), CMake e GCC (compilam o whisper.cpp), appimagetool e runtime do AppImage (MIT;
https://github.com/AppImage/appimagetool — o runtime vai embutido no início do arquivo AppImage, MIT),
patchelf (GPL-3.0), zsync (Artistic-2.0).
