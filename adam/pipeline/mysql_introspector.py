"""
MySQLIntrospector — real MySQL schema reader filling Slice 6's IntrospectionFn
seam (Slice 7b).

Reads `information_schema` (columns + key_column_usage) to build the rich
`IntrospectedSchema` (entities with per-field type/nullable/PK + FK
relationships). It does NOT change the Slice-6 ingestion lifecycle — it only
produces a schema that the existing lifecycle consumes.

Read-only: issues ONLY `information_schema` SELECTs (no DDL, no writes). The
MySQL driver (PyMySQL, same pin as Slice 7a) is imported LAZILY via the
injected-connection seam, so importing this module never requires the driver
and the fake-backed unit tests run with no server. Credentials are never
hardcoded — they come from an injected connection or a connect_fn.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .ingestion import (
    EntitySchema,
    FieldSchema,
    IntrospectedSchema,
    RelationshipSchema,
)
from .mysql_adapter import AdapterConnectionError

# Read-only schema queries (current database only). Explicit column order so
# the result tuples have a known shape.
_COLUMNS_SQL = (
    "SELECT table_name, column_name, data_type, is_nullable, column_key, ordinal_position "
    "FROM information_schema.columns "
    "WHERE table_schema = DATABASE() "
    "ORDER BY table_name, ordinal_position"
)
_FOREIGN_KEYS_SQL = (
    "SELECT table_name, column_name, referenced_table_name, referenced_column_name "
    "FROM information_schema.key_column_usage "
    "WHERE table_schema = DATABASE() AND referenced_table_name IS NOT NULL "
    "ORDER BY table_name, column_name"
)


class MySQLIntrospector:
    """Callable matching IntrospectionFn: (source_name) -> IntrospectedSchema.
    Bound to a real MySQL connection (or a lazy connect_fn)."""

    def __init__(
        self,
        *,
        connection: Any = None,
        connect_fn: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._conn = connection
        self._connect_fn = connect_fn

    def _get_connection(self) -> Any:
        if self._conn is not None:
            return self._conn
        if self._connect_fn is None:
            raise AdapterConnectionError("no connection or connect_fn provided")
        try:
            self._conn = self._connect_fn()
        except Exception as e:
            raise AdapterConnectionError(str(e)) from e
        return self._conn

    def __call__(self, source_name: str) -> IntrospectedSchema:
        conn = self._get_connection()
        cur = conn.cursor()

        cur.execute(_COLUMNS_SQL)
        col_rows = cur.fetchall()
        cur.execute(_FOREIGN_KEYS_SQL)
        fk_rows = cur.fetchall()

        # Group columns into entities (insertion order; fingerprint normalizes).
        by_table: Dict[str, List[FieldSchema]] = {}
        for row in col_rows:
            table, column, data_type, is_nullable, column_key = row[0], row[1], row[2], row[3], row[4]
            by_table.setdefault(table, []).append(FieldSchema(
                name=column,
                source_type=data_type,
                nullable=str(is_nullable).upper() == "YES",
                primary_key=str(column_key).upper() == "PRI",
            ))
        entities = tuple(
            EntitySchema(name=table, fields=tuple(fields))
            for table, fields in by_table.items()
        )

        relationships = tuple(
            RelationshipSchema(
                from_entity=row[0], from_field=row[1],
                to_entity=row[2], to_field=row[3],
                relationship_type="foreign_key",
            )
            for row in fk_rows
        )

        return IntrospectedSchema(
            entities=entities, relationships=relationships, source_name=source_name,
        )
