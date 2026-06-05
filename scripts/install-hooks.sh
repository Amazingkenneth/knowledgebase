#!/usr/bin/env bash
#
# Point git at the version-controlled hooks in scripts/git-hooks/.
# Run once per clone:
#
#     ./scripts/install-hooks.sh
#
# This sets core.hooksPath, which replaces .git/hooks wholesale — that is why
# scripts/git-hooks/ also carries the Git LFS passthrough hooks. To revert:
#
#     git config --unset core.hooksPath
#
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
chmod +x scripts/git-hooks/* scripts/deploy-docs.sh
git config core.hooksPath scripts/git-hooks
echo "Installed git hooks → core.hooksPath = scripts/git-hooks"
echo "Pushing doc changes on 'main' will now auto-deploy to gh-pages."
