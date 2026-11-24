"""Immutable Agent baseline derived structurally from the last Workspace snapshot."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
)
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import conv

from workbench_agent.persistence.schema_v20 import metadata as historical

SCHEMA_VERSION = 'dod-storage-agent-v1'
CHECKPOINT_CODEC_VERSION = 'dod-checkpoint-v1'
MIGRATION_HEAD = '0001_agent_domain'


def _identifier(name):
    return name.replace('workspaces', 'agents').replace('workspace_id', 'agent_id')


metadata = MetaData(naming_convention=dict(historical.naming_convention))
_columns = {}
for _source in historical.tables.values():
    _table = Table(
        _identifier(_source.name),
        metadata,
        comment=_source.comment,
        info=dict(_source.info),
        **dict(_source.kwargs),
    )
    for _column in _source.columns:
        _new = Column(
            _identifier(_column.name),
            _column.type.copy(),
            nullable=_column.nullable,
            autoincrement=_column.autoincrement,
            server_default=_column.server_default.arg
            if _column.server_default is not None
            else None,
            comment=_column.comment,
            info=dict(_column.info),
            **dict(_column.kwargs),
        )
        _table.append_column(_new)
        _columns[_column] = _new

for _source in historical.tables.values():
    _table = metadata.tables[_identifier(_source.name)]
    for _constraint in _source.constraints:
        _options = dict(
            name=conv(_identifier(_constraint.name)) if _constraint.name else None,
            deferrable=_constraint.deferrable,
            initially=_constraint.initially,
            info=dict(_constraint.info),
            **dict(_constraint.kwargs),
        )
        _local = [_columns[c] for c in _constraint.columns]
        if isinstance(_constraint, ForeignKeyConstraint):
            _copy = ForeignKeyConstraint(
                _local,
                [_identifier(e.target_fullname) for e in _constraint.elements],
                ondelete=_constraint.ondelete,
                onupdate=_constraint.onupdate,
                use_alter=_constraint.use_alter,
                match=_constraint.match,
                **_options,
            )
        elif isinstance(_constraint, PrimaryKeyConstraint):
            _copy = PrimaryKeyConstraint(*_local, **_options)
        elif isinstance(_constraint, UniqueConstraint):
            _copy = UniqueConstraint(*_local, **_options)
        elif isinstance(_constraint, CheckConstraint):
            _expression = visitors.replacement_traverse(
                _constraint.sqltext, {}, lambda element: _columns.get(element)
            )
            _copy = CheckConstraint(_expression, **_options)
        else:
            raise TypeError(f'unsupported historical constraint: {_constraint!r}')
        _table.append_constraint(_copy)
    for _index in _source.indexes:
        Index(
            _identifier(_index.name),
            *[
                visitors.replacement_traverse(expression, {}, lambda element: _columns.get(element))
                for expression in _index.expressions
            ],
            unique=_index.unique,
            info=dict(_index.info),
            **dict(_index.kwargs),
        )
