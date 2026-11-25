#!/usr/bin/env python3
"""Reject new or increased Agent complexity without relying on source line numbers."""

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "config/quality/python-complexity-baseline.json"
LIMITS = {"C901": 12, "PLR0915": 50}


def _functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = []

    def visit(node, parents):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = parents + (child.name,)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result.append((child.lineno, ".".join(name)))
                visit(child, name)
            else:
                visit(child, parents)

    visit(tree, ())
    if len({name for _, name in result}) != len(result):
        raise ValueError(f"ambiguous duplicate function identity in {path}")
    return dict(result)


def identify(root, diagnostics):
    result = {}
    files = {}
    for diagnostic in diagnostics:
        try:
            path = Path(diagnostic["filename"]).resolve()
            relative = path.relative_to(root.resolve()).as_posix()
            row = diagnostic["location"]["row"]
            rule = diagnostic["code"]
            message = diagnostic["message"]
            match = re.search(r"\((\d+) > (\d+)\)$", message)
            if rule not in LIMITS or not match or int(match.group(2)) != LIMITS[rule]:
                raise ValueError("unexpected Ruff diagnostic")
            if path not in files:
                files[path] = _functions(path)
            name = files[path][row]
            key = (relative, name, rule)
            if key in result:
                raise ValueError(f"duplicate diagnostic: {key}")
            result[key] = int(match.group(1))
        except (KeyError, TypeError, OSError, SyntaxError) as error:
            raise ValueError(
                f"cannot identify Ruff diagnostic: {diagnostic!r}"
            ) from error
    return result


def run_ruff(root, ruff):
    command = [
        str(ruff),
        "check",
        "src",
        "--isolated",
        "--force-exclude",
        "--exclude",
        "src/workbench_agent/persistence/agent_migrations/versions,src/workbench_agent/persistence/migrations/versions",
        "--ignore-noqa",
        "--select",
        "C901,PLR0915",
        "--config",
        "lint.mccabe.max-complexity=12",
        "--config",
        "lint.pylint.max-statements=50",
        "--output-format",
        "json",
    ]
    try:
        result = subprocess.run(
            command, cwd=root / "apps/agent", capture_output=True, text=True
        )
    except OSError as error:
        raise ValueError(f"Ruff could not run: {error}") from error
    if result.returncode not in (0, 1):
        raise ValueError(f"Ruff failed: {result.stderr}")
    try:
        diagnostics = json.loads(result.stdout)
        if not isinstance(diagnostics, list):
            raise ValueError("Ruff output is not an array")
        if bool(diagnostics) != (result.returncode == 1):
            raise ValueError("Ruff status contradicts its diagnostics")
        return diagnostics
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("invalid Ruff JSON") from error


def compare(root, baseline, current, moves=None):
    moves = moves or {}
    effective = {}
    errors = []
    for (path, name, rule), value in baseline.items():
        destination = moves.get((path, name), (path, name))
        key = (*destination, rule)
        if key in effective:
            errors.append(f"duplicate mapped baseline: {key}")
        effective[key] = value
        try:
            functions = set(_functions(root / destination[0]).values())
        except (OSError, SyntaxError) as error:
            errors.append(f"stale baseline {key}: {error}")
            continue
        if destination[1] not in functions:
            errors.append(f"stale baseline: {key}")
    for key, value in current.items():
        if key not in effective:
            errors.append(f"new complexity {key}: {value}")
        elif value > effective[key]:
            errors.append(f"increased complexity {key}: {effective[key]} -> {value}")
    return errors


def compare_baselines(old, new, moves=None):
    moves = moves or {}
    allowed = {}
    for (path, name, rule), value in old.items():
        key = (*moves.get((path, name), (path, name)), rule)
        if key in allowed:
            return [f"duplicate baseline migration: {key}"]
        allowed[key] = value
    return [
        f"baseline added or increased: {key}"
        for key, value in new.items()
        if key not in allowed or value > allowed[key]
    ]


def read_baseline(data):
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("entries"), list)
    ):
        raise ValueError("invalid baseline format")
    entries = {}
    for item in data["entries"]:
        if not isinstance(item, dict) or set(item) != {"path", "name", "rule", "value"}:
            raise ValueError("invalid baseline entry")
        key = (item["path"], item["name"], item["rule"])
        if (
            not all(isinstance(value, str) and value for value in key)
            or not key[0].startswith("apps/agent/src/")
            or ".." in Path(key[0]).parts
            or key[2] not in LIMITS
            or type(item["value"]) is not int
            or item["value"] <= LIMITS[key[2]]
            or key in entries
        ):
            raise ValueError(f"invalid baseline entry: {item!r}")
        entries[key] = item["value"]
    moves = {}
    for move in data.get("moves", []):
        if (
            not isinstance(move, dict)
            or set(move) != {"from", "to", "reason"}
            or not move["reason"]
        ):
            raise ValueError("invalid migration")
        source, target = move["from"], move["to"]
        if (
            not isinstance(source, list)
            or not isinstance(target, list)
            or len(source) != 2
            or len(target) != 2
            or not all(isinstance(value, str) and value for value in source + target)
        ):
            raise ValueError("invalid migration identity")
        if tuple(source) in moves or tuple(target) in moves.values():
            raise ValueError("duplicate migration identity")
        moves[tuple(source)] = tuple(target)
    return entries, moves


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Git ref whose baseline must not be increased")
    parser.add_argument("--ruff", default=str(ROOT / "apps/agent/.venv/bin/ruff"))
    args = parser.parse_args()
    try:
        baseline, moves = read_baseline(
            json.loads((ROOT / BASELINE).read_text(encoding="utf-8"))
        )
        errors = compare(ROOT, baseline, identify(ROOT, run_ruff(ROOT, args.ruff)))
        if args.base:
            ref = subprocess.run(
                ["git", "rev-parse", "--verify", args.base + "^{commit}"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            if ref.returncode:
                raise ValueError("invalid base ref")
            listed = subprocess.run(
                ["git", "ls-tree", "--name-only", args.base, "--", BASELINE],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            if listed.returncode:
                raise ValueError("cannot inspect base baseline")
            if listed.stdout.strip():
                old = subprocess.run(
                    ["git", "show", f"{args.base}:{BASELINE}"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                if old.returncode:
                    raise ValueError("cannot read base baseline")
                previous, _ = read_baseline(json.loads(old.stdout))
                errors.extend(compare_baselines(previous, baseline, moves))
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print(f"Python complexity gate passed ({len(baseline)} baseline entries)")
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"Python complexity gate failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
