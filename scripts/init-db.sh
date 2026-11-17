#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
config="$root/config/local/agent.yaml"
agent="$root/apps/agent/.venv/bin/workbench"
python="$root/apps/agent/.venv/bin/python"

if [[ ${1:-} == --help ]]; then
  printf 'Usage: scripts/init-db.sh\nInitialize a new Agent database selected by WORKBENCH_DATABASE_URL. Never overwrites an existing file.\n'
  exit 0
fi
if [[ $# -ne 0 ]]; then
  printf 'Unexpected argument. Run scripts/init-db.sh --help.\n' >&2
  exit 2
fi
if [[ ! -f "$config" || ! -x "$agent" || ! -x "$python" ]]; then
  printf 'Run scripts/install.sh and configure config/local/agent.yaml first.\n' >&2
  exit 1
fi
if [[ -z ${WORKBENCH_DATABASE_URL:-} || -z ${WORKBENCH_PERSISTENT_ROOT:-} ]]; then
  printf 'Set WORKBENCH_DATABASE_URL and WORKBENCH_PERSISTENT_ROOT explicitly.\n' >&2
  exit 1
fi
if [[ $WORKBENCH_PERSISTENT_ROOT != /* ]]; then
  printf 'WORKBENCH_PERSISTENT_ROOT must be an absolute path.\n' >&2
  exit 1
fi

export WORKBENCH_ROOT="$root"
export WORKBENCH_ASSET_ROOT="$root"
export WORKBENCH_FRONTEND_DIST_ROOT="$root/apps/web/dist"
database_path=$("$python" - "$config" <<'PY'
import sys

from workbench_agent.settings import load_settings, require_sqlite_absolute_path

try:
    storage = load_settings(sys.argv[1]).storage
    path = require_sqlite_absolute_path(storage.database_url.get_secret_value())
    if storage.persistent_root is None or not path.is_relative_to(storage.persistent_root):
        raise ValueError("database outside persistent root")
except Exception:
    raise SystemExit("Invalid configuration or database path outside WORKBENCH_PERSISTENT_ROOT") from None
print(path)
PY
)
if [[ -e "$database_path" || -L "$database_path" ]]; then
  printf 'Database file already exists; use the documented maintenance path instead: %s\n' "$database_path" >&2
  exit 1
fi

mkdir -p "$WORKBENCH_PERSISTENT_ROOT" "$(dirname "$database_path")"
cd "$root/apps/agent"
"$agent" db migrate --config "$config"
"$agent" bootstrap --config "$config"
"$agent" doctor --config "$config"
