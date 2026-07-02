# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Export MCAT performance attempts from a sidecar DB for offline eval.

The local sidecar ``collection.mcat_perf.db`` is the source of truth for
performance data (see MCAT/docs/DECISIONS.md §5). Stock Anki sync does NOT carry
these custom tables, so this read-only export is how attempt data leaves the
device for eval. No AI, no network.

Dumps ``perf_attempts`` (joined with ``perf_questions`` for topic/section/split
context) to CSV and/or JSON. ``feature_json`` is emitted both raw (a column) and
expanded into ``feat_<key>`` columns (CSV) / a nested ``feature`` object (JSON).

With ``--calibration`` it instead emits the probe-aware CALIBRATION view: only
the rows the objective content re-check probe labeled (``recheck_correct IS NOT
NULL``), flattened to one analysis-ready row per attempt (see
``anki.mcat_perf.CALIBRATION_COLUMNS``), and prints a diagnosis/probe agreement
summary — the headline honest number for "how often does the heuristic
diagnosis agree with the objective probe?" and the basis for calibrating the
hand-set weights (``w_mis``, confidence bands, HIGH_M / LOW_M). No AI, no
network. See MCAT/docs/DECISIONS.md (calibration export).

Usage:
  python tools/mcat_export_perf.py --db /path/collection.mcat_perf.db \
      --out /path/perf_export --format both
  python tools/mcat_export_perf.py --db collection.mcat_perf.db \
      --out attempts.csv --format csv
  python tools/mcat_export_perf.py --db collection.mcat_perf.db \
      --out /path/calibration --calibration --format both
"""

from __future__ import annotations

import argparse
import os
import sys

from anki.mcat_perf import (
    build_calibration_rows,
    calibration_agreement,
    export_attempts,
    write_calibration,
)


def _print_agreement(summary: dict) -> None:
    """Print the diagnosis/probe agreement confusion tally + overall rate."""
    t = summary["tally"]
    print(f"probe-labeled attempts: {summary['labeled']}")
    print("  diagnosis / probe agreement (pre-probe heuristic vs objective):")
    print(
        f"    probe FAIL & engine content_gap    = {t['fail_content_gap']:>4}  (agree)"
    )
    print(
        f"    probe PASS & engine not-content_gap= {t['pass_other']:>4}  (agree)"
    )
    print(
        f"    probe FAIL & engine not-content_gap= {t['fail_other']:>4}  (disagree)"
    )
    print(
        f"    probe PASS & engine content_gap    = {t['pass_content_gap']:>4}  (disagree)"
    )
    rate = summary["agreement_rate"]
    if rate is None:
        print("  agreement %: n/a (no probe-labeled rows — honest abstain)")
    else:
        print(
            f"  diagnosis/probe agreement: {summary['agree']}/{summary['labeled']} "
            f"= {rate * 100:.1f}%"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to collection.mcat_perf.db")
    ap.add_argument(
        "--out",
        required=True,
        help="output path (for --format both, the extension is dropped and "
        ".csv/.json siblings are written)",
    )
    ap.add_argument(
        "--format",
        choices=["csv", "json", "both"],
        default="both",
        help="output format (default: both)",
    )
    ap.add_argument(
        "--calibration",
        action="store_true",
        help="emit the probe-aware calibration view (probe-labeled rows only) "
        "and print the diagnosis/probe agreement summary",
    )
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"db not found: {args.db}", file=sys.stderr)
        sys.exit(2)

    if args.calibration:
        rows = build_calibration_rows(args.db)
        n = write_calibration(rows, args.out, fmt=args.format)
        print(
            f"exported {n} probe-labeled calibration rows from {args.db} "
            f"({args.format}) -> {args.out}"
        )
        _print_agreement(calibration_agreement(rows))
        return

    n = export_attempts(args.db, args.out, fmt=args.format)
    print(f"exported {n} attempts from {args.db} ({args.format}) -> {args.out}")


if __name__ == "__main__":
    main()
