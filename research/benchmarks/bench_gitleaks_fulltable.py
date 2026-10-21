"""
bench_gitleaks_fulltable.py — Boundary-mutation testing over Gitleaks' FULL
rule table (195 rules at v8.21.2), with credentials generated from each
rule's own regular expression.

WHY THIS EXPERIMENT
  bench_crosstool.py applies the battery to ten credential types shared by
  three scanners, generated from the primary subject's rules. That supports a
  mechanism claim but not a prevalence claim: with ten types, nothing can be
  said about how much of a rule table a defect reaches. This harness removes
  both limits for Gitleaks: every rule in its table is tested, and every
  credential is drawn from the language of that rule's own secret group, so
  the expected rule is the generating rule itself and no freezing step is
  needed.

TWO STRATA
  PREFIX rules identify the credential by its own shape (ghp_..., AKIA...).
  They receive the identical twelve-embedding battery of bench_crosstool.py,
  with the neutral identifier cfg_value.

  KEYWORD rules match only when a provider keyword precedes the value
  (adafruit_value = "..."). A neutral identifier makes them unmatchable by
  construction, so the identifier is generated from the rule's own keyword
  group. Two embeddings of the PREFIX battery cannot carry a keyword in that
  position (a bare call argument and a list member); for this stratum they
  are replaced by the keyword-argument call Client(kw=v) and
  Client(kw=v, retries=3), which preserve the mechanism being exercised --
  the credential terminated by ')' and by ',' respectively. The two strata
  are therefore reported separately and never pooled.

SAMPLE VALIDITY
  A generated sample is kept only if Gitleaks' own post-match filters would
  accept it in the canonical context: Shannon entropy strictly above the
  rule's threshold, and no hit on the rule's or the global allowlist regexes
  and stopwords. Without this, a miss caused by a filter would be scored as a
  boundary failure. A forced final character is applied only when the
  mutated value is still in the language of the secret group and still
  passes the same filters, which is the paper's applicability condition.

Seeded (20260823) via the `random` module that exrex draws from.

Run: python bench_gitleaks_fulltable.py > results/gitleaks_fulltable_bench.json
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections import Counter

import exrex

SEED = 20260823
N_SAMPLES = 40
MAX_DRAWS = 400          # attempts per rule to find N_SAMPLES valid values
GENERIC = "generic-api-key"

_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GITLEAKS = os.environ.get("GITLEAKS", os.path.join(_ROOT, "gitleaks"))
GITLEAKS_TOML = os.environ.get("GITLEAKS_TOML", os.path.join(_ROOT, "gitleaks.toml"))

# Gitleaks' explicit terminator group, appended to most of its rules.
TERM = r"(?:['|\"|\n|\r|\s|\x60|;]|$)"
KW_PREFIX = re.compile(r"\[\\w\.-\]\{0,\d+\}\?")

PREFIX_CONTEXTS = {
    "canonical":      lambda k, v: f'{k} = "{v}"',
    "single_quoted":  lambda k, v: f"{k} = '{v}'",
    "unquoted":       lambda k, v: f"{k} = {v}",
    "yaml_value":     lambda k, v: f"{k}: {v}",
    "json_value":     lambda k, v: f'{{"{k}": "{v}"}}',
    "env_file":       lambda k, v: f"{k.upper()}={v}",
    "url_query":      lambda k, v: f'endpoint = "https://api.example.com/v1?p={v}"',
    "trailing_comma": lambda k, v: f'values = ["{v}", "next"]',
    "in_parens":      lambda k, v: f"client = Client({v})",
    "leading_space":  lambda k, v: f'{k} =    "{v}"',
}
KEYWORD_CONTEXTS = dict(PREFIX_CONTEXTS)
KEYWORD_CONTEXTS.update({
    "url_query":      lambda k, v: f'endpoint = "https://api.example.com/v1?{k}={v}"',
    "trailing_comma": lambda k, v: f"client = Client({k}={v}, retries=3)",
    "in_parens":      lambda k, v: f"client = Client({k}={v})",
})

# Beyond the paper's battery. url_query_amp is the query-string shape the
# paper names as the most consequential (a credential followed by '&'), which
# its battery did not itself contain. The two *_spaced contexts are minimal-
# pair controls: identical to in_parens / trailing_comma except for one space
# before the terminator. They are reported but excluded from BR, so that the
# battery's metrics stay comparable with the paper's.
EXTENSION_CONTEXTS = {
    "prefix": {
        "url_query_amp":     lambda k, v: f'endpoint = "https://api.example.com/v1?p={v}&next=1"',
        "in_parens_spaced":  lambda k, v: f"client = Client({v} )",
    },
    "keyword": {
        "url_query_amp":     lambda k, v: f'endpoint = "https://api.example.com/v1?{k}={v}&next=1"',
        "in_parens_spaced":  lambda k, v: f"client = Client({k}={v} )",
        "trailing_comma_spaced": lambda k, v: f"client = Client({k}={v} , retries=3)",
    },
}
CONTROLS = {"in_parens_spaced", "trailing_comma_spaced"}
NOT_IN_BR = CONTROLS | {"url_query_amp"}
FINAL_CHAR = {"trailing_hyphen": "-", "trailing_under": "_"}


# ── regex surgery ───────────────────────────────────────────────────────────
def _group_end(p, start):
    """Index one past the ')' closing the group opened at p[start]."""
    i, depth, incls = start, 0, False
    while i < len(p):
        ch = p[i]
        if ch == "\\":
            i += 2
            continue
        if incls:
            if ch == "]":
                incls = False
        elif ch == "[":
            incls = True
            if p[i + 1:i + 2] == "]":
                i += 1
            elif p[i + 1:i + 3] == "^]":
                i += 2
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced group")


def _first_capture(p):
    i, incls = 0, False
    while i < len(p):
        ch = p[i]
        if ch == "\\":
            i += 2
            continue
        if incls:
            if ch == "]":
                incls = False
        elif ch == "[":
            incls = True
        elif ch == "(" and (not p.startswith("(?", i) or p.startswith("(?P<", i)):
            return i, _group_end(p, i)
        i += 1
    return None


# Go's \w \d \s are ASCII-only; exrex draws Python's Unicode classes (it
# emitted 'ª', 'À', ... for \w and '\n' for \s), which produced strings
# outside the Go rule's language. \s is narrowed to space and tab so a
# generated value cannot contain a line break; that is still a subset of
# Go's \s, so every value remains in the rule's language.
_ASCII_CLASS = {"w": "A-Za-z0-9_", "d": "0-9", "s": " \\t"}


def _ascii_classes(p):
    out, i, incls = [], 0, False
    while i < len(p):
        ch = p[i]
        if ch == "\\" and i + 1 < len(p):
            esc = p[i + 1]
            if esc in _ASCII_CLASS:
                body = _ASCII_CLASS[esc]
                out.append(body if incls else f"[{body}]")
            else:
                out.append(p[i:i + 2])
            i += 2
            continue
        if incls and ch == "]":
            incls = False
        elif not incls and ch == "[":
            incls = True
            out.append(ch)
            i += 1
            if p[i:i + 1] == "^":
                out.append("^")
                i += 1
            if p[i:i + 1] == "]":          # literal ']' first in a class
                out.append("]")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _to_python(p):
    """Go inline flags that Python rejects mid-pattern. Generation only needs
    a member of the language, so dropping case-insensitivity is safe: every
    generated string is still accepted by the case-insensitive original."""
    return _ascii_classes(p.replace("(?i)", "").replace("(?-i:", "(?:"))


def _printable_ascii(s):
    return s.isascii() and s.isprintable()


def secret_pattern(rule):
    """The secret is the first capture group (no rule at v8.21.2 sets
    secretGroup), or the whole match when the rule has no group."""
    if "secretGroup" in rule:
        raise ValueError("secretGroup is not supported by this harness")
    p = rule["regex"]
    span = _first_capture(p)
    if span is None:
        return _to_python(p)
    inner = p[span[0] + 1:span[1] - 1]
    if inner.startswith("?P<"):
        inner = inner[inner.index(">") + 1:]
    return _to_python(inner)


def keyword_pattern(rule):
    m = KW_PREFIX.search(rule["regex"])
    if not m:
        return None
    start = m.end()
    end = _group_end(rule["regex"], start)
    return _to_python(rule["regex"][start:end])


# ── Gitleaks' own post-match filters ────────────────────────────────────────
def shannon(s):
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def _compile_all(patterns):
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(_to_python(p), re.I if "(?i)" in p else 0))
        except re.error:
            pass
    return out


class Filters:
    def __init__(self, cfg, rule):
        g = cfg.get("allowlist", {})
        a = rule.get("allowlist", {})
        self.entropy = rule.get("entropy", 0.0)
        # Gitleaks runs a rule only on fragments containing one of its
        # keywords (case-insensitively). An unescaped '.' in a rule such as
        # hooks.slack.com lets exrex emit a value without the literal keyword.
        self.keywords = [k.lower() for k in rule.get("keywords", [])]
        self.regexes = _compile_all(g.get("regexes")) + _compile_all(a.get("regexes"))
        self.stopwords = [w.lower() for w in g.get("stopwords", []) + a.get("stopwords", [])]

    def accepts(self, secret, line):
        if self.keywords and not any(k in line.lower() for k in self.keywords):
            return False
        if self.entropy and shannon(secret) <= self.entropy:
            return False
        low = secret.lower()
        if any(w in low for w in self.stopwords):
            return False
        return not any(r.search(secret) or r.search(line) for r in self.regexes)


# ── corpus construction ─────────────────────────────────────────────────────
def build(cfg, workdir):
    meta, rules_out, idx = {}, {}, 0
    for rule in cfg["rules"]:
        rid = rule["id"]
        kpat = keyword_pattern(rule)
        stratum = "keyword" if kpat else "prefix"
        contexts = KEYWORD_CONTEXTS if kpat else PREFIX_CONTEXTS
        info = {"stratum": stratum, "has_terminator_group": TERM in rule["regex"]}
        rules_out[rid] = info
        if "path" in rule:
            info["excluded"] = f"path-restricted rule ({rule['path']}); battery uses .txt files"
            continue
        try:
            spat = secret_pattern(rule)
            sre = re.compile(spat, re.ASCII)
            kre = re.compile(kpat) if kpat else None
        except (re.error, ValueError) as e:
            info["excluded"] = f"pattern not translatable: {e}"
            continue
        filt = Filters(cfg, rule)

        def ident():
            if not kre:
                return "cfg_value"
            for _ in range(50):
                k = exrex.getone(kpat, limit=4)
                if re.fullmatch(r"[A-Za-z0-9_.-]+", k):
                    return k
            return None

        samples, draws = [], 0
        while len(samples) < N_SAMPLES and draws < MAX_DRAWS:
            draws += 1
            v = exrex.getone(spat, limit=40)
            k = ident()
            if k is None or not _printable_ascii(v) or not sre.fullmatch(v):
                continue
            if filt.accepts(v, PREFIX_CONTEXTS["canonical"](k, v)):
                samples.append((k, v))
        info["n_valid_samples"] = len(samples)
        if len(samples) < N_SAMPLES:
            info["excluded"] = f"only {len(samples)} filter-valid samples in {MAX_DRAWS} draws"
            continue

        applicable = []
        for mut, ch in FINAL_CHAR.items():
            ok = all(sre.fullmatch(v[:-1] + ch) and
                     filt.accepts(v[:-1] + ch, f'{k} = "{v[:-1] + ch}"')
                     for k, v in samples)
            if ok:
                applicable.append(mut)
        info["final_char_applicable"] = applicable

        for ctx, fn in {**contexts, **EXTENSION_CONTEXTS[stratum]}.items():
            for k, v in samples:
                idx += 1
                sid = f"s{idx:06d}"
                with open(os.path.join(workdir, f"{sid}.txt"), "w") as fh:
                    fh.write(fn(k, v) + "\n")
                meta[sid] = (rid, ctx)
        for mut in applicable:
            ch = FINAL_CHAR[mut]
            for k, v in samples:
                idx += 1
                sid = f"s{idx:06d}"
                with open(os.path.join(workdir, f"{sid}.txt"), "w") as fh:
                    fh.write(contexts["canonical"](k, v[:-1] + ch) + "\n")
                meta[sid] = (rid, mut)
    return meta, rules_out


def scan(workdir, meta):
    rep = os.path.join(workdir, "_gl.json")
    t0 = time.perf_counter()
    subprocess.run([GITLEAKS, "detect", "--source", workdir, "--no-git",
                    "--config", GITLEAKS_TOML, "--report-format", "json",
                    "--report-path", rep, "--no-banner", "--log-level", "error"],
                   capture_output=True, timeout=7200)
    el = time.perf_counter() - t0
    out = {sid: set() for sid in meta}
    for f in json.load(open(rep)) or []:
        m = re.match(r"(s\d{6})\.txt", os.path.basename(f.get("File", "")))
        if m and m.group(1) in out:
            out[m.group(1)].add(f.get("RuleID", "?"))
    return out, el


# ── analysis ────────────────────────────────────────────────────────────────
def analyse(meta, findings, rules):
    cells = {}
    for sid, (rid, ctx) in meta.items():
        c = cells.setdefault(rid, {}).setdefault(
            ctx, {"RULE_OK": 0, "OTHER": 0, "FALLBACK": 0, "MISSED": 0})
        names = findings[sid]
        if rid in names:
            c["RULE_OK"] += 1
        elif not names:
            c["MISSED"] += 1
        elif names - {GENERIC}:
            c["OTHER"] += 1
        else:
            c["FALLBACK"] += 1

    for rid, info in rules.items():
        if rid not in cells:
            continue
        rates = {}
        for ctx, c in cells[rid].items():
            n = sum(c.values())
            rates[ctx] = {"n": n, **{k.lower() + "_rate": round(v / n, 4)
                                     for k, v in c.items()}}
        info["cells"] = rates
        canon = rates["canonical"]["rule_ok_rate"]
        info["canonical_rule_ok_rate"] = canon
        info["detected_canonically"] = canon >= 0.5
        if info["detected_canonically"]:
            info["BR"] = min(r["rule_ok_rate"] for k, r in rates.items()
                             if k not in NOT_IN_BR)
            info["fragile_contexts"] = {
                k: r["rule_ok_rate"] for k, r in rates.items()
                if k not in NOT_IN_BR and r["rule_ok_rate"] < canon - 0.10}
    return rules


def summarise(rules):
    out = {}
    for stratum in ("prefix", "keyword"):
        rs = {k: v for k, v in rules.items() if v["stratum"] == stratum}
        cov = {k: v for k, v in rs.items() if v.get("detected_canonically")}
        by_ctx = {}
        for v in cov.values():
            for ctx, r in v["cells"].items():
                a = by_ctx.setdefault(ctx, {"rules": 0, "rules_total_miss": 0,
                                            "sum_rule_ok": 0.0})
                a["rules"] += 1
                a["sum_rule_ok"] += r["rule_ok_rate"]
                a["rules_total_miss"] += r["rule_ok_rate"] == 0.0
        for a in by_ctx.values():
            a["macro_rule_ok"] = round(a.pop("sum_rule_ok") / a["rules"], 4)
        # 2x2 over rules: terminator group present x fails the ')'-terminated
        # context. (In the PREFIX battery trailing_comma is quote-terminated,
        # so ')' is the one terminator both strata share.)
        tab = Counter()
        for v in cov.values():
            fails = v["cells"]["in_parens"]["rule_ok_rate"] < 0.5
            tab[(v["has_terminator_group"], fails)] += 1
        # Minimal pair: of the rules failing in_parens, how many pass once a
        # single space separates the credential from ')'.
        failing = [v for v in cov.values() if v["cells"]["in_parens"]["rule_ok_rate"] < 0.5]
        rescued = sum(1 for v in failing
                      if v["cells"]["in_parens_spaced"]["rule_ok_rate"] >= 0.5)
        out[stratum] = {
            "n_rules": len(rs),
            "n_excluded": sum(1 for v in rs.values() if "excluded" in v),
            "n_not_detected_canonically": sum(
                1 for v in rs.values() if "cells" in v and not v["detected_canonically"]),
            "n_analysed": len(cov),
            "n_boundary_fragile": sum(1 for v in cov.values() if v["fragile_contexts"]),
            "by_context": by_ctx,
            "paren_failures_rescued_by_one_space": f"{rescued}/{len(failing)}",
            "excluded_rules": {k: v["excluded"] for k, v in rs.items() if "excluded" in v},
            "not_detected_canonically": sorted(
                k for k, v in rs.items() if "cells" in v and not v["detected_canonically"]),
            "terminator_x_paren_failure": {
                "term_and_fails": tab[(True, True)],
                "term_and_passes": tab[(True, False)],
                "noterm_and_fails": tab[(False, True)],
                "noterm_and_passes": tab[(False, False)],
            },
        }
    return out


def main():
    random.seed(SEED)
    raw = open(GITLEAKS_TOML, "rb").read()
    cfg = tomllib.loads(raw.decode())
    wd = tempfile.mkdtemp(prefix="gl_full_")
    try:
        meta, rules = build(cfg, wd)
        findings, el = scan(wd, meta)
        rules = analyse(meta, findings, rules)
        return {
            "design": __doc__.strip().splitlines()[0],
            "seed": SEED,
            "n_samples_per_cell": N_SAMPLES,
            "gitleaks_toml_sha256": hashlib.sha256(raw).hexdigest(),
            "n_rules": len(cfg["rules"]),
            "n_files": len(meta),
            "scan_wall_s": round(el, 2),
            "summary": summarise(rules),
            "per_rule": rules,
        }
    finally:
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
