#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/scripts/lib/local-paths.sh"
config="$root/config/local/agent.yaml"
agent="$root/apps/agent/.venv/bin/workbench"
python="$root/apps/agent/.venv/bin/python"

if [[ ${1:-} == --help ]]; then
  printf 'Usage: scripts/init-db.sh\nInitialize a new Agent database in the local data directory. Never overwrites an existing file.\n'
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
workbench_export_storage "$root"

export WORKBENCH_ROOT="$root"
export WORKBENCH_ASSET_ROOT="$root"
export WORKBENCH_FRONTEND_DIST_ROOT="$root/apps/web/dist"
database_path=$("$python" - "$config" <<'PY'
import sys
from pathlib import Path

from sqlalchemy.engine import make_url
from workbench_agent.settings import load_settings, require_sqlite_absolute_path

try:
    storage = load_settings(sys.argv[1]).storage
    url = storage.database_url.get_secret_value()
    raw_path = Path(make_url(url).database)
    if raw_path.is_symlink():
        raise SystemExit(f"Database file must not be a symlink: {raw_path}")
    path = require_sqlite_absolute_path(url)
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
