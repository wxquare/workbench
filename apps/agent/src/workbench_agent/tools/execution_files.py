"""Read execution-directory files without following symlinks."""
import os
from pathlib import PurePosixPath
import stat


def open_execution_file(root, relative):
    if not isinstance(relative, str) or not relative or '\x00' in relative or '\\' in relative:
        raise ValueError('invalid execution path')
    path = PurePosixPath(relative)
    parts = relative.split('/')
    if path.is_absolute() or any(part in ('', '.', '..') for part in parts):
        raise ValueError('outside execution directory')
    directory = None
    descriptor = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('not a regular execution file')
        opened = os.fdopen(descriptor, 'rb')
        descriptor = None
        return opened
    except OSError as error:
        raise ValueError('invalid or outside execution directory') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
