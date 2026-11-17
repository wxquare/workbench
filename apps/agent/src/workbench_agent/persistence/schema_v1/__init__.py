"""Immutable v1 schema snapshot used by migration 0001; future revisions get new modules.

common defines portable types/builders, configuration owns published resources,
runtime owns Run/checkpoint data, operations owns delivery/management data.
"""
from .common import metadata
from . import configuration, runtime, operations
