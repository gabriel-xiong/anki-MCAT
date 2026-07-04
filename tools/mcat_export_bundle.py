# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Export a portable MCAT performance **sync bundle** from a sidecar DB.

Two-way sync (see MCAT/docs/DECISIONS.md §22): stock Anki sync will not carry
our custom sidecar tables, so cross-device perf sync uses a portable, versioned
JSON bundle that is UNION-MERGED on the other device with
``tools/mcat_import_perf.py``. The bundle contains the append-only
``perf_attempts`` (each with a stable ``uuid``) plus the question bank so the
receiving device can reconstruct question context. No AI, no network.

This is distinct from ``tools/mcat_export_perf.py`` (the read-only eval CSV/JSON
export): a bundle is the *importable* interchange format.

Memory calibration (ADDITIVE): pass ``--collection /path/collection.anki2`` to
also embed the tester's raw ``revlog`` rows under ``memory_revlog`` (read-only),
and ``--participant`` to embed a tester label under ``participant`` so
attribution survives file renames/merges. The revlog can be fed to the
memory-calibration harness via ``scripts/revlog_from_bundle.py`` (MCAT repo).

Usage:
  python tools/mcat_export_bundle.py --db /path/collection.mcat_perf.db \
      --out /path/collection.perf_bundle.json \
      [--collection /path/collection.anki2] [--participant AB]
"""

from __future__ import annotations

import argparse
import os
import sys

from anki.mcat_perf import export_bundle


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to collection.mcat_perf.db")
    ap.add_argument(
        "--out", required=True, help="output bundle path (JSON)"
    )
    ap.add_argument(
        "--collection",
        default=None,
        help="optional collection.anki2 to embed revlog (memory calibration)",
    )
    ap.add_argument(
        "--participant",
        default=None,
        help="optional tester label embedded in the bundle payload",
    )
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"db not found: {args.db}", file=sys.stderr)
        sys.exit(2)
    if args.collection is not None and not os.path.exists(args.collection):
        print(f"collection not found: {args.collection}", file=sys.stderr)
        sys.exit(2)

    res = export_bundle(
        args.db,
        args.out,
        collection_path=args.collection,
        participant=args.participant,
    )
    print(
        f"exported sync bundle from {args.db}: "
        f"{res['attempts']} attempts, {res['questions']} questions, "
        f"{res['revlog']} revlog rows -> {args.out}"
    )


if __name__ == "__main__":
    main()
