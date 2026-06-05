#!/usr/bin/env bash
#
# Build the bilingual MkDocs site and publish it to the gh-pages branch.
#
# Used by the pre-push git hook (scripts/git-hooks/pre-push) for automatic
# publishing, but also safe to run by hand any time:
#
#     ./scripts/deploy-docs.sh
#
# Requires the `docs` extra (mkdocs-material + mkdocs-static-i18n); `uv run`
# pulls it in on demand. Pushes to GitHub Pages over the existing `origin`
# remote, so it relies on the ambient git credentials (gh auth / token).
#
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

SHA="$(git rev-parse --short HEAD)"
echo "[docs] building + deploying gh-pages (commit ${SHA})…"

# --strict turns broken links / bad anchors into a hard failure so we never
# publish a broken site. {sha}/{version} are expanded by mkdocs itself.
uv run --extra docs mkdocs gh-deploy \
  --strict \
  --message "docs: deploy {sha} via deploy-docs.sh [skip ci]"

echo "[docs] published → https://amazingkenneth.github.io/knowledgebase/"
