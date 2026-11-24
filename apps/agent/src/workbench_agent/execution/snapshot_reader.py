"""Read bounded source lines from an immutable, verified Git tar object.

No archive member is extracted to the host filesystem. All members are
validated before selecting a path so links and ambiguous entries fail closed.
"""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
import tarfile

from workbench_agent.execution.snapshots import MAX_ARCHIVE_BYTES, MAX_ENTRIES

MAX_FILE_BYTES = 256 * 1024
MAX_LINES = 200
MAX_LINE_BYTES = 4096


@dataclass(frozen=True)
class SnapshotLines:
    lines: tuple[tuple[int, str], ...]
    sha256: str


def _path(name):
    if not isinstance(name, str) or not name or '\x00' in name or '\\' in name:
        raise ValueError('invalid snapshot path')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
        raise ValueError('invalid snapshot path')
    return name


def read_snapshot_lines(archive: bytes, path: str, start: int, end: int) -> SnapshotLines:
    path = _path(path)
    if (
        type(start) is not int
        or type(end) is not int
        or start < 1
        or end < start
        or end - start + 1 > MAX_LINES
    ):
        raise ValueError('invalid line range')
    if not isinstance(archive, bytes) or len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError('invalid snapshot size')
    try:
        with tarfile.open(fileobj=BytesIO(archive), mode='r:') as tar:
            seen, total, selected = set(), 0, None
            for index, member in enumerate(tar):
                if index >= MAX_ENTRIES:
                    raise ValueError('too many snapshot entries')
                name = _path(member.name.rstrip('/') if member.isdir() else member.name)
                if name in seen or not (member.isfile() or member.isdir()):
                    raise ValueError('unsafe or duplicate snapshot entry')
                seen.add(name)
                total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError('snapshot content too large')
                if name == path and member.isfile():
                    if member.size > MAX_FILE_BYTES:
                        raise ValueError('snapshot file too large')
                    stream = tar.extractfile(member)
                    selected = stream.read(MAX_FILE_BYTES + 1) if stream else None
                    if selected is None or len(selected) != member.size:
                        raise ValueError('invalid snapshot file')
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ValueError('invalid snapshot archive') from error
    if selected is None:
        raise ValueError('snapshot file unavailable')
    try:
        content = selected.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('snapshot file is not text') from error
    if '\x00' in content:
        raise ValueError('snapshot file is not text')
    # Git counts LF bytes as line boundaries; Unicode separators and bare CR
    # are content. Strip CR only from a line terminated by CRLF.
    parts = content.split('\n')
    terminated = len(parts) - 1
    lines = [line.removesuffix('\r') if i < terminated else line for i, line in enumerate(parts)]
    if content.endswith('\n'):
        lines.pop()
    if not content:
        lines = []
    if start > len(lines) or end > len(lines):
        raise ValueError('line range outside file')
    selected_lines = lines[start - 1 : end]
    if any(len(line.encode('utf-8')) > MAX_LINE_BYTES for line in selected_lines):
        raise ValueError('snapshot line too large')
    return SnapshotLines(
        tuple((number, line) for number, line in enumerate(selected_lines, start)),
        sha256(selected).hexdigest(),
    )
