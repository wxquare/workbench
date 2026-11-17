#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/scripts/lib/local-paths.sh"
config="$root/config/local/agent.yaml"
agent="$root/apps/agent/.venv/bin/workbench"
mcp_config="$root/config/local/mcp-server.yaml"
mcp_agent="$root/apps/mcp-server/.venv/bin/workbench-mcp-server"
with_mcp=0

for argument in "$@"; do
  case "$argument" in
    --mcp) with_mcp=1 ;;
    --help)
      printf 'Usage: scripts/start.sh [--mcp]\nStart API, Worker, and Scheduler after doctor. --mcp also starts the MCP server.\n'
      exit 0
      ;;
    *) printf 'Unknown argument: %s\n' "$argument" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$config" || ! -x "$agent" ]]; then
  printf 'Run scripts/install.sh and configure config/local/agent.yaml first.\n' >&2
  exit 1
fi
workbench_export_storage "$root"
if [[ $with_mcp -eq 1 && ( ! -f "$mcp_config" || ! -x "$mcp_agent" ) ]]; then
  printf 'Configure config/local/mcp-server.yaml and run scripts/install.sh before --mcp.\n' >&2
  exit 1
fi

export WORKBENCH_ROOT="$root"
export WORKBENCH_ASSET_ROOT="$root"
export WORKBENCH_FRONTEND_DIST_ROOT="$root/apps/web/dist"
cd "$root/apps/agent"
"$agent" doctor --config "$config"
if [[ $with_mcp -eq 1 ]]; then
  "$mcp_agent" check --config "$mcp_config"
fi

pids=()
cleanup() {
  status=$?
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  for ((attempt = 0; attempt < 30; attempt++)); do
    if [[ -z $(jobs -pr) ]]; then
      break
    fi
    sleep 0.1
  done
  for pid in $(jobs -pr); do
    kill -KILL "$pid" 2>/dev/null || true
  done
  for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$agent" api --config "$config" --host 127.0.0.1 --port "${WORKBENCH_API_PORT:-8000}" &
pids+=("$!")
"$agent" worker --config "$config" &
pids+=("$!")
"$agent" scheduler --config "$config" &
pids+=("$!")
if [[ $with_mcp -eq 1 ]]; then
  "$mcp_agent" serve --config "$mcp_config" --transport streamable-http &
  pids+=("$!")
fi

printf 'Workbench: http://127.0.0.1:%s/app/ (Ctrl-C to stop)\n' "${WORKBENCH_API_PORT:-8000}"
while :; do
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null || true
      printf 'A Workbench process stopped; shutting down the others.\n' >&2
      exit 1
    fi
  done
  sleep 1
done
