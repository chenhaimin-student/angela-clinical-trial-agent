from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a temporary CDE demo report")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    request = _load(Path(args.request))
    result = _load(Path(args.result))
    Path(args.output).write_text(_html(request, result), encoding="utf-8")
    return 0


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _value(value: Any) -> str:
    return html.escape("—" if value in (None, "") else str(value))


def _list(values: list[Any]) -> str:
    if not values:
        return "<li>无</li>"
    return "".join(f"<li>{_value(item)}</li>" for item in values)


def _html(request: dict[str, Any], result: dict[str, Any]) -> str:
    patient = request.get("patient", {})
    rows = "".join(
        f"<tr><th>{label}</th><td>{_value(value)}</td></tr>"
        for label, value in (
            ("性别", "男性" if patient.get("sex") == "male" else patient.get("sex")),
            ("年龄", f"{patient.get('age')} 岁" if patient.get("age") is not None else None),
            ("血型", patient.get("blood_type")),
            ("病症", "、".join(patient.get("condition_terms", []))),
            ("发布日期范围", f"{result.get('published_after')} 至今" if result.get("published_after") else "未限制"),
        )
    )
    cards = []
    for item in result.get("matches", []):
        recruitment_status = item.get("recruitment_status")
        status_class = "pending" if "尚未招募" in str(recruitment_status) else "open"
        cards.append(
            "<article class='card'>"
            f"<h2>{_value(item.get('registration_no'))}</h2>"
            f"<p class='status {status_class}'>{_value(recruitment_status)}</p>"
            f"<p><b>适应症：</b>{_value(item.get('indication'))}</p>"
            f"<p><b>试验阶段：</b>{_value(item.get('trial_phase'))}</p>"
            f"<p><b>匹配依据：</b>{_value('；'.join(item.get('matched_fields', [])))}</p>"
            f"<p><a href='{_value(item.get('detail_url'))}'>查看 CDE 原始登记页</a></p>"
            "<p class='note'>仅为初步信息匹配，是否招募及是否可入组需由研究医生和试验机构确认。</p>"
            "</article>"
        )
    content = "".join(cards) or "<section class='empty'><h2>没有找到符合当前条件的候选试验</h2><p>这不表示不存在合适试验；本次仅查询 CDE 搜索结果的有限候选，并限定首次公示日期在近三个月内。</p></section>"
    warnings = _list(result.get("warnings", []))
    return f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>CDE 临床试验模拟检索</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:920px;margin:32px auto;background:#f6f8fb;color:#172033;line-height:1.55}}header,.card,.empty,section{{background:#fff;border:1px solid #e3e8f0;border-radius:14px;padding:22px;margin-bottom:18px}}h1{{margin-top:0}}h2{{margin:0 0 8px}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #edf0f5;padding:9px;text-align:left}}th{{width:140px;color:#526075}}.status{{display:inline-block;border-radius:99px;padding:3px 10px;font-weight:600}}.status.open{{background:#e7f7ed;color:#146c36}}.status.pending{{background:#fff3d6;color:#895c00}}.note{{color:#7a3340;font-size:14px}}a{{color:#1264c4}}.empty{{border-left:5px solid #d39b26}}details{{color:#596579;font-size:14px}}</style>
<header><h1>CDE 临床试验模拟检索</h1><p>虚构病例演示；未使用或保存真实患者数据。血型仅展示，未参与本次 CDE 匹配。</p><table>{rows}</table></header>
<main>{content}</main><section><details><summary>查询说明与已跳过记录</summary><ul>{warnings}</ul></details></section>
</html>"""


if __name__ == "__main__":
    raise SystemExit(main())
