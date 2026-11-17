#!/usr/bin/env bash

workbench_validate_data_dir() {
  if [[ $1 != /* || $1 == *$'\n'* || $1 == *$'\r'* ]]; then
    printf 'Data directory must be an absolute path without newlines.\n' >&2
    return 1
  fi
}

workbench_data_dir() {
  local root=$1 choice="$1/config/local/data-dir" data_dir
  if [[ -n ${WORKBENCH_PERSISTENT_ROOT:-} ]]; then
    data_dir=$WORKBENCH_PERSISTENT_ROOT
  elif [[ -L $choice ]]; then
    printf 'Data directory choice file must not be a symlink: %s\n' "$choice" >&2
    return 1
  elif [[ -e $choice ]]; then
    if [[ ! -f $choice ]]; then
      printf 'Data directory choice is not a file: %s\n' "$choice" >&2
      return 1
    fi
    data_dir=$(<"$choice")
  else
    data_dir="${HOME:?HOME must be set}/.workbench"
  fi
  workbench_validate_data_dir "$data_dir" || return 1
  printf '%s\n' "$data_dir"
}

workbench_export_storage() {
  local data_dir
  data_dir=$(workbench_data_dir "$1") || return 1
  export WORKBENCH_PERSISTENT_ROOT="$data_dir"
  export WORKBENCH_DATABASE_URL="${WORKBENCH_DATABASE_URL:-sqlite+aiosqlite:///$data_dir/agents.sqlite3}"
}
