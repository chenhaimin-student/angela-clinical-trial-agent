from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class ParsedCdePage:
    snapshot: dict[str, Any]
    criteria: list[dict[str, Any]]
    sites: list[dict[str, Any]]
    parties: list[dict[str, Any]]
    contacts: list[dict[str, Any]]


def _text(node: Any) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _paired_fields(soup: BeautifulSoup) -> dict[str, str]:
    fields: dict[str, str] = {}
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        for index, cell in enumerate(cells[:-1]):
            if cell.name != "th":
                continue
            label = _text(cell).rstrip(":：")
            value = _text(cells[index + 1])
            if label and value and label not in fields:
                fields[label] = value
    return fields


def _criteria(soup: BeautifulSoup) -> tuple[list[dict[str, Any]], str | None, str | None]:
    items: list[dict[str, Any]] = []
    raw_by_type: dict[str, list[str]] = {"inclusion": [], "exclusion": []}
    for kind, label in (("inclusion", "入选标准"), ("exclusion", "排除标准")):
        header = next((node for node in soup.find_all("th") if _text(node) == label), None)
        if header is None:
            continue
        row = header.find_parent("tr")
        if row is None:
            continue
        direct_cells = row.find_all(["th", "td"], recursive=False)
        value_cell = direct_cells[1] if len(direct_cells) > 1 else None
        nested_rows = value_cell.find_all("tr") if value_cell is not None else []
        if nested_rows:
            for nested in nested_rows:
                values = [_text(cell) for cell in nested.find_all("td", recursive=False)]
                candidate = values[-1] if len(values) > 1 else ""
                if not candidate:
                    continue
                sequence = len(raw_by_type[kind]) + 1
                raw_by_type[kind].append(candidate)
                items.append({
                    "criterion_type": kind,
                    "sequence_no": sequence,
                    "raw_text": candidate,
                    "plain_text": candidate,
                    "extraction_method": "rule",
                    "confidence": 1.0,
                    "source_anchor": label,
                })
            continue
        sequence = 0
        current = row
        while current is not None:
            cells = current.find_all(["th", "td"], recursive=False)
            if current is not row and any(cell.name == "th" for cell in cells):
                break
            values = [_text(cell) for cell in cells]
            candidate = "" if current is row else (values[-1] if len(values) > 1 else "")
            if candidate:
                sequence += 1
                raw_by_type[kind].append(candidate)
                items.append({
                    "criterion_type": kind,
                    "sequence_no": sequence,
                    "raw_text": candidate,
                    "plain_text": candidate,
                    "extraction_method": "rule",
                    "confidence": 1.0,
                    "source_anchor": label,
                })
            current = current.find_next_sibling("tr")
    return (
        items,
        "\n".join(raw_by_type["inclusion"]) or None,
        "\n".join(raw_by_type["exclusion"]) or None,
    )


def _sites(soup: BeautifulSoup) -> list[dict[str, Any]]:
    sites: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [_text(cell) for cell in rows[0].find_all(["th", "td"], recursive=False)]
        if "机构名称" not in headers or "主要研究者" not in headers:
            continue
        for sequence, row in enumerate(rows[1:], start=1):
            values = [_text(cell) for cell in row.find_all("td", recursive=False)]
            if len(values) < len(headers):
                continue
            mapped = dict(zip(headers, values))
            if not mapped.get("机构名称"):
                continue
            sites.append({
                "sequence_no": sequence,
                "site_name_raw": mapped["机构名称"],
                "province": mapped.get("省（州）"),
                "city": mapped.get("城市"),
                "principal_investigator": mapped.get("主要研究者"),
                "source_anchor": "各参加机构",
            })
        if sites:
            break
    return sites


def _contacts(fields: dict[str, str]) -> list[dict[str, Any]]:
    contacts = []
    applicant_name = fields.get("申请人联系人")
    if applicant_name:
        contacts.append({
            "contact_type": "applicant",
            "name_raw": applicant_name,
            "organization_raw": fields.get("申请人名称"),
            "source_anchor": "申请人联系人",
        })
    study = {
        "contact_type": "study",
        "name_raw": fields.get("联系人姓名"),
        "organization_raw": fields.get("单位名称"),
        "phone_raw": fields.get("联系人手机号") or fields.get("联系人座机"),
        "email_raw": fields.get("联系人Email"),
        "address_raw": fields.get("联系人邮政地址"),
        "postal_code_raw": fields.get("联系人邮编"),
        "source_anchor": "联系人姓名",
    }
    if any(study.get(key) for key in ("name_raw", "phone_raw", "email_raw")):
        contacts.append(study)
    return contacts


def parse_cde_detail(content: str, detail_url: str, recruitment_status: str | None) -> ParsedCdePage:
    soup = BeautifulSoup(content, "html.parser")
    fields = _paired_fields(soup)
    registration_no = fields.get("登记号")
    if not registration_no or not registration_no.startswith("CTR"):
        raise ValueError("CDE detail page is missing its registration number")
    criteria, inclusion_raw, exclusion_raw = _criteria(soup)
    applicant = fields.get("申请人名称")
    parties = ([{
        "role": "applicant",
        "name_raw": applicant,
        "source_anchor": "申请人名称",
    }] if applicant else [])
    snapshot = {
        "registration_no": registration_no,
        "detail_url": detail_url,
        "public_title": fields.get("试验通俗题目"),
        "professional_title": fields.get("试验专业题目"),
        "indication_raw": fields.get("适应症"),
        "drug_name_raw": fields.get("药物名称"),
        "sponsor_name_raw": applicant,
        "trial_phase_raw": fields.get("试验分期"),
        "trial_scope_raw": fields.get("试验范围"),
        "overall_status_raw": fields.get("试验状态"),
        "recruitment_status_raw": recruitment_status,
        "first_publication_date": fields.get("首次公示信息日期"),
        # CDE's protocol “版本日期” is not necessarily the public page's
        # update timestamp, so it must not be used as source_update_date.
        "source_update_date": None,
        "inclusion_criteria_raw": inclusion_raw,
        "exclusion_criteria_raw": exclusion_raw,
    }
    return ParsedCdePage(snapshot, criteria, _sites(soup), parties, _contacts(fields))
