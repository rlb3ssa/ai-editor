#!/usr/bin/env bash
# Gera o AppImage do AI Editor com tudo embutido (Python + Qt, libmpv, ffmpeg/ffprobe, mediainfo, exiftool, c2patool).
#
#   packaging/build_appimage.sh            # build completo → dist/AI_Editor-<versão>-x86_64.AppImage (+ .zsync)
#
# Requisitos (Ubuntu 24.04): uv, libmpv2, mediainfo, perl, patchelf (ou uvx), curl, xz-utils, dpkg.
# Downloads e arquivos intermediários ficam em $AI_EDITOR_BUILD_DIR (padrão ~/.cache/ai-editor-build), fora do repo.
# Variáveis úteis: FFMPEG_TAG, EXIFTOOL_VERSAO, C2PATOOL_VERSAO, JOBS (paralelismo do mksquashfs).
set -euo pipefail
export LC_ALL=C.UTF-8   # o PyInstaller lê a saída do ldconfig e não entende mensagens traduzidas

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${AI_EDITOR_BUILD_DIR:-$HOME/.cache/ai-editor-build}"
DL="$BUILD/downloads"
APPDIR="$BUILD/AppDir"
DIST="${AI_EDITOR_DIST:-$RAIZ/dist}"
JOBS="${JOBS:-4}"

# FFMPEG_ORIGEM=btbn (padrão): build GPL do FFmpeg-Builds, versão nova e independente do sistema.
# FFMPEG_ORIGEM=sistema: ffmpeg/ffprobe do Ubuntu, que reaproveitam as libav* já trazidas pela libmpv (AppImage ~70 MB menor).
FFMPEG_ORIGEM="${FFMPEG_ORIGEM:-btbn}"
FFMPEG_TAG="${FFMPEG_TAG:-n8.1}"   # build "shared" GPL do FFmpeg-Builds (BtbN): libs autocontidas, ~3x menor que 2 estáticos
FFMPEG_ARQ="ffmpeg-${FFMPEG_TAG}-latest-linux64-gpl-shared-${FFMPEG_TAG#n}.tar.xz"
FFMPEG_URL="https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/$FFMPEG_ARQ"
EXIFTOOL_VERSAO="${EXIFTOOL_VERSAO:-13.62}"
C2PATOOL_VERSAO="${C2PATOOL_VERSAO:-0.27.22}"
C2PATOOL_URL="https://github.com/contentauth/c2pa-rs/releases/download/c2patool-v${C2PATOOL_VERSAO}/c2patool-v${C2PATOOL_VERSAO}-x86_64-unknown-linux-gnu.tar.gz"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
ATUALIZACAO="gh-releases-zsync|rlb3ssa|ai-editor|latest|AI_Editor-*x86_64.AppImage.zsync"

VERSAO="$(sed -n 's/^version = "\(.*\)"/\1/p' "$RAIZ/pyproject.toml" | head -1)"
SAIDA="$DIST/AI_Editor-${VERSAO}-x86_64.AppImage"

# Bibliotecas que NÃO vão no AppImage: base do sistema e as que precisam casar com o driver/servidor gráfico/de som.
EXCLUIR='^(ld-linux|linux-vdso|libc\.so|libm\.so|libmvec|libdl\.so|libpthread|librt\.so|libresolv|libutil\.so|libcrypt\.so|libstdc\+\+|libgcc_s|libGL|libEGL|libGLX|libGLdispatch|libOpenGL|libgbm|libdrm|libva|libvdpau|libX11|libXext\.|libXau\.|libXdmcp|libXrender|libXfixes|libXi\.|libXcursor|libXrandr|libxcb|libxkbcommon|libwayland|libasound|libpulse|libpipewire|libfontconfig|libfreetype|libharfbuzz|libexpat|libz\.so|libdbus-1|libsystemd|libudev|libgpg-error|libcom_err|libuuid|libselinux|libICE|libSM|libusb)'

msg() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
memoria() { awk '/MemAvailable/ {printf "    (memória disponível: %d MB)\n", $2/1024}' /proc/meminfo; }

PATCHELF="$(command -v patchelf || true)"
[ -n "$PATCHELF" ] || PATCHELF="uvx --from patchelf patchelf"

baixar() {  # baixar URL ARQUIVO — só baixa se ainda não estiver no cache
    local url="$1" arq="$DL/$2"
    if [ ! -s "$arq" ]; then
        echo "    baixando $url"
        curl -fL --retry 3 -o "$arq.part" "$url"
        mv "$arq.part" "$arq"
    fi
}

copiar_dependencias() {  # copiar_dependencias BINARIO DESTINO [PULAR_SE_EXISTE_EM]
    local bin="$1" dest="$2" ja="${3:-}"
    ldd "$bin" | awk '/=> \// {print $3}' | while read -r lib; do
        local nome; nome="$(basename "$lib")"
        if [[ "$nome" =~ $EXCLUIR ]]; then continue; fi
        if [ -n "$ja" ] && [ -e "$ja/$nome" ]; then continue; fi
        [ -e "$dest/$nome" ] || cp -L "$lib" "$dest/$nome"
        echo "$lib" >> "$BUILD/libs-sistema.txt"
    done
}

mkdir -p "$DL" "$DIST"
: > "$BUILD/libs-sistema.txt"
memoria

# ---------------------------------------------------------------- downloads (com cache)
msg "Baixando componentes (cache em $DL)"
[ "$FFMPEG_ORIGEM" = sistema ] || baixar "$FFMPEG_URL" "$FFMPEG_ARQ"
baixar "https://sourceforge.net/projects/exiftool/files/Image-ExifTool-${EXIFTOOL_VERSAO}.tar.gz/download" "exiftool-${EXIFTOOL_VERSAO}.tar.gz" \
    || baixar "https://exiftool.org/Image-ExifTool-${EXIFTOOL_VERSAO}.tar.gz" "exiftool-${EXIFTOOL_VERSAO}.tar.gz"
baixar "$C2PATOOL_URL" "c2patool-${C2PATOOL_VERSAO}.tar.gz"
baixar "$APPIMAGETOOL_URL" "appimagetool-x86_64.AppImage"
chmod +x "$DL/appimagetool-x86_64.AppImage"
ZSYNCMAKE="$(command -v zsyncmake || true)"
if [ -z "$ZSYNCMAKE" ]; then  # sem sudo: extrai o zsyncmake do pacote .deb do Ubuntu
    if [ ! -x "$DL/zsync/usr/bin/zsyncmake" ]; then
        (cd "$DL" && apt-get download zsync >/dev/null && dpkg-deb -x zsync_*.deb zsync)
    fi
    ZSYNCMAKE="$DL/zsync/usr/bin/zsyncmake"
fi

# ---------------------------------------------------------------- Python + Qt (PyInstaller, one-dir)
msg "PyInstaller (one-dir)"
memoria
cd "$RAIZ"
uv sync --frozen 2>/dev/null || uv sync
EXCLUIR_PY=()
for m in QtWebEngineCore QtWebEngineWidgets QtWebEngineQuick QtWebChannel QtWebSockets QtWebView \
         Qt3DCore Qt3DRender Qt3DInput Qt3DLogic Qt3DExtras Qt3DAnimation QtQuick QtQuick3D QtQuickWidgets \
         QtQuickControls2 QtQml QtMultimedia QtMultimediaWidgets QtCharts QtDataVisualization QtGraphs QtPdf \
         QtPdfWidgets QtDesigner QtBluetooth QtNfc QtSql QtTest QtPositioning QtLocation QtSensors QtSerialPort \
         QtSerialBus QtRemoteObjects QtScxml QtStateMachine QtTextToSpeech QtHelp QtUiTools QtSpatialAudio \
         QtHttpServer QtAxContainer QtConcurrent QtXml QtSvgWidgets QtPrintSupport; do
    EXCLUIR_PY+=(--exclude-module "PySide6.$m")
done
uv run --frozen pyinstaller --noconfirm --clean --onedir --name ai-editor \
    --distpath "$BUILD/pyi/dist" --workpath "$BUILD/pyi/work" --specpath "$BUILD/pyi" \
    --paths "$RAIZ/src" \
    --collect-submodules ai_editor \
    --add-data "$RAIZ/src/ai_editor/recursos:ai_editor/recursos" \
    --collect-data qtawesome \
    --copy-metadata ai-editor \
    --exclude-module tkinter --exclude-module matplotlib --exclude-module IPython \
    "${EXCLUIR_PY[@]}" \
    "$RAIZ/packaging/entrada.py"

# ---------------------------------------------------------------- AppDir
msg "Montando o AppDir em $APPDIR"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" "$APPDIR/usr/share/doc/ai-editor" \
         "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp -a "$BUILD/pyi/dist/ai-editor/." "$APPDIR/usr/bin/"
INTERNO="$APPDIR/usr/bin/_internal"
# O PyInstaller pode ter recolhido a libmpv do sistema (o python-mpv usa ctypes); ela vai para usr/lib.
rm -f "$INTERNO"/libmpv.so*

# libmpv + dependências (as que o PyInstaller já trouxe são reaproveitadas via RPATH)
msg "libmpv e dependências"
LIBMPV="$(ldconfig -p | awk '/libmpv\.so\.2 .*x86-64/ {print $NF; exit}')"
[ -n "$LIBMPV" ] || { echo "libmpv.so.2 não encontrada (sudo apt install libmpv2)"; exit 1; }
cp -L "$LIBMPV" "$APPDIR/usr/lib/libmpv.so.2"
echo "$LIBMPV" >> "$BUILD/libs-sistema.txt"
copiar_dependencias "$LIBMPV" "$APPDIR/usr/lib" "$INTERNO"

if [ "$FFMPEG_ORIGEM" = sistema ]; then
    msg "ffmpeg do sistema"
    for b in ffmpeg ffprobe; do
        cp -L "$(command -v $b)" "$APPDIR/usr/bin/$b"
        echo "$(command -v $b)" >> "$BUILD/libs-sistema.txt"
        copiar_dependencias "$APPDIR/usr/bin/$b" "$APPDIR/usr/lib" "$INTERNO"
        $PATCHELF --force-rpath --set-rpath '$ORIGIN/../lib:$ORIGIN/_internal' "$APPDIR/usr/bin/$b"
    done
fi

msg "Podando o bundle do PyInstaller"
python3 "$RAIZ/packaging/podar.py" "$APPDIR" "$EXCLUIR"
for so in "$APPDIR"/usr/lib/*.so*; do
    $PATCHELF --force-rpath --set-rpath '$ORIGIN:$ORIGIN/../bin/_internal' "$so"
done

if [ "$FFMPEG_ORIGEM" = sistema ]; then
    FFMPEG_VER="$("$APPDIR/usr/bin/ffmpeg" -hide_banner -version | sed -n '1s/ffmpeg version \([^ ]*\).*/\1/p')"
    FFMPEG_ARQ="pacote ffmpeg do Ubuntu (ver lista de bibliotecas abaixo)"
else
# ffmpeg/ffprobe (com as libs próprias, isoladas em usr/lib/ffmpeg; RPATH $ORIGIN/../lib já vem do build)
msg "ffmpeg $FFMPEG_TAG"
rm -rf "$BUILD/ffmpeg-x" && mkdir -p "$BUILD/ffmpeg-x"
tar xf "$DL/$FFMPEG_ARQ" -C "$BUILD/ffmpeg-x"
FF="$(ls -d "$BUILD"/ffmpeg-x/*/)"
mkdir -p "$APPDIR/usr/lib/ffmpeg/bin" "$APPDIR/usr/lib/ffmpeg/lib"
cp "$FF/bin/ffmpeg" "$FF/bin/ffprobe" "$APPDIR/usr/lib/ffmpeg/bin/"
for so in "$FF"/lib/*.so.*; do  # só os nomes com soname (sem os links de desenvolvimento .so)
    [[ "$(basename "$so")" =~ \.so\.[0-9]+$ ]] && cp -L "$so" "$APPDIR/usr/lib/ffmpeg/lib/"
done
ln -s ../lib/ffmpeg/bin/ffmpeg "$APPDIR/usr/bin/ffmpeg"
ln -s ../lib/ffmpeg/bin/ffprobe "$APPDIR/usr/bin/ffprobe"
cp "$FF/LICENSE.txt" "$APPDIR/usr/share/doc/ai-editor/ffmpeg-LICENSE.txt"
FFMPEG_VER="$("$APPDIR/usr/bin/ffmpeg" -hide_banner -version | sed -n '1s/ffmpeg version \([^ ]*\).*/\1/p')"
fi

# mediainfo (do sistema, com as dependências; isolado em usr/lib/mediainfo)
msg "mediainfo"
MI="$(command -v mediainfo)" || { echo "mediainfo não encontrado (sudo apt install mediainfo)"; exit 1; }
mkdir -p "$APPDIR/usr/lib/mediainfo"
cp -L "$MI" "$APPDIR/usr/lib/mediainfo/mediainfo"
echo "$MI" >> "$BUILD/libs-sistema.txt"
copiar_dependencias "$MI" "$APPDIR/usr/lib/mediainfo" "$INTERNO"
for f in "$APPDIR"/usr/lib/mediainfo/*; do
    $PATCHELF --force-rpath --set-rpath '$ORIGIN:$ORIGIN/../../bin/_internal' "$f"
done
ln -s ../lib/mediainfo/mediainfo "$APPDIR/usr/bin/mediainfo"

# exiftool (Perl puro; usa o perl do sistema)
msg "exiftool $EXIFTOOL_VERSAO"
rm -rf "$BUILD/exiftool-x" && mkdir -p "$BUILD/exiftool-x"
tar xzf "$DL/exiftool-${EXIFTOOL_VERSAO}.tar.gz" -C "$BUILD/exiftool-x"
ET="$(ls -d "$BUILD"/exiftool-x/*/)"
mkdir -p "$APPDIR/usr/lib/exiftool"
cp -a "$ET/exiftool" "$ET/lib" "$APPDIR/usr/lib/exiftool/"
cat > "$APPDIR/usr/bin/exiftool" <<'EOF'
#!/bin/sh
# exiftool do pacote: roda com o perl do sistema (presente em praticamente todo Linux).
exec perl "$(dirname "$(readlink -f "$0")")/../lib/exiftool/exiftool" "$@"
EOF
chmod +x "$APPDIR/usr/bin/exiftool"

# c2patool (binário oficial, sem dependências além da libc)
msg "c2patool $C2PATOOL_VERSAO"
tar xzf "$DL/c2patool-${C2PATOOL_VERSAO}.tar.gz" -C "$BUILD" c2patool/c2patool c2patool/README.md
install -m755 "$BUILD/c2patool/c2patool" "$APPDIR/usr/bin/c2patool"

# ---------------------------------------------------------------- integração e documentos
msg "Atalho, ícone, licenças"
cp "$RAIZ/packaging/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp "$RAIZ/src/ai_editor/recursos/ai-editor.png" "$APPDIR/ai-editor.png"   # .DirIcon precisa ser PNG
cp "$RAIZ/src/ai_editor/recursos/ai-editor.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/ai-editor.svg"
ln -s ai-editor.png "$APPDIR/.DirIcon"
cat > "$APPDIR/ai-editor.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AI Editor
GenericName=Player e editor de vídeo
Comment=Player e editor de vídeo e áudio controlado por IA (MCP)
Exec=ai-editor %f
Icon=ai-editor
Terminal=false
Categories=AudioVideo;Video;Player;AudioVideoEditing;
StartupWMClass=ai-editor
MimeType=video/mp4;video/x-matroska;video/webm;video/quicktime;video/x-msvideo;audio/mpeg;audio/flac;audio/ogg;audio/x-wav;
X-AppImage-Version=$VERSAO
EOF
cp "$APPDIR/ai-editor.desktop" "$APPDIR/usr/share/applications/"

DOC="$APPDIR/usr/share/doc/ai-editor"
cp "$RAIZ/LICENSE" "$DOC/LICENSE"
cp "$RAIZ/packaging/LICENCAS_TERCEIROS.md" "$DOC/"
cp "$ET/README" "$DOC/exiftool-README" 2>/dev/null || true
cp "$BUILD/c2patool/README.md" "$DOC/c2patool-README.md"
# Inventário exato do que foi empacotado (versões, pacotes Debian de origem e links para o código-fonte).
{
    echo "# Componentes deste AppImage (gerado em $(date -u +%Y-%m-%dT%H:%MZ) por packaging/build_appimage.sh)"
    echo
    echo "AI Editor $VERSAO — GPL-3.0-or-later — https://github.com/rlb3ssa/ai-editor"
    echo "ffmpeg/ffprobe $FFMPEG_VER — GPL-3.0 (build $FFMPEG_ARQ) — fonte: https://github.com/FFmpeg/FFmpeg ; receita: https://github.com/BtbN/FFmpeg-Builds"
    echo "exiftool $EXIFTOOL_VERSAO — Artistic/GPL-1.0+ — fonte: https://exiftool.org/ (incluso: usr/lib/exiftool)"
    echo "c2patool $C2PATOOL_VERSAO — MIT/Apache-2.0 — fonte: https://github.com/contentauth/c2pa-rs/tree/c2patool-v$C2PATOOL_VERSAO"
    echo
    echo "## Pacotes Python (embutidos pelo PyInstaller; PySide6 traz o Qt $(uv run --frozen python -c 'import PySide6.QtCore as q; print(q.qVersion())' 2>/dev/null))"
    (cd "$RAIZ" && uv export --frozen --no-dev --no-hashes --no-emit-project --no-header | grep -v '^ *#' | sed 's/ ;.*//')
    echo
    echo "## Programas e bibliotecas copiados do Ubuntu $(. /etc/os-release && echo "$VERSION_ID") (libmpv, mediainfo e dependências)"
    echo "Código-fonte de cada pacote: https://launchpad.net/ubuntu/+source/<pacote-fonte>/<versão>"
    echo "(ou: apt-get source <pacote-fonte>=<versão>)"
    echo
    # Tudo o que está no AppDir e veio do sistema (inclusive o que o PyInstaller recolheu para _internal).
    {
        cat "$BUILD/libs-sistema.txt"
        find "$APPDIR/usr" -name '*.so*' -not -path '*/PySide6/*' -not -path '*/ffmpeg/*' -not -path '*.libs/*' \
            -printf '%f\n' | while read -r n; do ldconfig -p | awk -v n="$n" '$1 == n && /x86-64/ {print $NF; exit}'; done
    } | awk -F/ '!v[$NF]++' | while read -r lib; do
        pkg="$(dpkg -S "$(readlink -f "$lib")" 2>/dev/null | head -1 | sed 's/: .*//')"
        [ -n "$pkg" ] || pkg="$(dpkg -S "$lib" 2>/dev/null | head -1 | sed 's/: .*//')"
        if [ -n "$pkg" ]; then
            fonte="$(dpkg-query -W -f='${source:Package} ${source:Version}\n' "$pkg" 2>/dev/null | head -1)"
            echo "$(basename "$lib")  ←  $pkg  (fonte: ${fonte% *} ${fonte#* }: https://launchpad.net/ubuntu/+source/${fonte% *}/${fonte#* })"
        else
            echo "$(basename "$lib")  ←  (pacote desconhecido)"
        fi
    done
} > "$DOC/COMPONENTES.txt"
cp "$DOC/COMPONENTES.txt" "$DIST/COMPONENTES.txt"

# ---------------------------------------------------------------- AppImage + zsync
msg "appimagetool"
memoria
du -sh "$APPDIR"
rm -f "$SAIDA" "$SAIDA.zsync"
export ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1
# zstd nível 19 com blocos de 1 MiB: ~7% menor que o padrão e descompressão rápida (COMP=xz: mais ~8% menor, abre mais devagar).
COMP="${COMP:-zstd}"
OPCOES_COMP=(--comp "$COMP" --mksquashfs-opt -b --mksquashfs-opt 1M)
[ "$COMP" = zstd ] && OPCOES_COMP+=(--mksquashfs-opt -Xcompression-level --mksquashfs-opt 19)
# (roda dentro de dist/: o appimagetool grava o .zsync no diretório atual)
(cd "$DIST" && PATH="$(dirname "$ZSYNCMAKE"):$PATH" "$DL/appimagetool-x86_64.AppImage" --no-appstream \
    --mksquashfs-opt -processors --mksquashfs-opt "$JOBS" "${OPCOES_COMP[@]}" \
    -u "$ATUALIZACAO" "$APPDIR" "$SAIDA")
if [ ! -f "$SAIDA.zsync" ]; then  # appimagetool sem zsyncmake no PATH: gera aqui
    (cd "$DIST" && "$ZSYNCMAKE" -u "$(basename "$SAIDA")" -o "$(basename "$SAIDA").zsync" "$(basename "$SAIDA")")
fi
chmod +x "$SAIDA"

msg "Bundle .mcpb (Claude Desktop) e documentos da release"
(cd "$RAIZ" && uv run --frozen python packaging/mcpb/empacotar_mcpb.py)
cp "$RAIZ/packaging/LICENCAS_TERCEIROS.md" "$DIST/"
msg "Pronto: $SAIDA ($(du -h "$SAIDA" | cut -f1))"
ls -la "$DIST"
