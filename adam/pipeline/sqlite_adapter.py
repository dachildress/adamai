"""
SQLite adapter — physical planning/translation for the SQLite source type.

Takes a VALIDATED, Sentinel-approved query plan and translates it into a
single parameterized SQL statement, then executes it against a SQLite
connection and returns a structured result.

Security model (the load-bearing part of this slice):

  * SQL is assembled EXCLUSIVELY from structured plan fields. There is no
    code path that accepts a raw SQL string from the plan.
  * Entity and field identifiers are mapped through an allowlist DERIVED
    FROM THE SOURCE MODEL via ``SourceModel.resolve`` — names from the plan
    are never trusted directly. A reference that does not resolve in the
    model raises ``AdapterError`` (validation would already have caught it;
    this is defense in depth).
  * Filter values are passed as bound parameters (``?``). No filter value
    is ever concatenated into the SQL text. A value containing SQL
    metacharacters (e.g. ``'; DROP TABLE students;--``) is inert.
  * ``limit`` is a validated positive int and is the only literal inlined;
    aggregation aliases are checked against a strict identifier pattern and
    quoted.

This adapter advertises SQLITE_CAPABILITIES (join/group/agg/order).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from .adapter import READY, Adapter, AdapterHealth, health as make_health
from .adapter_capabilities import AdapterCapabilities, SQLITE_CAPABILITIES
from .execution_plan import ExecutionPlan, QueryBody
from .query_result import QueryResult  # relocated (Slice 7); re-exported for back-compat
from .sentinel import AdapterCostEstimate
from .source_model import SourceModel

# Output-alias identifiers must be simple to be quoted safely.
_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Filter op -> SQL operator (for the binary forms).
_BINARY_OPS = {
    "eq": "=", "ne": "!=", "lt": "<", "lte": "<=",
    "gt": ">", "gte": ">=", "like": "LIKE",
}
_JOIN_SQL = {"inner": "INNER JOIN", "left": "LEFT JOIN",
             "right": "RIGHT JOIN", "full": "FULL OUTER JOIN"}


class AdapterError(Exception):
    """Raised on a translation failure (e.g. an identifier that does not
    resolve in the source model). Distinct from a validation rejection."""


class SQLiteAdapter(Adapter):
    def __init__(
        self,
        connection: sqlite3.Connection,
        source_model: SourceModel,
        capabilities: AdapterCapabilities = SQLITE_CAPABILITIES,
        *,
        health_status: str = READY,
        health_detail: Optional[str] = None,
    ) -> None:
        self.connection = connection
        self.source_model = source_model
        self._capabilities = capabilities
        # Forced health for tests. A live in-memory DB is READY by default;
        # tests construct the adapter with e.g. health_status=OFFLINE to
        # exercise the runner's terminal short-circuit.
        self._health_status = health_status
        self._health_detail = health_detail

    # -- Adapter interface ------------------------------------------------

    def capabilities(self) -> AdapterCapabilities:
        return self._capabilities

    def health(self) -> AdapterHealth:
        """Report operational state. Defaults to READY for a live in-memory
        DB; honors a forced status set at construction (for tests)."""
        return make_health(self._health_status, self._health_detail)

    def estimate_cost(self, plan: ExecutionPlan) -> Optional[AdapterCostEstimate]:
        """A deterministic HEURISTIC, not a true optimizer estimate (SQLite
        has no cost oracle):

          * rows  — a cheap COUNT(*) on the base entity, capped by the
                    plan's limit. Coarse (ignores filter selectivity), but
                    honest and bounded.
          * complexity — derived from how many of {joins, aggregations,
                    group_by} the plan uses: 0 -> low, 1 -> medium, >=2 -> high.

        Returns None only if the base table can't be counted. The point of
        this method is the plumbing (adapter produces -> Sentinel consumes),
        not accuracy.
        """
        body = plan.body
        if not isinstance(body, QueryBody) or not body.entities:
            return None
        try:
            cur = self.connection.execute(f"SELECT COUNT(*) FROM {self._safe_table(body.entities[0])}")
            total = cur.fetchone()[0]
        except Exception:
            return None
        rows = total
        if rows is not None and body.limit is not None:
            rows = min(rows, body.limit)
        features = sum(bool(x) for x in (body.joins, body.aggregations, body.group_by))
        complexity = "low" if features == 0 else ("medium" if features == 1 else "high")
        return AdapterCostEstimate(rows=rows, bytes_scanned=None, complexity=complexity)

    # -- identifier mapping (allowlist via source model) ------------------

    def _safe_col(self, ref: str, entities: Tuple[str, ...]) -> str:
        """Map a plan field reference to a quoted "entity"."field" using
        ONLY names the source model declares. Never trusts the plan text."""
        resolved = self.source_model.resolve(ref, entities)
        if resolved is None:
            raise AdapterError(f"field reference does not resolve in source model: {ref!r}")
        entity, col = resolved
        # entity/col came from the model's own allowlist, so they are safe;
        # quote defensively regardless.
        return f'"{entity}"."{col}"'

    def _safe_table(self, entity: str) -> str:
        if not self.source_model.has_entity(entity):
            raise AdapterError(f"entity not in source model: {entity!r}")
        return f'"{entity}"'

    @staticmethod
    def _safe_alias(alias: str) -> str:
        if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
            raise AdapterError(f"unsafe aggregation alias: {alias!r}")
        return f'"{alias}"'

    # -- translation ------------------------------------------------------

    def translate(self, plan: ExecutionPlan) -> Tuple[str, List[Any]]:
        body = plan.body
        if not isinstance(body, QueryBody) or body.operation != "select":
            raise AdapterError("adapter only translates select query bodies")

        entities = body.entities
        params: List[Any] = []
        alias_set = {a.as_ for a in body.aggregations}

        # SELECT list: projection fields (skip any that are agg aliases),
        # then aggregation expressions.
        select_parts: List[str] = []
        for p in body.projection:
            if p in alias_set:
                continue
            select_parts.append(self._safe_col(p, entities))
        for a in body.aggregations:
            select_parts.append(
                f'{a.fn.upper()}({self._safe_col(a.field, entities)}) AS {self._safe_alias(a.as_)}'
            )
        if not select_parts:
            raise AdapterError("empty SELECT list after translation")

        # FROM + JOINs. First entity is the base table; each join attaches
        # the entity on its right-hand reference.
        from_sql = f"FROM {self._safe_table(entities[0])}"
        for j in body.joins:
            right_entity = j.right.split(".")[0]
            join_kw = _JOIN_SQL.get(j.type)
            if join_kw is None:
                raise AdapterError(f"unsupported join type: {j.type!r}")
            on = f"{self._safe_col(j.left, entities)} = {self._safe_col(j.right, entities)}"
            from_sql += f" {join_kw} {self._safe_table(right_entity)} ON {on}"

        # WHERE — every value bound as a parameter; nothing concatenated.
        where_parts: List[str] = []
        for f in body.filters:
            col = self._safe_col(f.field, entities)
            op = f.op
            if op in _BINARY_OPS:
                where_parts.append(f"{col} {_BINARY_OPS[op]} ?")
                params.append(f.value)
            elif op in ("in", "not_in"):
                values = list(f.value) if isinstance(f.value, (list, tuple)) else [f.value]
                placeholders = ", ".join("?" for _ in values)
                kw = "IN" if op == "in" else "NOT IN"
                where_parts.append(f"{col} {kw} ({placeholders})")
                params.extend(values)
            elif op == "between":
                lo, hi = f.value
                where_parts.append(f"{col} BETWEEN ? AND ?")
                params.extend([lo, hi])
            elif op == "is_null":
                where_parts.append(f"{col} IS NULL")
            elif op == "not_null":
                where_parts.append(f"{col} IS NOT NULL")
            else:
                raise AdapterError(f"unsupported filter op: {op!r}")
        where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

        # GROUP BY
        group_sql = ""
        if body.group_by:
            cols = ", ".join(self._safe_col(g, entities) for g in body.group_by)
            group_sql = f" GROUP BY {cols}"

        # ORDER BY — a column ref OR an aggregation alias.
        order_sql = ""
        if body.order_by:
            parts = []
            for o in body.order_by:
                if o.field in alias_set:
                    ref = self._safe_alias(o.field)
                else:
                    ref = self._safe_col(o.field, entities)
                direction = "ASC" if o.direction == "asc" else "DESC"
                parts.append(f"{ref} {direction}")
            order_sql = f" ORDER BY {', '.join(parts)}"

        # LIMIT — validated positive int; safe to inline.
        limit_sql = f" LIMIT {int(body.limit)}"

        sql = f"SELECT {', '.join(select_parts)} {from_sql}{where_sql}{group_sql}{order_sql}{limit_sql}"
        return sql, params

    # -- execution --------------------------------------------------------

    def execute(self, plan: ExecutionPlan) -> QueryResult:
        sql, params = self.translate(plan)
        cur = self.connection.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
        return QueryResult(
            columns=columns,
            rows=[tuple(r) for r in rows],
            row_count=len(rows),
            sql=sql,
            params=list(params),
            source_lineage={
                "source_model_version": plan.source_model_version,
                "entities": list(plan.body.entities),
                "plan_id": plan.plan_id,
            },
        )


# ---------------------------------------------------------------------------
# Synthetic test database (matches synthetic-school-v1)
# ---------------------------------------------------------------------------

def create_synthetic_db(connection: Optional[sqlite3.Connection] = None) -> sqlite3.Connection:
    """Create and seed an in-memory synthetic school database whose tables
    and columns match the synthetic-school-v1 source model. Returns the
    connection."""
    conn = connection or sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.executescript(
        """
        DROP TABLE IF EXISTS attendance;
        DROP TABLE IF EXISTS students;
        DROP TABLE IF EXISTS schools;

        CREATE TABLE schools (id INTEGER PRIMARY KEY, name TEXT, level TEXT);
        CREATE TABLE students (
            id INTEGER PRIMARY KEY, name TEXT, school_id INTEGER,
            grade_level TEXT, enrolled INTEGER
        );
        CREATE TABLE attendance (
            id INTEGER PRIMARY KEY, student_id INTEGER, school_id INTEGER,
            period TEXT, rate REAL, date TEXT
        );
        """
    )
    cur.executemany("INSERT INTO schools VALUES (?,?,?)", [
        (1, "Maple Elementary", "elementary"),
        (2, "Oak Elementary", "elementary"),
        (3, "Cedar Middle", "middle"),
    ])
    cur.executemany("INSERT INTO students VALUES (?,?,?,?,?)", [
        (1, "Ann", 1, "3", 1),
        (2, "Ben", 1, "4", 1),
        (3, "Cy", 2, "2", 1),
        (4, "Dee", 3, "7", 1),
    ])
    cur.executemany("INSERT INTO attendance VALUES (?,?,?,?,?,?)", [
        (1, 1, 1, "2026-Q1", 0.95, "2026-01-15"),
        (2, 2, 1, "2026-Q1", 0.90, "2026-01-15"),
        (3, 3, 2, "2026-Q1", 0.80, "2026-01-15"),
        (4, 4, 3, "2026-Q1", 0.99, "2026-01-15"),
    ])
    conn.commit()
    return conn
