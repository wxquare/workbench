"""Path-independent helpers for evaluation scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT / 'apps/agent/src'))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_yaml(path: str) -> dict:
    import yaml

    value = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('configuration must be a mapping')
    return value


def resolve(relative: str) -> Path:
    path = Path(relative)
    return path if path.is_absolute() else ROOT / path


def token(config: dict) -> str:
    value = os.environ.get(config['token_env'])
    if not value:
        raise ValueError(f"{config['token_env']} is not set")
    return value
