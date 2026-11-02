#!/usr/bin/env bash
set -euo pipefail

case "${SENTRY_TEST_WORKERS:-2}" in
    1|2) ;;
    *) echo 'SENTRY_TEST_WORKERS must be 1 or 2.' >&2; exit 2 ;;
esac
shopt -s nullglob
wheels=(/work/.e2e/sentry-tests-wheel/sentry_gitea-*.whl)
if [[ ${#wheels[@]} != 1 ]]; then
    echo 'Expected one wheel; run make sentry-tests from the repository.' >&2
    exit 2
fi
# The blame tests run git, which the Sentry image lacks (as self-hosted's enhance-image.sh adds it).
if ! command -v git >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq --no-install-recommends git >/dev/null
fi
python -m pip install --no-deps --no-index --force-reinstall "${wheels[0]}"
# Let the Sentry pytest plugin initialize Django, including its DB aliases.
unset DJANGO_SETTINGS_MODULE
exec python -m pytest -p no:rerunfailures tests/sentry -n "${SENTRY_TEST_WORKERS:-2}" --maxprocesses=2 \
    --nomigrations -o cache_dir=/tmp/pytest-cache -v
