"""
Adapter capability declaration.

The adapter advertises what it can express so the skill/validation plans
within them, rather than discovering limitations at execution time. A plan
that requires a capability the adapter does not advertise is rejected with
a CAPABILITY_ERROR (statically, at validation, when detectable).

This slice models the minimal set the SQLite adapter needs.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterCapabilities:
    supports_join: bool = False
    supports_grouping: bool = False
    supports_aggregation: bool = False
    supports_ordering: bool = False


# The SQLite adapter in this slice can do all four. Tests that exercise a
# capability rejection construct their own AdapterCapabilities with the
# relevant flag set False (e.g. supports_join=False).
SQLITE_CAPABILITIES = AdapterCapabilities(
    supports_join=True,
    supports_grouping=True,
    supports_aggregation=True,
    supports_ordering=True,
)
