"""
bench_mutation.py — Systematic boundary-mutation benchmark over every
credential rule in the scanner's table, measured at two levels.

METHOD
  For each credential rule, generate credentials DIRECTLY FROM that rule's own
  regular expression (via exrex), so the corpus is derived from the artifact
  under test rather than from the experimenter's guesses. Then embed each
  credential in a battery of boundary contexts -- each a way a real credential
  legitimately appears in a real repository -- and record which detector fired.

  IMPORTANT HARNESS CONTROL: the surrounding identifier is deliberately
  NEUTRAL (`cfg_value`, `param`, ...). An identifier such as `SECRET` or
  `api_key` independently triggers the scanner's generic keyword rules
  ("Generic Token", "Generic API Key"), which would mask the failure of the
  specific rule under test and make every rule appear perfectly robust. An
  earlier version of this harness used `SECRET =` and measured a uniform 100%
  rule-match rate for exactly that reason; the neutral identifier isolates the
  rule actually being measured.

  Every (rule x context x sample) outcome is classified as:
    RULE_OK     the credential's OWN named rule matched  (correct label+severity)
    OTHER_RULE  a different NAMED rule matched           (different severity)
    FALLBACK    only the unanchored high-entropy detector fired
                -> reported as "High Entropy String (unclassified)", MEDIUM
                   severity / MEDIUM confidence, where the named rule would
                   have reported CRITICAL / HIGH
    MISSED      nothing reported at all                  (silent false negative)

Run: python bench_mutation.py > results/mutation_bench.json
"""
from __future__ import annotations

import json
import logging
import re
import os
import random
import sys
import time

SUBJECT_REPO_PATH = os.environ.get(
    "SUBJECT_REPO", os.path.expanduser("~/github-autopilot"))
sys.path.insert(0, SUBJECT_REPO_PATH)

import exrex  # noqa: E402

from app.security.enhanced_secrets import PATTERNS, scan_diff  # noqa: E402

logging.disable(logging.CRITICAL)

N_SAMPLES = 120
SEED = 20260823
SCANNED_FILE = "app/settings.py"
FALLBACK_NAME = "High Entropy String (unclassified)"

STRUCTURAL_PATTERNS = {
    "RSA Private Key", "EC Private Key", "Generic Private Key",
    "PGP Private Key", "Connection String", "Slack Webhook",
    "Azure Storage Key",
}

# Rules that are themselves keyword-anchored (they REQUIRE a keyword such as
# "aws...secret" or "password=" adjacent to the value). A neutral identifier
# would make them unmatchable by construction, so the neutral-identifier
# control cannot be applied to them and they are reported separately.
KEYWORD_ANCHORED = {
    "AWS Secret Access Key", "AWS Session Token", "Firebase API Key",
    "Azure Client Secret", "Twilio Auth Token", "Cloudflare API Token",
    "Docker Hub PAT", "Heroku API Key", "Datadog API Key",
    "Generic API Key", "Generic Password", "Generic Token",
}


def gen_value(pattern: str) -> str:
    core = re.sub(r"^\\b", "", pattern)
    core = re.sub(r"\\b$", "", core)
    core = core.replace("(?i)", "")
    return exrex.getone(core, limit=8)


def final_class_body(pattern: str) -> str | None:
    m = re.search(r"\[([^\]]+)\]\s*(?:\{[^}]*\}|[+*])?\s*(?:\\b)?$", pattern)
    return m.group(1) if m else None


def charclass_allows(pattern: str, ch: str) -> bool:
    body = final_class_body(pattern)
    if body is None:
        return False
    if ch == "-":
        return bool(re.search(r"\\-", body) or re.search(r"(^-|-$)", body))
    return ch in body.replace("\\", "")


# Neutral identifiers only -- see HARNESS CONTROL note above.
CONTEXT_MUTATIONS = {
    "canonical":      lambda v: f'+cfg_value = "{v}"',
    "single_quoted":  lambda v: f"+cfg_value = '{v}'",
    "unquoted":       lambda v: f"+cfg_value = {v}",
    "yaml_value":     lambda v: f"+  cfg_value: {v}",
    "json_value":     lambda v: f'+  "cfg_value": "{v}",',
    "env_file":       lambda v: f"+CFG_VALUE={v}",
    "url_query":      lambda v: f'+endpoint = "https://api.example.com/v1?p={v}"',
    "trailing_comma": lambda v: f'+values = ["{v}", "next"]',
    "in_parens":      lambda v: f"+client = Client({v})",
    "leading_space":  lambda v: f'+cfg_value =    "{v}"',
}

FINAL_CHAR_MUTATIONS = {"trailing_hyphen": "-", "trailing_under": "_"}


def classify(diff_line: str, rule_name: str) -> str:
    findings = scan_diff(diff_line, file_path=SCANNED_FILE)
    if not findings:
        return "MISSED"
    names = {f.pattern_name for f in findings}
    if rule_name in names:
        return "RULE_OK"
    if names - {FALLBACK_NAME}:
        return "OTHER_RULE"
    return "FALLBACK"


OUTCOMES = ("RULE_OK", "OTHER_RULE", "FALLBACK", "MISSED")


def run_cell(pattern, rule_name, render, force_char):
    counts = dict.fromkeys(OUTCOMES, 0)
    for _ in range(N_SAMPLES):
        try:
            v = gen_value(pattern)
        except Exception:
            continue
        if force_char is not None:
            v = v[:-1] + force_char
        counts[classify(render(v), rule_name)] += 1
    n = sum(counts.values()) or 1
    return {
        "n": n,
        "rule_match_rate": round(counts["RULE_OK"] / n, 4),
        "other_rule_rate": round(counts["OTHER_RULE"] / n, 4),
        "fallback_rate": round(counts["FALLBACK"] / n, 4),
        "missed_rate": round(counts["MISSED"] / n, 4),
        "severity_correct_rate": round(counts["RULE_OK"] / n, 4),
        "counts": counts,
    }


def main():
    random.seed(SEED)
    per_pattern, agg = {}, {}
    for m in list(CONTEXT_MUTATIONS) + list(FINAL_CHAR_MUTATIONS):
        agg[m] = dict.fromkeys(OUTCOMES, 0) | {"n": 0}

    t0 = time.perf_counter()

    for name, pattern, severity, entropy_gated in PATTERNS:
        if name in STRUCTURAL_PATTERNS or name in KEYWORD_ANCHORED:
            continue
        try:
            if not gen_value(pattern):
                continue
        except Exception as e:
            per_pattern[name] = {"error": str(e)}
            continue

        cells = {}
        for mut, render in CONTEXT_MUTATIONS.items():
            c = run_cell(pattern, name, render, None)
            cells[mut] = c
            for k in OUTCOMES:
                agg[mut][k] += c["counts"][k]
            agg[mut]["n"] += c["n"]

        for mut, ch in FINAL_CHAR_MUTATIONS.items():
            if not charclass_allows(pattern, ch):
                cells[mut] = None
                continue
            c = run_cell(pattern, name, lambda v: f'+cfg_value = "{v}"', ch)
            cells[mut] = c
            for k in OUTCOMES:
                agg[mut][k] += c["counts"][k]
            agg[mut]["n"] += c["n"]

        applicable = {k: v for k, v in cells.items() if v is not None}
        canon = cells["canonical"]["rule_match_rate"]
        fragile = {
            k: {"rule_match_rate": v["rule_match_rate"],
                "fallback_rate": v["fallback_rate"],
                "missed_rate": v["missed_rate"]}
            for k, v in applicable.items()
            if v["rule_match_rate"] < canon - 0.10
        }
        per_pattern[name] = {
            "severity": severity,
            "entropy_gated": entropy_gated,
            "ends_with_word_boundary": pattern.endswith(r"\b"),
            "final_char_class": final_class_body(pattern),
            "canonical_rule_match_rate": canon,
            "cells": {k: (None if v is None else {
                "rule_match_rate": v["rule_match_rate"],
                "other_rule_rate": v["other_rule_rate"],
                "fallback_rate": v["fallback_rate"],
                "missed_rate": v["missed_rate"],
            }) for k, v in cells.items()},
            "boundary_fragile_contexts": fragile,
            "is_boundary_fragile": bool(fragile),
            "worst_rule_match_rate": min(v["rule_match_rate"] for v in applicable.values()),
            "worst_missed_rate": max(v["missed_rate"] for v in applicable.values()),
        }

    elapsed = time.perf_counter() - t0

    agg_out = {}
    for m, d in agg.items():
        n = d["n"] or 1
        agg_out[m] = {
            "n": d["n"],
            "rule_match_rate": round(d["RULE_OK"] / n, 4),
            "other_rule_rate": round(d["OTHER_RULE"] / n, 4),
            "fallback_rate": round(d["FALLBACK"] / n, 4),
            "missed_rate": round(d["MISSED"] / n, 4),
        }

    fragile = {k: v for k, v in per_pattern.items()
               if isinstance(v, dict) and v.get("is_boundary_fragile")}

    return {
        "n_samples_per_cell": N_SAMPLES,
        "n_patterns_tested": len([v for v in per_pattern.values() if "error" not in v]),
        "n_patterns_total": len(PATTERNS),
        "n_structural_excluded": len(STRUCTURAL_PATTERNS),
        "n_keyword_anchored_excluded": len(KEYWORD_ANCHORED),
        "elapsed_s": round(elapsed, 2),
        "aggregate_by_mutation": agg_out,
        "n_boundary_fragile_patterns": len(fragile),
        "boundary_fragile_patterns": {
            k: {
                "severity": v["severity"],
                "canonical_rule_match_rate": v["canonical_rule_match_rate"],
                "fragile_contexts": v["boundary_fragile_contexts"],
                "worst_missed_rate": v["worst_missed_rate"],
                "final_char_class": v["final_char_class"],
            } for k, v in fragile.items()
        },
        "per_pattern": per_pattern,
        "seed": SEED,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
