from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from .models import CdeRssRecord, CdeTrial


CDE_DETAIL_URL = "https://www.chinadrugtrials.org.cn/clinicaltrials.searchlistdetail.dhtml?id={detail_id}"
DETAIL_ID_PATTERN = re.compile(r"(?:[a-fA-F0-9]{32}(?:_p)?|[0-9]{1,20}_p)")


def parse_rss(content: str) -> list[CdeRssRecord]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"CDE RSS is not valid XML: {exc}") from exc

    records: list[CdeRssRecord] = []
    for item in root.findall(".//item"):
        registration_no = (item.findtext("guid") or "").strip()
        detail_url = (item.findtext("link") or "").strip()
        detail_id = parse_qs(urlparse(detail_url).query).get("id", [""])[0]
        if not registration_no.startswith("CTR") or not detail_url or not detail_id:
            continue
        records.append(
            CdeRssRecord(
                registration_no=registration_no,
                title=html.unescape(item.findtext("title") or "").strip(),
                description=html.unescape(item.findtext("description") or "").strip(),
                detail_url=detail_url,
                detail_id=detail_id,
            )
        )
    return records


def parse_result_page(content: str) -> list[CdeRssRecord]:
    """Parse CDE's rendered search table without relying on its RSS endpoint."""
    soup = BeautifulSoup(content, "html.parser")
    table = soup.select_one("table.searchTable")
    if table is None:
        raise ValueError("CDE search page is missing its result table")
    records: list[CdeRssRecord] = []
    for row in table.select("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 6:
            continue
        values = [cell.get_text(" ", strip=True).replace("\xa0", " ") for cell in cells]
        link = cells[1].find("a")
        detail_id = (link.get("id") if link else "") or ""
        registration_no = values[1].replace(" ", "")
        if not registration_no.startswith("CTR") or not DETAIL_ID_PATTERN.fullmatch(detail_id):
            continue
        records.append(
            CdeRssRecord(
                registration_no=registration_no,
                title=values[5],
                description=" | ".join(values[2:5]),
                detail_url=CDE_DETAIL_URL.format(detail_id=detail_id),
                detail_id=detail_id,
            )
        )
    return records


def parse_trial_html(content: str, detail_url: str) -> CdeTrial:
    soup = BeautifulSoup(content, "html.parser")
    rows: list[tuple[str, str]] = []
    for row in soup.select("tr"):
        cells = [
            re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
            for cell in row.find_all(["th", "td"], recursive=False)
        ]
        for index in range(0, len(cells) - 1, 2):
            if cells[index] and cells[index + 1]:
                rows.append((cells[index], cells[index + 1]))
    if not rows:
        raise ValueError("CDE detail page contains no readable table data")

    fields = _field_map(rows)
    registration_no = _first(fields, "登记号")
    if not registration_no or not registration_no.startswith("CTR"):
        raise ValueError("CDE detail page is missing its registration number")
    text = "\n".join(f"{label}: {value}" for label, value in rows)
    return CdeTrial(
        registration_no=registration_no,
        status=_first(fields, "试验状态"),
        recruitment_status=None,
        first_publication_date=_first(fields, "首次公示信息日期"),
        sponsor_name=_first(fields, "申请人名称"),
        drug_name=_first(fields, "药物名称"),
        indication=_first(fields, "适应症"),
        public_title=_first(fields, "试验通俗题目"),
        professional_title=_first(fields, "试验专业题目"),
        trial_phase=_first(fields, "试验分期"),
        trial_scope=_first(fields, "试验范围"),
        inclusion_criteria=_criteria(text, "入选标准", "排除标准"),
        exclusion_criteria=_criteria(text, "排除标准", "终止标准"),
        detail_url=detail_url,
        raw_text=text,
    )


def _field_map(rows: list[tuple[str, str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for label, value in rows:
        result.setdefault(label, []).append(value)
    return result


def _first(fields: dict[str, list[str]], label: str) -> str | None:
    values = fields.get(label, [])
    return values[0] if values else None


def _criteria(text: str, start: str, end: str) -> str | None:
    match = re.search(rf"{re.escape(start)}[:：]?\s*(.*?)(?={re.escape(end)}[:：]?|$)", text, re.DOTALL)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None
