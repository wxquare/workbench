import hashlib
import io
import tarfile

import pytest

from workbench_agent.execution.snapshot_reader import read_snapshot_lines


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        for name, data, kind in entries:
            item = tarfile.TarInfo(name)
            if kind == 'link':
                item.type = tarfile.SYMTYPE
                item.linkname = '../../secret'
                tar.addfile(item)
            else:
                item.size = len(data)
                tar.addfile(item, io.BytesIO(data))
    return output.getvalue()


def test_reads_exact_lines_and_hashes_original_file():
    raw = b'one\ntwo\nthree\n'
    result = read_snapshot_lines(archive([('app/main.py', raw, 'file')]), 'app/main.py', 2, 3)
    assert result.lines == ((2, 'two'), (3, 'three'))
    assert result.sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize('path', ['../secret', '/etc/passwd', 'a//b', 'a/./b', 'a\\b', ''])
def test_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        read_snapshot_lines(archive([('a/b', b'hello', 'file')]), path, 1, 1)


def test_rejects_symlinks_and_duplicate_entries():
    for entries in [[('a', b'', 'link')], [('a', b'one', 'file'), ('a', b'two', 'file')]]:
        with pytest.raises(ValueError):
            read_snapshot_lines(archive(entries), 'a', 1, 1)


@pytest.mark.parametrize('start,end', [(0, 1), (2, 1), (1, 201)])
def test_rejects_invalid_line_ranges(start, end):
    with pytest.raises(ValueError):
        read_snapshot_lines(archive([('a', b'one', 'file')]), 'a', start, end)


def test_lines_follow_git_lf_boundaries_not_unicode_separators():
    raw = 'one\u2028two\u0085three\vfour\r\nfive\nlast\r'.encode()
    result = read_snapshot_lines(archive([('source.py', raw, 'file')]), 'source.py', 1, 3)
    assert result.lines == ((1, 'one\u2028two\u0085three\vfour'), (2, 'five'), (3, 'last\r'))
    with pytest.raises(ValueError):
        read_snapshot_lines(archive([('source.py', b'one\ntwo\n', 'file')]), 'source.py', 1, 3)
