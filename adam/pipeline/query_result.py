"""
QueryResult — the source-neutral structured result every adapter returns.

Relocated out of sqlite_adapter.py (Slice 7) so multiple adapters (SQLite,
MySQL, …) import it from a non-SQLite home. The `sql` / `params` fields are
SQL-family conveniences (debug/inspection); a non-SQL adapter leaves them
empty. The Adapter ABC only references this type — never a SQL concept.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass
class QueryResult:
    columns: List[str]
    rows: List[Tuple[Any, ...]]
    row_count: int
    sql: str
    params: List[Any]
    source_lineage: Dict[str, Any] = field(default_factory=dict)
