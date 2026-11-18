"""Normalize Markdown and derive stable, source-exact bounded passages."""
from hashlib import sha256
import re

from workbench_agent.application.idempotency import canonical

CHUNKER_VERSION = 'kb-heading-v1'
MAX_BODY_BYTES = 16_384
MAX_CHUNK_CHARS = 1500
_HEADING = re.compile(r'^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\n)?$')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')


def normalize_markdown(raw: bytes) -> str:
    if len(raw) > MAX_BODY_BYTES + 3:
        raise ValueError('body exceeds byte limit')
    body = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    if len(body.encode('utf-8')) > MAX_BODY_BYTES or not body.strip():
        raise ValueError('body is empty or exceeds byte limit')
    if any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127 for c in body):
        raise ValueError('body contains control characters')
    return body


def chunk_markdown(body: str):
    from workbench_agent.knowledge.models import KBChunk

    if not body:
        return ()
    sections = []
    headings = []
    section_start = 0
    section_path = ()
    offset = 0
    fence_char = None
    fence_length = 0
    for line in body.splitlines(keepends=True):
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if fence_char is None:
                fence_char, fence_length = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length and not line[fence.end():].strip():
                fence_char = None
            offset += len(line)
            continue
        heading = _HEADING.match(line) if fence_char is None else None
        if heading:
            if offset > section_start:
                sections.append((section_start, offset, section_path))
            level = len(heading.group(1))
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, heading.group(2).strip()))
            section_start, section_path = offset, tuple(name for _, name in headings)
        offset += len(line)
    if section_start < len(body):
        sections.append((section_start, len(body), section_path))

    result = []
    for start, end, path in sections:
        while start < end:
            stop = min(start + MAX_CHUNK_CHARS, end)
            if stop < end:
                # Prefer paragraph then line boundaries, but never emit an empty chunk.
                boundary = body.rfind('\n\n', start, stop)
                if boundary < start + 1:
                    boundary = body.rfind('\n', start, stop)
                    boundary = boundary + 1 if boundary >= start + 1 else -1
                else:
                    boundary += 2
                if boundary > start:
                    stop = boundary
            content = body[start:stop]
            line_start = body.count('\n', 0, start) + 1
            line_end = body.count('\n', 0, stop - 1) + 1
            identity = {'chunker': CHUNKER_VERSION, 'path': path, 'start': line_start,
                        'end': line_end, 'start_offset': start, 'end_offset': stop, 'content': content}
            result.append(KBChunk(chunk_id=sha256(canonical(identity).encode('utf-8')).hexdigest()[:24],
                heading_path=path, line_start=line_start, line_end=line_end, content=content,
                content_hash=sha256(content.encode('utf-8')).hexdigest(), chunker_version=CHUNKER_VERSION))
            start = stop
    return tuple(result)
