import asyncio
import multiprocessing as mp
import sqlite3
import time
import pytest
from sqlalchemy import text


# A separate process owns the SQLite write lock; no shared connection or event loop.
def hold_lock(path, barrier, release):
    with sqlite3.connect(path, timeout=0) as db:
        db.execute('BEGIN IMMEDIATE')
        barrier.wait(timeout=10)
        release.wait(timeout=10)
        db.rollback()


async def test_process_contention_respects_total_deadline(tmp_path):
    from workbench_agent.cli.db import migrate
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.persistence.errors import StorageError

    path = tmp_path / 'db'
    url = f'sqlite+aiosqlite:///{path}'
    migrate(url)
    ctx = mp.get_context('spawn')
    barrier, release = ctx.Barrier(2), ctx.Event()
    process = ctx.Process(target=hold_lock, args=(str(path), barrier, release))
    process.start()
    barrier.wait(timeout=10)
    backend = SQLiteBackend(url)
    started = time.monotonic()
    try:
        with pytest.raises(StorageError) as error:
            async with backend.create_uow().begin_write() as uow:
                await uow.execute(
                    text(
                        "INSERT INTO teams(team_id,name,status) VALUES ('partial','partial','active')"
                    )
                )
        assert error.value.code == 'STORAGE_BUSY'
        assert time.monotonic() - started < 5.15
    finally:
        release.set()
        process.join(timeout=10)
        await backend.close()
        if process.is_alive():
            process.terminate()
            process.join()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM teams').fetchone()[0] == 0


async def test_independent_backends_serialize_unique_race(tmp_path):
    from workbench_agent.cli.db import migrate
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.persistence.errors import StorageError

    path = tmp_path / 'db'
    url = f'sqlite+aiosqlite:///{path}'
    migrate(url)
    left, right = SQLiteBackend(url), SQLiteBackend(url)

    async def insert(backend):
        try:
            async with backend.create_uow().begin_write() as uow:
                await uow.execute(
                    text("INSERT INTO teams(team_id,name,status) VALUES ('same','same','active')")
                )
            return 'committed'
        except StorageError as exc:
            return exc.code

    try:
        assert sorted(await asyncio.gather(insert(left), insert(right))) == [
            'UNIQUE_CONFLICT',
            'committed',
        ]
    finally:
        await left.close()
        await right.close()


def race_insert(path, barrier, results):
    async def run():
        from workbench_agent.persistence.backends.sqlite import SQLiteBackend
        from workbench_agent.persistence.errors import StorageError

        backend = SQLiteBackend(f'sqlite+aiosqlite:///{path}')
        barrier.wait(timeout=10)
        try:
            async with backend.create_uow().begin_write() as uow:
                await uow.execute(
                    text("INSERT INTO teams(team_id,name,status) VALUES ('race','race','active')")
                )
            results.put('committed')
        except StorageError as error:
            results.put(error.code)
        finally:
            await backend.close()

    asyncio.run(run())


def test_independent_processes_resolve_unique_race(tmp_path):
    from workbench_agent.cli.db import migrate

    path = tmp_path / 'db'
    migrate(f'sqlite+aiosqlite:///{path}')
    ctx = mp.get_context('spawn')
    barrier, results = ctx.Barrier(3), ctx.Queue()
    processes = [
        ctx.Process(target=race_insert, args=(str(path), barrier, results)) for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        assert sorted(results.get(timeout=10) for _ in processes) == [
            'UNIQUE_CONFLICT',
            'committed',
        ]
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join()
        results.close()
        results.join_thread()
    assert [process.exitcode for process in processes] == [0, 0]
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM teams').fetchone()[0] == 1
