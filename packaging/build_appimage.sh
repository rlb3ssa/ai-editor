#!/usr/bin/env bash
# Gera o AppImage do AI Editor com tudo embutido (Python + Qt + ONNX Runtime, libmpv, ffmpeg/ffprobe, mediainfo,
# exiftool, c2patool, whisper-cli portátil, deep-filter) e os modelos de IA: o AppImage não baixa nada ao rodar.
#
#   packaging/build_appimage.sh            # build completo → dist/AI_Editor-<versão>-x86_64.AppImage (+ .zsync)
#
# Requisitos (Ubuntu 24.04): uv, libmpv2, mediainfo, perl, patchelf (ou uvx), curl, xz-utils, dpkg,
# g++ e make (build-essential) e cmake (do sistema ou, sem ele, o pacote pip `cmake` via uvx) para o whisper.cpp.
# Downloads e arquivos intermediários ficam em $AI_EDITOR_BUILD_DIR (padrão ~/.cache/ai-editor-build), fora do repo.
# Variáveis úteis: FFMPEG_ORIGEM, COMP, JOBS (paralelismo do mksquashfs).
#
# Tudo o que é baixado tem versão fixa e é conferido com packaging/SHA256SUMS (sha256sum -c) logo após o download.
# Para atualizar um componente: troque a versão abaixo, rode `packaging/build_appimage.sh --atualizar-sums` (baixa e
# grava os novos hashes; confira-os com os publicados pelo projeto de origem) e commite os dois arquivos juntos.
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
# Build "shared" GPL do FFmpeg-Builds (BtbN): libs autocontidas, ~3x menor que 2 estáticos. Release DATADO (o "latest"
# muda todo dia). O BtbN apaga autobuilds antigos depois de algumas semanas: se o download sumir, mude para um release
# novo (FFMPEG_RELEASE, FFMPEG_ARQ e FFMPEG_RECEITA = commit da tag) e atualize o SHA256SUMS.
FFMPEG_RELEASE="autobuild-2026-11-09-12-42"
FFMPEG_RECEITA="c9fe5114060e507409f7ee07edfd82efaf25d767"   # commit do BtbN/FFmpeg-Builds desse release
FFMPEG_ARQ="ffmpeg-n8.1.3-34-ga35157abb9-linux64-gpl-shared-8.1.tar.xz"
FFMPEG_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/$FFMPEG_RELEASE/$FFMPEG_ARQ"
EXIFTOOL_VERSAO="13.62"
C2PATOOL_VERSAO="0.27.22"
# whisper.cpp: a versão vem de src/ai_editor/fala.py (WHISPER_VERSAO), a mesma do build local sob demanda; o tarball
# é conferido aqui pelo SHA256SUMS e de novo pelo fala.py (WHISPER_TARBALL_SHA256).
WHISPER_VERSAO="$(sed -n 's/^WHISPER_VERSAO = "\(.*\)"/\1/p' "$RAIZ/src/ai_editor/fala.py")"
WHISPER_URL="https://github.com/ggml-org/whisper.cpp/archive/refs/tags/${WHISPER_VERSAO}.tar.gz"
WHISPER_ARQ="whisper.cpp-${WHISPER_VERSAO}.tar.gz"
# DeepFilterNet (deep-filter, binário musl estático oficial): versão e SHA-256 de src/ai_editor/deepfilter.py, repetidos
# no SHA256SUMS. As licenças (MIT e Apache-2.0) vêm do repositório na mesma tag.
DF_VERSAO="$(sed -n 's/^VERSAO = "\(.*\)"/\1/p' "$RAIZ/src/ai_editor/deepfilter.py")"
DF_URL="https://github.com/Rikorose/DeepFilterNet/releases/download/v${DF_VERSAO}/deep-filter-${DF_VERSAO}-x86_64-unknown-linux-musl"
DF_ARQ="deep-filter-${DF_VERSAO}-x86_64-unknown-linux-musl"
DF_LICENCAS="https://raw.githubusercontent.com/Rikorose/DeepFilterNet/v${DF_VERSAO}"
# Modelos de IA embutidos (baixados pelo próprio código do app, com revisão, tamanho e SHA-256 fixos; cache em
# $BUILD/modelos-cache). Separação: os marcados "embutir": True em separar.MODELOS (separar:embutidos). Ficam de fora
# (sob demanda só no código-fonte): whisper tiny/base/q5_1 e SAM 2.1 small.
MODELOS_EMBUTIDOS="${MODELOS_EMBUTIDOS:-whisper:small objetos:sam2.1-tiny objetos:lama separar:embutidos}"
C2PATOOL_URL="https://github.com/contentauth/c2pa-rs/releases/download/c2patool-v${C2PATOOL_VERSAO}/c2patool-v${C2PATOOL_VERSAO}-x86_64-unknown-linux-gnu.tar.gz"
APPIMAGETOOL_VERSAO="1.9.1"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_VERSAO/appimagetool-x86_64.AppImage"
RUNTIME_VERSAO="20251108"   # runtime type2 embutido no AppImage (sem isso o appimagetool baixa o "continuous")
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/$RUNTIME_VERSAO/runtime-x86_64"
SUMS="$RAIZ/packaging/SHA256SUMS"
ATUALIZAR_SUMS=0
[ "${1:-}" = --atualizar-sums ] && ATUALIZAR_SUMS=1
ATUALIZACAO="gh-releases-zsync|rlb3ssa|ai-editor|latest|AI_Editor-*x86_64.AppImage.zsync"

VERSAO="$(sed -n 's/^version = "\(.*\)"/\1/p' "$RAIZ/pyproject.toml" | head -1)"
SAIDA="$DIST/AI_Editor-${VERSAO}-x86_64.AppImage"

# Bibliotecas que NÃO vão no AppImage: base do sistema e as que precisam casar com o driver/servidor gráfico/de som.
# Exceção proposital: a família xcb-util (libxcb-cursor, -icccm, -image, -keysyms, -render-util, -util) VAI no pacote —
# o plugin xcb do Qt ≥ 6.5 precisa da libxcb-cursor0, que falta em muitas instalações (o programa nem abriria).
EXCLUIR='^(ld-linux|linux-vdso|libc\.so|libm\.so|libmvec|libdl\.so|libpthread|librt\.so|libresolv|libutil\.so|libcrypt\.so|libstdc\+\+|libgcc_s|libGL|libEGL|libGLX|libGLdispatch|libOpenGL|libgbm|libdrm|libva|libvdpau|libX11|libXext\.|libXau\.|libXdmcp|libXrender|libXfixes|libXi\.|libXcursor|libXrandr|libxcb(\.so|-(composite|damage|dpms|dri2|dri3|glx|present|randr|record|render|res|screensaver|shape|shm|sync|xf86dri|xfixes|xinerama|xinput|xkb|xtest|xv|xvmc)\.so)|libxkbcommon|libwayland|libasound|libpulse|libpipewire|libfontconfig|libfreetype|libharfbuzz|libexpat|libz\.so|libdbus-1|libsystemd|libudev|libgpg-error|libcom_err|libuuid|libselinux|libICE|libSM|libusb)'

msg() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
memoria() { awk '/MemAvailable/ {printf "    (memória disponível: %d MB)\n", $2/1024}' /proc/meminfo; }

PATCHELF="$(command -v patchelf || true)"
[ -n "$PATCHELF" ] || PATCHELF="uvx --from patchelf patchelf"

conferir() {  # conferir ARQUIVO — confere o hash com packaging/SHA256SUMS (ou o registra, com --atualizar-sums)
    local nome="$1"
    if [ $ATUALIZAR_SUMS = 1 ]; then
        { grep -v "  $nome\$" "$SUMS" 2>/dev/null || true; (cd "$DL" && sha256sum "$nome"); } | sort -k2 > "$SUMS.novo"
        mv "$SUMS.novo" "$SUMS"
        return 0
    fi
    grep -q "  $nome\$" "$SUMS" || { echo "    $nome não está em packaging/SHA256SUMS (use --atualizar-sums)"; return 1; }
    (cd "$DL" && grep "  $nome\$" "$SUMS" | sha256sum -c --quiet -)
}

baixar() {  # baixar URL ARQUIVO — só baixa se ainda não estiver no cache; sempre confere o SHA-256
    local url="$1" nome="$2" arq="$DL/$2"
    if [ ! -s "$arq" ]; then
        echo "    baixando $url"
        rm -f "$arq.part"
        curl -fL --retry 3 -o "$arq.part" "$url" || { rm -f "$arq.part"; return 1; }
        mv "$arq.part" "$arq"
    fi
    if ! conferir "$nome"; then
        echo "    SHA-256 de $nome não confere: arquivo removido do cache (download truncado ou alterado)" >&2
        rm -f "$arq"
        return 1
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
baixar "$WHISPER_URL" "$WHISPER_ARQ"
baixar "$DF_URL" "$DF_ARQ"
baixar "$DF_LICENCAS/LICENSE-MIT" "deepfilternet-${DF_VERSAO}-LICENSE-MIT"
baixar "$DF_LICENCAS/LICENSE-APACHE" "deepfilternet-${DF_VERSAO}-LICENSE-APACHE"
APPIMAGETOOL="$DL/appimagetool-${APPIMAGETOOL_VERSAO}-x86_64.AppImage"
baixar "$APPIMAGETOOL_URL" "$(basename "$APPIMAGETOOL")"
chmod +x "$APPIMAGETOOL"
baixar "$RUNTIME_URL" "runtime-${RUNTIME_VERSAO}-x86_64"
if [ $ATUALIZAR_SUMS = 1 ]; then
    echo "packaging/SHA256SUMS atualizado:"; cat "$SUMS"; exit 0
fi
ZSYNCMAKE="$(command -v zsyncmake || true)"
if [ -z "$ZSYNCMAKE" ]; then  # sem sudo: extrai o zsyncmake do pacote .deb do Ubuntu (apt confere a assinatura)
    if [ ! -x "$DL/zsync/usr/bin/zsyncmake" ]; then
        (cd "$DL" && apt-get download zsync >/dev/null && dpkg-deb -x zsync_*.deb zsync)
    fi
    ZSYNCMAKE="$DL/zsync/usr/bin/zsyncmake"
fi
# Receita do FFmpeg-Builds no commit do release (lista das bibliotecas embutidas no ffmpeg, com repositório e commit).
# Buscada por hash de commit: o git garante a integridade.
RECEITA="$BUILD/btbn-receita"
if [ "$FFMPEG_ORIGEM" != sistema ] && [ "$(git -C "$RECEITA" rev-parse HEAD 2>/dev/null)" != "$FFMPEG_RECEITA" ]; then
    rm -rf "$RECEITA" && mkdir -p "$RECEITA"
    git -C "$RECEITA" init -q
    git -C "$RECEITA" fetch -q --depth 1 https://github.com/BtbN/FFmpeg-Builds "$FFMPEG_RECEITA"
    git -C "$RECEITA" checkout -q FETCH_HEAD
fi

# ---------------------------------------------------------------- Python + Qt (PyInstaller, one-dir)
msg "PyInstaller (one-dir)"
memoria
cd "$RAIZ"
# --extra ia: ONNX Runtime (+ flatbuffers, protobuf, packaging), versões e hashes do uv.lock (objetos com IA).
uv sync --frozen --extra ia
EXCLUIR_PY=()
for m in QtWebEngineCore QtWebEngineWidgets QtWebEngineQuick QtWebChannel QtWebSockets QtWebView \
         Qt3DCore Qt3DRender Qt3DInput Qt3DLogic Qt3DExtras Qt3DAnimation QtQuick QtQuick3D QtQuickWidgets \
         QtQuickControls2 QtQml QtMultimedia QtMultimediaWidgets QtCharts QtDataVisualization QtGraphs QtPdf \
         QtPdfWidgets QtDesigner QtBluetooth QtNfc QtSql QtTest QtPositioning QtLocation QtSensors QtSerialPort \
         QtSerialBus QtRemoteObjects QtScxml QtStateMachine QtTextToSpeech QtHelp QtUiTools QtSpatialAudio \
         QtHttpServer QtAxContainer QtConcurrent QtXml QtSvgWidgets QtPrintSupport; do
    EXCLUIR_PY+=(--exclude-module "PySide6.$m")
done
uv run --frozen --extra ia pyinstaller --noconfirm --clean --onedir --name ai-editor \
    --distpath "$BUILD/pyi/dist" --workpath "$BUILD/pyi/work" --specpath "$BUILD/pyi" \
    --paths "$RAIZ/src" \
    --collect-submodules ai_editor \
    --add-data "$RAIZ/src/ai_editor/recursos:ai_editor/recursos" \
    --collect-data qtawesome \
    --hidden-import onnxruntime \
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
msg "ffmpeg ($FFMPEG_RELEASE)"
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
# O tarball do BtbN só traz o LICENSE.txt do FFmpeg; as bibliotecas linkadas estaticamente nas libav* (x264, x265,
# libvpx, dav1d...) vêm da receita: registra repositório + commit de cada uma e copia a licença da própria receita.
mkdir -p "$APPDIR/usr/share/doc/ai-editor/ffmpeg"
cp "$RECEITA/LICENSE" "$APPDIR/usr/share/doc/ai-editor/ffmpeg/FFmpeg-Builds-LICENSE"
{
    echo "# Bibliotecas da receita BtbN/FFmpeg-Builds @ $FFMPEG_RECEITA ($FFMPEG_RELEASE, variante linux64-gpl-shared)"
    echo "# Receita: https://github.com/BtbN/FFmpeg-Builds/tree/$FFMPEG_RECEITA"
    echo "# Nem todas entram na variante linux64; as habilitadas aparecem em \`ffmpeg -version\` (configuration)."
    echo "# Cada linha: script da receita, repositório (código-fonte) e commit usados."
    echo "# Textos das licenças comuns (GPL/LGPL/BSD/Apache/MPL): usr/share/doc/ai-editor/licencas-comuns/"
    find "$RECEITA/scripts.d" -name '*.sh' | sort | while read -r sc; do
        repo="$(sed -n 's/^SCRIPT_REPO="\(.*\)"/\1/p' "$sc" | head -1)"
        com="$(sed -n 's/^SCRIPT_\(COMMIT\|REV\|TAG\)="\(.*\)"/\2/p' "$sc" | head -1)"
        if [ -n "$repo" ]; then printf '%s\t%s\t%s\n' "${sc#"$RECEITA"/scripts.d/}" "$repo" "$com"; fi
    done
} > "$APPDIR/usr/share/doc/ai-editor/ffmpeg/BIBLIOTECAS.tsv"
"$APPDIR/usr/bin/ffmpeg" -hide_banner -version > "$APPDIR/usr/share/doc/ai-editor/ffmpeg/VERSAO.txt"
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

# whisper-cli PORTÁTIL (whisper.cpp): sem -march=native; a ggml vem em .so com o backend de CPU em todas as variantes
# x86-64 (SSE4.2/AVX/AVX2/AVX-512/AMX...), escolhida ao rodar. Tudo em usr/lib/whisper com RPATH $ORIGIN (a ggml procura
# as variantes na pasta do executável real). Compilação demorada (~5–10 min com -j4): fica em cache em $BUILD,
# invalidada se mudar a versão ou as opções de compilação no fala.py.
msg "whisper-cli $WHISPER_VERSAO (portátil)"
memoria
CHAVE_WHISPER="$(cd "$RAIZ" && uv run --frozen --extra ia python -c \
    'from ai_editor import fala; print(fala.WHISPER_VERSAO, fala.WHISPER_TARBALL_SHA256, fala._OPCOES_CMAKE, fala._OPCOES_PORTATIL)' \
    | sha256sum | cut -c1-16)"
WCACHE="$BUILD/whisper-portatil-$CHAVE_WHISPER"
if [ ! -x "$WCACHE/whisper-cli" ]; then
    rm -rf "$BUILD"/whisper-portatil-*
    (cd "$RAIZ" && AI_EDITOR_BUILD_DIR="$BUILD" uv run --frozen --extra ia python -m ai_editor.fala \
        --compilar-portatil "$WCACHE.tmp" --tarball "$DL/$WHISPER_ARQ" --jobs "$JOBS")
    mv "$WCACHE.tmp" "$WCACHE"
fi
mkdir -p "$APPDIR/usr/lib/whisper"
cp -a "$WCACHE/." "$APPDIR/usr/lib/whisper/"
ln -s ../lib/whisper/whisper-cli "$APPDIR/usr/bin/whisper-cli"
# Conferências: RPATH $ORIGIN, nada "not found" e só bibliotecas de base que toda distro tem (glibc, libstdc++, libgcc_s).
for f in "$APPDIR"/usr/lib/whisper/*; do
    readelf -d "$f" | grep -qE '(RPATH|RUNPATH).*\[\$ORIGIN\]' || { echo "    $f sem RPATH \$ORIGIN"; exit 1; }
    if env -u LD_LIBRARY_PATH ldd "$f" | grep -q 'not found'; then echo "    $f: dependência ausente"; ldd "$f"; exit 1; fi
    for n in $(readelf -d "$f" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p'); do
        [[ "$n" =~ ^(libc\.so|libm\.so|libstdc\+\+\.so|libgcc_s\.so|ld-linux|libwhisper\.so|libggml) ]] \
            || { echo "    $(basename "$f") depende de $n (fora da base do sistema)"; exit 1; }
    done
done
echo "    símbolos mais novos exigidos: $(objdump -T "$APPDIR"/usr/lib/whisper/* 2>/dev/null \
    | grep -oE 'GLIBC(XX)?_[0-9.]+' | sort -Vu | awk -F_ '{m[$1]=$0} END {print m["GLIBC"], m["GLIBCXX"]}')"
env -u LD_LIBRARY_PATH "$APPDIR/usr/bin/whisper-cli" --version > "$BUILD/whisper-versao.txt" 2>&1 || true
grep -q "version" "$BUILD/whisper-versao.txt" || { echo "    whisper-cli não roda"; cat "$BUILD/whisper-versao.txt"; exit 1; }
du -sh "$APPDIR/usr/lib/whisper"

# deep-filter (DeepFilterNet; binário estático musl, modelo DeepFilterNet3 dentro dele)
msg "deep-filter $DF_VERSAO"
install -m755 "$DL/$DF_ARQ" "$APPDIR/usr/bin/deep-filter"
"$APPDIR/usr/bin/deep-filter" --version

# Modelos de IA (somente leitura em usr/share/ai-editor/modelos, mesma estrutura de ~/.local/share/ai-editor/modelos;
# o app procura aqui primeiro — binarios.pastas_modelos)
msg "Modelos de IA embutidos"
memoria
mkdir -p "$APPDIR/usr/share/ai-editor/modelos"
# shellcheck disable=SC2086
(cd "$RAIZ" && uv run --frozen --extra ia python packaging/modelos.py "$BUILD/modelos-cache" \
    "$APPDIR/usr/share/ai-editor/modelos" $MODELOS_EMBUTIDOS)
du -sh "$APPDIR/usr/share/ai-editor/modelos"

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
# whisper.cpp e ggml: MIT (o LICENSE do repositório cobre os dois)
mkdir -p "$DOC/whisper.cpp"
tar xzf "$DL/$WHISPER_ARQ" -O "whisper.cpp-${WHISPER_VERSAO#v}/LICENSE" > "$DOC/whisper.cpp/LICENSE"
test -s "$DOC/whisper.cpp/LICENSE"
"$APPDIR/usr/bin/whisper-cli" --version > "$DOC/whisper.cpp/VERSAO.txt" 2>&1 || true
mkdir -p "$DOC/deepfilternet"
cp "$DL/deepfilternet-${DF_VERSAO}-LICENSE-MIT" "$DOC/deepfilternet/LICENSE-MIT"
cp "$DL/deepfilternet-${DF_VERSAO}-LICENSE-APACHE" "$DOC/deepfilternet/LICENSE-APACHE"
# Modelos: procedência (repositório, revisão, licença, SHA-256) de cada um; textos MIT/Apache-2.0 ao lado.
mkdir -p "$DOC/modelos"
for d in "$APPDIR"/usr/share/ai-editor/modelos/*/; do
    n="$(basename "$d")"
    if [ -f "$d/ORIGEM.txt" ]; then cp "$d/ORIGEM.txt" "$DOC/modelos/$n-ORIGEM.txt"; fi
done
cp "$DOC/whisper.cpp/LICENSE" "$DOC/modelos/whisper-MIT"
# Textos das licenças comuns (o Qt/PySide6/shiboken6 são LGPL-3.0 — as wheels não trazem o texto — e várias libs do
# ffmpeg e do Ubuntu são GPL/LGPL/BSD/Apache/MPL).
mkdir -p "$DOC/licencas-comuns"
for l in GPL-2 GPL-3 LGPL-2.1 LGPL-3 Apache-2.0 BSD MPL-2.0 Artistic; do
    cp -L "/usr/share/common-licenses/$l" "$DOC/licencas-comuns/$l"
done
cp "$DOC/licencas-comuns/Apache-2.0" "$DOC/modelos/Apache-2.0"
# Pacotes Python embutidos: os arquivos de licença dos dist-info (PySide6/shiboken6: LGPL-3 acima + o campo License).
mkdir -p "$DOC/python"
SITE="$(cd "$RAIZ" && uv run --frozen --extra ia python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
(cd "$RAIZ" && uv export --frozen --no-dev --extra ia --no-hashes --no-emit-project --no-header) | grep -v '^ *#' \
    | sed -n 's/^\([A-Za-z0-9_.-]*\)==\([^ ;]*\).*/\1 \2/p' | while read -r pkg ver; do
    norm="$(echo "$pkg" | tr 'A-Z.-' 'a-z__')"
    di="$(find "$SITE" -maxdepth 1 -iname "${norm}-${ver}.dist-info" | head -1)"
    [ -n "$di" ] || continue
    mkdir -p "$DOC/python/$pkg"
    grep -m1 -E '^License(-Expression)?:' "$di/METADATA" > "$DOC/python/$pkg/METADATA-licenca.txt" || true
    find "$di" \( -iname 'LICEN[CS]E*' -o -iname 'COPYING*' -o -iname 'NOTICE*' -o -iname 'AUTHORS*' \
        -o -iname '*ThirdPartyNotices*' \) -type f -exec cp {} "$DOC/python/$pkg/" \;
    if [ -d "$di/licenses" ]; then cp -r "$di/licenses/." "$DOC/python/$pkg/"; fi
    # alguns pacotes (onnxruntime) põem LICENSE/ThirdPartyNotices na pasta do módulo, não no dist-info
    if [ -f "$di/top_level.txt" ]; then
        while read -r mod; do
            [ -n "$mod" ] && [ -d "$SITE/$mod" ] || continue
            find "$SITE/$mod" -maxdepth 1 -type f \( -iname 'LICEN[CS]E*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \
                -o -iname '*ThirdPartyNotices*' \) -exec cp {} "$DOC/python/$pkg/" \;
        done < "$di/top_level.txt"
    fi
    # sem texto nenhum (ex.: flatbuffers): o texto comum da licença declarada
    if [ -z "$(find "$DOC/python/$pkg" -type f ! -name METADATA-licenca.txt)" ] \
        && grep -qi apache "$DOC/python/$pkg/METADATA-licenca.txt" 2>/dev/null; then
        cp "$DOC/licencas-comuns/Apache-2.0" "$DOC/python/$pkg/"
    fi
done
for d in "$DOC"/python/pyside6* "$DOC"/python/shiboken6; do
    if [ -d "$d" ]; then cp "$DOC/licencas-comuns/LGPL-3" "$DOC/licencas-comuns/GPL-3" "$d/"; fi
done
# Inventário exato do que foi empacotado (versões, pacotes Debian de origem e links para o código-fonte).
{
    echo "# Componentes deste AppImage (gerado em $(date -u +%Y-%m-%dT%H:%MZ) por packaging/build_appimage.sh)"
    echo
    echo "AI Editor $VERSAO — GPL-3.0-or-later — https://github.com/rlb3ssa/ai-editor"
    if [ "$FFMPEG_ORIGEM" = sistema ]; then
        echo "ffmpeg/ffprobe $FFMPEG_VER — do Ubuntu (ver lista de pacotes abaixo)"
    else
        echo "ffmpeg/ffprobe $FFMPEG_VER — GPL-3.0 — build $FFMPEG_ARQ do release $FFMPEG_RELEASE"
        echo "    https://github.com/BtbN/FFmpeg-Builds/releases/tag/$FFMPEG_RELEASE"
        echo "    receita (commit $FFMPEG_RECEITA): https://github.com/BtbN/FFmpeg-Builds/tree/$FFMPEG_RECEITA"
        echo "    fonte do FFmpeg: https://github.com/FFmpeg/FFmpeg ; bibliotecas embutidas: usr/share/doc/ai-editor/ffmpeg/BIBLIOTECAS.tsv"
    fi
    echo "exiftool $EXIFTOOL_VERSAO — Artistic/GPL-1.0+ — fonte: https://exiftool.org/ (incluso: usr/lib/exiftool)"
    echo "c2patool $C2PATOOL_VERSAO — MIT/Apache-2.0 — fonte: https://github.com/contentauth/c2pa-rs/tree/c2patool-v$C2PATOOL_VERSAO"
    echo "whisper-cli (whisper.cpp $WHISPER_VERSAO + ggml) — MIT — compilado do código-fonte $WHISPER_URL"
    echo "    (portátil: GGML_NATIVE=OFF, GGML_BACKEND_DL, GGML_CPU_ALL_VARIANTS; em usr/lib/whisper) — https://github.com/ggml-org/whisper.cpp/tree/$WHISPER_VERSAO"
    echo "deep-filter (DeepFilterNet) $DF_VERSAO — MIT OR Apache-2.0 — binário oficial $DF_URL"
    echo "    fonte: https://github.com/Rikorose/DeepFilterNet/tree/v$DF_VERSAO"
    echo
    echo "## Modelos de IA embutidos (usr/share/ai-editor/modelos; procedência em usr/share/doc/ai-editor/modelos/)"
    for d in "$APPDIR"/usr/share/ai-editor/modelos/*/; do
        echo "$(basename "$d") — $(du -sm "$d" | cut -f1) MB"
        sed 's/^/    /' "$d/ORIGEM.txt" 2>/dev/null || true
    done
    echo
    echo "## Pacotes Python (embutidos pelo PyInstaller; PySide6 traz o Qt $(uv run --frozen python -c 'import PySide6.QtCore as q; print(q.qVersion())' 2>/dev/null))"
    (cd "$RAIZ" && uv export --frozen --no-dev --extra ia --no-hashes --no-emit-project --no-header | grep -v '^ *#' | sed 's/ ;.*//')
    echo
    echo "AppImage: appimagetool $APPIMAGETOOL_VERSAO, runtime type2 $RUNTIME_VERSAO (MIT) — https://github.com/AppImage"
    echo "Licenças: usr/share/doc/ai-editor/{licencas-comuns,python,ubuntu,ffmpeg} (dentro do AppImage: --appimage-extract)"
    echo
    echo "## Programas e bibliotecas copiados do Ubuntu $(. /etc/os-release && echo "$VERSION_ID") (libmpv, mediainfo e dependências)"
    echo "Código-fonte de cada pacote: https://launchpad.net/ubuntu/+source/<pacote-fonte>/<versão>"
    echo "(ou: apt-get source <pacote-fonte>=<versão>)"
    echo
    # Tudo o que está no AppDir e veio do sistema (inclusive o que o PyInstaller recolheu para _internal).
    {
        cat "$BUILD/libs-sistema.txt"
        find "$APPDIR/usr" -name '*.so*' -not -path '*/PySide6/*' -not -path '*/ffmpeg/*' -not -path '*/whisper/*' -not -path '*.libs/*' \
            -printf '%f\n' | while read -r n; do ldconfig -p | awk -v n="$n" '$1 == n && /x86-64/ {print $NF; exit}'; done
    } | awk -F/ '!v[$NF]++' | while read -r lib; do
        pkg="$(dpkg -S "$(readlink -f "$lib")" 2>/dev/null | head -1 | sed 's/: .*//')"
        [ -n "$pkg" ] || pkg="$(dpkg -S "$lib" 2>/dev/null | head -1 | sed 's/: .*//')"
        if [ -n "$pkg" ]; then
            # Licença do pacote Debian (copyright) junto, em usr/share/doc/ai-editor/ubuntu/<pacote>/
            base="${pkg%%:*}"
            if [ -f "/usr/share/doc/$base/copyright" ] && [ ! -e "$DOC/ubuntu/$base/copyright" ]; then
                mkdir -p "$DOC/ubuntu/$base" && cp -L "/usr/share/doc/$base/copyright" "$DOC/ubuntu/$base/copyright"
            fi
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
(cd "$DIST" && PATH="$(dirname "$ZSYNCMAKE"):$PATH" "$APPIMAGETOOL" --no-appstream \
    --mksquashfs-opt -processors --mksquashfs-opt "$JOBS" "${OPCOES_COMP[@]}" \
    --runtime-file "$DL/runtime-${RUNTIME_VERSAO}-x86_64" -u "$ATUALIZACAO" "$APPDIR" "$SAIDA")
if [ ! -f "$SAIDA.zsync" ]; then  # appimagetool sem zsyncmake no PATH: gera aqui
    (cd "$DIST" && "$ZSYNCMAKE" -u "$(basename "$SAIDA")" -o "$(basename "$SAIDA").zsync" "$(basename "$SAIDA")")
fi
chmod +x "$SAIDA"

msg "Bundle .mcpb (Claude Desktop) e documentos da release"
(cd "$RAIZ" && uv run --frozen python packaging/mcpb/empacotar_mcpb.py)
cp "$RAIZ/packaging/LICENCAS_TERCEIROS.md" "$DIST/"
msg "Pronto: $SAIDA ($(du -h "$SAIDA" | cut -f1))"
ls -la "$DIST"
