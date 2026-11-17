"""Shared bounded read-only inspection used by backend health and the doctor CLI."""
from pathlib import Path
import re
import sqlite3

METADATA_TABLE = "dod_storage_metadata"
EXPECTED_PRAGMAS = {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1,
                    "busy_timeout": 5000, "wal_autocheckpoint": 1000}

_SQL_TOKENS = re.compile(
    r"--[^\r\n]*|/\*.*?\*/|\s+|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
    r"`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|>=|<=|<>|!=|==|[^\s]",
    re.DOTALL,
)


def _sql_tokens(sql):
    return tuple(token if token[0] in "'\"`[" else token.casefold()
                 for token in _SQL_TOKENS.findall(sql)
                 if not token.isspace() and not token.startswith(('--', '/*')))


def _check_expressions(table_sql):
    """Read top-level CHECK bodies without mistaking comments or literals for SQL."""
    tokens = _sql_tokens(table_sql or '')
    checks = set()
    depth = 0
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token == 'check' and depth == 1 and tokens[position+1:position+2] == ('(',):
            start = position+2
            position = start
            nested = 1
            while position < len(tokens):
                if tokens[position] == '(':
                    nested += 1
                elif tokens[position] == ')':
                    nested -= 1
                if nested == 0:
                    checks.add(tokens[start:position])
                    break
                position += 1
        elif token == '(':
            depth += 1
        elif token == ')':
            depth -= 1
        position += 1
    return checks


def open_readonly_connection(path: Path, timeout_seconds: float = 5) -> sqlite3.Connection:
    """Only connection-local settings are applied; persisted journal mode is inspected."""
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=timeout_seconds)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA wal_autocheckpoint=1000")
        return connection
    except BaseException:
        connection.close()
        raise


def validate_required_schema(connection, constrain_lock_wait=lambda:None, *, baseline=False, revision=None) -> list[str]:
    """Read-only verification of the Agent baseline's actual structure."""
    from sqlalchemy import CheckConstraint, UniqueConstraint
    from sqlalchemy.dialects.sqlite import dialect
    if baseline or revision == '0001_agent_domain':
        from workbench_agent.persistence.schema_agent_v1 import metadata, MIGRATION_HEAD
    elif revision == '0002_shared_skills':
        from workbench_agent.persistence.schema_agent_v2 import metadata, MIGRATION_HEAD
    else:
        from workbench_agent.persistence.schema_agent_v3 import metadata, MIGRATION_HEAD
    def query(sql):
        constrain_lock_wait()
        return connection.execute(sql).fetchall()
    actual_tables = dict(query("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    if 'workspaces' in actual_tables:
        return ["legacy Workspace storage is incompatible; choose a fresh Agent database path; existing database left untouched"]
    try:
        if query("SELECT version_num FROM alembic_version") != [(MIGRATION_HEAD,)]:
            return ["storage migration revision is not current; explicit maintenance migration required"]
    except sqlite3.Error:
        return ["storage migration revision missing"]
    for name,table in metadata.tables.items():
        if name not in actual_tables:
            return ["required business schema is missing/incompatible"]
        # Names come exclusively from the fixed schema, never user input.
        columns = {row[1]:row for row in query(f'PRAGMA table_info("{name}")')}
        for column in table.columns:
            actual = columns.get(column.name)
            if actual is None or actual[2].upper() != column.type.compile(dialect=dialect()).upper() or bool(actual[3]) != (not column.nullable):
                return ["required business schema columns are incompatible"]
        expected_pk = tuple(column.name for column in table.primary_key.columns)
        actual_pk = tuple(row[1] for row in sorted(columns.values(),key=lambda r:r[5]) if row[5])
        if actual_pk != expected_pk:
            return ["required business schema primary key is incompatible"]
        indexes = set()
        for row in query(f'PRAGMA index_list("{name}")'):
            if row[4]:
                continue
            index_name = row[1].replace('"','""')
            names = tuple(item[2] for item in query(f'PRAGMA index_info("{index_name}")'))
            indexes.add((bool(row[2]),names))
        expected_indexes = {(True,tuple(c.name for c in constraint.columns)) for constraint in table.constraints if isinstance(constraint,UniqueConstraint)}
        expected_indexes.update((bool(index.unique),tuple(c.name for c in index.columns)) for index in table.indexes)
        if not expected_indexes <= indexes:
            return ["required business schema indexes are missing/incompatible"]
        fk_groups = {}
        for row in query(f'PRAGMA foreign_key_list("{name}")'):
            fk_groups.setdefault(row[0],[]).append(row)
        actual_fks = set()
        for rows in fk_groups.values():
            rows.sort(key=lambda r:r[1])
            actual_fks.add((tuple(row[3] for row in rows),rows[0][2],tuple(row[4] for row in rows),rows[0][6]))
        expected_fks = {(tuple(e.parent.name for e in fk.elements),fk.referred_table.name,
            tuple(e.column.name for e in fk.elements),fk.ondelete) for fk in table.foreign_key_constraints}
        if not expected_fks <= actual_fks:
            return ["required business schema foreign keys are missing/incompatible"]
        expected_checks = {_sql_tokens(str(constraint.sqltext.compile(dialect=dialect())))
                           for constraint in table.constraints if isinstance(constraint, CheckConstraint)}
        if expected_checks and not expected_checks <= _check_expressions(actual_tables[name]):
            return ["required business schema CHECK constraints are missing/incompatible"]
    if query("PRAGMA quick_check") != [("ok",)] or query("PRAGMA foreign_key_check"):
        return ["storage integrity validation failed"]
    return []
