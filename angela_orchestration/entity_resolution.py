from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


_LEGAL_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "股份公司", "集团公司", "集团",
)
_INDUSTRY_SUFFIXES = (
    "生物医药", "医药科技", "生物科技", "制药", "医药", "生物", "科技",
)
_AMBIGUOUS_ALIASES = {
    "中国", "国际", "集团", "医药", "生物", "科技", "制药", "药业", "公司",
    "北京", "上海", "天津", "重庆", "广州", "深圳", "苏州", "杭州", "南京",
    "华东", "华南", "华中", "华北", "东北", "西南", "西北",
}
_BRACKETED_REGION = re.compile(r"[（(][^()（）]{1,12}[）)]")
_SPACES = re.compile(r"\s+")
_LATIN_LEGAL_SUFFIX = re.compile(
    r"(?:,?\s+|\b)(?:co\.?|company|corp\.?|corporation|inc\.?|incorporated|"
    r"ltd\.?|limited|llc|plc|ag|gmbh|sa|nv)\s*$",
    re.IGNORECASE,
)
_LATIN_ALIAS_STOPWORDS = {
    "company", "corporation", "incorporated", "limited", "holdings",
    "holding", "group", "international", "pharmaceutical", "pharmaceuticals",
    "pharma", "biotech", "biotechnology", "therapeutics", "technology",
}


@dataclass(frozen=True)
class SponsorResolution:
    raw_alias: str
    canonical_names: tuple[str, ...]


@dataclass(frozen=True)
class LocationResolution:
    raw_alias: str
    canonical_name: str
    kind: str


@dataclass(frozen=True)
class SiteResolution:
    raw_alias: str
    canonical_name: str


def _clean(value: str) -> str:
    return _SPACES.sub("", (value or "").strip())


def _normalize_location_text(value: str) -> str:
    """Normalize spacing while preserving token boundaries for Latin names."""
    return _SPACES.sub(" ", (value or "").strip()).casefold()


def company_aliases(name: str) -> tuple[str, ...]:
    """Generate conservative aliases using legal-name morphology, not a company map."""
    full = _clean(name)
    if not full:
        return ()
    queue = [full]
    aliases: set[str] = set()
    while queue:
        value = queue.pop()
        if value in aliases:
            continue
        aliases.add(value)
        without_region = _BRACKETED_REGION.sub("", value)
        if without_region and without_region != value:
            queue.append(without_region)
        for suffix in (*_LEGAL_SUFFIXES, *_INDUSTRY_SUFFIXES):
            if value.endswith(suffix) and len(value) > len(suffix):
                queue.append(value[: -len(suffix)])
    # For Latin legal names, also index the last meaningful brand token. This
    # is morphology-based rather than company-specific: ``Example Research and
    # Company`` and ``Example Pharma Ltd`` each yield the final meaningful
    # token, while generic industry/legal words are excluded.
    latin_name = (name or "").strip()
    if re.search(r"[A-Za-z]", latin_name):
        without_legal = latin_name
        while True:
            shortened = _LATIN_LEGAL_SUFFIX.sub("", without_legal).strip(" ,.-")
            if shortened == without_legal:
                break
            without_legal = shortened
        compact_legal = _clean(without_legal)
        if compact_legal:
            aliases.add(compact_legal)
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", without_legal)
        brand = next(
            (
                token for token in reversed(tokens)
                if len(token) >= 4 and token.casefold() not in _LATIN_ALIAS_STOPWORDS
            ),
            None,
        )
        if brand:
            aliases.add(brand)
    safe = {
        value for value in aliases
        if value not in _AMBIGUOUS_ALIASES
        and (len(value) >= 2 if re.search(r"[\u4e00-\u9fff]", value) else len(value) >= 3)
    }
    return tuple(sorted(safe, key=lambda value: (-len(value), value)))


def split_party_names(value: str) -> tuple[str, ...]:
    """Split CDE fields that store several legal entities in one string."""
    parts = re.split(r"[/／；;]+", value or "")
    cleaned = []
    for part in parts:
        name = re.sub(r"^\s*\d+\s*[）).、]\s*", "", part).strip()
        if name:
            cleaned.append(name)
    return tuple(dict.fromkeys(cleaned))


class SponsorEntityResolver:
    """Resolve sponsor mentions from the read-only CDE database at startup."""

    def __init__(self, database_path: str | Path | None):
        self._aliases: list[tuple[str, tuple[str, ...]]] = []
        path = Path(database_path) if database_path else None
        if path is None or not path.is_file():
            return
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute(
                "SELECT DISTINCT sponsor_name_raw FROM trials "
                "WHERE is_active=1 AND sponsor_name_raw IS NOT NULL AND TRIM(sponsor_name_raw)<>'' "
                "UNION SELECT DISTINCT p.name_raw FROM trial_parties p "
                "JOIN trials t ON t.current_version_id=p.trial_version_id "
                "WHERE t.is_active=1 AND p.role IN ('applicant','sponsor') "
                "AND p.name_raw IS NOT NULL AND TRIM(p.name_raw)<>''"
            ).fetchall()
        finally:
            connection.close()
        alias_map: dict[str, set[str]] = {}
        for (raw_name,) in rows:
            canonical = str(raw_name).strip()
            # CDE commonly joins the applicant, domestic agent and manufacturer
            # with slashes or semicolons. Index each legal entity separately so
            # a user can name one party without reproducing the entire source
            # field verbatim.
            for party_name in split_party_names(canonical) or (canonical,):
                for alias in company_aliases(party_name):
                    alias_map.setdefault(alias.casefold(), set()).add(canonical)
        self._aliases = sorted(
            (
                (alias, tuple(sorted(names)))
                for alias, names in alias_map.items()
            ),
            key=lambda item: (-len(item[0]), item[0]),
        )

    def resolve(self, text: str) -> tuple[SponsorResolution, ...]:
        normalized = _clean(text).casefold()
        matches: list[SponsorResolution] = []
        occupied: list[tuple[int, int]] = []
        for alias, names in self._aliases:
            if re.search(r"[a-z]", alias):
                found = re.search(
                    rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
                    normalized,
                    re.IGNORECASE,
                )
                start = found.start() if found else -1
            else:
                start = normalized.find(alias)
            if start < 0:
                continue
            span = (start, start + len(alias))
            if any(left <= span[0] and span[1] <= right for left, right in occupied):
                continue
            matches.append(SponsorResolution(alias, names))
            occupied.append(span)
        return tuple(matches)


class LocationEntityResolver:
    """Resolve public research-site cities/provinces from the current database."""

    def __init__(self, database_path: str | Path | None):
        self._cjk_aliases: list[tuple[str, str, str]] = []
        self._latin_aliases: dict[str, tuple[str, str]] = {}
        self._latin_pattern: re.Pattern[str] | None = None
        path = Path(database_path) if database_path else None
        if path is None or not path.is_file():
            return
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute(
                "SELECT DISTINCT s.city,'city' FROM trial_sites s "
                "JOIN trials t ON t.current_version_id=s.trial_version_id "
                "WHERE t.is_active=1 AND s.city IS NOT NULL AND TRIM(s.city)<>'' "
                "UNION SELECT DISTINCT s.province,'province' FROM trial_sites s "
                "JOIN trials t ON t.current_version_id=s.trial_version_id "
                "WHERE t.is_active=1 AND s.province IS NOT NULL AND TRIM(s.province)<>''"
            ).fetchall()
        finally:
            connection.close()
        aliases: dict[str, tuple[str, str]] = {}
        for raw_name, kind in rows:
            canonical = str(raw_name).strip()
            candidates = {canonical}
            if canonical.endswith(("市", "省")) and len(canonical) > 2:
                candidates.add(canonical[:-1])
            for alias in candidates:
                cleaned = _normalize_location_text(alias)
                minimum = 2 if re.search(r"[\u4e00-\u9fff]", cleaned) else 3
                if len(cleaned) >= minimum:
                    aliases.setdefault(cleaned, (canonical, str(kind)))
        for alias, value in aliases.items():
            if re.search(r"[A-Za-z0-9]", alias) and not re.search(
                r"[\u4e00-\u9fff]", alias
            ):
                self._latin_aliases[alias] = value
            else:
                self._cjk_aliases.append((alias, *value))
        self._cjk_aliases.sort(key=lambda item: (-len(item[0]), item[0]))
        if self._latin_aliases:
            alternatives = "|".join(
                re.escape(alias)
                for alias in sorted(self._latin_aliases, key=lambda value: (-len(value), value))
            )
            self._latin_pattern = re.compile(
                rf"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])",
                re.IGNORECASE,
            )

    def resolve(self, text: str) -> tuple[LocationResolution, ...]:
        normalized = _normalize_location_text(text)
        candidates: list[tuple[tuple[int, int], str, str, str]] = []
        if self._latin_pattern is not None:
            for match in self._latin_pattern.finditer(normalized):
                alias = match.group(0).casefold()
                canonical, kind = self._latin_aliases[alias]
                candidates.append((match.span(), alias, canonical, kind))
        for alias, canonical, kind in self._cjk_aliases:
            start = normalized.find(alias)
            if start >= 0:
                candidates.append(((start, start + len(alias)), alias, canonical, kind))
        candidates.sort(key=lambda item: (item[0][0], -(item[0][1] - item[0][0])))

        matches: list[LocationResolution] = []
        occupied: list[tuple[int, int]] = []
        for span, alias, canonical, kind in candidates:
            if any(left <= span[0] and span[1] <= right for left, right in occupied):
                continue
            matches.append(LocationResolution(alias, canonical, kind))
            occupied.append(span)
        return tuple(matches)


def site_aliases(name: str) -> tuple[str, ...]:
    """Build conservative aliases from public research-site names."""
    full = _clean(name)
    if not full:
        return ()
    aliases = {full, full.replace("医学院", "")}
    match = re.search(r"附属(.{2,20}?(?:医院|医学中心|研究中心))$", full)
    if match:
        aliases.add(match.group(1))
    return tuple(sorted(
        (value for value in aliases if len(value) >= 4),
        key=lambda value: (-len(value), value),
    ))


class SiteEntityResolver:
    """Resolve an explicitly named public research center from the database."""

    def __init__(self, database_path: str | Path | None):
        self._aliases: list[tuple[str, str]] = []
        path = Path(database_path) if database_path else None
        if path is None or not path.is_file():
            return
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute(
                "SELECT DISTINCT s.site_name_raw FROM trial_sites s "
                "JOIN trials t ON t.current_version_id=s.trial_version_id "
                "WHERE t.is_active=1 AND s.site_name_raw IS NOT NULL "
                "AND TRIM(s.site_name_raw)<>''"
            ).fetchall()
        finally:
            connection.close()
        aliases: dict[str, str] = {}
        for (raw_name,) in rows:
            canonical = str(raw_name).strip()
            for alias in site_aliases(canonical):
                aliases.setdefault(alias.casefold(), canonical)
        self._aliases = sorted(aliases.items(), key=lambda item: (-len(item[0]), item[0]))

    def resolve(self, text: str) -> tuple[SiteResolution, ...]:
        normalized = _clean(text).casefold()
        matches: list[SiteResolution] = []
        occupied: list[tuple[int, int]] = []
        for alias, canonical in self._aliases:
            start = normalized.find(alias)
            if start < 0:
                continue
            span = (start, start + len(alias))
            if any(left <= span[0] and span[1] <= right for left, right in occupied):
                continue
            matches.append(SiteResolution(alias, canonical))
            occupied.append(span)
        return tuple(matches)
