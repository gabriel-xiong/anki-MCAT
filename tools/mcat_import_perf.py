# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Import (union-merge) an MCAT performance **sync bundle** into a sidecar DB.

Two-way sync (see MCAT/docs/DECISIONS.md §22): counterpart to
``tools/mcat_export_bundle.py``. Performs an append-only UNION merge —
``perf_attempts`` are deduped by their stable ``uuid`` (re-importing the same
bundle is a no-op) and missing ``perf_questions`` are inserted by ``id`` (the
bank is deterministic from the curated source, so existing local rows win). The
merge is idempotent and order-independent, so after each device imports the
other's bundle both converge to the union of all attempts. No AI, no network.

If ``--db`` does not exist it is created (schema + migrations applied) so a
bundle can seed a fresh device.

Usage:
  python tools/mcat_import_perf.py --db /path/collection.mcat_perf.db \
      --bundle /path/other-device.perf_bundle.json
"""

from __future__ import annotations

import argparse
import os
import sys

from anki.mcat_perf import import_bundle


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db",
        required=True,
        help="path to collection.mcat_perf.db (created if missing)",
    )
    ap.add_argument(
        "--bundle", required=True, help="path to a sync bundle (JSON) to merge"
    )
    args = ap.parse_args()

    if not os.path.exists(args.bundle):
        print(f"bundle not found: {args.bundle}", file=sys.stderr)
        sys.exit(2)

    try:
        res = import_bundle(args.db, args.bundle)
    except Exception as exc:  # surface a clean message for bad/incompatible files
        print(f"import failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        f"merged {args.bundle} -> {args.db}: "
        f"{res['attempts_added']} new attempts "
        f"({res['attempts_skipped']} already present), "
        f"{res['questions_added']} questions added"
    )


if __name__ == "__main__":
    main()
