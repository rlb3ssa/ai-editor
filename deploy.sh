#!/usr/bin/env bash
# Release local do AI Editor: confere, sobe a versão, cria a tag. O GitHub Actions (release.yml) faz o resto
# quando a tag chega ao GitHub (build do AppImage + .mcpb e publicação na Release).
#
#   ./deploy.sh 0.2.0 --dry-run    # só confere e mostra o que faria (não altera nada: nem .venv, nem __pycache__)
#   ./deploy.sh 0.2.0              # commit "Versão 0.2.0" + tag v0.2.0 (sem push)
#   ./deploy.sh 0.2.0 --push       # idem e envia main + tag para o origin
#
# Versões: X.Y.Z ou pré-lançamento PEP 440 normalizado (X.Y.ZaN, X.Y.ZbN, X.Y.ZrcN). Nunca envia nada sem --push.
set -euo pipefail
cd "$(dirname "$0")"

VERSAO=""
SECO=0
PUSH=0
for a in "$@"; do
    case "$a" in
        --dry-run|--seco) SECO=1 ;;
        --push) PUSH=1 ;;
        -h|--help|--ajuda) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "opção desconhecida: $a" >&2; exit 2 ;;
        *) VERSAO="$a" ;;
    esac
done
[ -n "$VERSAO" ] || { echo "uso: ./deploy.sh X.Y.Z [--dry-run] [--push]" >&2; exit 2; }
RX_VERSAO='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)((a|b|rc)(0|[1-9][0-9]*))?$'
[[ "$VERSAO" =~ $RX_VERSAO ]] || { echo "versão inválida: $VERSAO (use X.Y.Z, X.Y.ZaN, X.Y.ZbN ou X.Y.ZrcN)" >&2; exit 2; }
TAG="v$VERSAO"
[ $SECO = 1 ] && [ $PUSH = 1 ] && { echo "--dry-run e --push juntos não fazem sentido" >&2; exit 2; }

PROBLEMAS=0
ok()    { printf '  \033[32m✔\033[0m %s\n' "$*"; }
falha() { printf '  \033[31m✘\033[0m %s\n' "$*"; PROBLEMAS=$((PROBLEMAS + 1)); }
fazer() {  # executa (ou só mostra, no modo a seco)
    if [ $SECO = 1 ]; then printf '  [a seco] %s\n' "$*"; else printf '  $ %s\n' "$*"; "$@"; fi
}

echo "Release $TAG$([ $SECO = 1 ] && echo ' (a seco: nada será alterado)')"
echo
echo "Conferências:"
RAMO="$(git rev-parse --abbrev-ref HEAD)"
[ "$RAMO" = main ] && ok "na branch main" || falha "branch atual é '$RAMO' (precisa ser main: git switch main && git merge --ff-only dev)"
if [ -z "$(git status --porcelain)" ]; then ok "árvore de trabalho limpa"; else falha "há mudanças não commitadas (git status)"; fi
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then falha "a tag $TAG já existe"; else ok "tag $TAG ainda não existe"; fi
ATUAL="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
# Ordem PEP 440 (só para o formato aceito acima): X.Y.ZaN < X.Y.ZbN < X.Y.ZrcN < X.Y.Z.
# (sort -V erraria: põe 1.0.0 antes de 1.0.0rc1.)
chave_versao() {
    [[ "$1" =~ $RX_VERSAO ]] || return 1
    local fase=3 num=0
    case "${BASH_REMATCH[5]}" in a) fase=0 ;; b) fase=1 ;; rc) fase=2 ;; esac
    [ -n "${BASH_REMATCH[6]}" ] && num="${BASH_REMATCH[6]}"
    printf '%010d.%010d.%010d.%d.%010d\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" "$fase" "$num"
}
if ! K_ATUAL="$(chave_versao "$ATUAL")"; then
    falha "versão atual do pyproject.toml ($ATUAL) fora do formato X.Y.Z[a|b|rcN]"
elif [[ "$(chave_versao "$VERSAO")" > "$K_ATUAL" ]]; then
    ok "versão $ATUAL → $VERSAO"
else
    falha "a versão nova ($VERSAO) precisa ser maior que a atual ($ATUAL)"
fi
# Python do projeto sem passar pelo uv (uv run poderia sincronizar o .venv) e sem gravar bytecode.
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
export PYTHONDONTWRITEBYTECODE=1
compila() {  # compile() em memória: confere a sintaxe sem gravar .pyc nem __pycache__
    "$PY" -B - "$@" <<'EOF'
import pathlib, sys
erros = 0
for raiz in sys.argv[1:]:
    for p in sorted(pathlib.Path(raiz).rglob("*.py")):
        try:
            compile(p.read_bytes(), str(p), "exec", dont_inherit=True)
        except SyntaxError as e:
            print(f"    {p}:{e.lineno}: {e.msg}")
            erros += 1
sys.exit(1 if erros else 0)
EOF
}
if compila src packaging; then ok "sintaxe Python (src, packaging)"; else falha "erro de sintaxe Python"; fi
if bash -n packaging/build_appimage.sh && bash -n deploy.sh; then ok "sintaxe dos scripts"; else falha "erro de sintaxe nos scripts"; fi
if [ -d tests ]; then
    if "$PY" -B -m unittest discover -s tests -q >/dev/null 2>&1; then ok "testes"; else falha "testes falharam"; fi
fi

if [ $PROBLEMAS -gt 0 ]; then
    echo
    echo "$PROBLEMAS problema(s)."
    [ $SECO = 1 ] || exit 1
fi

echo
echo "Passos:"
fazer sed -i "s/^version = \"$ATUAL\"/version = \"$VERSAO\"/" pyproject.toml
fazer uv lock
fazer git commit -am "Versão $VERSAO"
fazer git tag -a "$TAG" -m "AI Editor $VERSAO"

echo
if [ $PUSH = 1 ]; then
    fazer git push origin main "$TAG"
    echo "Enviado. Acompanhe o build em https://github.com/rlb3ssa/ai-editor/actions"
else
    echo "Para publicar (dispara o GitHub Actions):"
    echo "  git push origin main $TAG"
    echo "(ou rode de novo com --push)"
fi
[ $SECO = 1 ] && [ $PROBLEMAS -gt 0 ] && exit 1
exit 0
