# MCAT Speedrun (Anki fork)

**MCAT Speedrun** is a fork of [Anki](https://apps.ankiweb.net) that adds an
MCAT-focused study loop on top of Anki's spaced-repetition engine. It is a
prototype over ~15–25 high-yield topics — **not** a full prep course.

It reports **three separate scores that are never blended**:

1. **Memory** — recall on flashcards, driven by Anki's FSRS scheduler / revlog.
2. **Performance** — accuracy on a curated bank of exam-style MCQs, kept
   completely separate from flashcards.
3. **Readiness** — an honest mapped range (472–528) with a coverage penalty
   that **abstains** when topic coverage is too low.

Two modes stay separate: **Memory mode** is normal Anki review; **Performance
mode** is a distinct session that only shows a topic's MCQs once that topic is
**unlocked** (≥3 cards seen and ≥5 Good/Easy reviews). CARS is
performance-only (no flashcard gate). The app never flips a flashcard straight
into a scored performance question, and **no AI runs at review time** — the
question bank is curated offline from named sources.

## What this fork changes

- **Rust mastery query** (`rslib/src/mcat/`) — a per-topic mastery query over
  the collection (cards seen, Good/Easy counts, retrievability, unlock status),
  exposed to Python via protobuf (`proto/anki/mcat.proto`).
- **Performance mode** (`pylib/anki/mcat_perf.py`, `qt/aqt/mcat/`) — a
  topic-gated MCQ session with self-reported error typing
  (content gap / passage mapping / reasoning / misread), stored in a local
  **sidecar** database (`collection.mcat_perf.db`) next to the collection.
  The sidecar deliberately does **not** ride stock Anki sync (a full sync would
  wipe custom tables); performance data syncs cross-device via a portable,
  uuid-deduped export/import bundle instead. Memory data (`revlog`/`cards`) still
  uses stock Anki sync. See the companion repo's `docs/SYNC-CONFLICT-RULE.md`.
- **Three-score dashboard** (`pylib/anki/mcat_scores.py`,
  `qt/aqt/deckbrowser.py`) — memory / performance / readiness, coverage %, and
  a single next action, embedded on Anki's home screen, plus a **Topic Mastery**
  viewer.
- **Curated content** lives in the companion `MCAT/` repo
  (`data/questions.json`, `data/flashcards-dev*.csv`,
  `data/mcat-outline.v1.json`).

See the companion repo's `docs/` (PRD, ARCHITECTURE, DECISIONS,
MASTERY-QUERY-SPEC, PERFORMANCE-MODE-SPEC) for full specs and the decision log.

## Building & running from source

Anki builds with its standard toolchain (Rust + a bundled Python env driven by
`ninja`). From the repo root:

```bash
./run          # build everything and launch the desktop app
```

The first build compiles the Rust backend (including the mastery query) and
generates the protobuf bindings. See [docs/development.md](./docs/development.md)
for platform prerequisites.

### Tests

```bash
# Rust: mastery-query unit tests
cargo test -p anki mcat

# Python: end-to-end tests that call the Rust query + perf/scoring logic
export PYTHONPATH="$(pwd)/out/pylib:$(pwd)/pylib"
python -m pytest pylib/tests/test_mcat_mastery.py \
                 pylib/tests/test_mcat_perf.py \
                 pylib/tests/test_mcat_scores.py
```

### Loading the deck & questions

- Import the flashcards from `MCAT/data/flashcards-dev-cloze.csv` and
  `MCAT/data/flashcards-dev-basic.csv` (field `text` / `back`, tag
  `topic:{id}`).
- Load the question bank via **Tools → MCAT: Load question bank…** and pick
  `MCAT/data/questions.json`.
- Start a session from the home-screen dashboard or
  **Tools → MCAT: Performance session**.

## License & credit

This project is a fork of **Anki** by Ankitects Pty Ltd
([`ankitects/anki`](https://github.com/ankitects/anki)) and is distributed
under the **GNU AGPL-3.0-or-later** — see [LICENSE](./LICENSE). The mobile
companion work is based on **AnkiDroid**
([`ankidroid/Anki-Android`](https://github.com/ankidroid/Anki-Android)), also
AGPL-3.0-or-later.

All upstream copyright remains with the original Anki and AnkiDroid authors;
the MCAT Speedrun additions above are contributed under the same
AGPL-3.0-or-later license, and the complete corresponding source is published
with any distribution (AGPL §13). This is an independent project and is
**not affiliated with or endorsed by** Ankitects or the AnkiDroid team.

---

# Anki (upstream)

[![Build Status](https://github.com/ankitects/anki/actions/workflows/ci.yml/badge.svg)](https://github.com/ankitects/anki/actions/workflows/ci.yml)
[![Documentation](https://img.shields.io/badge/docs-dev--docs.ankiweb.net-blue)](https://dev-docs.ankiweb.net)

This repo is forked from the source code for the computer version of
[Anki](https://apps.ankiweb.net).

## About

Anki is a spaced repetition program. Please see the [website](https://apps.ankiweb.net) to learn more.

## Getting Started

### Contributing

Want to contribute to Anki? Check out the [Contribution Guidelines](./docs/contributing.md).

For more information on building and developing, please see [Development](./docs/development.md).

#### Contributors

The following people have contributed to Anki: [CONTRIBUTORS](./CONTRIBUTORS)

### Anki Betas

If you'd like to try development builds of Anki but don't feel comfortable
building the code, please see [Anki betas](https://betas.ankiweb.net/).

## License

Anki's license: [LICENSE](./LICENSE)
