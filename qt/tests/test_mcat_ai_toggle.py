# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Runtime AI on/off toggle — choke-point gating in aqt.mcat.ai_bridge.

The toggle is layered ON TOP of the env provider gate: when off it must force
the static, source-grounded fallback for BOTH the explainer and the follow-up
paths, EVEN IF a live provider + key are configured. Verified here without
importing the full ``aqt``/Qt stack: ``ai_bridge`` only imports stdlib at module
scope (the ``aqt.mw`` handle is resolved lazily inside ``_collection``), so we
load it by file path and stub the collection + bridge.
"""

from __future__ import annotations

import importlib.util
import os
import types

_BRIDGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "aqt", "mcat", "ai_bridge.py"
)


def _load_ai_bridge():
    spec = importlib.util.spec_from_file_location("_mcat_ai_bridge_test", _BRIDGE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeCol:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    def get_config(self, key, default=None):
        if key == "mcat_ai_enabled":
            return self._enabled
        return default


def _install_configured_provider(mod, monkeypatch):
    """Stub a bridge that ALWAYS reports a working live provider.

    So any degrade to static is attributable to the toggle, not a missing key.
    """

    calls = {"explain": 0, "followup": 0}

    class _Expl:
        provider = "openai"
        chosen_letter = "A"
        correct_letter = "B"
        why_wrong = "because"
        correct_path = "The correct answer is B. Do X."
        next_action = ""
        attribution = ""

    class _Provider:
        def explain(self, q, idx):
            calls["explain"] += 1
            return _Expl()

    ax = types.SimpleNamespace(
        build_live_call_model_from_env=lambda: (object(), "openai"),
        live_provider_from_env=lambda: _Provider(),
        safety_block=lambda expl, q: False,
        static_fallback_explanation=lambda q, idx: _Expl(),
    )

    def _fake_followup_caller():
        calls["followup"] += 1
        return object(), "openai"

    qa = types.SimpleNamespace(
        live_followup_caller_from_env=_fake_followup_caller,
        serve_followup_result=lambda *a, **k: types.SimpleNamespace(
            status="answered",
            answer=types.SimpleNamespace(
                answer="ans", source_name="OpenStax", provider="openai"
            ),
            detail=None,
        ),
    )
    monkeypatch.setattr(mod, "_load_bridge", lambda: {"ax": ax, "qa": qa, "root": "."})
    return calls


def test_toggle_off_forces_static_even_with_provider(monkeypatch):
    mod = _load_ai_bridge()
    monkeypatch.setattr(mod, "_collection", lambda: _FakeCol(enabled=False))
    calls = _install_configured_provider(mod, monkeypatch)

    q = {"stem": "s", "choices": ["a", "b"], "correct": "B"}

    # A configured provider is available, yet the OFF toggle short-circuits:
    assert mod.ai_toggle_enabled() is False
    assert mod.ai_available() is False
    assert mod.fetch_ai_explanation(q, 0) == (None, "off")
    assert mod.fetch_followup_answer(q, 0, "why?") == (None, "off")
    # The live provider was never invoked — we degraded before building it.
    assert calls == {"explain": 0, "followup": 0}


def test_toggle_on_uses_provider(monkeypatch):
    mod = _load_ai_bridge()
    monkeypatch.setattr(mod, "_collection", lambda: _FakeCol(enabled=True))
    calls = _install_configured_provider(mod, monkeypatch)

    q = {"stem": "s", "choices": ["a", "b"], "correct": "B"}

    assert mod.ai_toggle_enabled() is True
    assert mod.ai_available() is True
    text, tag = mod.fetch_ai_explanation(q, 0)
    assert text and tag == "openai"
    ftext, ftag = mod.fetch_followup_answer(q, 0, "why?")
    assert ftext and ftag == "openai"
    assert calls["explain"] == 1


def test_default_enabled_without_collection(monkeypatch):
    mod = _load_ai_bridge()
    monkeypatch.setattr(mod, "_collection", lambda: None)
    # Headless / no open collection: defaults to enabled so only the env gate
    # is in effect (unchanged shipped behaviour).
    assert mod.ai_toggle_enabled() is True
