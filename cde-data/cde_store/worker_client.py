from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class WorkerClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerResource:
    kind: str
    query_term: str | None = None
    page: int | None = None
    detail_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "detail":
            return {"kind": "detail", "detail_id": self.detail_id}
        if self.kind == "catalog_page":
            return {"kind": "catalog_page", "page": self.page}
        return {"kind": self.kind, "query_term": self.query_term, "page": self.page}


class BrowserWorkerClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8766", timeout_seconds: int = 180):
        if not base_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise ValueError("batch worker must be bound to the local loopback interface")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def fetch(
        self,
        job_id: str,
        request_id: str,
        resources: list[WorkerResource],
    ) -> list[dict[str, Any]]:
        response = self._request(
            "POST",
            "/v1/cde/fetch",
            {
                "job_id": job_id,
                "request_id": request_id,
                "resources": [resource.to_dict() for resource in resources],
            },
        )
        results = response.get("results")
        if not isinstance(results, list) or len(results) != len(resources):
            raise WorkerClientError("batch worker returned an incomplete response")
        return results

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise WorkerClientError(f"batch worker returned HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise WorkerClientError("batch worker is unavailable or timed out") from error
        if not isinstance(result, dict):
            raise WorkerClientError("batch worker returned invalid JSON")
        return result
