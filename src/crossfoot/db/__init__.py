"""Postgres access: the `ref` schema and the loaders that fill it.

Phase 2.0 puts the two companion tables (spec 01 §3.5) in the database so date and currency
logic can run SQL-side -- Phase 5's blocking computes date-tolerance windows in the query, and
it needs the same holiday list the generator used, not a second one.
"""

from crossfoot.db.connect import (
    RO_PASSWORD,
    RO_USER,
    connection,
    database_url,
    readonly_database_url,
)
from crossfoot.db.reference import LoadCounts, load_reference
from crossfoot.db.roles import apply_roles
from crossfoot.db.schema import apply_er, apply_script, sql

__all__ = [
    "RO_PASSWORD",
    "RO_USER",
    "LoadCounts",
    "apply_er",
    "apply_roles",
    "apply_script",
    "connection",
    "database_url",
    "load_reference",
    "readonly_database_url",
    "sql",
]
