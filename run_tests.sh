#!/usr/bin/env bash
# Run the test suite. Hermetic: conftest.py points the config loader at a
# throwaway file and every client is fed a fake transport, so nothing here
# touches the network or a real store.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

exec python -m pytest "$@"
