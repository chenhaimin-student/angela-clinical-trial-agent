from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class CdeWorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerResource:
    kind: str
    query_term: str | None = None
    page: int | None = None
    detail_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind}
        if self.kind == "detail":
            result["detail_id"] = self.detail_id
        else:
            result["query_term"] = self.query_term
            result["page"] = self.page
        return result


class CdeBrowserWorkerClient:
    """Small client for the local-only CDE browser worker API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout_seconds: int = 120):
        if not base_url.startswith("http://127.0.0.1") and not base_url.startswith("http://localhost"):
            raise ValueError("CDE Browser Worker must be a local loopback service")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def fetch(self, job_id: str, request_id: str, resources: list[WorkerResource]) -> list[dict[str, Any]]:
        payload = json.dumps(
            {
                "job_id": job_id,
                "request_id": request_id,
                "resources": [item.to_dict() for item in resources],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/v1/cde/fetch",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise CdeWorkerError(f"CDE Browser Worker returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise CdeWorkerError("CDE Browser Worker is unavailable") from exc
        results = data.get("results")
        if not isinstance(results, list):
            raise CdeWorkerError("CDE Browser Worker returned an invalid response")
        failures = [item for item in results if item.get("status") != "succeeded"]
        if failures:
            codes = ", ".join(str(item.get("error_code", "unknown")) for item in failures)
            raise CdeWorkerError(f"CDE query could not complete: {codes}")
        return results
