#!/usr/bin/env bash
set -euo pipefail

# Example only. The installer writes a block with the exact built wheel filename.
# Only for blame-based suspect commits (GITEA_BLAME_ENABLED): the Sentry image has no git.
apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
python -m pip install --no-deps --no-index --force-reinstall \
    /usr/src/sentry/sentry_gitea-*.whl
