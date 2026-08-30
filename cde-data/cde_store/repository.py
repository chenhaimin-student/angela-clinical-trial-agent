from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Iterable

from .contracts import (
    DATE_COLUMN_BY_FILTER,
    SORT_COLUMN_BY_FIELD,
    RecruitmentStatus,
    TrialQuery,
    reject_direct_identifiers,
)


VERSION_FIELDS = (
    "overall_status_raw",
    "recruitment_status_raw",
    "recruitment_status_normalized",
    "public_title",
    "professional_title",
    "indication_raw",
    "drug_name_raw",
    "sponsor_name_raw",
    "trial_phase_raw",
    "trial_scope_raw",
    "first_publication_date",
    "source_update_date",
    "inclusion_criteria_raw",
    "exclusion_criteria_raw",
)

TRIAL_COPY_FIELDS = (
    "detail_id",
    "detail_url",
    "public_title",
    "professional_title",
    "indication_raw",
    "drug_name_raw",
    "sponsor_name_raw",
    "trial_phase_raw",
    "trial_phase_normalized",
    "trial_scope_raw",
    "overall_status_raw",
    "recruitment_status_raw",
    "recruitment_status_normalized",
    "first_publication_date",
    "source_update_date",
)

PUBLIC_RESULT_COLUMNS = (
    "registration_no",
    "detail_url",
    "public_title",
    "professional_title",
    "indication_raw",
    "drug_name_raw",
    "trial_phase_raw",
    "trial_scope_raw",
    "overall_status_raw",
    "recruitment_status_raw",
    "recruitment_status_normalized",
    "first_publication_date",
    "source_update_date",
    "last_verified_at",
    "status_changed_at",
)

RESTRICTED_RESULT_COLUMNS = ("sponsor_name_raw",)

SEARCHABLE_COLUMNS = (
    "public_title",
    "professional_title",
    "indication_raw",
    "drug_name_raw",
    "sponsor_name_raw",
    "inclusion_criteria_raw",
    "exclusion_criteria_raw",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_hash(
    snapshot: dict[str, Any],
    *,
    criteria: list[dict[str, Any]] | None = None,
    sites: list[dict[str, Any]] | None = None,
    parties: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
) -> str:
    canonical = {
        "fields": {field: snapshot.get(field) for field in VERSION_FIELDS},
        "criteria": criteria or [],
        "sites": sites or [],
        "parties": parties or [],
        "contacts": contacts or [],
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_recruitment_status(raw: str | None) -> RecruitmentStatus:
    text = (raw or "").replace(" ", "")
    if "尚未招募" in text:
        return RecruitmentStatus.NOT_YET_RECRUITING
    if "招募完成" in text:
        return RecruitmentStatus.RECRUITMENT_COMPLETE
    if "招募中" in text:
        return RecruitmentStatus.RECRUITING
    if "暂停" in text:
        return RecruitmentStatus.SUSPENDED
    if "终止" in text:
        return RecruitmentStatus.TERMINATED
    if "撤销" in text:
        return RecruitmentStatus.WITHDRAWN
    if "已完成" in text:
        return RecruitmentStatus.COMPLETED
    return RecruitmentStatus.UNKNOWN


class TrialRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def upsert_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        raw_document_id: int | None,
        observed_at: str | None = None,
        parser_version: str = "cde-data-v1",
        parse_status: str = "success",
        criteria: Iterable[dict[str, Any]] = (),
        sites: Iterable[dict[str, Any]] = (),
        parties: Iterable[dict[str, Any]] = (),
        contacts: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        reject_direct_identifiers(snapshot)
        criteria = list(criteria)
        sites = list(sites)
        parties = list(parties)
        contacts = list(contacts)
        registration_no = str(snapshot.get("registration_no") or "").strip().upper()
        detail_url = str(snapshot.get("detail_url") or "").strip()
        if not registration_no.startswith("CTR"):
            raise ValueError("registration_no must be a CTR number")
        if not detail_url:
            raise ValueError("detail_url is required")

        normalized = dict(snapshot)
        normalized["registration_no"] = registration_no
        normalized["detail_url"] = detail_url
        normalized["recruitment_status_normalized"] = str(
            snapshot.get("recruitment_status_normalized")
            or normalize_recruitment_status(snapshot.get("recruitment_status_raw"))
        )
        observed = observed_at or utc_now()
        digest = canonical_hash(
            normalized,
            criteria=criteria,
            sites=sites,
            parties=parties,
            contacts=contacts,
        )

        with self.connection:
            trial = self.connection.execute(
                "SELECT * FROM trials WHERE registration_no = ?", (registration_no,)
            ).fetchone()
            if trial is not None and trial["content_hash"] == digest:
                new_detail_id = normalized.get("detail_id") or trial["detail_id"]
                new_detail_url = normalized.get("detail_url") or trial["detail_url"]
                technical_change = (
                    new_detail_id != trial["detail_id"]
                    or new_detail_url != trial["detail_url"]
                )
                self.connection.execute(
                    """
                    UPDATE trials SET detail_id=?,detail_url=?,last_seen_at=?,
                        last_verified_at=?,updated_at=? WHERE id=?
                    """,
                    (
                        new_detail_id,
                        new_detail_url,
                        observed,
                        observed,
                        observed,
                        trial["id"],
                    ),
                )
                self._record_observation(
                    trial_id=trial["id"],
                    version_id=trial["current_version_id"],
                    observed_at=observed,
                    observation_type="unchanged",
                    content_hash=digest,
                    raw_document_id=raw_document_id,
                    detail_id=new_detail_id,
                    detail_url=new_detail_url,
                )
                return {
                    "trial_id": trial["id"],
                    "version_id": trial["current_version_id"],
                    "previous_version_id": trial["current_version_id"],
                    "change": "unchanged",
                    "technical_change": technical_change,
                }

            # The current page can return to a snapshot seen in the past. Reuse
            # that immutable version and record a new observation instead of
            # violating UNIQUE(trial_id, content_hash).
            restored_version = None
            if trial is not None:
                restored_version = self.connection.execute(
                    "SELECT * FROM trial_versions WHERE trial_id=? AND content_hash=?",
                    (trial["id"], digest),
                ).fetchone()
            if restored_version is not None:
                previous_version_id = trial["current_version_id"]
                previous_version = self.connection.execute(
                    "SELECT * FROM trial_versions WHERE id=?", (previous_version_id,)
                ).fetchone()
                self._record_changes(
                    trial,
                    previous_version,
                    normalized,
                    previous_version_id,
                    restored_version["id"],
                    observed,
                )
                values = [normalized.get(field) for field in TRIAL_COPY_FIELDS]
                assignments = ", ".join(f"{field} = ?" for field in TRIAL_COPY_FIELDS)
                old_status = trial["recruitment_status_normalized"]
                status_changed_at = (
                    observed
                    if old_status != normalized["recruitment_status_normalized"]
                    else trial["status_changed_at"]
                )
                self.connection.execute(
                    f"""
                    UPDATE trials SET {assignments}, last_seen_at=?,last_verified_at=?,
                        status_changed_at=?,current_version_id=?,content_hash=?,
                        parser_version=?,updated_at=? WHERE id=?
                    """,
                    (
                        *values, observed, observed, status_changed_at,
                        restored_version["id"], digest, parser_version, observed, trial["id"],
                    ),
                )
                self._record_observation(
                    trial_id=trial["id"],
                    version_id=restored_version["id"],
                    observed_at=observed,
                    observation_type="restored",
                    content_hash=digest,
                    raw_document_id=raw_document_id,
                    detail_id=normalized.get("detail_id"),
                    detail_url=normalized.get("detail_url"),
                )
                self._refresh_fts(trial["id"], normalized)
                return {
                    "trial_id": trial["id"],
                    "version_id": restored_version["id"],
                    "previous_version_id": previous_version_id,
                    "change": "updated",
                    "technical_change": False,
                    "restored_version": True,
                }

            if trial is None:
                values = [normalized.get(field) for field in TRIAL_COPY_FIELDS]
                placeholders = ", ".join("?" for _ in values)
                columns = ", ".join(TRIAL_COPY_FIELDS)
                cursor = self.connection.execute(
                    f"""
                    INSERT INTO trials(
                        registration_no, {columns}, first_seen_at, last_seen_at,
                        last_verified_at, content_hash, parser_version, created_at, updated_at
                    ) VALUES (?, {placeholders}, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        registration_no,
                        *values,
                        observed,
                        observed,
                        observed,
                        digest,
                        parser_version,
                        observed,
                        observed,
                    ),
                )
                trial_id = cursor.lastrowid
                version_no = 1
                previous_version_id = None
                change = "created"
            else:
                trial_id = trial["id"]
                version_no = self.connection.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM trial_versions WHERE trial_id = ?",
                    (trial_id,),
                ).fetchone()[0]
                previous_version_id = trial["current_version_id"]
                change = "updated"

            structured_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
            version_values = [normalized.get(field) for field in VERSION_FIELDS]
            version_cursor = self.connection.execute(
                f"""
                INSERT INTO trial_versions(
                    trial_id, version_no, observed_at, content_hash,
                    {', '.join(VERSION_FIELDS)}, structured_json, raw_document_id,
                    parser_version, parse_status, created_at
                ) VALUES (?, ?, ?, ?, {', '.join('?' for _ in VERSION_FIELDS)}, ?, ?, ?, ?, ?)
                """,
                (
                    trial_id,
                    version_no,
                    observed,
                    digest,
                    *version_values,
                    structured_json,
                    raw_document_id,
                    parser_version,
                    parse_status,
                    observed,
                ),
            )
            version_id = version_cursor.lastrowid

            self._insert_criteria(version_id, criteria)
            self._insert_sites(version_id, sites)
            self._insert_parties(version_id, parties)
            self._insert_contacts(version_id, contacts)

            if trial is not None:
                previous_version = self.connection.execute(
                    "SELECT * FROM trial_versions WHERE id = ?", (previous_version_id,)
                ).fetchone()
                self._record_changes(
                    trial,
                    previous_version,
                    normalized,
                    previous_version_id,
                    version_id,
                    observed,
                )
                values = [normalized.get(field) for field in TRIAL_COPY_FIELDS]
                assignments = ", ".join(f"{field} = ?" for field in TRIAL_COPY_FIELDS)
                old_status = trial["recruitment_status_normalized"]
                status_changed_at = observed if old_status != normalized["recruitment_status_normalized"] else trial["status_changed_at"]
                self.connection.execute(
                    f"""
                    UPDATE trials SET {assignments}, last_seen_at = ?, last_verified_at = ?,
                        status_changed_at = ?, current_version_id = ?, content_hash = ?,
                        parser_version = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        *values,
                        observed,
                        observed,
                        status_changed_at,
                        version_id,
                        digest,
                        parser_version,
                        observed,
                        trial_id,
                    ),
                )
            else:
                self.connection.execute(
                    "UPDATE trials SET current_version_id = ? WHERE id = ?",
                    (version_id, trial_id),
                )

            self._record_observation(
                trial_id=trial_id,
                version_id=version_id,
                observed_at=observed,
                observation_type="created" if change == "created" else "updated",
                content_hash=digest,
                raw_document_id=raw_document_id,
                detail_id=normalized.get("detail_id"),
                detail_url=normalized.get("detail_url"),
            )

            self._refresh_fts(trial_id, normalized)
            return {
                "trial_id": trial_id,
                "version_id": version_id,
                "previous_version_id": previous_version_id,
                "change": change,
                "technical_change": False,
            }

    def _record_observation(
        self,
        *,
        trial_id: int,
        version_id: int,
        observed_at: str,
        observation_type: str,
        content_hash: str,
        raw_document_id: int | None,
        detail_id: str | None,
        detail_url: str | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO trial_observations(
                trial_id,trial_version_id,observed_at,observation_type,
                content_hash,raw_document_id,detail_id,detail_url,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                trial_id, version_id, observed_at, observation_type,
                content_hash, raw_document_id, detail_id, detail_url, utc_now(),
            ),
        )

    def _insert_criteria(self, version_id: int, criteria: Iterable[dict[str, Any]]) -> None:
        for index, item in enumerate(criteria, start=1):
            self.connection.execute(
                """
                INSERT INTO trial_criteria(
                    trial_version_id, criterion_type, sequence_no, raw_text,
                    plain_text, normalized_rule_json, extraction_method,
                    confidence, needs_human_review, source_anchor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    item["criterion_type"],
                    item.get("sequence_no", index),
                    item["raw_text"],
                    item.get("plain_text"),
                    json.dumps(item.get("normalized_rule"), ensure_ascii=False) if item.get("normalized_rule") else None,
                    item.get("extraction_method", "rule"),
                    item.get("confidence"),
                    bool(item.get("needs_human_review", False)),
                    item.get("source_anchor"),
                ),
            )

    def _insert_sites(self, version_id: int, sites: Iterable[dict[str, Any]]) -> None:
        for index, item in enumerate(sites, start=1):
            self.connection.execute(
                """
                INSERT INTO trial_sites(
                    trial_version_id, sequence_no, site_name_raw, province, city,
                    principal_investigator, site_status_raw, contact_raw, source_anchor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    item.get("sequence_no", index),
                    item["site_name_raw"],
                    item.get("province"),
                    item.get("city"),
                    item.get("principal_investigator"),
                    item.get("site_status_raw"),
                    item.get("contact_raw"),
                    item.get("source_anchor"),
                ),
            )

    def _insert_parties(self, version_id: int, parties: Iterable[dict[str, Any]]) -> None:
        for index, item in enumerate(parties, start=1):
            self.connection.execute(
                """
                INSERT INTO trial_parties(trial_version_id, role, name_raw, sequence_no, source_anchor)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    item["role"],
                    item["name_raw"],
                    item.get("sequence_no", index),
                    item.get("source_anchor"),
                ),
            )

    def _insert_contacts(self, version_id: int, contacts: Iterable[dict[str, Any]]) -> None:
        for index, item in enumerate(contacts, start=1):
            self.connection.execute(
                """
                INSERT INTO trial_contacts(
                    trial_version_id, contact_type, sequence_no, name_raw,
                    organization_raw, phone_raw, email_raw, address_raw,
                    postal_code_raw, source_anchor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    item.get("contact_type", "other"),
                    item.get("sequence_no", index),
                    item.get("name_raw"),
                    item.get("organization_raw"),
                    item.get("phone_raw"),
                    item.get("email_raw"),
                    item.get("address_raw"),
                    item.get("postal_code_raw"),
                    item.get("source_anchor"),
                ),
            )

    def _record_changes(
        self,
        trial: sqlite3.Row,
        previous_version: sqlite3.Row,
        current: dict[str, Any],
        from_version_id: int | None,
        to_version_id: int,
        observed: str,
    ) -> None:
        for field in VERSION_FIELDS:
            old = previous_version[field]
            new = current.get(field)
            if old == new:
                continue
            if field in {"recruitment_status_raw", "recruitment_status_normalized", "overall_status_raw"}:
                change_type, importance = "status", "high"
            elif field in {"inclusion_criteria_raw", "exclusion_criteria_raw"}:
                change_type, importance = "criteria", "high"
            elif field in {"public_title", "professional_title"}:
                change_type, importance = "title", "medium"
            elif field in {"first_publication_date", "source_update_date"}:
                change_type, importance = "date", "medium"
            else:
                change_type, importance = "other", "low"
            self.connection.execute(
                """
                INSERT INTO trial_changes(
                    trial_id, from_version_id, to_version_id, changed_at,
                    change_type, field_name, old_value, new_value, importance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trial["id"],
                    from_version_id,
                    to_version_id,
                    observed,
                    change_type,
                    field,
                    old,
                    new,
                    importance,
                ),
            )

    def _refresh_fts(self, trial_id: int, snapshot: dict[str, Any]) -> None:
        self.connection.execute("DELETE FROM trial_search_fts WHERE rowid = ?", (trial_id,))
        self.connection.execute(
            """
            INSERT INTO trial_search_fts(
                rowid, registration_no, public_title, professional_title,
                indication_raw, drug_name_raw, sponsor_name_raw,
                inclusion_criteria_raw, exclusion_criteria_raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trial_id,
                snapshot.get("registration_no"),
                snapshot.get("public_title"),
                snapshot.get("professional_title"),
                snapshot.get("indication_raw"),
                snapshot.get("drug_name_raw"),
                snapshot.get("sponsor_name_raw"),
                snapshot.get("inclusion_criteria_raw"),
                snapshot.get("exclusion_criteria_raw"),
            ),
        )

    def search(self, query: TrialQuery) -> list[dict[str, Any]]:
        columns = list(PUBLIC_RESULT_COLUMNS)
        if query.include_restricted_fields:
            columns.extend(RESTRICTED_RESULT_COLUMNS)
        sql = [f"SELECT DISTINCT {', '.join('t.' + column for column in columns)} FROM trials t"]
        parameters: list[Any] = []
        where = ["t.is_active = 1"]

        if query.text:
            search_text = query.text.strip()
            if search_text:
                # FTS5 trigram gives fast, literal substring matching for terms
                # of three or more characters. Short CJK disease names such as
                # “肺癌” cannot form a trigram, so use parameterized LIKE.
                if len(search_text) >= 3:
                    sql.append("JOIN trial_search_fts f ON f.rowid = t.id")
                    where.append("trial_search_fts MATCH ?")
                    parameters.append('"' + search_text.replace('"', '""') + '"')
                else:
                    escaped = search_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    where.append(
                        "(" + " OR ".join(
                            f"COALESCE(f.{column}, '') LIKE ? ESCAPE '\\'"
                            for column in SEARCHABLE_COLUMNS
                        ) + ")"
                    )
                    sql.append("JOIN trial_search_fts f ON f.rowid = t.id")
                    parameters.extend(f"%{escaped}%" for _ in SEARCHABLE_COLUMNS)

        if not query.include_non_recruiting:
            placeholders = ", ".join("?" for _ in query.recruitment_statuses)
            where.append(f"t.recruitment_status_normalized IN ({placeholders})")
            parameters.extend(str(status) for status in query.recruitment_statuses)

        date_column = DATE_COLUMN_BY_FILTER[query.date_filter_type]
        if query.date_from:
            where.append(f"t.{date_column} >= ?")
            parameters.append(query.date_from.isoformat())
        if query.date_to:
            where.append(f"t.{date_column} <= ?")
            parameters.append(query.date_to.isoformat())

        sql.append("WHERE " + " AND ".join(where))
        status_priority = (
            "CASE t.recruitment_status_normalized "
            "WHEN 'recruiting' THEN 0 WHEN 'not_yet_recruiting' THEN 1 ELSE 2 END"
        )
        sort_column = SORT_COLUMN_BY_FIELD[query.sort_by]
        sql.append(f"ORDER BY {status_priority}, t.{sort_column} {query.sort_order.upper()}, t.registration_no ASC")
        sql.append("LIMIT ?")
        parameters.append(query.limit)
        return [dict(row) for row in self.connection.execute(" ".join(sql), parameters)]

    def get_trial(
        self,
        registration_no: str,
        *,
        include_restricted_fields: bool = False,
    ) -> dict[str, Any] | None:
        """Return one current trial, keeping contacts/parties opt-in."""
        columns = list(PUBLIC_RESULT_COLUMNS)
        if include_restricted_fields:
            columns.extend(RESTRICTED_RESULT_COLUMNS)
        row = self.connection.execute(
            f"SELECT {', '.join(columns)} FROM trials WHERE registration_no = ? AND is_active = 1",
            (registration_no.strip().upper(),),
        ).fetchone()
        if row is None:
            return None

        result = dict(row)
        if not include_restricted_fields:
            return result

        current_version_id = self.connection.execute(
            "SELECT current_version_id FROM trials WHERE registration_no = ?",
            (registration_no.strip().upper(),),
        ).fetchone()[0]
        result["sites"] = [
            dict(item)
            for item in self.connection.execute(
                """
                SELECT sequence_no, site_name_raw, province, city,
                       principal_investigator, site_status_raw, contact_raw
                FROM trial_sites WHERE trial_version_id = ? ORDER BY sequence_no
                """,
                (current_version_id,),
            )
        ]
        result["parties"] = [
            dict(item)
            for item in self.connection.execute(
                """
                SELECT role, name_raw, sequence_no FROM trial_parties
                WHERE trial_version_id = ? ORDER BY role, sequence_no
                """,
                (current_version_id,),
            )
        ]
        result["contacts"] = [
            dict(item)
            for item in self.connection.execute(
                """
                SELECT contact_type, sequence_no, name_raw, organization_raw,
                       phone_raw, email_raw, address_raw, postal_code_raw
                FROM trial_contacts WHERE trial_version_id = ?
                ORDER BY contact_type, sequence_no
                """,
                (current_version_id,),
            )
        ]
        return result
