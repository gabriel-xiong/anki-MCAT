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

Usage:
  python tools/mcat_export_perf.py --db /path/collection.mcat_perf.db \
      --out /path/perf_export --format both
  python tools/mcat_export_perf.py --db collection.mcat_perf.db \
      --out attempts.csv --format csv
"""

from __future__ import annotations

import argparse
import os
import sys

from anki.mcat_perf import export_attempts


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
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"db not found: {args.db}", file=sys.stderr)
        sys.exit(2)

    n = export_attempts(args.db, args.out, fmt=args.format)
    print(f"exported {n} attempts from {args.db} ({args.format}) -> {args.out}")


if __name__ == "__main__":
    main()
