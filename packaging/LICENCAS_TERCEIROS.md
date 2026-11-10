# AI Editor — componentes de terceiros e licenças

O AI Editor é software livre sob a **GPL-3.0-or-later** (arquivo `LICENSE`). O AppImage distribui, junto com o
programa, os componentes abaixo. Cada um continua sob a própria licença; todas são compatíveis com a GPL-3.0.
As versões exatas de cada build estão em `COMPONENTES.txt` (gerado pelo `packaging/build_appimage.sh` e
incluído no AppImage em `usr/share/doc/ai-editor/` e na página da Release).

## Oferta de código-fonte (GPL, seção 6)
- **AI Editor**: https://github.com/rlb3ssa/ai-editor — a tag `vX.Y.Z` corresponde exatamente ao AppImage `X.Y.Z`,
  e o script de empacotamento (`packaging/`) faz parte do código-fonte.
- **Binários de terceiros**: os links abaixo apontam para o código-fonte correspondente. Se algum link deixar de
  funcionar, abra uma issue no repositório que fornecemos uma cópia do código-fonte correspondente por no mínimo
  três anos a partir da data da release.

## Programas embutidos
| Componente | Onde fica no AppImage | Licença | Código-fonte |
|---|---|---|---|
| FFmpeg (ffmpeg, ffprobe, libav*) — build GPL "shared" do FFmpeg-Builds, com x264, x265, libvpx, opus, SVT-AV1, LAME, libass, rubberband, entre outras | `usr/lib/ffmpeg/` | GPL-3.0 (build com `--enable-gpl --enable-version3`); ver `ffmpeg-LICENSE.txt` | https://github.com/FFmpeg/FFmpeg (tag da versão em `COMPONENTES.txt`); receita do build e versões de cada biblioteca: https://github.com/BtbN/FFmpeg-Builds |
| libmpv (mpv) e dependências, do Ubuntu 24.04 | `usr/lib/` | GPL-2.0-or-later (libmpv), demais LGPL/MIT/BSD etc. | https://github.com/mpv-player/mpv ; pacote e versão de cada `.so` em `COMPONENTES.txt`, fonte em https://launchpad.net/ubuntu/+source/ |
| MediaInfo (CLI) e dependências, do Ubuntu 24.04 | `usr/lib/mediainfo/` | BSD-2-Clause (MediaInfo/MediaInfoLib/ZenLib); dependências em `COMPONENTES.txt` | https://github.com/MediaArea/MediaInfo |
| ExifTool (Image::ExifTool) | `usr/lib/exiftool/` (o próprio código Perl) | Artistic License ou GPL-1.0-or-later (mesmos termos do Perl) | https://exiftool.org/ — o AppImage já contém o código-fonte completo |
| c2patool | `usr/bin/c2patool` | MIT ou Apache-2.0 | https://github.com/contentauth/c2pa-rs |

## Python e bibliotecas (embutidos pelo PyInstaller em `usr/bin/_internal/`)
| Componente | Licença | Código-fonte |
|---|---|---|
| CPython 3.12 | PSF-2.0 | https://github.com/python/cpython |
| PySide6 / Qt 6 (só os módulos usados) | LGPL-3.0 | https://code.qt.io/cgit/pyside/pyside-setup.git , https://code.qt.io/cgit/qt/ |
| python-mpv | GPL-2.0-or-later ou LGPL-2.1-or-later | https://github.com/jaseg/python-mpv |
| QtAwesome (+ fontes Phosphor, Material Design Icons, Font Awesome...) | MIT; fontes MIT/Apache-2.0/OFL-1.1 | https://github.com/spyder-ide/qtawesome |
| mcp (SDK Python do Model Context Protocol) e dependências (pydantic, anyio, starlette, httpx...) | MIT/BSD/Apache-2.0 | https://github.com/modelcontextprotocol/python-sdk |
| NumPy | BSD-3-Clause | https://github.com/numpy/numpy |
| Bootloader do PyInstaller | GPL-2.0 com exceção para o bootloader | https://github.com/pyinstaller/pyinstaller |

A lista completa de pacotes Python, com versões, está em `COMPONENTES.txt`.

## Não incluído (vem do sistema)
Para funcionar com o driver de vídeo e o servidor gráfico/de som da máquina, o AppImage **usa as bibliotecas do
sistema** para: glibc, libstdc++, OpenGL/EGL/GBM/DRM, VA-API/VDPAU, X11/xcb, Wayland, ALSA/PulseAudio/PipeWire,
fontconfig/freetype/harfbuzz, zlib, D-Bus/systemd/udev. O **ExifTool** usa o `perl` do sistema.

## Ferramentas usadas só para construir (não distribuídas)
PyInstaller (o bootloader acima é distribuído), appimagetool e runtime do AppImage (MIT;
https://github.com/AppImage/appimagetool — o runtime vai embutido no início do arquivo AppImage, MIT),
patchelf (GPL-3.0), zsync (Artistic-2.0).
