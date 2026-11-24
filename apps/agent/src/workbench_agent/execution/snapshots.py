"""Produce bounded Git archives from immutable commits, without reading a worktree."""

import asyncio
from io import BytesIO
import os
from pathlib import PurePosixPath
import re
import tarfile

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 10000


async def _git(repository, *arguments, limit):
    process = await asyncio.create_subprocess_exec(
        'git',
        '-C',
        os.fspath(repository),
        *arguments,
        env={
            'PATH': os.defpath,
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0',
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )

    async def collect():
        output = bytearray()
        while True:
            chunk = await process.stdout.read(min(65536, limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > limit:
                raise ValueError('repository archive exceeds limit')
        if await process.wait():
            raise ValueError('repository commit unavailable')
        return bytes(output)

    try:
        return await asyncio.wait_for(collect(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def archive_commit(repository, commit):
    if not isinstance(commit, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit):
        raise ValueError('full immutable commit required')
    if (await _git(repository, 'cat-file', '-t', commit, limit=64)).strip() != b'commit':
        raise ValueError('commit required')
    archive = await _git(repository, 'archive', '--format=tar', commit, limit=MAX_ARCHIVE_BYTES)
    total = 0
    try:
        with tarfile.open(fileobj=BytesIO(archive), mode='r:') as reader:
            members = reader.getmembers()
            if len(members) > MAX_ENTRIES:
                raise ValueError('too many repository entries')
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or '..' in path.parts
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError('unsafe repository archive entry')
                total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError('repository content exceeds limit')
    except tarfile.TarError as error:
        raise ValueError('invalid repository archive') from error
    return archive
