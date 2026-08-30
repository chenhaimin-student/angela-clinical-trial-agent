"""Auditable local CDE trial storage.

The package is used by both the browser-backed collection pipeline and the
read-only local query path.  Keep package imports lazy so a query process does
not need browser/parser dependencies such as BeautifulSoup.
"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "DateFilterType": ("contracts", "DateFilterType"),
    "RecruitmentStatus": ("contracts", "RecruitmentStatus"),
    "SortField": ("contracts", "SortField"),
    "TrialQuery": ("contracts", "TrialQuery"),
    "CatalogSyncService": ("catalog_sync", "CatalogSyncService"),
    "CatalogTotals": ("catalog_sync", "CatalogTotals"),
    "parse_catalog_totals": ("catalog_sync", "parse_catalog_totals"),
    "ParsedCdePage": ("cde_parser", "ParsedCdePage"),
    "parse_cde_detail": ("cde_parser", "parse_cde_detail"),
    "connect_database": ("database", "connect_database"),
    "initialize_database": ("database", "initialize_database"),
    "DiseaseConcepts": ("local_query", "DiseaseConcepts"),
    "LocalTrialQuery": ("local_query", "LocalTrialQuery"),
    "LocalTrialQueryService": ("local_query", "LocalTrialQueryService"),
    "archive_html": ("raw_store", "archive_html"),
    "TrialRepository": ("repository", "TrialRepository"),
    "CdeSyncService": ("sync_service", "CdeSyncService"),
    "BrowserWorkerClient": ("worker_client", "BrowserWorkerClient"),
    "WorkerClientError": ("worker_client", "WorkerClientError"),
    "WorkerResource": ("worker_client", "WorkerResource"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value

__all__ = [
    "DateFilterType",
    "CatalogSyncService",
    "CatalogTotals",
    "ParsedCdePage",
    "RecruitmentStatus",
    "SortField",
    "TrialQuery",
    "TrialRepository",
    "CdeSyncService",
    "BrowserWorkerClient",
    "WorkerClientError",
    "WorkerResource",
    "archive_html",
    "connect_database",
    "initialize_database",
    "DiseaseConcepts",
    "LocalTrialQuery",
    "LocalTrialQueryService",
    "parse_cde_detail",
    "parse_catalog_totals",
]
