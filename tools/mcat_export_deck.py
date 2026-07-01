"""Export the MCAT exam deck from the desktop collection to a .apkg.

Copies the live collection to a temp dir first (non-destructive), lists decks
with card counts, then exports the chosen deck (or whole collection) as an
Anki package for import into AnkiDroid.

Usage:
  python tools/mcat_export_deck.py --list
  python tools/mcat_export_deck.py --deck "Deck Name" --out /path/mcat.apkg
  python tools/mcat_export_deck.py --all --out /path/mcat.apkg
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

from anki.collection import Collection, DeckIdLimit, ExportAnkiPackageOptions

SRC = r"C:\Users\gpdxi\AppData\Roaming\Anki2\User 1\collection.anki2"


def open_copy() -> tuple[Collection, str]:
    tmp = tempfile.mkdtemp(prefix="mcat_export_")
    dst = os.path.join(tmp, "collection.anki2")
    shutil.copy2(SRC, dst)
    # copy media db if present (not required for export of notes w/o media)
    return Collection(dst), tmp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--deck")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()

    col, tmp = open_copy()
    try:
        if args.list:
            for d in col.decks.all_names_and_ids():
                n = len(col.find_cards(f'deck:"{d.name}"'))
                print(f"{d.id}\t{n}\t{d.name}")
            print("total notes:", col.note_count(), "cards:", col.card_count())
            return

        if not args.out:
            print("need --out", file=sys.stderr)
            sys.exit(2)

        options = ExportAnkiPackageOptions(
            with_scheduling=True,
            with_media=True,
            legacy=True,
        )
        if args.all:
            limit = None
        else:
            did = next(
                d.id for d in col.decks.all_names_and_ids() if d.name == args.deck
            )
            limit = DeckIdLimit(deck_id=did)
        count = col.export_anki_package(
            out_path=args.out, options=options, limit=limit
        )
        print(f"exported {count} notes -> {args.out}")
    finally:
        col.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
