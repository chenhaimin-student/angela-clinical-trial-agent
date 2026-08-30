from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class StringEnum(str, Enum):
    """String-valued enum compatible with the Mac system Python 3.9."""

    def __str__(self) -> str:
        return self.value


class RecruitmentStatus(StringEnum):
    NOT_YET_RECRUITING = "not_yet_recruiting"
    RECRUITING = "recruiting"
    RECRUITMENT_COMPLETE = "recruitment_complete"
    COMPLETED = "completed"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"
    WITHDRAWN = "withdrawn"
    UNKNOWN = "unknown"


PATIENT_DEFAULT_STATUSES = (
    RecruitmentStatus.RECRUITING,
    RecruitmentStatus.NOT_YET_RECRUITING,
)


class DateFilterType(StringEnum):
    FIRST_PUBLICATION = "first_publication"
    SOURCE_UPDATE = "source_update"
    LAST_VERIFIED = "last_verified"
    STATUS_CHANGED = "status_changed"


class SortField(StringEnum):
    FIRST_PUBLICATION_DATE = "first_publication_date"
    SOURCE_UPDATE_DATE = "source_update_date"
    LAST_VERIFIED_AT = "last_verified_at"
    STATUS_CHANGED_AT = "status_changed_at"


DATE_COLUMN_BY_FILTER = {
    DateFilterType.FIRST_PUBLICATION: "first_publication_date",
    DateFilterType.SOURCE_UPDATE: "source_update_date",
    DateFilterType.LAST_VERIFIED: "last_verified_at",
    DateFilterType.STATUS_CHANGED: "status_changed_at",
}

SORT_COLUMN_BY_FIELD = {
    SortField.FIRST_PUBLICATION_DATE: "first_publication_date",
    SortField.SOURCE_UPDATE_DATE: "source_update_date",
    SortField.LAST_VERIFIED_AT: "last_verified_at",
    SortField.STATUS_CHANGED_AT: "status_changed_at",
}


@dataclass(frozen=True)
class TrialQuery:
    """Validated query contract produced from a user's natural-language request.

    A bare phrase such as "最近三个月的试验" must be converted by the caller to
    FIRST_PUBLICATION plus explicit date_from/date_to values. SQL owns the date
    comparison and ordering; the language model must not compare dates itself.
    """

    text: str | None = None
    date_filter_type: DateFilterType = DateFilterType.FIRST_PUBLICATION
    date_from: date | None = None
    date_to: date | None = None
    recruitment_statuses: tuple[RecruitmentStatus, ...] = PATIENT_DEFAULT_STATUSES
    sort_by: SortField = SortField.FIRST_PUBLICATION_DATE
    sort_order: str = "desc"
    limit: int = 10
    include_non_recruiting: bool = False
    include_restricted_fields: bool = False

    def __post_init__(self) -> None:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        if self.sort_order not in {"asc", "desc"}:
            raise ValueError("sort_order must be asc or desc")
        if not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not self.include_non_recruiting and not self.recruitment_statuses:
            raise ValueError("patient queries require at least one recruitment status")


DIRECT_IDENTIFIER_KEYS = frozenset(
    {
        "name",
        "patient_name",
        "phone",
        "telephone",
        "id_number",
        "medical_record_number",
        "sample_id",
        "姓名",
        "手机号",
        "电话",
        "身份证",
        "病历号",
        "样本编号",
    }
)


def reject_direct_identifiers(value: object) -> None:
    if isinstance(value, dict):
        forbidden = DIRECT_IDENTIFIER_KEYS.intersection(value)
        if forbidden:
            raise ValueError("direct patient identifiers are not permitted")
        for child in value.values():
            reject_direct_identifiers(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            reject_direct_identifiers(child)
