"""
analysis_expanded.py — Derive the extended study's reported figures from the
raw results of the three extended harnesses. Pure: reads results/, scans
nothing, and is deterministic.

Boundary and encoding figures are computed over LABEL-MATCHED types only: a
type a tool "covers" under another provider's rule (the subject's PagerDuty
rule on 84 other providers' credentials) is excluded, because its behaviour
under mutation is that other rule's, not the tool's handling of the type.

Run: python analysis_expanded.py > results/expanded_summary.json
"""
from __future__ import annotations

import json
import os
from collections import Counter

_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RES = os.path.join(_ROOT, "results")
TOOLS = ("autopilot", "gitleaks", "trufflehog")

# Contexts reported in the manuscript: the paper's battery, the two
# extensions, and the encoding families.
BATTERY = ("single_quoted", "unquoted", "yaml_value", "json_value", "env_file",
           "url_query", "trailing_comma", "in_parens", "leading_space",
           "trailing_hyphen", "trailing_under")
EXTENSION = ("url_query_amp", "in_parens_spaced", "trailing_comma_spaced")


def _load(name):
    with open(os.path.join(RES, name)) as fh:
        return json.load(fh)


def context_table(per_type, tool, contexts, stratum=None):
    """Per context: label-matched types where the context applies, and how
    many of them the tool misses entirely (paired retention 0)."""
    out = {}
    for ctx in contexts:
        rows = [v[tool]["cells"][ctx]["paired_retention"]
                for v in per_type.values()
                if v[tool]["label_matched"]
                and (stratum is None or v["stratum"] == stratum)
                and ctx in v[tool]["cells"]
                and v[tool]["cells"][ctx]["paired_retention"] is not None]
        if rows:
            out[ctx] = {"types": len(rows), "total_failure": sum(r == 0.0 for r in rows)}
    return out


def hyphen_marginal_label_matched(e1):
    """The harness reports the marginal over every covered type; restrict it
    to label-matched types, like every other figure here."""
    pt, per = e1["per_type"], e1["hyphen_marginal_probability"]["per_type"]
    out = {}
    for tool in TOOLS:
        vals = [r[tool] for t, r in per.items()
                if r[tool] is not None and pt[t][tool]["label_matched"]]
        out[tool] = {
            "types": len(vals),
            "types_marginal_at_least_1_in_100": sum(v >= 0.01 for v in vals),
            "types_marginal_nonzero": sum(v > 0 for v in vals),
            "max_marginal": max(vals) if vals else None,
            "mean_marginal": round(sum(vals) / len(vals), 5) if vals else None,
        }
    return out


def main():
    e1 = _load("threetool_expanded_bench.json")
    gl = _load("gitleaks_fulltable_bench.json")
    e2 = _load("realworld_multilang_bench.json")
    pt = e1["per_type"]
    enc = sorted(e1["encoding_families"])

    label_matched = {t: sum(v[t]["label_matched"] for v in pt.values()) for t in TOOLS}
    by_stratum = {t: dict(Counter(v["stratum"] for v in pt.values() if v[t]["label_matched"]))
                  for t in TOOLS}

    pb = e2["part_b_credential_shaped_literals"]
    pc = e2["part_c_credential_shaped_tokens"]
    unstable = sum(p["tools"]["trufflehog"].get("unstable_across_runs", 0)
                   for p in e2["projects"].values())

    return {
        "design": __doc__.strip().splitlines()[0],
        "gitleaks_fulltable": {
            "n_rules": gl["n_rules"],
            "strata": {s: {k: v[k] for k in (
                "n_rules", "n_excluded", "n_analysed", "terminator_x_paren_failure",
                "paren_failures_rescued_by_one_space")} for s, v in gl["summary"].items()},
            "huggingface_unquoted_newline": {
                r: gl["per_rule"][r]["fragile_contexts"]
                for r in ("huggingface-access-token", "huggingface-organization-api-token")},
        },
        "three_tool": {
            "n_files": e1["n_files"],
            "types_generated": e1["types_generated"],
            "n_types": sum(e1["types_generated"].values()),
            "n_types_excluded": len(e1["types_excluded"]),
            "trufflehog_detectors_not_single_pattern": e1["trufflehog_detectors_not_single_pattern"],
            "coverage_any_rule": e1["coverage"],
            "coverage_label_matched": e1["coverage_label_matched"],
            "label_matched_total": label_matched,
            "label_matched_by_stratum": by_stratum,
            "mislabelled_coverage_by_rule": e1["mislabelled_coverage_by_rule"],
            "paired_pooled": e1["paired_coverage_pooled"],
            "paired_neutral": e1["paired_coverage_neutral"],
            "battery_label_matched": {t: context_table(pt, t, BATTERY + EXTENSION) for t in TOOLS},
            "encoding_label_matched": {
                t: {st: context_table(pt, t, enc, st) for st in ("prefix", "keyword")}
                for t in TOOLS},
            "hyphen_marginal": hyphen_marginal_label_matched(e1),
        },
        "realworld": {
            "n_projects": e2["n_projects"],
            "n_languages": e2["n_languages"],
            "n_files": e2["n_files"],
            "n_lines": e2["n_lines"],
            "totals": e2["totals"],
            "totals_by_stratum": e2["totals_by_stratum"],
            "totals_by_language": e2["totals_by_language"],
            "trufflehog_unstable_findings": unstable,
            "part_b": {k: pb[k] for k in (
                "n_occurrences", "n_types_occurring", "in_test_or_doc_path",
                "following_character", "detection_overall", "detection_by_path")},
            "part_c": {
                "definition": pc["definition"],
                "pooled": {k: pc["overall"][k] for k in (
                    "n_tokens", "share_followed_by_char_gitleaks_terminator_rejects")},
                "other_path": pc["by_path"]["other_path"],
                "test_doc_or_fixture_path": {k: pc["by_path"]["test_doc_or_fixture_path"][k]
                                             for k in ("n_tokens",
                                                       "share_followed_by_char_gitleaks_terminator_rejects")},
                "repository_weighted": pc["repository_weighted"],
                "by_language": {lg: {k: v[k] for k in (
                    "n_tokens", "share_followed_by_char_gitleaks_terminator_rejects")}
                    for lg, v in pc["by_language"].items()},
            },
        },
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
