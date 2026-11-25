import pytest

from workbench_agent.tools.execution_files import open_execution_file


def test_read_within_execution_directory_and_reject_symlink_and_parent(tmp_path):
    root = tmp_path / 'execution'
    root.mkdir()
    (root / 'safe.txt').write_text('safe')
    (tmp_path / 'secret').write_text('secret')
    (root / 'escape').symlink_to(tmp_path / 'secret')
    with open_execution_file(root, 'safe.txt') as opened:
        assert opened.read() == b'safe'
    for path in ('escape', '../secret', '/etc/passwd'):
        with pytest.raises(ValueError):
            open_execution_file(root, path)


def test_reject_symlink_intermediate_and_non_regular_file(tmp_path):
    root = tmp_path / 'execution'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'file').write_text('sensitive')
    (root / 'link').symlink_to(outside, target_is_directory=True)
    (root / 'dir').mkdir()
    for path in ('link/file', 'dir'):
        with pytest.raises(ValueError):
            open_execution_file(root, path)
