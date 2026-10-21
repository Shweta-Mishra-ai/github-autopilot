"""
AI Response Validator - app/ai/validator.py
V4 changes:

FIXED (LOOPHOLE 18): Field name standardization.
  Old: validate_pr_analysis() returned {"improved_title": ...}
       But pull_request.py reads r.get("suggested_title") → always got None.
       PR title auto-update was silently using empty string.
  Fix: Return {"suggested_title": ...} everywhere to match the reader.

IMPROVED: Better defaults, stricter type checking, cleaner sanitization.
"""

import logging
import re
from typing import Any

log = logging.getLogger(__name__)


# The words models actually use. Anything unknown falls to "minor", so a
# missing word here demotes a finding: "P0", "fatal", "security" and "bug"
# were all published as minor, under findings the model ranked below them.
_SEVERITY_SYNONYMS = {
    "blocker": "critical",
    "severe": "critical",
    "fatal": "critical",
    "urgent": "critical",
    "p0": "critical",
    "high": "major",
    "error": "major",
    "p1": "major",
    "bug": "major",
    "security": "major",
    "serious": "major",
    "important": "major",
    "danger": "major",
    "dangerous": "major",
    "medium": "minor",
    "moderate": "minor",
    "warning": "minor",
    "p2": "minor",
    "low": "nit",
    "info": "nit",
    "style": "nit",
    "suggestion": "nit",
    "p3": "nit",
    "nitpick": "nit",
    "trivial": "nit",
    "optional": "nit",
    "cosmetic": "nit",
}


def self_estimate(value) -> str:
    """
    A model's confidence in its own answer, labelled as exactly that — or ""
    when it gave no number.

    It was printed as "Confidence: 85%", which reads as a measured
    probability. It is the model's own guess about itself, uncalibrated, and
    a fluent wrong answer rates itself as highly as a right one.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if 1.0 < v <= 100.0:  # "85" meant 85%
        v /= 100.0
    if not 0.0 <= v <= 1.0:
        return ""
    return f"Model's self-estimate: {round(v * 100)}% (its own guess, not a measured probability)"


def _get(data: dict, key: str, default: Any = None) -> Any:
    val = data.get(key, default)
    return val if val is not None else default


def as_text(value: Any, default: str = "") -> str:
    """
    A string for rendering, whatever the model actually sent.

    `r.get("risk_level", "low").upper()` reads as safe and is not: the key can
    be present holding a number, a list or null, and `.upper()` then raises
    inside a blanket handler that replaces a finished analysis with an error
    message. Absent, null or empty takes the default; anything else is coerced
    rather than allowed to raise.
    """
    if value is None or value == "":
        return default
    return value if isinstance(value, str) else str(value)


def _word(data: dict, key: str, default: str) -> str:
    """A lowercase enum value. `.lower()` on the raw field raised
    AttributeError for `{"risk_level": 2}` or `{"priority": ["high"]}`."""
    val = _get(data, key, default)
    return val.strip().lower() if isinstance(val, str) else default


def _bool(val: Any) -> bool:
    """`bool("false")` is True; a model writes booleans as strings often."""
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1")
    return bool(val)


def verdict(data: Any, key: str, allowed) -> str | None:
    """
    The model's answer for an enum field, or None when it gave none or an
    answer outside `allowed`.

    Handlers used `r.get("health", "good")`: a model that answered with prose,
    or a different shape, was published as "Health: Good" — a default
    rendered as a verdict nobody made.
    """
    if not isinstance(data, dict):
        return None
    val = data.get(key)
    if not isinstance(val, str):
        return None
    val = val.strip().lower().replace(" ", "_")
    return val if val in allowed else None


def flag(data: Any, key: str) -> bool | None:
    """True/False only when the model actually said so; None otherwise.
    `bool("false")` is True, so "requires_migration": "false" read as Yes."""
    if not isinstance(data, dict):
        return None
    val = data.get(key)
    if isinstance(val, bool):
        return val
    if isinstance(val, str) and val.strip().lower() in ("true", "yes"):
        return True
    if isinstance(val, str) and val.strip().lower() in ("false", "no"):
        return False
    return None


def _str(val: Any, max_len: int = 300) -> str:
    """Safe string conversion with length cap."""
    return str(val)[:max_len].strip() if val is not None else ""


def _list_of_str(val: Any, max_items: int = 10, max_item_len: int = 100) -> list[str]:
    """Safe list-of-strings extraction."""
    if not isinstance(val, list):
        return []
    return [str(item)[:max_item_len] for item in val if item][:max_items]


def is_unusable(raw: Any) -> bool:
    """
    True when an LLM payload must NOT be rendered as a real result.

    `_extract_json` returns {"raw": text} when the model produced no parseable
    JSON. That dict has no "error" key, so the old validators fell through to
    their defaults and published a fabricated result (e.g. "Score: 7/10 — no
    issues found") for a review that never happened. Treat it as a hard failure.
    """
    if not isinstance(raw, dict) or not raw:
        # An empty object is not an answer: validate_pr_analysis({}) produced
        # a non-degraded "risk_level: medium, confidence 0.5" from nothing.
        return True
    return bool(raw.get("error")) or ("raw" in raw)


# ── PR Analysis ───────────────────────────────────────────────────────────────


def validate_pr_analysis(raw: dict) -> dict:
    """
    Validate and sanitize PR analysis response.

    Every field returned here is read by app/handlers/pull_request/analysis.py.
    That is a rule, not an observation: `pr_type` and `labels` used to be
    validated here and consumed nowhere, and `improved_title` was returned
    under a name the reader did not use, which is how every PR shipped with a
    blank title suggestion. tests/test_validator.py::TestNoDeadValidatorFields
    fails the build if a field is added here without a reader.
    """
    VALID_RISK = {"low", "medium", "high"}

    if is_unusable(raw):
        log.warning(f"validate_pr_analysis: unusable payload — {str(raw)[:120]}")
        return {
            "suggested_title": "",
            "description": "",
            "risk_level": "medium",
            "risk_reason": "Could not analyze — using safe defaults",
            "review_focus": [],
            "confidence": 0.0,
            "_degraded": True,
        }

    # A more severe word than the scale has is HIGH, never "medium" — mapping
    # "critical" down made the gravest answer the least alarming. A missing or
    # unrecognised value is shown as medium but marked unmeasured, so it is
    # never recorded as the PR's risk for auto-merge to trust.
    risk = _word(raw, "risk_level", "")
    risk_measured = True
    if risk in ("critical", "severe", "blocker"):
        risk = "high"
    elif risk not in VALID_RISK:
        risk, risk_measured = "medium", False

    review_focus = raw.get("review_focus", [])
    if not isinstance(review_focus, list):
        review_focus = []
    review_focus = [str(f)[:200] for f in review_focus if f][:5]

    confidence = 0.5
    try:
        confidence = float(raw.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        pass

    return {
        "suggested_title": _str(raw.get("suggested_title") or raw.get("improved_title", ""), 200),
        "description": _str(raw.get("description", ""), 5000),
        "risk_level": risk,
        "risk_measured": risk_measured,
        "risk_reason": _str(raw.get("risk_reason", ""), 300),
        "review_focus": review_focus,
        "confidence": confidence,
    }


# ── Issue Triage ──────────────────────────────────────────────────────────────


# What a triage model cannot know: it sees one issue's title and body, not
# the tracker, the releases or the team. "This is a duplicate of #42, which
# was fixed in v2.3.1 — upgrading resolves it. I've assigned @bob." was
# posted as the bot's own statement, and it was all invented.
_STATUS_CLAIM = re.compile(
    r"\bduplicat|\balready (?:been )?(?:fixed|resolved|reported|addressed|tracked)"
    r"|\b(?:fixed|resolved|addressed|patched) (?:in|by)\b|\bhas been (?:fixed|resolved)"
    r"|\bassign|\bi(?:'ve| have) (?:labell?ed|closed|linked|tagged)"
    r"|\bwill be (?:closed|fixed|released|merged)|\bupgrad(?:e|ing)\b.*\b(?:resolv|fix)"
    r"|\bknown (?:issue|bug)|#\d+",
    re.I,
)
_WELCOME_DEFAULT = "Thanks for reporting this!"


def _welcome(value) -> str:
    """The model's welcome without status claims it cannot know, and with
    its mentions made inert."""
    from app.core.sanitizer import quiet_mentions

    text = _str(value if value is not None else _WELCOME_DEFAULT, 500)
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [x for x in sentences if x and not _STATUS_CLAIM.search(x)]
    if len(kept) < len([x for x in sentences if x]):
        log.info("validator.welcome_claims_dropped")
    return quiet_mentions(" ".join(kept)) or _WELCOME_DEFAULT


def validate_issue_triage(raw: dict) -> dict:
    """Validate and sanitize issue triage response."""
    # These MUST stay in sync with the enums the triage prompt asks for in
    # app/handlers/issues.py. When they drifted, "critical" fell out of the
    # allow-list and every critical issue was silently relabelled "medium" —
    # which is why security issue #76 carries `priority: medium`.
    VALID_TYPES = {"bug", "feature", "question", "docs", "performance", "security", "refactor"}
    VALID_PRIORITIES = {"critical", "high", "medium", "low"}
    VALID_COMPLEXITY = {"trivial", "simple", "moderate", "complex", "epic"}
    VALID_ESTIMATES = {"< 1 hour", "1-4 hours", "1-3 days", "1-2 weeks", "> 2 weeks"}

    if is_unusable(raw):
        log.warning(f"validate_issue_triage: unusable payload — {str(raw)[:120]}")
        return {
            "type": "question",
            "priority": "medium",
            "labels": [],
            "welcome": "Thanks for reporting this! We'll look into it.",
            "needs_info": False,
            "questions": [],
            "complexity": "moderate",
            "time_estimate": "",
            "_degraded": True,
        }

    # None, not a default, for a field the model did not answer validly. The
    # defaults were rendered as its judgement: "Login returns 500 for all
    # users — prod is down", answered with a welcome line only, was posted as
    # Type: Question, Priority: Medium and labelled `priority: medium`.
    issue_type = _word(raw, "type", "")
    if issue_type not in VALID_TYPES:
        issue_type = None

    priority = _word(raw, "priority", "")
    if priority in ("urgent", "blocker", "severe"):
        priority = "critical"
    if priority not in VALID_PRIORITIES:
        priority = None

    complexity = _word(raw, "complexity", "")
    if complexity not in VALID_COMPLEXITY:
        complexity = None

    questions = raw.get("questions", [])
    if not isinstance(questions, list):
        questions = []
    from app.core.sanitizer import quiet_mentions

    questions = [quiet_mentions(str(q)[:200]) for q in questions if q][:3]

    time_estimate = _str(raw.get("time_estimate", ""), 20)
    if time_estimate not in VALID_ESTIMATES:
        time_estimate = ""

    return {
        "type": issue_type,
        "priority": priority,
        "labels": _list_of_str(raw.get("labels"), max_items=8, max_item_len=50),
        "welcome": _welcome(raw.get("welcome")),
        "needs_info": bool(flag(raw, "needs_info")),
        "questions": questions,
        "complexity": complexity,
        "time_estimate": time_estimate,
    }


# ── Code Review ───────────────────────────────────────────────────────────────


def validate_code_review(raw: dict) -> dict:
    """Validate code review for a single file."""
    if is_unusable(raw):
        log.warning(f"validate_code_review: unusable payload — {str(raw)[:120]}")
        return {
            "score": None,
            "summary": "",
            "issues": [],
            "confidence": 0.0,
            "_degraded": True,
        }

    # Score: float 0-10, or None when the model gave none. This defaulted to
    # 7.0 — "acceptable quality" — so a review with no score at all rendered
    # as 7/10: the fabricated-result bug is_unusable() exists to prevent.
    score = None
    try:
        if raw.get("score") is not None:
            score = max(0.0, min(10.0, float(raw["score"])))
    except (TypeError, ValueError):
        score = None

    # Issues: list of dicts with severity + issue + fix
    VALID_SEVERITIES = {"critical", "major", "minor", "nit"}
    raw_issues = raw.get("issues", [])
    if not isinstance(raw_issues, list):
        raw_issues = []

    # Every finding is validated and the list is ordered worst first BEFORE
    # anything is cut. This took the first ten in the model's order, so a
    # critical finding listed eleventh was dropped — and not counted in the
    # "N less severe finding(s) not shown" note, which is computed later.
    clean_issues = []
    for item in raw_issues[:50]:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "minor")).strip().lower()
        # The model's own scale, mapped rather than flattened: "high" fell to
        # the minor default, under findings it had ranked below it.
        sev = _SEVERITY_SYNONYMS.get(sev, sev)
        if sev not in VALID_SEVERITIES:
            log.info(f"validator.unknown_severity value={sev[:20]!r} — treated as minor")
            sev = "minor"
        clean_issues.append(
            {
                "severity": sev,
                "line": _str(item.get("line", ""), 20),
                # The quoted line, which grounding.ground_finding() checks
                # against the diff. Read there, so not a dead field.
                "code": _str(item.get("code", ""), 300),
                "issue": _str(item.get("issue", ""), 300),
                "fix": _str(item.get("fix", ""), 500),
            }
        )
    _rank = {"critical": 0, "major": 1, "minor": 2, "nit": 3}
    clean_issues.sort(key=lambda i: _rank[i["severity"]])

    confidence = 0.5
    try:
        confidence = float(raw.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        pass

    # The model's overall assessment. "verdict" is still accepted as an INPUT
    # alias because older prompts emitted that name, but it is not returned:
    # the duplicate output field was justified by a comment claiming
    # app/mcp/handlers.py and evals/ read it, and neither ever did.
    assessment = _str(raw.get("summary") or raw.get("verdict", ""), 200)

    return {
        "score": score,
        "summary": assessment,
        "issues": clean_issues,
        "confidence": confidence,
    }


# ── Example values copied from the prompt ─────────────────────────────────────
#
# Every JSON prompt shows the model the shape it wants, with placeholder
# values: "risk_reason": "why this risk level", "review_focus": ["area1",
# "area2"], "description": "structured PR description with ## Summary, …".
# Models copy them. The confidence gate scored such an answer around 0.9 —
# the fields were long and fluent — and the placeholder description was
# written into the author's PR. Any answer field that equals the prompt's own
# example for that key is removed, so handlers see it as missing ("not
# assessed") rather than publishing it.

_EXAMPLE_STR = re.compile(r'"(\w+)"\s*:\s*"([^"\n]{4,300})"')
_EXAMPLE_LIST = re.compile(r'"(\w+)"\s*:\s*\[([^\]\n]{1,300})\]')


def _canon(value) -> str:
    return " ".join(str(value).lower().split()).strip(" .,:;")


def prompt_examples(prompt: str) -> dict[str, set[str]]:
    """{key: {example values}} for the JSON shape shown in `prompt`."""
    out: dict[str, set[str]] = {}
    for key, value in _EXAMPLE_STR.findall(prompt or ""):
        out.setdefault(key, set()).add(_canon(value))
    for key, items in _EXAMPLE_LIST.findall(prompt or ""):
        for item in re.findall(r'"([^"\n]{2,120})"', items):
            out.setdefault(key, set()).add(_canon(item))
    return out


def strip_copied_examples(answer, prompt: str):
    """
    `answer` with every field equal to the prompt's example for that key
    removed (recursively; list items too). Returns (answer, removed_keys).
    """
    examples = prompt_examples(prompt)
    removed: list[str] = []
    if not examples:
        return answer, removed

    def clean(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                ex = examples.get(k)
                if ex and isinstance(v, str) and _canon(v) in ex:
                    removed.append(k)
                    continue
                if ex and isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                    kept = [x for x in v if _canon(x) not in ex]
                    if len(kept) != len(v):
                        removed.append(k)
                    if not kept:
                        continue
                    v = kept
                out[k] = clean(v)
            return out
        if isinstance(node, list):
            return [clean(x) for x in node]
        return node

    return clean(answer), removed
