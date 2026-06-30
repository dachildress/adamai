"""
MySQLAdapter — a second concrete `Adapter`, proving a genuinely different SQL
dialect fits behind the contract WITHOUT touching governance.

The dialect differences vs. SQLite — MySQL uses `%s` placeholders and
backtick quoting where SQLite uses `?` and double quotes — stay ENTIRELY
inside this module. The `Adapter` ABC, runner, Sentinel, and skill never see
`%s` or backticks. That containment is the whole proof.

Deliberately NOT a subclass of a shared SQL base: two SQL adapters are not
enough evidence to extract one (SQLite + MySQL are deceptively similar; a
base built from two would bake in accidental agreements a third adapter
fights). MySQLAdapter's only base is `Adapter`; any sharing is via free
helper functions, not inheritance.

The real driver (PyMySQL) is imported LAZILY (only when a real connection is
built), so importing this module — and the whole pipeline — never requires
the driver, and the fake-backed unit tests run with no server.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from .adapter import (
    AUTHENTICATION_FAILED,
    OFFLINE,
    READY,
    Adapter,
    AdapterHealth,
    health as make_health,
)
from .adapter_capabilities import AdapterCapabilities
from .execution_plan import ExecutionPlan, QueryBody
from .query_result import QueryResult
from .sentinel import AdapterCostEstimate
from .source_model import SourceModel

# MySQL advertises the same expressive set as the SQLite adapter (honest:
# MySQL supports joins/grouping/aggregation/ordering).
MYSQL_CAPABILITIES = AdapterCapabilities(
    supports_join=True, supports_grouping=True,
    supports_aggregation=True, supports_ordering=True,
)

# Dialect details — PRIVATE to this module. (`%s`, backticks live ONLY here.)
_PLACEHOLDER = "%s"
_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BINARY_OPS = {
    "eq": "=", "ne": "!=", "lt": "<", "lte": "<=",
    "gt": ">", "gte": ">=", "like": "LIKE",
}
_JOIN_SQL = {"inner": "INNER JOIN", "left": "LEFT JOIN",
             "right": "RIGHT JOIN", "full": "FULL OUTER JOIN"}

# MySQL access-denied error codes (driver-agnostic: matched on the numeric
# code in the exception args, so we don't import pymysql to classify).
_AUTH_ERROR_CODES = {1044, 1045, 1698, 1396}


# ---------------------------------------------------------------------------
# Typed adapter errors (internal diagnostics, by layer)
# ---------------------------------------------------------------------------

IDENTIFIER_RESOLUTION_ERROR = "IDENTIFIER_RESOLUTION_ERROR"
TRANSLATION_ERROR = "TRANSLATION_ERROR"
EXECUTION_ERROR = "EXECUTION_ERROR"
CONNECTION_ERROR = "CONNECTION_ERROR"


class MySQLAdapterError(Exception):
    category = "ADAPTER_ERROR"


class IdentifierResolutionError(MySQLAdapterError):
    category = IDENTIFIER_RESOLUTION_ERROR


class TranslationError(MySQLAdapterError):
    category = TRANSLATION_ERROR


class ExecutionError(MySQLAdapterError):
    category = EXECUTION_ERROR


class AdapterConnectionError(MySQLAdapterError):
    category = CONNECTION_ERROR


@dataclass
class TranslatedQuery:
    """Pure plan→dialect artifact (no I/O): MySQL SQL + bound params."""
    sql: str
    params: List[Any]


# ---------------------------------------------------------------------------
# Production connection seam (lazy driver import)
# ---------------------------------------------------------------------------

def make_pymysql_connect_fn(**connect_kwargs) -> Callable[[], Any]:
    """Return a zero-arg factory that lazily imports PyMySQL and opens a real
    connection. Credentials come from connect_kwargs (host/user/password/
    database/port) — NEVER hardcoded. The import is inside the closure so the
    driver is only required when a real connection is actually opened."""
    def _connect():
        import pymysql  # lazy — keeps the pipeline importable without the driver
        return pymysql.connect(**connect_kwargs)
    return _connect


def _classify_connection_error(exc: Exception) -> str:
    """Map a driver/connection exception to a terminal health status by error
    code (auth vs. unreachable), without importing the driver."""
    code = exc.args[0] if getattr(exc, "args", None) and isinstance(exc.args[0], int) else None
    return AUTHENTICATION_FAILED if code in _AUTH_ERROR_CODES else OFFLINE


# ---------------------------------------------------------------------------
# MySQLAdapter
# ---------------------------------------------------------------------------

class MySQLAdapter(Adapter):
    def __init__(
        self,
        source_model: SourceModel,
        *,
        connection: Any = None,
        connect_fn: Optional[Callable[[], Any]] = None,
        capabilities: AdapterCapabilities = MYSQL_CAPABILITIES,
        health_ttl_seconds: float = 5.0,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source_model = source_model
        self._conn = connection
        self._connect_fn = connect_fn
        self._capabilities = capabilities
        self.health_ttl_seconds = health_ttl_seconds
        self._monotonic = monotonic_fn
        self._health_cache: Optional[Tuple[AdapterHealth, float]] = None

    # -- connection -------------------------------------------------------

    def _get_connection(self) -> Any:
        if self._conn is not None:
            return self._conn
        if self._connect_fn is None:
            raise AdapterConnectionError("no connection or connect_fn provided")
        try:
            self._conn = self._connect_fn()
        except Exception as e:  # driver/network/auth error
            # Chain the original so health() can read its driver error code.
            raise AdapterConnectionError(str(e)) from e
        return self._conn

    # -- Adapter interface ------------------------------------------------

    def capabilities(self) -> AdapterCapabilities:
        return self._capabilities

    def health(self) -> AdapterHealth:
        """REAL connection health with a short TTL cache. READY when the
        connection pings; OFFLINE/AUTHENTICATION_FAILED on connect/auth
        failure. Within the TTL a repeat call returns the cached result
        without re-pinging."""
        now = self._monotonic()
        if self._health_cache is not None and (now - self._health_cache[1]) < self.health_ttl_seconds:
            return self._health_cache[0]
        try:
            conn = self._get_connection()
            conn.ping()  # PyMySQL Connection.ping; fakes implement it too
            h = make_health(READY)
        except AdapterConnectionError as e:
            # The underlying cause carries the driver code; classify it.
            cause = e.__cause__ or e
            self._conn = None  # force reconnect next time
            h = make_health(_classify_connection_error(cause), detail=str(e))
        except Exception as e:
            self._conn = None
            h = make_health(_classify_connection_error(e), detail=str(e))
        self._health_cache = (h, now)
        return h

    def estimate_cost(self, plan: ExecutionPlan) -> Optional[AdapterCostEstimate]:
        """Heuristic (not a real optimizer estimate): complexity from join/
        aggregation/group_by count; a coarse COUNT(*) on the base table when
        reachable, capped by limit. Reuses the shared AdapterCostEstimate."""
        body = plan.body
        if not isinstance(body, QueryBody) or not body.entities:
            return None
        features = sum(bool(x) for x in (body.joins, body.aggregations, body.group_by))
        complexity = "low" if features == 0 else ("medium" if features == 1 else "high")
        rows: Optional[int] = None
        try:
            cur = self._get_connection().cursor()
            cur.execute(f"SELECT COUNT(*) FROM {self._q_table(body.entities[0])}")
            row = cur.fetchone()
            rows = row[0] if row else None
            if rows is not None and body.limit is not None:
                rows = min(rows, body.limit)
        except Exception:
            rows = None
        return AdapterCostEstimate(rows=rows, bytes_scanned=None, complexity=complexity)

    def execute(self, plan: ExecutionPlan) -> QueryResult:
        tq = self.translate(plan)
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(tq.sql, tq.params)
            rows = cur.fetchall()
            columns = [d[0] for d in cur.description] if cur.description else []
        except AdapterConnectionError:
            raise
        except Exception as e:
            raise ExecutionError(str(e)) from e
        return QueryResult(
            columns=list(columns),
            rows=[tuple(r) for r in rows],
            row_count=len(rows),
            sql=tq.sql,
            params=list(tq.params),
            source_lineage={
                "source_model_version": plan.source_model_version,
                "plan_id": plan.plan_id,
                "adapter": "mysql",
            },
        )

    # -- identifier mapping (allowlist via source model; backticks PRIVATE) --

    def _q_col(self, ref: str, entities: Tuple[str, ...]) -> str:
        resolved = self.source_model.resolve(ref, entities)
        if resolved is None:
            raise IdentifierResolutionError(f"field reference not in source model: {ref!r}")
        entity, col = resolved
        return f"`{entity}`.`{col}`"

    def _q_table(self, entity: str) -> str:
        if not self.source_model.has_entity(entity):
            raise IdentifierResolutionError(f"entity not in source model: {entity!r}")
        return f"`{entity}`"

    @staticmethod
    def _q_alias(alias: str) -> str:
        if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
            raise TranslationError(f"unsafe aggregation alias: {alias!r}")
        return f"`{alias}`"

    # -- translation: plan -> MySQL TranslatedQuery (pure; no I/O) ---------

    def translate(self, plan: ExecutionPlan) -> TranslatedQuery:
        body = plan.body
        if not isinstance(body, QueryBody) or body.operation != "select":
            raise TranslationError("adapter only translates select query bodies")

        entities = body.entities
        params: List[Any] = []
        alias_set = {a.as_ for a in body.aggregations}

        select_parts: List[str] = []
        for p in body.projection:
            if p in alias_set:
                continue
            select_parts.append(self._q_col(p, entities))
        for a in body.aggregations:
            select_parts.append(
                f"{a.fn.upper()}({self._q_col(a.field, entities)}) AS {self._q_alias(a.as_)}"
            )
        if not select_parts:
            raise TranslationError("empty SELECT list after translation")

        # FROM/JOIN: emit every entity EXACTLY ONCE. Tables are not aliased and
        # columns are qualified as `entity`.`col`, so referencing the same
        # physical table twice (base == a join target, or two joins sharing a
        # right entity) would produce MySQL error 1066 ("Not unique table/alias").
        # We dedup: start at entities[0], then for each join add only the side
        # that introduces a NOT-yet-emitted entity. A join whose BOTH sides are
        # already emitted is a redundant equality predicate -> routed to WHERE
        # (built with the same _q_col resolution, never raw model strings). A
        # join whose connecting table isn't present yet is deferred; if no join
        # can make progress and entities remain unconnected, the plan is
        # malformed -> TranslationError (never a silent cross join).
        emitted = {entities[0]}
        from_sql = f"FROM {self._q_table(entities[0])}"
        extra_join_predicates: List[str] = []
        remaining = list(body.joins)
        progressed = True
        while remaining and progressed:
            progressed = False
            deferred: List[Any] = []
            for j in remaining:
                join_kw = _JOIN_SQL.get(j.type)
                if join_kw is None:
                    raise TranslationError(f"unsupported join type: {j.type!r}")
                left_entity = j.left.split(".")[0]
                right_entity = j.right.split(".")[0]
                on = f"{self._q_col(j.left, entities)} = {self._q_col(j.right, entities)}"
                left_in, right_in = left_entity in emitted, right_entity in emitted
                if left_in and right_in:
                    # Both tables already joined: keep the equality as a WHERE
                    # predicate (safe — resolved via _q_col), do not re-add a table.
                    extra_join_predicates.append(on)
                    progressed = True
                elif left_in and not right_in:
                    from_sql += f" {join_kw} {self._q_table(right_entity)} ON {on}"
                    emitted.add(right_entity)
                    progressed = True
                elif right_in and not left_in:
                    from_sql += f" {join_kw} {self._q_table(left_entity)} ON {on}"
                    emitted.add(left_entity)
                    progressed = True
                else:
                    deferred.append(j)  # neither side present yet -> retry next pass
            remaining = deferred
        if remaining:
            raise TranslationError(
                "malformed plan: joins do not connect all entities (disconnected)")
        unconnected = [e for e in entities if e not in emitted]
        if unconnected:
            raise TranslationError(
                f"malformed plan: entities not connected by any join: {unconnected!r}")

        where_parts: List[str] = list(extra_join_predicates)
        for f in body.filters:
            col = self._q_col(f.field, entities)
            op = f.op
            if op in _BINARY_OPS:
                where_parts.append(f"{col} {_BINARY_OPS[op]} {_PLACEHOLDER}")
                params.append(f.value)
            elif op in ("in", "not_in"):
                values = list(f.value) if isinstance(f.value, (list, tuple)) else [f.value]
                placeholders = ", ".join(_PLACEHOLDER for _ in values)
                kw = "IN" if op == "in" else "NOT IN"
                where_parts.append(f"{col} {kw} ({placeholders})")
                params.extend(values)
            elif op == "between":
                lo, hi = f.value
                where_parts.append(f"{col} BETWEEN {_PLACEHOLDER} AND {_PLACEHOLDER}")
                params.extend([lo, hi])
            elif op == "is_null":
                where_parts.append(f"{col} IS NULL")
            elif op == "not_null":
                where_parts.append(f"{col} IS NOT NULL")
            else:
                raise TranslationError(f"unsupported filter op: {op!r}")
        where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

        group_sql = ""
        if body.group_by:
            cols = ", ".join(self._q_col(g, entities) for g in body.group_by)
            group_sql = f" GROUP BY {cols}"

        # HAVING (post-aggregation filter, alias-based). The alias is quoted with
        # the SAME _q_alias helper ORDER BY uses (MySQL allows HAVING to reference
        # a SELECT alias); the threshold is a BOUND parameter, never inlined.
        having_sql = ""
        if body.having:
            hparts = []
            for h in body.having:
                op_sql = _BINARY_OPS.get(h.op)
                if op_sql is None:
                    raise TranslationError(f"unsupported having op: {h.op!r}")
                hparts.append(f"{self._q_alias(h.field)} {op_sql} {_PLACEHOLDER}")
                params.append(h.value)
            having_sql = f" HAVING {' AND '.join(hparts)}"

        order_sql = ""
        if body.order_by:
            parts = []
            for o in body.order_by:
                ref = self._q_alias(o.field) if o.field in alias_set else self._q_col(o.field, entities)
                direction = "ASC" if o.direction == "asc" else "DESC"
                parts.append(f"{ref} {direction}")
            order_sql = f" ORDER BY {', '.join(parts)}"

        limit_sql = f" LIMIT {int(body.limit)}"   # validated positive int; safe to inline

        sql = f"SELECT {', '.join(select_parts)} {from_sql}{where_sql}{group_sql}{having_sql}{order_sql}{limit_sql}"
        return TranslatedQuery(sql=sql, params=params)
