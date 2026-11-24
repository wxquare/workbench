#!/usr/bin/env python3
"""Check documentation metadata, local references, diagrams and PR changes."""
import argparse
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

NAME = re.compile(r'^(?:README|\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*)\.md$')
LINK = re.compile(r'\]\(\s*<?([^\s)>]+)>?(?:\s+["\'].+?["\'])?\s*\)')
HEADING = re.compile(r'^#{1,6}\s+(.+?)\s*#*\s*$', re.M)
FENCE = re.compile(r'^\s*(`{3,}|~{3,}).*?^\s*\1\s*$', re.M | re.S)
SVG_WITHOUT_SOURCE = {'19-langchain-langgraph.svg'}
CODE_TO_DOC = {
    'apps/agent/src/workbench_agent/api/': 'docs/06-reference/01-api.md',
    'apps/agent/src/workbench_agent/persistence/': 'docs/06-reference/02-data-dictionary.md',
    'apps/agent/src/workbench_agent/settings.py': 'docs/06-reference/03-configuration.md',
    'config/': 'docs/06-reference/03-configuration.md',
    'apps/web/src/router.ts': 'docs/01-product.md',
    'scripts/': 'docs/04-operations.md',
    'deploy/': 'docs/04-operations.md',
}


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, text=True, capture_output=True)


def metadata(text):
    match = re.match(r'\A---\s*\n(.*?)\n---\s*\n', text, re.S)
    if not match:
        return None
    return dict(line.split(':', 1) for line in match.group(1).splitlines() if ':' in line)


def clean_metadata(text):
    return {k.strip(): v.strip() for k, v in (metadata(text) or {}).items()}


def slug(value):
    value = re.sub(r'<[^>]*>', '', value).lower().strip()
    return ''.join(c if c != ' ' else '-' for c in value if c.isalnum() or c in '_- ')


def anchors(text):
    counts, result = {}, set()
    for heading in HEADING.findall(FENCE.sub('', text)):
        name = slug(heading)
        count = counts.get(name, 0)
        counts[name] = count + 1
        result.add(name if count == 0 else f'{name}-{count}')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--base')
    args = parser.parse_args()
    root = args.root.resolve()
    docs = root / 'docs'
    files = sorted(p for p in docs.rglob('*.md') if 'superpowers' not in p.relative_to(docs).parts)
    texts = {p: p.read_text(encoding='utf-8') for p in files}
    errors, warnings = [], []
    for path, text in texts.items():
        name = path.relative_to(root).as_posix()
        if not NAME.fullmatch(path.name) or (path.name == 'README.md' and path.parent != docs):
            errors.append(f'{name}: invalid documentation filename')
        meta = metadata(text)
        data = clean_metadata(text)
        if meta is None or not data.get('title') or data.get('status') not in ('stable', 'draft'):
            errors.append(f'{name}: invalid front matter')
        commit = data.get('verified_commit', '')
        if not re.fullmatch(r'[0-9a-f]{7,40}', commit) or git(root, 'cat-file', '-e', f'{commit}^{{commit}}').returncode or git(root, 'merge-base', '--is-ancestor', commit, 'HEAD').returncode:
            errors.append(f'{name}: invalid or non-ancestor verified_commit {commit}')
        for target in LINK.findall(FENCE.sub('', text)):
            parts = urlsplit(target)
            if parts.scheme or target.startswith('//'):
                continue
            dest = (path.parent / unquote(parts.path)).resolve() if parts.path else path
            if not dest.exists():
                errors.append(f'{name}: missing link {target}')
            elif parts.fragment and dest.is_file() and dest.suffix == '.md':
                if unquote(parts.fragment) not in anchors(dest.read_text(encoding='utf-8')):
                    errors.append(f'{name}: missing anchor {target}')
    assets = docs / 'assets'
    sources = {p.stem for p in assets.glob('*.mmd')}
    renders = {p.stem for p in assets.glob('*.svg')}
    errors.extend(f'docs/assets/{stem}.mmd: missing SVG' for stem in sorted(sources - renders))
    errors.extend(f'docs/assets/{stem}.svg: missing Mermaid source' for stem in sorted(renders - sources) if stem + '.svg' not in SVG_WITHOUT_SOURCE)
    if args.base:
        merge = git(root, 'merge-base', args.base, 'HEAD')
        if merge.returncode:
            errors.append(f'cannot compare base {args.base}')
        else:
            base = merge.stdout.strip()
            diff = git(root, 'diff', '--name-status', '-M', base, 'HEAD')
            if diff.returncode:
                errors.append(f'cannot compare base {args.base}')
            else:
                changed = set()
                for line in diff.stdout.splitlines():
                    status, *paths = line.split('\t')
                    changed.update(paths)
                    if status[0] not in 'MR' or os.getenv('DOCS_SKIP_BUMP'):
                        continue
                    old, new = paths[0], paths[-1]
                    path = root / new
                    if path not in texts:
                        continue
                    previous = git(root, 'show', f'{base}:{old}')
                    if previous.returncode == 0 and clean_metadata(previous.stdout).get('verified_commit') == clean_metadata(texts[path]).get('verified_commit'):
                        errors.append(f'{new}: verified_commit unchanged')
                for stem in sources:
                    if f'docs/assets/{stem}.mmd' in changed and f'docs/assets/{stem}.svg' not in changed:
                        errors.append(f'docs/assets/{stem}.mmd changed without SVG')
                for prefix, doc in CODE_TO_DOC.items():
                    if doc not in changed and any(p.startswith(prefix) for p in changed):
                        warnings.append(f'{prefix} changed without {doc}; check whether documentation needs updating')
    for message in warnings:
        print(f'::warning::{message}')
    for message in errors:
        print(f'::error::{message}')
    print(f'Documentation: {len(files)} files, {len(errors)} errors, {len(warnings)} warnings')
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
