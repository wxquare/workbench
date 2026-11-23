"""Explicit maintenance entry point; ordinary services never call migrate."""
from pathlib import Path
import sqlite3
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, URL
from workbench_agent.settings import require_sqlite_absolute_path, MIN_SQLITE_VERSION


class FreshAgentDatabaseRequired(RuntimeError):
    """Known initialization boundary, safe to explain without exposing driver errors."""


def _preflight(connection) -> str:
    """Inspect existing data without persisted writes and identify supported upgrades."""
    from workbench_agent.persistence.schema_agent_v1 import SCHEMA_VERSION, CHECKPOINT_CODEC_VERSION
    from workbench_agent.persistence.sqlite_diagnostics import validate_required_schema
    objects = connection.execute("SELECT name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
    if not objects:
        return 'fresh'
    message = ('Agent storage requires a fresh database path for legacy Workspace or incompatible storage; '
               'choose a fresh Agent database path; existing database left untouched')
    try:
        versions = connection.execute(
            'SELECT schema_version,checkpoint_codec_version FROM dod_storage_metadata'
        ).fetchall()
        revision = connection.execute('SELECT version_num FROM alembic_version').fetchall()
        from workbench_agent.persistence.schema_agent_v6 import MIGRATION_HEAD
        known = {'0001_agent_domain', '0002_shared_skills', '0003_run_trace_contexts', '0004_knowledge_base', '0005_graph_id', MIGRATION_HEAD}
        current_revision = revision[0][0] if len(revision) == 1 else None
        if (current_revision not in known or versions != [(SCHEMA_VERSION, CHECKPOINT_CODEC_VERSION)]
                or validate_required_schema(connection, revision=current_revision)):
            raise FreshAgentDatabaseRequired(message)
        if current_revision in {'0001_agent_domain', '0002_shared_skills', '0003_run_trace_contexts', '0004_knowledge_base'} and any(connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in ('runtime_profiles', 'runs', 'run_jobs')):
            raise FreshAgentDatabaseRequired('Graph ID migration requires an empty database; clear or recreate it first')
        if connection.execute('PRAGMA journal_mode').fetchone() != ('wal',):
            raise FreshAgentDatabaseRequired('existing Agent database requires WAL (journal_mode=wal); '
                               'choose a fresh Agent database path; existing database left untouched')
    except sqlite3.Error as error:
        raise FreshAgentDatabaseRequired(message) from error
    return 'current' if current_revision == MIGRATION_HEAD else 'upgrade'


def migrate(database_url:str) -> None:
    path = require_sqlite_absolute_path(database_url)
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        raise RuntimeError("linked SQLite runtime must be >=3.51.3")
    # Reject old, populated or empty Workspace schemas before WAL or any DDL.
    if path.exists():
        from contextlib import closing
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as existing:
            if _preflight(existing) == 'current':
                return
    # Resolve packaged Python migration resources, independent of cwd/alembic.ini.
    directory = Path(__file__).resolve().parents[1] / "persistence" / "agent_migrations"
    config = Config()
    config.set_main_option("script_location",str(directory))
    engine = create_engine(URL.create("sqlite",database=str(path)))
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA busy_timeout=5000")
            # Retain the exclusive lock across the commit required before WAL.
            # The authoritative freshness check runs under that same lock.
            connection.exec_driver_sql("PRAGMA locking_mode=EXCLUSIVE")
            connection.exec_driver_sql("BEGIN EXCLUSIVE")
            state = _preflight(connection.connection.driver_connection)
            if state == 'current':
                return
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA synchronous=FULL")
            mode = connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
            if mode != "wal":
                raise RuntimeError("maintenance migration requires WAL")
            connection.exec_driver_sql("PRAGMA wal_autocheckpoint=1000")
            connection.commit()
            config.attributes["connection"] = connection
            # Explicit transaction makes SQLite DDL and revision stamp atomic.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                command.upgrade(config,"head")
                violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError("migration foreign key validation failed")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    finally:
        engine.dispose()
