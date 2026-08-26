"""
bench_crosstool.py — Boundary-mutation testing applied to THREE independent
scanners, to test whether the method generalises beyond the scanner in which
the defect was originally found.

WHY THIS EXPERIMENT
  The method was developed on one scanner and found a defect there. That
  establishes the method works once. It does not establish that the method
  TRANSFERS, nor whether the defect class is scanner-specific. Both are
  empirical questions, and both are answered here by running the identical
  mutation battery against Gitleaks 8.21.2 and TruffleHog 3.82.13.

RULE-LEVEL MEASUREMENT FOR EXTERNAL TOOLS
  Both external tools report which rule fired -- Gitleaks emits `RuleID`,
  TruffleHog emits `DetectorName` -- so the same two-level analysis used for
  the in-process scanner applies unchanged:

    RULE_OK   the expected rule for this credential type fired
    OTHER     a different named rule fired (label/severity differs)
    MISSED    nothing reported

  The EXPECTED rule for each credential type is not hard-coded. It is
  determined EMPIRICALLY from the canonical context: whichever rule the tool
  itself fires on a canonically-formatted credential of that type is taken as
  that tool's intended rule for it. This avoids encoding the experimenter's
  assumptions about another project's rule naming, and it means a tool is only
  ever judged against its own behaviour.

  Credential types for which a tool fires nothing even canonically are
  excluded from that tool's boundary analysis (the tool does not claim to
  detect that shape under these conditions), and reported separately.

Run: python bench_crosstool.py > results/crosstool_bench.json
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets as _secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time

SUBJECT_REPO_PATH = os.environ.get(
    "SUBJECT_REPO", os.path.expanduser("~/github-autopilot"))
sys.path.insert(0, SUBJECT_REPO_PATH)
from app.security.enhanced_secrets import scan_diff  # noqa: E402

logging.disable(logging.CRITICAL)

# Package root, derived from this file's location so the package is
# portable. Override with BMT_ROOT if you relocate results/ elsewhere.
_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRATCH = _ROOT
GITLEAKS = os.path.join(SCRATCH, "gitleaks")
TRUFFLEHOG = os.path.join(SCRATCH, "trufflehog")

ALNUM = string.ascii_letters + string.digits
UPPER_NUM = string.ascii_uppercase + string.digits
FALLBACK_NAME = "High Entropy String (unclassified)"

N_SAMPLES = 40


def r(a, n):
    return "".join(_secrets.choice(a) for _ in range(n))


def b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def realistic_jwt(force_last=None):
    h = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    p = b64url(json.dumps({"sub": r(string.digits, 10), "name": "Service Account",
                           "iat": 1716239022, "exp": 1716242622},
                          separators=(",", ":")).encode())
    s = b64url(_secrets.token_bytes(32))
    if force_last:
        s = s[:-1] + force_last
    return f"{h}.{p}.{s}"


def aws_key(force_last=None):
    while True:
        k = r(UPPER_NUM, 16)
        if not re.search(r"(.)\1{3,}", k):
            break
    v = "AKIA" + k
    return (v[:-1] + force_last) if force_last else v


def _tail(v, force_last=None):
    """Force the final character, used by the boundary mutations."""
    return (v[:-1] + force_last) if force_last else v


def _mk(prefix, alphabet, n):
    def gen(force_last=None):
        v = prefix + r(alphabet, n)
        return (v[:-1] + force_last) if force_last else v
    return gen


# Credential types, with the character sets their published formats allow.
# `hyphen_ok` marks types whose format permits a hyphen in the final position,
# i.e. the types for which the trailing-hyphen mutation is meaningful.
TYPES = {
    "github_pat":         {"gen": _mk("ghp_", ALNUM, 36), "hyphen_ok": False, "underscore_ok": False},
    "github_oauth":       {"gen": _mk("gho_", ALNUM, 36), "hyphen_ok": False, "underscore_ok": False},
    "stripe_secret_key":  {"gen": _mk("sk_live_", ALNUM, 24), "hyphen_ok": False, "underscore_ok": False},
    "npm_token":          {"gen": _mk("npm_", ALNUM, 36), "hyphen_ok": False, "underscore_ok": False},
    "slack_bot_token":    {"gen": lambda force_last=None: _tail(
        f"xoxb-{r(string.digits,11)}-{r(string.digits,12)}-{r(ALNUM,24)}",
        force_last), "hyphen_ok": False, "underscore_ok": False},
    "aws_access_key_id":  {"gen": aws_key, "hyphen_ok": False, "underscore_ok": False},
    "gcp_api_key":        {"gen": _mk("AIza", ALNUM + "_-", 35), "hyphen_ok": True, "underscore_ok": True},
    "sendgrid_api_key":   {"gen": lambda force_last=None: _tail(
        f"SG.{r(ALNUM+'_-',22)}.{r(ALNUM+'_-',43)}", force_last),
        "hyphen_ok": True, "underscore_ok": True},
    "openai_project_key": {"gen": _mk("sk-proj-", ALNUM + "_-", 52), "hyphen_ok": True, "underscore_ok": True},
    "jwt":                {"gen": realistic_jwt, "hyphen_ok": True, "underscore_ok": True},
}

CONTEXTS = {
    "canonical":      lambda v: f'cfg_value = "{v}"',
    "single_quoted":  lambda v: f"cfg_value = '{v}'",
    "unquoted":       lambda v: f"cfg_value = {v}",
    "yaml_value":     lambda v: f"cfg_value: {v}",
    "json_value":     lambda v: f'{{"cfg_value": "{v}"}}',
    "env_file":       lambda v: f"CFG_VALUE={v}",
    "url_query":      lambda v: f'endpoint = "https://api.example.com/v1?p={v}"',
    "trailing_comma": lambda v: f'values = ["{v}", "next"]',
    "in_parens":      lambda v: f"client = Client({v})",
    "leading_space":  lambda v: f'cfg_value =    "{v}"',
}

FINAL_CHAR = {"trailing_hyphen": "-", "trailing_under": "_"}


def mutation_applies(spec, mut):
    """A forced final character is only meaningful when the credential's own
    published format permits that character in the final position. Forcing an
    underscore onto a format whose alphabet excludes it produces a string that
    is no longer a credential of that type, so a miss there is correct
    behaviour rather than a defect."""
    if mut == "trailing_hyphen":
        return spec["hyphen_ok"]
    if mut == "trailing_under":
        return spec["underscore_ok"]
    return True


# ── corpus construction ─────────────────────────────────────────────────────
def build(workdir):
    """One file per sample. Returns id -> (type, context)."""
    meta, idx = {}, 0
    for tname, spec in TYPES.items():
        for ctx in CONTEXTS:
            for _ in range(N_SAMPLES):
                idx += 1
                sid = f"s{idx:06d}"
                v = spec["gen"]()
                open(os.path.join(workdir, f"{sid}.txt"), "w").write(
                    CONTEXTS[ctx](v) + "\n")
                meta[sid] = (tname, ctx)
        for mut, ch in FINAL_CHAR.items():
            if not mutation_applies(spec, mut):
                continue
            for _ in range(N_SAMPLES):
                idx += 1
                sid = f"s{idx:06d}"
                v = spec["gen"](force_last=ch)
                open(os.path.join(workdir, f"{sid}.txt"), "w").write(
                    CONTEXTS["canonical"](v) + "\n")
                meta[sid] = (tname, mut)
    return meta


# ── per-tool scanning, returning sid -> set(rule names) ─────────────────────
def scan_autopilot(workdir, meta):
    out = {}
    t0 = time.perf_counter()
    for sid in meta:
        line = open(os.path.join(workdir, f"{sid}.txt")).read().rstrip("\n")
        f = scan_diff("+" + line + "\n", file_path="app/settings.py")
        out[sid] = {x.pattern_name for x in f}
    return out, time.perf_counter() - t0


def scan_gitleaks(workdir, meta):
    rep = os.path.join(workdir, "_gl.json")
    t0 = time.perf_counter()
    subprocess.run([GITLEAKS, "detect", "--source", workdir, "--no-git",
                    "--report-format", "json", "--report-path", rep],
                   capture_output=True, timeout=3600)
    el = time.perf_counter() - t0
    out = {sid: set() for sid in meta}
    try:
        for f in json.load(open(rep)) or []:
            m = re.match(r"(s\d{6})\.txt", os.path.basename(f.get("File", "")))
            if m and m.group(1) in out:
                out[m.group(1)].add(f.get("RuleID", "?"))
    except Exception:
        pass
    return out, el


def scan_trufflehog(workdir, meta):
    t0 = time.perf_counter()
    p = subprocess.run([TRUFFLEHOG, "filesystem", workdir, "--json",
                        "--no-verification", "--no-update"],
                       capture_output=True, timeout=7200, text=True)
    el = time.perf_counter() - t0
    out = {sid: set() for sid in meta}
    for line in p.stdout.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if "DetectorName" not in rec:
            continue
        fp = (rec.get("SourceMetadata", {}).get("Data", {})
                 .get("Filesystem", {}).get("file", ""))
        m = re.match(r"(s\d{6})\.txt", os.path.basename(fp))
        if m and m.group(1) in out:
            out[m.group(1)].add(rec["DetectorName"])
    return out, el


# ── analysis ────────────────────────────────────────────────────────────────
def analyse(tool, findings, meta, fallback_names=frozenset()):
    """Determine the expected rule per type from the canonical context, then
    classify every cell as RULE_OK / OTHER / FALLBACK / MISSED."""
    # empirical expected rule: most common non-fallback rule in canonical ctx
    expected = {}
    for tname in TYPES:
        counts = {}
        for sid, (t, ctx) in meta.items():
            if t != tname or ctx != "canonical":
                continue
            for rule in findings[sid] - set(fallback_names):
                counts[rule] = counts.get(rule, 0) + 1
        expected[tname] = max(counts, key=counts.get) if counts else None

    # A tool is only meaningfully measured on types it detects at all. Types
    # it never fires on are excluded from the aggregate (and reported
    # separately) so that coverage gaps are not scored as boundary fragility.
    canonical_ok = set()
    for tname in TYPES:
        sids = [s for s, (t, c) in meta.items() if t == tname and c == "canonical"]
        exp0 = expected[tname]
        if exp0 and sids and sum(
                1 for s in sids if exp0 in findings[s]) / len(sids) >= 0.5:
            canonical_ok.add(tname)

    per_type, agg = {}, {}
    for tname in TYPES:
        exp = expected[tname]
        cells = {}
        ctxs = list(CONTEXTS) + [m for m in FINAL_CHAR
                                 if mutation_applies(TYPES[tname], m)]
        for ctx in ctxs:
            sids = [s for s, (t, c) in meta.items() if t == tname and c == ctx]
            if not sids:
                continue
            c = {"RULE_OK": 0, "OTHER": 0, "FALLBACK": 0, "MISSED": 0}
            for s in sids:
                names = findings[s]
                if not names:
                    c["MISSED"] += 1
                elif exp and exp in names:
                    c["RULE_OK"] += 1
                elif names - set(fallback_names):
                    c["OTHER"] += 1
                else:
                    c["FALLBACK"] += 1
            n = len(sids)
            cells[ctx] = {
                "n": n,
                "rule_match_rate": round(c["RULE_OK"] / n, 4),
                "other_rule_rate": round(c["OTHER"] / n, 4),
                "fallback_rate": round(c["FALLBACK"] / n, 4),
                "missed_rate": round(c["MISSED"] / n, 4),
            }
            if tname in canonical_ok:
                a = agg.setdefault(ctx, {"RULE_OK": 0, "OTHER": 0,
                                         "FALLBACK": 0, "MISSED": 0, "n": 0})
                for k in ("RULE_OK", "OTHER", "FALLBACK", "MISSED"):
                    a[k] += c[k]
                a["n"] += n

        canon = cells.get("canonical", {}).get("rule_match_rate", 0.0)
        detected_canonically = canon >= 0.5
        fragile = {k: v["rule_match_rate"] for k, v in cells.items()
                   if v["rule_match_rate"] < canon - 0.10} if detected_canonically else {}
        # Boundary robustness BR(R): min rule-match rate over applicable contexts
        br = min((v["rule_match_rate"] for v in cells.values()), default=None)
        per_type[tname] = {
            "expected_rule": exp,
            "detected_canonically": detected_canonically,
            "canonical_rule_match_rate": canon,
            "boundary_robustness_BR": round(br, 4) if br is not None else None,
            "cells": cells,
            "boundary_fragile_contexts": fragile,
            "is_boundary_fragile": bool(fragile),
        }

    agg_out = {}
    for ctx, a in agg.items():
        n = a["n"] or 1
        agg_out[ctx] = {
            "n": a["n"],
            "rule_match_rate": round(a["RULE_OK"] / n, 4),
            "other_rule_rate": round(a["OTHER"] / n, 4),
            "fallback_rate": round(a["FALLBACK"] / n, 4),
            "missed_rate": round(a["MISSED"] / n, 4),
        }

    covered = sorted(canonical_ok)
    fragile_types = [t for t in covered if per_type[t]["is_boundary_fragile"]]
    return {
        "tool": tool,
        "types_detected_canonically": covered,
        "types_not_detected_canonically": [t for t in TYPES if t not in covered],
        "n_types_analysed": len(covered),
        "n_boundary_fragile": len(fragile_types),
        "boundary_fragile_types": fragile_types,
        "aggregate_note": ("aggregated only over types this tool detects "
                            "canonically, and only over mutations the "
                            "credential format permits"),
        "aggregate_by_context": agg_out,
        "per_type": per_type,
    }


def main():
    wd = tempfile.mkdtemp(prefix="crosstool_")
    try:
        meta = build(wd)
        ap, ap_t = scan_autopilot(wd, meta)
        gl, gl_t = scan_gitleaks(wd, meta)
        th, th_t = scan_trufflehog(wd, meta)

        return {
            "design": (
                "Identical boundary-mutation battery applied to three scanners. "
                "The expected rule per credential type is determined empirically "
                "from each tool's own behaviour in the canonical context, so no "
                "tool is judged against another project's rule naming."),
            "n_samples_per_cell": N_SAMPLES,
            "n_files": len(meta),
            "n_types": len(TYPES),
            "n_contexts": len(CONTEXTS) + len(FINAL_CHAR),
            "scan_wall_s": {"autopilot": round(ap_t, 2),
                            "gitleaks": round(gl_t, 2),
                            "trufflehog": round(th_t, 2)},
            "results": {
                "GitHub Autopilot": analyse("GitHub Autopilot", ap, meta,
                                            {FALLBACK_NAME}),
                "Gitleaks 8.21.2": analyse("Gitleaks 8.21.2", gl, meta,
                                           {"generic-api-key"}),
                "TruffleHog 3.82.13": analyse("TruffleHog 3.82.13", th, meta, set()),
            },
            "seed_note": "credential material from secrets.SystemRandom (CSPRNG)",
        }
    finally:
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
