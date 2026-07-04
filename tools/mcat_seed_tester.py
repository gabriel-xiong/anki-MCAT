"""Build a TURNKEY, import-free MCAT Speedrun tester base directory.

Produces a preseeded Anki *base folder* (the thing `anki.exe -b <base>` opens)
so a tester's first launch is fully set up with ZERO manual steps: the MCAT
flashcard deck, the curated performance question bank, and the
application-practice remediation pool are all already loaded. No deck import, no
"Load question bank", no config.

WHY a preseeded base (not a first-run code bootstrap):
  * It touches NO application source — only packaging/data. The perf features,
    eligibility gates (>=3 cards seen + >=5 Good/Easy per topic), the three
    separate scores (memory / performance / readiness), Bayesian-shrinkage
    performance, FSRS memory, and numeric readiness confidence are ALREADY
    compiled into the build (pylib/anki/mcat_scores.py + mcat_perf.py) — nothing
    to ship for those.
  * The only per-user content that isn't in the binary is (a) the deck (notes +
    topic tags) and (b) the sidecar `collection.mcat_perf.db` (question bank +
    remediation pool). Both are seeded here.

WHAT gets seeded (all offline, NO AI, NO network, NO secrets):
  <base>/prefs21.db                         # Anki profile registry ("User 1")
  <base>/User 1/collection.anki2            # deck: topic-tagged notes
  <base>/User 1/collection.mcat_perf.db     # sidecar: questions + remediation

The tester reviews locally; there is NO dependency on the builder's self-hosted
sync server. Data comes back via the app's one-click "Export my data" button
(Tools/dashboard) -> a portable *.perf_bundle.json (revlog + perf). Tester-facing
steps: MCAT/docs/TESTER-QUICKSTART.md; builder data-flow reference:
MCAT/docs/TESTER-HANDOFF.md.

Usage (from the anki-MCAT repo root, using the fork's own build venv):
  out/pyenv/Scripts/python.exe tools/mcat_seed_tester.py
  out/pyenv/Scripts/python.exe tools/mcat_seed_tester.py --out out/tester-dist \
      --mcat ../MCAT
This script never opens the builder's live profile under %APPDATA%/Anki2.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# Mirror tools/run.py so `anki`/`aqt` resolve to this build (source + out/).
_REPO = Path(__file__).resolve().parents[1]
for _p in ("pylib", "qt", "out/pylib", "out/qt"):
    sys.path.insert(0, str(_REPO / _p))

# Anki's profile registry code touches Qt at import; run fully headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROFILE_NAME = "User 1"
DECK_NAME = "MCAT Speedrun"


def _default_mcat_root() -> Path:
    """Sibling MCAT repo (…/alphaProjects/MCAT) by default."""
    env = (os.environ.get("MCAT_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (_REPO.parent / "MCAT").resolve()


def _guard_not_live_base(base: Path) -> None:
    """Refuse to write anywhere near the builder's live %APPDATA%/Anki2."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    live = Path(appdata) / "Anki2"
    try:
        base.resolve().relative_to(live.resolve())
    except ValueError:
        return
    raise SystemExit(
        f"REFUSING to seed into the live Anki base ({live}).\n"
        "Pick a scratch --out dir (default is out/tester-dist)."
    )


def _seed_profile(base: Path) -> str:
    """Create prefs21.db with a 'User 1' profile; return the collection path."""
    from aqt.profiles import ProfileManager

    base.mkdir(parents=True, exist_ok=True)
    pm = ProfileManager(base)
    pm.setupMeta()
    pm.create(PROFILE_NAME)  # no-op if it already exists
    prof_dir = base / PROFILE_NAME
    prof_dir.mkdir(parents=True, exist_ok=True)
    return str(prof_dir / "collection.anki2")


def _import_deck(col, data_dir: Path) -> int:
    """Import the flashcard CSVs (cloze + basic) with their pinned notetypes."""
    from anki.collection import ImportCsvRequest

    imported = 0
    for fname in ("flashcards-dev-cloze.csv", "flashcards-dev-basic.csv"):
        path = data_dir / fname
        if not path.exists():
            raise SystemExit(f"missing deck CSV: {path}")
        meta = col.get_csv_metadata(str(path), None)
        log = col.import_csv(ImportCsvRequest(path=str(path), metadata=meta))
        imported += len(log.log.new)
    return imported


def _rename_default_deck(col) -> None:
    """Rename the single Default deck to 'MCAT Speedrun' (no stray empty deck)."""
    from anki.decks import DeckId

    default = col.decks.get(DeckId(1))
    if default and default["name"] == "Default":
        col.decks.rename(default, DECK_NAME)


def _seed_sidecar(col, data_dir: Path) -> tuple[int, int]:
    """Load the question bank + application-practice pool into the sidecar."""
    from anki.mcat_perf import PerfStore

    store = PerfStore(col)
    try:
        n_q = store.load_questions(str(data_dir / "questions.json"))
        n_r = 0
        pool = data_dir / "application-practice.json"
        if pool.exists():
            n_r = store.load_remediation(str(pool))
        return n_q, n_r
    finally:
        store.close()


def _write_launcher(dist: Path, base_rel: str) -> None:
    """Emit the double-click Windows launcher + a plain-text READ ME."""
    launcher = dist / "Start MCAT Speedrun.cmd"
    launcher.write_text(
        "@echo off\r\n"
        "REM Turnkey launcher: opens Anki on the preseeded MCAT base folder.\r\n"
        "setlocal\r\n"
        f'set "BASE=%~dp0{base_rel}"\r\n'
        'set "ANKI="\r\n'
        'if exist "%PROGRAMFILES%\\Anki\\Anki.exe" '
        'set "ANKI=%PROGRAMFILES%\\Anki\\Anki.exe"\r\n'
        'if not defined ANKI if exist "%LOCALAPPDATA%\\Programs\\Anki\\Anki.exe" '
        'set "ANKI=%LOCALAPPDATA%\\Programs\\Anki\\Anki.exe"\r\n'
        'if not defined ANKI if exist "%ProgramFiles(x86)%\\Anki\\Anki.exe" '
        'set "ANKI=%ProgramFiles(x86)%\\Anki\\Anki.exe"\r\n'
        "if not defined ANKI (\r\n"
        '  echo Could not find Anki. Please install "anki-26.05-win-x64.msi" '
        "first, then run this again.\r\n"
        "  pause\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        'start "" "%ANKI%" -b "%BASE%"\r\n'
        "endlocal\r\n",
        encoding="ascii",
    )
    readme = dist / "READ ME FIRST.txt"
    readme.write_text(
        "MCAT Speedrun - tester build\r\n"
        "============================\r\n\r\n"
        "1. Install Anki: double-click  anki-26.05-win-x64.msi  and click "
        "through (Next / Finish).\r\n"
        '2. Double-click  "Start MCAT Speedrun.cmd"  (in this folder).\r\n\r\n'
        "That's it. Everything is preloaded - you do NOT import a deck or set "
        "anything up.\r\n"
        "You'll land on the MCAT home screen with your deck and the three "
        "scores.\r\n\r\n"
        "What to do: press 'Study Flashcards' and review over 2-3 days. When "
        "you're done,\r\n"
        "press 'Export my data' and send the file back. Full instructions: "
        "TESTER-QUICKSTART.\r\n",
        encoding="ascii",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        default=str(_REPO / "out" / "tester-dist"),
        help="distribution dir to assemble (default: out/tester-dist)",
    )
    ap.add_argument(
        "--mcat",
        default=None,
        help="path to the MCAT data/docs repo (default: sibling ../MCAT)",
    )
    ap.add_argument(
        "--keep",
        action="store_true",
        help="do not wipe an existing --out dir first",
    )
    args = ap.parse_args()

    mcat_root = Path(args.mcat).expanduser().resolve() if args.mcat else _default_mcat_root()
    data_dir = mcat_root / "data"
    if not (data_dir / "questions.json").exists():
        raise SystemExit(f"MCAT data not found under {data_dir}")

    dist = Path(args.out).resolve()
    base = dist / "MCAT-Speedrun" / "mcat-base"
    _guard_not_live_base(base)

    if dist.exists() and not args.keep:
        shutil.rmtree(dist, ignore_errors=True)
    (dist / "MCAT-Speedrun").mkdir(parents=True, exist_ok=True)

    from anki.collection import Collection
    from anki import mcat_perf

    col_path = _seed_profile(base)
    col = Collection(col_path)
    try:
        n_notes = _import_deck(col, data_dir)
        _rename_default_deck(col)
        n_cards = col.card_count()
        n_topic = len(col.find_cards("tag:topic:*"))
        n_q, n_r = _seed_sidecar(col, data_dir)
        sidecar = mcat_perf.sidecar_path(col)
    finally:
        col.close()

    _write_launcher(dist / "MCAT-Speedrun", "mcat-base")

    print("=== MCAT tester base seeded ===")
    print(f"base dir     : {base}")
    print(f"collection   : {col_path}")
    print(f"sidecar      : {sidecar}  (exists={os.path.exists(sidecar)})")
    print(f"notes        : {n_notes}")
    print(f"cards        : {n_cards}")
    print(f"topic-tagged : {n_topic}")
    print(f"questions    : {n_q}")
    print(f"remediation  : {n_r}")
    print(f"distribution : {dist / 'MCAT-Speedrun'}")
    print("\nNext: install the MSI, then double-click 'Start MCAT Speedrun.cmd'.")


if __name__ == "__main__":
    main()
