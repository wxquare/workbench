#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)

if [[ ${1:-} == --help ]]; then
  printf 'Usage: scripts/install.sh\nInstall development dependencies, build the web UI, and copy missing local config examples.\n'
  exit 0
fi
if [[ $# -ne 0 ]]; then
  printf 'Unexpected argument. Run scripts/install.sh --help.\n' >&2
  exit 2
fi

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

printf 'Installed. Configure config/local/*.yaml and the required environment before initializing the database.\n'
