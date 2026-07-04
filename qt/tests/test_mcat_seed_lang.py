# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Regression: preseeded tester base must not crash on language setup."""

from __future__ import annotations

import os
import pickle
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for _p in ("pylib", "qt", "out/pylib", "out/qt"):
    sys.path.insert(0, str(_REPO / _p))

_PY = _REPO / "out" / "pyenv" / "Scripts" / "python.exe"
_SEED = _REPO / "tools" / "mcat_seed_tester.py"


def _read_default_lang(base: Path) -> str | None:
    db = base / "prefs21.db"
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "select data from profiles where name='_global'"
        ).fetchone()
        meta = pickle.loads(row[0])
        return meta.get("defaultLang")
    finally:
        con.close()


def test_missing_default_lang_fallback_does_not_crash():
    """Mirror setupLangAndBackend guard when defaultLang is None."""
    import anki.lang

    lang = None
    force = None
    if not lang:
        lang = anki.lang.get_def_lang(force)[1]
    result = anki.lang.lang_to_disk_lang(lang)
    assert result == "en"


def test_old_broken_base_meta_would_crash_without_guard():
    """Document the original failure mode."""
    import anki.lang

    with pytest.raises(AttributeError):
        anki.lang.lang_to_disk_lang(None)  # type: ignore[arg-type]


@pytest.mark.skipif(not _PY.exists(), reason="anki build venv missing")
def test_seed_tool_sets_default_lang(tmp_path):
    mcat_root = _REPO.parent / "MCAT"
    if not (mcat_root / "data" / "questions.json").exists():
        pytest.skip("sibling MCAT repo not present")

    subprocess.run(
        [
            str(_PY),
            str(_SEED),
            "--out",
            str(tmp_path),
            "--mcat",
            str(mcat_root),
        ],
        check=True,
        cwd=_REPO,
    )

    base = tmp_path / "MCAT-Speedrun" / "mcat-base"
    assert _read_default_lang(base) == "en_US"
    assert (tmp_path / "MCAT-Speedrun" / "TESTER-QUICKSTART.md").exists()
    assert (tmp_path / "MCAT-Speedrun.zip").exists()
