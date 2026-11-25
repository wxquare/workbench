"""Immutable v1 schema snapshot used by migration 0001; future revisions get new modules.

common defines portable types/builders, configuration owns published resources,
runtime owns Run/checkpoint data, operations owns delivery/management data.
"""

from .common import metadata as metadata

# These imports register the historical tables in the shared metadata.
from . import configuration as configuration
from . import runtime as runtime
from . import operations as operations
