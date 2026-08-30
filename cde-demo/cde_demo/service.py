from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import uuid4

from .cde_client import CdeBrowserWorkerClient, WorkerResource
from .matching import preliminary_match
from .models import LookupResult, PatientProfile
from .parser import parse_result_page, parse_trial_html


class CdeLookupService:
    """Bounded public CDE lookup with an explicit coverage budget."""

    MAX_SEARCH_PAGES_PER_TERM = 3
    MAX_CANDIDATE_LIMIT = 40

    def __init__(self, client: CdeBrowserWorkerClient):
        self.client = client

    @staticmethod
    def _is_currently_recruiting(record) -> tuple[bool, str]:
        """Use the public result-page status to avoid needless detail fetches."""
        recruitment_status = record.description.split(" | ", 1)[0]
        return (
            "\u62db\u52df\u4e2d" in recruitment_status
            or "\u5c1a\u672a\u62db\u52df" in recruitment_status,
            recruitment_status,
        )

    def lookup(
        self,
        profile: PatientProfile,
        query_terms: tuple[str, ...],
        max_results: int = 3,
        published_after: date | None = None,
        search_pages: int = MAX_SEARCH_PAGES_PER_TERM,
        candidate_limit: int = MAX_CANDIDATE_LIMIT,
    ) -> LookupResult:
        if not query_terms:
            raise ValueError("At least one CDE query term is required")
        if not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10")
        if not 1 <= search_pages <= self.MAX_SEARCH_PAGES_PER_TERM:
            raise ValueError(f"search_pages must be between 1 and {self.MAX_SEARCH_PAGES_PER_TERM}")
        if not max_results <= candidate_limit <= self.MAX_CANDIDATE_LIMIT:
            raise ValueError(
                f"candidate_limit must be between max_results and {self.MAX_CANDIDATE_LIMIT}"
            )

        job_id = f"cde-demo-{uuid4().hex}"
        candidates = []
        warnings: list[str] = []
        seen = set()
        for index, term in enumerate(query_terms[:3], start=1):
            for page in range(1, search_pages + 1):
                results = self.client.fetch(
                    job_id,
                    f"{job_id}-search-{index}-page-{page}",
                    [WorkerResource(kind="result_page", query_term=term, page=page)],
                )
                if len(results) != 1:
                    raise ValueError("CDE Browser Worker returned an incomplete search response")
                for record in parse_result_page(results[0]["content"]):
                    is_recruiting, recruitment_status = self._is_currently_recruiting(record)
                    if not is_recruiting:
                        warnings.append(
                            f"Skipped {record.registration_no}: CDE search status is {recruitment_status}"
                        )
                        continue
                    if record.registration_no not in seen:
                        candidates.append(record)
                        seen.add(record.registration_no)
                    if len(candidates) >= candidate_limit:
                        break
                if len(candidates) >= candidate_limit:
                    break
            if len(candidates) >= candidate_limit:
                break

        detail_resources = [
            WorkerResource(kind="detail", detail_id=item.detail_id)
            for item in candidates[:candidate_limit]
        ]
        candidate_by_detail_id = {item.detail_id: item for item in candidates}
        matches = []
        for start in range(0, len(detail_resources), 5):
            batch = detail_resources[start : start + 5]
            results = self.client.fetch(job_id, f"{job_id}-detail-{start // 5 + 1}", batch)
            if len(results) != len(batch):
                raise ValueError("CDE Browser Worker returned an incomplete detail response")
            for resource, response in zip(batch, results):
                try:
                    trial = parse_trial_html(response["content"], response["source_url"])
                    candidate = candidate_by_detail_id[resource.detail_id or ""]
                    is_recruiting, recruitment_status = self._is_currently_recruiting(candidate)
                    if not is_recruiting:
                        warnings.append(
                            f"Skipped {trial.registration_no}: CDE search status is {recruitment_status}"
                        )
                        continue
                    if published_after is not None:
                        try:
                            publication_date = date.fromisoformat(trial.first_publication_date or "")
                        except ValueError:
                            warnings.append(
                                f"Skipped {trial.registration_no}: CDE publication date is missing or invalid"
                            )
                            continue
                        if publication_date < published_after:
                            warnings.append(
                                f"Skipped {trial.registration_no}: published before {published_after.isoformat()}"
                            )
                            continue
                    trial = replace(trial, recruitment_status=recruitment_status)
                    match = preliminary_match(profile, trial)
                    if match is not None:
                        matches.append(match)
                except ValueError as exc:
                    warnings.append(f"Skipped an unreadable CDE detail page: {exc}")
        matches.sort(key=lambda item: item.score, reverse=True)
        if not matches:
            warnings.append(
                "No preliminary matches were found in the limited CDE search. "
                "This does not mean no suitable trial exists."
            )
        return LookupResult(
            query_terms=query_terms,
            published_after=published_after.isoformat() if published_after else None,
            matches=matches[:max_results],
            warnings=warnings,
        )
