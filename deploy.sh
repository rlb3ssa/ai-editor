#!/usr/bin/env bash
# Release local do AI Editor: confere, sobe a versão, cria a tag. O GitHub Actions (release.yml) faz o resto
# quando a tag chega ao GitHub (build do AppImage + .mcpb e publicação na Release).
#
#   ./deploy.sh 0.2.0 --dry-run    # só mostra o que faria (nada é alterado)
#   ./deploy.sh 0.2.0              # commit "Versão 0.2.0" + tag v0.2.0 (sem push)
#   ./deploy.sh 0.2.0 --push       # idem e envia main + tag para o origin
#
# Nunca envia nada sem --push.
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
[[ "$VERSAO" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-]?(a|b|rc)[0-9]+)?$ ]] || { echo "versão inválida: $VERSAO" >&2; exit 2; }
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
if [ "$(printf '%s\n%s\n' "$ATUAL" "$VERSAO" | sort -V | tail -1)" = "$VERSAO" ] && [ "$ATUAL" != "$VERSAO" ]; then
    ok "versão $ATUAL → $VERSAO"
else
    falha "a versão nova ($VERSAO) precisa ser maior que a atual ($ATUAL)"
fi
if uv run --frozen python -m compileall -q src packaging >/dev/null; then ok "compileall"; else falha "compileall falhou"; fi
if bash -n packaging/build_appimage.sh && bash -n deploy.sh; then ok "sintaxe dos scripts"; else falha "erro de sintaxe nos scripts"; fi
if [ -d tests ]; then
    if uv run --frozen python -m unittest discover -s tests -q >/dev/null 2>&1; then ok "testes"; else falha "testes falharam"; fi
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
