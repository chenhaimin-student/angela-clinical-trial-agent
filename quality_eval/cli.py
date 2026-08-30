from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import annotation_metadata, load_annotations, load_suite
from .reporting import write_annotation_csv, write_reports
from .runner import QualityEvaluator, annotation_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Angela's read-only CDE quality evaluation")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--build-annotation-bundle", action="store_true")
    parser.add_argument("--allow-snapshot-drift", action="store_true")
    args = parser.parse_args()

    suite = load_suite(args.suite)
    annotations = load_annotations(args.annotations)
    evaluator = QualityEvaluator(project_root=args.project_root, database_path=args.database)
    report = evaluator.run(suite, annotations=annotations)
    if args.annotations:
        metadata = annotation_metadata(args.annotations)
        source_suite = metadata.get("suite_id", "")
        source_fingerprint = metadata.get("logical_fingerprint", "")
        current_fingerprint = report["database_snapshot"]["logical_fingerprint"]
        if source_suite and source_suite != suite.suite_id:
            raise SystemExit("annotation suite_id does not match evaluation suite")
        if source_fingerprint and source_fingerprint != current_fingerprint and not args.allow_snapshot_drift:
            raise SystemExit(
                "formal database changed after annotation sampling; rebuild annotations or pass "
                "--allow-snapshot-drift with an audit note"
            )
    json_path, markdown_path = write_reports(report, args.output_dir)
    bundle_path = None
    review_csv_path = None
    if args.build_annotation_bundle:
        bundle_path = args.output_dir / "annotation-bundle.json"
        bundle_path.write_text(
            json.dumps(annotation_bundle(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        review_csv_path = write_annotation_csv(
            json.loads(bundle_path.read_text(encoding="utf-8")),
            args.output_dir / "annotation-review.csv",
        )
    print(json.dumps({
        "overall_status": report["aggregate"]["overall_status"],
        "database_fingerprint": report["database_snapshot"]["logical_fingerprint"],
        "optional_audit_cases": report["aggregate"]["metrics"]["reviewed_case_count"],
        "total_cases": report["aggregate"]["metrics"]["total_case_count"],
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
        "annotation_bundle": str(bundle_path) if bundle_path else None,
        "annotation_review_csv": str(review_csv_path) if review_csv_path else None,
    }, ensure_ascii=False))
