#!/usr/bin/env bash
# Wrapper for the task dispatcher: activates the venv and cds into the repo, so
# it works from cron with an absolute path and from any working directory.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
else
    echo "No .venv found in $REPO." >&2
    echo "Create one with:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 2
fi

exec python -m cli "$@"
