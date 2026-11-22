"""Global search.

Each module registers a provider; the top-bar search asks every provider the
person is allowed to use and merges the results. Providers run inside the
active church context, so results are tenant-isolated automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from app.utils.tenancy import TenantContextMissing


@dataclass
class SearchResult:
    title: str
    url: str
    kind: str            # e.g. "Member", "Event"
    subtitle: str = ""
    icon: str = "search"


@dataclass
class _Provider:
    name: str
    permission: str | None
    fn: Callable[[str, int], Iterable[SearchResult]]


_PROVIDERS: list[_Provider] = []


def register(name: str, permission: str | None = None):
    """Decorator: ``@search.register("members", "members.view")``."""
    def wrap(fn):
        if not any(p.name == name for p in _PROVIDERS):
            _PROVIDERS.append(_Provider(name, permission, fn))
        return fn
    return wrap


def run(query: str, can: Callable[[str], bool], limit_per_provider: int = 8) -> list[SearchResult]:
    query = (query or "").strip()
    if len(query) < 2:
        return []
    results: list[SearchResult] = []
    for p in _PROVIDERS:
        if p.permission and not can(p.permission):
            continue
        try:
            results.extend(list(p.fn(query, limit_per_provider))[:limit_per_provider])
        except TenantContextMissing:        # no church selected: church data is not searchable
            continue
    return results
