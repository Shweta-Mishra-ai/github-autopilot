"""
bench_stability.py — How stable is the hyphen-boundary result across draws?

The mutation harness generates credentials from each rule's own expression.
For rules with a VARIABLE-count quantifier the generated length varies, and
whether a truncated match is still possible depends on that length. This
harness repeats the hyphen cell R times with distinct seeds and reports the
spread, separating rules whose failure is deterministic from those whose
measured rate is a sampling estimate.
"""
from __future__ import annotations
import json, logging, os, random, statistics as st, sys
SUBJECT_REPO_PATH = os.environ.get(
    "SUBJECT_REPO", os.path.expanduser("~/github-autopilot"))
sys.path.insert(0, SUBJECT_REPO_PATH)
import exrex
from app.security.enhanced_secrets import PATTERNS, scan_diff
logging.disable(logging.CRITICAL)

N, R = 120, 10
FALLBACK = "High Entropy String (unclassified)"
AFFECTED = {"OpenAI API Key (new)", "GCP API Key", "Google OAuth Token",
            "SendGrid API Key", "JWT Token"}
QUANT = {"OpenAI API Key (new)": "{50,} variable", "GCP API Key": "{35} fixed",
         "Google OAuth Token": "{68,} variable", "SendGrid API Key": "{43} fixed",
         "JWT Token": "{10,} variable"}

def classify(line, rule):
    f = scan_diff(line, file_path="app/settings.py")
    if not f: return "MISSED"
    names = {x.pattern_name for x in f}
    if rule in names: return "RULE_OK"
    return "OTHER" if names - {FALLBACK} else "FALLBACK"

rules = {n: p for n, p, _, _ in PATTERNS if n in AFFECTED}
out = {"design": __doc__.strip(), "n_per_run": N, "n_runs": R, "per_rule": {}}

for rule, pat in rules.items():
    body = pat[2:-2] if pat.startswith(r"\b") and pat.endswith(r"\b") else pat
    rates = []
    for r in range(R):
        random.seed(20260823 + r)
        creds = [exrex.getone(body) for _ in range(N)]
        creds = [c[:-1] + "-" for c in creds]
        rates.append(sum(classify(f'+cfg_value = "{v}"', rule) == "RULE_OK"
                         for v in creds) / N)
    mean = st.mean(rates)
    sd = st.stdev(rates) if len(set(rates)) > 1 else 0.0
    half = 1.96 * sd / (R ** 0.5)
    out["per_rule"][rule] = {
        "quantifier": QUANT[rule], "runs": rates,
        "mean": round(mean, 4), "stdev": round(sd, 4),
        "min": round(min(rates), 4), "max": round(max(rates), 4),
        "ci95_mean": [round(mean - half, 4), round(mean + half, 4)],
        "deterministic": sd == 0.0,
    }

print(json.dumps(out, indent=1))
