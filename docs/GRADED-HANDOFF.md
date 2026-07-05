# Graded build — grader handoff (anki-MCAT)

_Brief install/run notes for the **strict (graded)** desktop fork. Full grader page:
[`MCAT/docs/HOW-TO-VERIFY.md`](../../MCAT/docs/HOW-TO-VERIFY.md). Install walkthrough:
[`MCAT/docs/GRADED-QUICKSTART.md`](../../MCAT/docs/GRADED-QUICKSTART.md)._

---

## Install

1. Run **`MCAT-Speedrun-graded.msi`** (Windows; unsigned research prototype — *More info → Run anyway* if SmartScreen prompts).
2. Unzip **`MCAT-Speedrun.zip`** (graded bundle) to Desktop or similar.
3. **Do not** launch from `./run` or the Start Menu alone for the demo deck — use the bundle launcher below.

---

## Launcher path

Double-click **`Start MCAT Speedrun.cmd`** inside the unzipped **`MCAT-Speedrun/`** folder.

The script:

- Points Anki at the **pre-seeded profile** (`mcat-base` / User 1) with deck + question bank loaded.
- Sets **`SCORE_PROFILE=strict`** (compiled into this MSI — not the friend `tester` build).
- Uses the fork binary installed by the MSI.

Expected landing: MCAT home screen with **three-score dashboard** (likely all abstaining on a fresh strict profile — intentional).

---

## `mcat-ai-proxy.json`

Sits **next to** `Start MCAT Speedrun.cmd` in the bundle folder.

```json
{
  "proxy_url": "REPLACE_WITH_DEPLOYED_WORKER_URL",
  "bundle_token": "REPLACE_WITH_BUNDLE_TOKEN",
  "model": "gpt-4o-mini"
}
```

- **Placeholder values** → Assistant pill reads **"AI: Not set up"**; offline source-grounded explanations still work.
- **Filled after deploy** → pill **"AI: On"** when reachable; key never on the grader machine.
- Setup: `MCAT/docs/AI-PROXY-SETUP.md`, `MCAT/proxy/mcat-ai-proxy.example.json`.

---

## Strict profile behavior

| Gate | Strict (graded) |
|------|-----------------|
| Memory score | ≥ **200** graded reviews **and** ≥ **1** card with interval ≥ **21 days** |
| Performance score | ≥ **30** attempts on unlocked topics |
| Readiness range | All Memory + Performance gates + ≥ **50%** outline coverage + per-section attempt mins |

Scores show **range / coverage / missing-data note / one next action** — or **abstain**. No `provisional` badge (friend build shows that badge).

Source: `pylib/anki/mcat_scores.py` (`SCORE_PROFILE = "strict"` in graded MSI build).

---

## Auto-sync off

The graded seed script disables **Preferences → Sync → automatic sync on open/close**
for the bundled profile (`tools/mcat_seed_tester.py::_disable_auto_sync_on_open`).

- Avoids surprise AnkiWeb prompts during a grading session.
- Manual **Sync** still works when you configure a sync target.
- Memory data still uses **stock Anki collection sync** when you sync deliberately.

---

## Verify fork identity

```bash
cargo test -p anki mcat    # 4/4 mastery query tests
```

Strict vs friend MSI: graded compile sets `SCORE_PROFILE = "strict"`; friend sets `"tester"`. See `MCAT/docs/GRADED-QUICKSTART.md` §5.

---

## Related docs (MCAT repo)

- [`HOW-TO-VERIFY.md`](../../MCAT/docs/HOW-TO-VERIFY.md) — claim → evidence table
- [`SYNC-CONFLICT-RULE.md`](../../MCAT/docs/SYNC-CONFLICT-RULE.md) — memory sync merge rule
- [`MVP-TO-FINAL.md`](../../MCAT/docs/MVP-TO-FINAL.md) — demo narration deltas
