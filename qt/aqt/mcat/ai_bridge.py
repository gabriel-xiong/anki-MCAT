# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Bridge to MCAT Speedrun AI scripts (post-miss explainer + follow-up Q&A).

Lazy-imports ``MCAT/scripts`` only when the student opts in. AI stays OFF unless
``MCAT_LLM_PROVIDER`` is set (via ``MCAT/.env`` or the shell).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

_BRIDGE: dict[str, Any] | None = None

# User-facing runtime override, persisted in the collection config so it
# survives restarts (and rides along on stock Anki sync — a few bytes). This is
# layered ON TOP of the env provider gate (``MCAT_LLM_PROVIDER``): when False it
# force-disables the assistant so both the explainer and follow-up paths fall
# back to the static, source-grounded explanation, EVEN IF a provider + key are
# configured. Default True (unchanged behaviour: env gate still applies).
AI_TOGGLE_CONFIG_KEY = "mcat_ai_enabled"


def _collection() -> Any | None:
    """Best-effort handle to the open collection, or None when headless."""
    try:
        from aqt import mw  # type: ignore
    except Exception:
        return None
    return getattr(mw, "col", None) if mw is not None else None


def ai_toggle_enabled() -> bool:
    """True unless the student turned the assistant OFF from the UI.

    Read from the collection config (default True). Never raises: with no open
    collection (headless / early startup) it reports enabled so the env gate is
    the only thing in effect, matching the shipped default.
    """
    col = _collection()
    if col is None:
        return True
    try:
        return bool(col.get_config(AI_TOGGLE_CONFIG_KEY, True))
    except Exception:
        return True


def set_ai_toggle_enabled(enabled: bool) -> None:
    """Persist the runtime AI on/off override in the collection config."""
    col = _collection()
    if col is None:
        return
    try:
        col.set_config(AI_TOGGLE_CONFIG_KEY, bool(enabled))
    except Exception:
        pass

# ``correct_path`` from the explainer always opens with a "The correct answer
# is …." sentence, which just restates the header. Match it so we can drop the
# duplicate while keeping any actionable guidance that follows.
_CORRECT_RESTATEMENT = re.compile(
    r"^\s*the correct answer is\b.*?\.\s*", re.IGNORECASE
)


def _strip_correct_restatement(correct_path: str) -> str:
    """Drop the leading 'The correct answer is ….' restatement, keep the rest."""
    return _CORRECT_RESTATEMENT.sub("", correct_path or "", count=1).strip()


def _failure_tag(exc: BaseException | str) -> str:
    """Map provider failures to a small UI tag without importing SDK classes."""
    text = str(exc).lower()
    name = exc.__class__.__name__.lower() if not isinstance(exc, str) else ""
    # The provider SDK (openai/anthropic) is imported lazily inside the live
    # caller, so a machine without it configured but not installed fails here.
    # Surface that honestly instead of a generic "couldn't reach" transport tag.
    if (
        "modulenotfound" in name
        or "importerror" in name
        or "no module named" in text
    ):
        return "missing_sdk"
    if "timeout" in name or "timed out" in text or "timeout" in text:
        return "timeout"
    return "error"


def _anki_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_mcat_root() -> Path | None:
    override = (os.environ.get("MCAT_ROOT") or "").strip()
    if override:
        p = Path(override).expanduser()
        if (p / "scripts" / "ai_explain.py").is_file():
            return p.resolve()
    sibling = _anki_repo_root().parent / "MCAT"
    if (sibling / "scripts" / "ai_explain.py").is_file():
        return sibling.resolve()
    return None


def _load_bridge() -> dict[str, Any] | None:
    global _BRIDGE
    if _BRIDGE is not None:
        return _BRIDGE if _BRIDGE else None

    root = _resolve_mcat_root()
    if root is None:
        _BRIDGE = {}
        return None

    scripts = root / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))

    try:
        from mcat_env import ensure_mcat_env_loaded  # type: ignore

        ensure_mcat_env_loaded()
        import ai_explain as ax  # type: ignore
        import ai_qa as qa  # type: ignore

        _BRIDGE = {"ax": ax, "qa": qa, "root": root}
        return _BRIDGE
    except Exception:
        _BRIDGE = {}
        return None


def ai_provider_configured() -> bool:
    """True when a live AI backend is configured, IGNORING the user toggle.

    A "backend" is either a hosted **proxy** (keyless — ``mcat-ai-proxy.json`` /
    ``MCAT_AI_PROXY_URL``) or a direct provider + key (``MCAT_LLM_PROVIDER``).
    This is a cheap, network-free CONFIG-PRESENCE check (the SDKs / network are
    only touched when a call actually runs), so the UI can honestly show whether
    AI is set up on this build without pinging anything. Never raises.
    """
    bridge = _load_bridge()
    if not bridge:
        return False
    ax = bridge["ax"]
    try:
        caller, _ = ax.build_live_call_model_from_env()
    except Exception:
        # e.g. ProviderUnavailable (no key) or ValueError (unknown provider).
        return False
    return caller is not None


def ai_available() -> bool:
    """True when live AI is BOTH configured and enabled by the user toggle.

    Never raises: a backend that is named but unusable (missing key / bad
    provider value / no proxy URL) is reported as *not available* so the caller
    degrades to the AI-off path instead of crashing the GUI thread.
    """
    # Runtime override: an OFF toggle force-disables live AI regardless of env,
    # so the caller degrades to the static path exactly as AI-off does today.
    if not ai_toggle_enabled():
        return False
    return ai_provider_configured()


def _format_explanation_body(expl: Any) -> str:
    """Render an ``Explanation`` into the panel's multi-line body text.

    Shared by the live explainer and the offline/static fallback so both read
    identically. First line is the "You picked … · Correct: …" summary (the
    dialog bolds it as a header); any "The correct answer is …." restatement in
    ``correct_path`` is stripped so the answer isn't stated twice.
    """
    guidance = _strip_correct_restatement(expl.correct_path)
    parts = [
        f"You picked {expl.chosen_letter} · Correct: {expl.correct_letter}",
        (expl.why_wrong or "").strip(),
    ]
    if guidance:
        parts.append(guidance)
    if (expl.next_action or "").strip():
        parts.append(f"Next: {expl.next_action.strip()}")
    if (expl.attribution or "").strip():
        parts.append(expl.attribution.strip())
    return "\n".join(p for p in parts if p)


def static_explanation(q: dict, chosen_index: int) -> Optional[str]:
    """Source-grounded OFFLINE explanation — no provider or API key required.

    Backs the assistant panel's always-available fallback so it is never empty:
    the deterministic ``static_fallback_explanation`` is grounded in the item's
    own text and names the correct choice. Returns None only if the MCAT scripts
    can't be located next to Anki (then the caller uses the raw item text).
    """
    bridge = _load_bridge()
    if not bridge:
        return None
    ax = bridge["ax"]
    try:
        expl = ax.static_fallback_explanation(q, chosen_index)
    except Exception:
        return None
    return _format_explanation_body(expl)


def fetch_ai_explanation(q: dict, chosen_index: int) -> tuple[Optional[str], str]:
    """Opt-in per-choice AI explainer. Returns (text, provider_tag)."""
    # Honor the runtime toggle at the choke point: OFF short-circuits to the
    # AI-off path ("off") before any provider is built, even with env set.
    if not ai_toggle_enabled():
        return None, "off"
    bridge = _load_bridge()
    if not bridge:
        return None, "unavailable"
    ax = bridge["ax"]
    try:
        provider = ax.live_provider_from_env()
    except Exception:
        return None, "off"
    if provider is None:
        return None, "off"
    try:
        expl = provider.explain(q, chosen_index)
    except Exception as exc:
        return None, _failure_tag(exc)
    if ax.safety_block(expl, q):
        # A model response that fails grounding/correctness is not a transport
        # failure; keep the shipped safe fallback behavior for that case.
        expl = ax.static_fallback_explanation(q, chosen_index)
    return _format_explanation_body(expl), expl.provider


def fetch_followup_answer(
    q: dict, chosen_index: int, followup_question: str
) -> tuple[Optional[str], str]:
    """Opt-in follow-up Q&A. Returns (answer_text, tag).

    Mirrors the explainer's provider-resolution path: resolve the live follow-up
    caller from env and pass it through to ``serve_followup_result``, so the UI
    can DISTINGUISH why no answer came back — ``off`` (no provider/key),
    ``blocked`` (grounded-answer guarantee declined it), or ``error`` — instead
    of one vague message. On success the tag is the provider label. Grounding,
    opt-in, and source attribution are all preserved.
    """
    # Same runtime override as the explainer: an OFF toggle forces the static
    # fallback for follow-ups too (this is the follow-up path's choke point,
    # since it does not consult ai_available() first).
    if not ai_toggle_enabled():
        return None, "off"
    bridge = _load_bridge()
    if not bridge:
        return None, "unavailable"
    qa = bridge["qa"]
    # Resolve the live provider explicitly (mirrors fetch_ai_explanation), so a
    # missing/misconfigured provider is reported as "off" rather than "blocked".
    try:
        caller, label = qa.live_followup_caller_from_env()
    except Exception:
        return None, "off"
    if caller is None:
        return None, "off"
    result = qa.serve_followup_result(
        q, chosen_index, followup_question, caller, provider_label=label
    )
    if result.status == "answered" and result.answer is not None:
        ans = result.answer
        text = f"{ans.answer}\nSource: {ans.source_name}"
        return text, ans.provider
    tag = result.status if result.status in ("off", "blocked", "error") else "error"
    if tag == "error" and result.detail:
        tag = _failure_tag(result.detail)
    return None, tag
