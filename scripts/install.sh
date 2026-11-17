#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/scripts/lib/local-paths.sh"
choice="$root/config/local/data-dir"

if [[ ${1:-} == --help ]]; then
  printf 'Usage: scripts/install.sh [--data-dir ABSOLUTE_PATH]\nInstall dependencies, build the web UI, and record the local data directory.\n'
  exit 0
fi
case $# in
  0) requested_data_dir='' ;;
  2)
    if [[ $1 != --data-dir ]]; then
      printf 'Unexpected argument. Run scripts/install.sh --help.\n' >&2
      exit 2
    fi
    requested_data_dir=$2
    ;;
  *) printf 'Unexpected argument. Run scripts/install.sh --help.\n' >&2; exit 2 ;;
esac
if [[ -L $choice ]]; then
  printf 'Data directory choice file must not be a symlink: %s\n' "$choice" >&2
  exit 1
fi
if [[ -e $choice && ! -f $choice ]]; then
  printf 'Data directory choice is not a file: %s\n' "$choice" >&2
  exit 1
fi
if [[ $# -eq 2 ]]; then
  data_dir=$requested_data_dir
elif [[ -f $choice ]]; then
  data_dir=$(<"$choice")
else
  data_dir="${HOME:?HOME must be set}/.workbench"
fi
workbench_validate_data_dir "$data_dir"

for tool in uv npm; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf '%s is required; see docs/runbooks/source-layout.md.\n' "$tool" >&2
    exit 1
  fi
done

if [[ ! -e "$root/config/local/agent.yaml" && ! -L "$root/config/local/agent.yaml" ]]; then
  cp "$root/config/local/agent.example.yaml" "$root/config/local/agent.yaml"
fi
if [[ ! -e "$root/config/local/mcp-server.yaml" && ! -L "$root/config/local/mcp-server.yaml" ]]; then
  cp "$root/config/local/mcp-server.example.yaml" "$root/config/local/mcp-server.yaml"
fi

(
  cd "$root/apps/agent"
  uv sync --frozen --group dev
)
(
  cd "$root/apps/web"
  npm ci
  npm run build
)
(
  cd "$root/apps/mcp-server"
  uv sync --frozen --group dev
)

if [[ ! -f $choice || $(<"$choice") != "$data_dir" ]]; then
  if [[ -f $choice ]]; then
    printf 'Data directory changed; existing data was not moved.\n' >&2
  fi
  temp_choice=$(mktemp "$root/config/local/.data-dir.XXXXXX")
  trap 'rm -f "$temp_choice"' EXIT
  printf '%s\n' "$data_dir" > "$temp_choice"
  mv -f "$temp_choice" "$choice"
  trap - EXIT
fi

printf 'Installed. Data directory: %s\nConfigure config/local/*.yaml before initializing the database.\n' "$data_dir"
