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
import json
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

# Tester scope = the documented, best-supported CONTENT topics (DECISIONS §27/§28
# "locked eval trio": component-card granularity + the paraphrase instrument).
# The tester build ships ONLY these 3 topics — deck cards AND question bank — so
# a friend can realistically unlock all shipped topics and let Readiness compute
# over a weekend (coverage is scoped to these 3; see mcat_scores.shipped_scope_*).
# Card counts: bb_enzymes 29, cp_acids_bases 23, cp_kinetics 14 = 66 cards.
# Question counts: 62 curated MCQs (CP 40 + BB 22), all ≥ the gates.
SCOPE_TOPICS: tuple[str, ...] = ("cp_acids_bases", "bb_enzymes", "cp_kinetics")

# New-cards/day for the shipped 3-topic tester deck. UNCAPPED (9999 ≈ unlimited):
# per the user, a weekend tester should be able to get through ALL 66 cards of the
# 3-topic deck as fast as they want — in one sitting if they choose — with no
# daily gate. Anki's default (20) would otherwise ration new cards across days.
# (The by-topic interleave below is kept but is now immaterial to unlocking, since
# nothing is held back per day.)
NEW_CARDS_PER_DAY = 9999


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
    # A fresh meta has defaultLang=None. On the tester's launch the preseeded
    # base already exists, so Anki's first-run flow (which normally prompts for
    # and stores a default language) is skipped, and setupLangAndBackend would
    # feed None into lang_to_disk_lang -> crash. Set a sane default so the
    # shipped base is well-formed and launches cleanly with no prompt.
    pm.setLang("en_US")
    pm.create(PROFILE_NAME)  # no-op if it already exists
    prof_dir = base / PROFILE_NAME
    prof_dir.mkdir(parents=True, exist_ok=True)
    return str(prof_dir / "collection.anki2")


def _enable_fsrs(col) -> bool:
    """Enable the v3 scheduler + FSRS so reviews populate per-card memory state.

    WHY (Memory score dependency): the Memory "Recall strength" reads FSRS
    predicted retrievability, which only exists once a card carries an FSRS
    ``memory_state`` — and that is computed on review ONLY when FSRS is enabled.
    A freshly created collection defaults FSRS OFF (Rust ``BoolKey::Fsrs`` is
    false by default) and may sit on the legacy scheduler, so a tester's reviews
    would never yield a retrievability estimate and the Memory card could never
    compute (even at the lowered friend-tester gates). Enabling both here is a
    data/packaging change only — NO app source change. Default FSRS parameters
    are used; memory state is computed per review going forward (testers start at
    zero reviews, so every review they do is scored with FSRS on).
    """
    from anki.config import Config

    if col.sched_ver() != 2:
        col.upgrade_to_v2_scheduler()
    # v3 scheduler (required for FSRS).
    col.set_config_bool(Config.Bool.SCHED_2021, True)
    # FSRS enable is keyed by the generic "fsrs" config (Rust BoolKey::Fsrs);
    # there is no proto Config.Bool.FSRS member, so set the raw key.
    col.set_config("fsrs", True)
    col._load_scheduler()
    return bool(col.get_config("fsrs", False))


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


def _scope_deck_to_topics(col, scope_topics: tuple[str, ...]) -> int:
    """Delete imported notes whose topic tag is outside the shipped scope.

    The CSVs carry all 15 card-backed topics; the tester ships only
    ``scope_topics``, so drop everything else. Returns the remaining card count.
    """
    keep = " or ".join(f"tag:topic:{t}" for t in scope_topics)
    drop_nids = list(col.find_notes(f"-({keep})"))
    if drop_nids:
        col.remove_notes(drop_nids)
    return col.card_count()


def _interleave_new_cards_by_topic(col) -> int:
    """Round-robin the new-card positions across topics.

    The deck CSVs are grouped by topic (all of topic A, then all of topic B, …),
    so with Anki's default "Deck"/position gather a per-day new limit would drain
    whole topics front-to-back and starve later topics until the deck is nearly
    exhausted. Repositioning new cards round-robin by topic means the first N new
    cards each day span *all* topics, so a per-day limit unlocks a broad spread
    of topics per session instead of only the first few. Uses Anki's own
    reposition API — no scheduler/scoring change. Returns the topic count.
    """
    from collections import OrderedDict

    buckets: "OrderedDict[str, list]" = OrderedDict()
    # order by position so within-topic order is preserved (import order)
    for cid in col.find_cards("", order="c.due asc"):
        note = col.get_card(cid).note()
        topic = next(
            (t for t in note.tags if t.startswith("topic:")), "topic:_untagged"
        )
        buckets.setdefault(topic, []).append(cid)

    interleaved: list = []
    queues = list(buckets.values())
    while any(queues):
        for q in queues:
            if q:
                interleaved.append(q.pop(0))

    col.sched.reposition_new_cards(
        interleaved,
        starting_from=1,
        step_size=1,
        randomize=False,
        shift_existing=False,
    )
    return len(buckets)


def _set_new_cards_per_day(col, per_day: int) -> int:
    """Raise the deck's new-cards/day limit in its options preset.

    The single MCAT deck is the renamed Default deck (DeckId 1), which uses the
    Default options preset. Bumping ``new.perDay`` there (not on a live tweak)
    ships the limit in the preseeded base. Returns the value read back.
    """
    from anki.decks import DeckId

    conf = col.decks.config_dict_for_deck_id(DeckId(1))
    conf["new"]["perDay"] = per_day
    col.decks.update_config(conf)
    return col.decks.config_dict_for_deck_id(DeckId(1))["new"]["perDay"]


def _seed_sidecar(
    col, data_dir: Path, scope_topics: tuple[str, ...]
) -> tuple[int, int]:
    """Load the question bank + application-practice pool into the sidecar,
    FILTERED to the shipped scope so only ``scope_topics`` are testable.

    Filters the JSON to the scope topics and upserts directly (rather than
    ``load_questions``) so the sidecar's perf_questions/remediation_items hold
    only the shipped topics — this is what mcat_scores scopes coverage and the
    Readiness section gates against. Intentionally does NOT record the builder's
    absolute question-bank path in perf_meta (it wouldn't exist on the tester's
    machine and is unnecessary — the re-check probe resolves backing cards from
    the deck's ``supports_question`` tags).
    """
    from anki.mcat_perf import PerfStore

    scope = set(scope_topics)
    store = PerfStore(col)
    try:
        with open(data_dir / "questions.json", encoding="utf-8") as f:
            questions = json.load(f)
        scoped_q = [q for q in questions if q.get("topic_id") in scope]
        n_q = store.upsert_questions(scoped_q)

        n_r = 0
        pool = data_dir / "application-practice.json"
        if pool.exists():
            with open(pool, encoding="utf-8") as f:
                items = json.load(f)
            scoped_r = [x for x in items if x.get("topic_id") in scope]
            n_r = store.upsert_remediation(scoped_r)
        return n_q, n_r
    finally:
        store.close()


def _bundle_docs(dist: Path, mcat_root: Path) -> None:
    """Copy tester-facing docs into the distribution folder."""
    src = mcat_root / "docs" / "TESTER-QUICKSTART.md"
    if not src.exists():
        raise SystemExit(f"missing tester quickstart: {src}")
    shutil.copy2(src, dist / "TESTER-QUICKSTART.md")


def _bundle_ai_proxy(dist: Path, mcat_root: Path) -> list[str]:
    """Ship the keyless-AI-proxy client bits into the distribution (data only).

    Copies the MINIMAL runtime scripts the compiled AI bridge delegates to
    (``ai_explain``/``ai_qa``/``mcat_env``) under ``mcat-ai/scripts/`` so the app
    can resolve them via ``MCAT_ROOT`` (set by the launcher), drops a PLACEHOLDER
    ``mcat-ai-proxy.json`` next to the launcher for the human to fill in after
    deploy, and includes the setup doc. NO URL/token/key is baked in — the
    shipped config is inert (placeholder URL => proxy OFF => source-based
    fallback) until the human pastes real values. Returns a short manifest.

    This is additive + dormant: with the placeholder config the app behaves
    exactly like the friend build (offline, source-grounded). It only lights up
    live AI once a real proxy URL + token are pasted in — no rebuild needed.
    """
    manifest: list[str] = []
    ai_scripts = dist / "mcat-ai" / "scripts"
    ai_scripts.mkdir(parents=True, exist_ok=True)
    for fname in ("ai_explain.py", "ai_qa.py", "mcat_env.py"):
        src = mcat_root / "scripts" / fname
        if not src.exists():
            raise SystemExit(f"missing AI script to bundle: {src}")
        shutil.copy2(src, ai_scripts / fname)
        manifest.append(f"mcat-ai/scripts/{fname}")

    cfg_dst = dist / "mcat-ai-proxy.json"
    cfg_src = mcat_root / "proxy" / "mcat-ai-proxy.example.json"
    if cfg_src.exists():
        shutil.copy2(cfg_src, cfg_dst)
    else:  # self-contained fallback template (still a placeholder, no secrets)
        cfg_dst.write_text(
            json.dumps(
                {
                    "_comment": (
                        "Paste your deployed proxy URL + bundle token. NO API "
                        "key here. See AI-PROXY-SETUP.md."
                    ),
                    "proxy_url": "REPLACE_WITH_YOUR_PROXY_URL",
                    "bundle_token": "REPLACE_WITH_YOUR_BUNDLE_TOKEN",
                    "model": "gpt-4o-mini",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    manifest.append("mcat-ai-proxy.json (placeholder)")

    setup_doc = mcat_root / "docs" / "AI-PROXY-SETUP.md"
    if setup_doc.exists():
        shutil.copy2(setup_doc, dist / "AI-PROXY-SETUP.md")
        manifest.append("AI-PROXY-SETUP.md")
    return manifest


def _build_zip(dist: Path) -> Path:
    """Zip MCAT-Speedrun/ for shipping alongside the MSI."""
    zip_base = dist / "MCAT-Speedrun"
    zip_path = dist / "MCAT-Speedrun.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_base), "zip", root_dir=dist, base_dir="MCAT-Speedrun")
    return zip_path


def _write_launcher(dist: Path, base_rel: str, ai_proxy: bool = False) -> None:
    """Emit the double-click Windows launcher + a plain-text READ ME.

    When ``ai_proxy`` is set, the launcher ALSO exports two env vars that the
    already-compiled app reads at startup: ``MCAT_ROOT`` (points at the bundled
    ``mcat-ai`` scripts so the AI bridge resolves) and ``MCAT_AI_PROXY_CONFIG``
    (points at the editable ``mcat-ai-proxy.json`` next to this launcher). This
    is what makes the proxy URL/token CONFIG-DRIVEN — no MSI rebuild to set or
    change them; ``set`` before ``start`` so the launched Anki inherits them.
    """
    ai_env = ""
    if ai_proxy:
        ai_env = (
            'set "MCAT_ROOT=%~dp0mcat-ai"\r\n'
            'set "MCAT_AI_PROXY_CONFIG=%~dp0mcat-ai-proxy.json"\r\n'
        )
    launcher = dist / "Start MCAT Speedrun.cmd"
    launcher.write_text(
        "@echo off\r\n"
        "REM Turnkey launcher: opens Anki on the preseeded MCAT base folder.\r\n"
        "setlocal\r\n"
        f'set "BASE=%~dp0{base_rel}"\r\n'
        + ai_env
        + 'set "ANKI="\r\n'
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
        "TESTER-QUICKSTART.md (in this folder).\r\n",
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
    ap.add_argument(
        "--with-ai-proxy",
        action="store_true",
        help="also ship the keyless AI-proxy client bits (mcat-ai/ scripts + a "
        "PLACEHOLDER mcat-ai-proxy.json + setup doc) and point the launcher at "
        "them. Dormant until the human pastes a real proxy URL+token; no secret "
        "is baked in. Use for the GRADED build.",
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
        # Enable FSRS (+ v3 scheduler) FIRST so the Memory score can compute:
        # cards only gain an FSRS memory_state (-> retrievability) when reviewed
        # with FSRS on. Without this the tester Memory card never populates.
        fsrs_on = _enable_fsrs(col)
        n_imported = _import_deck(col, data_dir)
        _rename_default_deck(col)
        # Scope the DECK to the shipped 3 topics BEFORE interleaving/limits.
        n_cards = _scope_deck_to_topics(col, SCOPE_TOPICS)
        n_interleaved_topics = _interleave_new_cards_by_topic(col)
        new_per_day = _set_new_cards_per_day(col, NEW_CARDS_PER_DAY)
        n_topic = len(col.find_cards("tag:topic:*"))
        n_q, n_r = _seed_sidecar(col, data_dir, SCOPE_TOPICS)
        sidecar = mcat_perf.sidecar_path(col)
    finally:
        col.close()

    _write_launcher(dist / "MCAT-Speedrun", "mcat-base", ai_proxy=args.with_ai_proxy)
    _bundle_docs(dist / "MCAT-Speedrun", mcat_root)
    ai_manifest: list[str] = []
    if args.with_ai_proxy:
        ai_manifest = _bundle_ai_proxy(dist / "MCAT-Speedrun", mcat_root)
    zip_path = _build_zip(dist)

    print("=== MCAT tester base seeded ===")
    print(f"base dir     : {base}")
    print(f"collection   : {col_path}")
    print(f"sidecar      : {sidecar}  (exists={os.path.exists(sidecar)})")
    print(f"fsrs enabled : {fsrs_on}  (required for Memory retrievability)")
    print(f"scope topics : {', '.join(SCOPE_TOPICS)}")
    print(f"imported     : {n_imported} notes (all topics)")
    print(f"cards (kept) : {n_cards}  (scoped to {len(SCOPE_TOPICS)} topics)")
    print(f"topic-tagged : {n_topic}")
    print(f"new/day      : {new_per_day}  (interleaved across {n_interleaved_topics} topics)")
    print(f"questions    : {n_q}")
    print(f"remediation  : {n_r}")
    print(f"distribution : {dist / 'MCAT-Speedrun'}")
    print(f"zip          : {zip_path}")
    if args.with_ai_proxy:
        print(f"ai-proxy     : shipped -> {', '.join(ai_manifest)}")
        print("               (placeholder config; live AI OFF until URL+token pasted)")
    else:
        print("ai-proxy     : not shipped (friend build; AI off, source-based)")
    print("\nNext: install the MSI, then double-click 'Start MCAT Speedrun.cmd'.")


if __name__ == "__main__":
    main()
