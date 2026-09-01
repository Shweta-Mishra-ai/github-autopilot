"""
bench_comparative2.py — Head-to-head scanner evaluation under TWO corpus
constructions, to measure how corpus realism changes tool ranking.

WHY TWO CORPORA
  A first run of this experiment generated credentials from each provider's
  published prefix+alphabet+length specification. Under that corpus TruffleHog
  recalled only 0.60 and Gitleaks 0.80, while the system under test recalled
  0.9975 -- an apparently decisive result. Direct probing showed the cause was
  not detector quality but CORPUS CONSTRUCTION: TruffleHog and Gitleaks apply
  structural validation *beyond* the prefix (e.g. a synthetic JWT whose
  segments are random base64url characters is rejected because a real JWT's
  first segment must decode to a JSON header), so specification-shaped
  credentials that are not structurally well-formed are correctly refused.
  A shallower rule set (prefix + character class + length) accepts them.

  Reporting only the first corpus would therefore have credited the system
  under test with a recall advantage that is really a measure of how shallow
  its rules are. We report both:

    CORPUS A  "format-spec"  prefix + alphabet + length only
    CORPUS B  "structural"   additionally well-formed: real base64url-encoded
                             JWT header/payload, AWS key IDs without
                             placeholder-looking runs, etc.

  The difference between a tool's score on A and on B is itself a measurement:
  it quantifies how much of that tool's detection depends on shape alone.

Run: python bench_comparative2.py > results/comparative_bench.json
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import random
import re
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
REPO = SUBJECT_REPO_PATH

rng = random.Random(20260823)
ALNUM = string.ascii_letters + string.digits
UPPER_NUM = string.ascii_uppercase + string.digits
HEX = "0123456789abcdef"

N_POS_PER_TYPE = 40
N_NEG_REAL = 500
N_NEG_STRUCT = 200


def r(alphabet, n):
    # Draws from the seeded module RNG, NOT from `secrets`. An earlier version
    # of this harness recorded "seed": 20260823 in its output while generating
    # every credential with secrets.choice, which is a CSPRNG and cannot be
    # seeded. The corpus therefore differed on every run and the reported
    # recall moved between 1.000 and 0.990 depending on whether one of the 400
    # generated positives happened to be unmatchable. The seed is now actually
    # used, so the corpus is byte-identical across runs.
    return "".join(rng.choice(alphabet) for _ in range(n))


def b64url(obj: bytes) -> str:
    return base64.urlsafe_b64encode(obj).decode().rstrip("=")


# ── CORPUS A: published format spec only ────────────────────────────────────
SPEC_TYPES = {
    "aws_access_key_id":  lambda: "AKIA" + r(UPPER_NUM, 16),
    "github_pat":         lambda: "ghp_" + r(ALNUM, 36),
    "github_oauth":       lambda: "gho_" + r(ALNUM, 36),
    "slack_bot_token":    lambda: f"xoxb-{r(string.digits,11)}-{r(string.digits,12)}-{r(ALNUM,24)}",
    "stripe_secret_key":  lambda: "sk_live_" + r(ALNUM, 24),
    "sendgrid_api_key":   lambda: f"SG.{r(ALNUM+'_-',22)}.{r(ALNUM+'_-',43)}",
    "npm_token":          lambda: "npm_" + r(ALNUM, 36),
    "gcp_api_key":        lambda: "AIza" + r(ALNUM + "_-", 35),
    "jwt":                lambda: f"eyJ{r(ALNUM+'_-',20)}.{r(ALNUM+'_-',30)}.{r(ALNUM+'_-',43)}",
    "openai_project_key": lambda: "sk-proj-" + r(ALNUM + "_-", 52),
}


# ── CORPUS B: additionally structurally well-formed ─────────────────────────
def realistic_jwt():
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"},
                               separators=(",", ":")).encode())
    payload = b64url(json.dumps(
        {"sub": r(string.digits, 10), "name": "Service Account",
         "iat": 1716239022, "exp": 1716242622},
        separators=(",", ":")).encode())
    sig = b64url(bytes(rng.getrandbits(8) for _ in range(32)))
    return f"{header}.{payload}.{sig}"


def realistic_aws_key():
    # AWS key IDs are uppercase alnum; avoid long single-character runs, which
    # several detectors treat as a placeholder signal.
    while True:
        k = r(UPPER_NUM, 16)
        if not re.search(r"(.)\1{3,}", k):
            return "AKIA" + k


STRUCTURAL_TYPES = dict(SPEC_TYPES)
STRUCTURAL_TYPES["jwt"] = realistic_jwt
STRUCTURAL_TYPES["aws_access_key_id"] = realistic_aws_key


def real_code_lines(n):
    files = []
    for root, _d, fnames in os.walk(os.path.join(REPO, "app")):
        for f in fnames:
            if f.endswith(".py"):
                files.append(os.path.join(root, f))
    lines = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.rstrip("\n")
                    if 25 <= len(ln) <= 200 and not ln.lstrip().startswith("#"):
                        lines.append(ln)
        except Exception:
            continue
    local = random.Random(20260823)
    local.shuffle(lines)
    return lines[:n]


def structural_negatives(n):
    gens = [
        lambda: f'COMMIT = "{r(HEX,40)}"',
        lambda: f'DIGEST = "{r(HEX,64)}"',
        lambda: f'ETAG = "{r(HEX,32)}"',
        lambda: f'UUID = "{r(HEX,8)}-{r(HEX,4)}-4{r(HEX,3)}-{r(HEX,4)}-{r(HEX,12)}"',
        lambda: f'"integrity": "sha512-{r(ALNUM+"+/",86)}"',
        lambda: f'image = "app@sha256:{r(HEX,64)}"',
        lambda: f'CLASS = "sc-{r(ALNUM,6)} jsx-{r(string.digits,10)}"',
        lambda: 'API_KEY = "your-api-key-here-replace-me"',
        lambda: f'B64 = "{r(ALNUM+"+/",44)}"',
        lambda: f'HASHED = "{r(HEX,128)}"',
    ]
    local = random.Random(4242)
    return [local.choice(gens)() for _ in range(n)]


def build_corpus(workdir, types):
    truth, idx = {}, 0
    os.makedirs(workdir, exist_ok=True)
    for tname, gen in types.items():
        for _ in range(N_POS_PER_TYPE):
            idx += 1
            sid = f"s{idx:05d}"
            content = f'cfg_value = "{gen()}"\n'
            open(os.path.join(workdir, f"{sid}.py"), "w").write(content)
            truth[sid] = {"label": 1, "type": tname, "content": content}
    for ln in real_code_lines(N_NEG_REAL):
        idx += 1
        sid = f"s{idx:05d}"
        open(os.path.join(workdir, f"{sid}.py"), "w").write(ln + "\n")
        truth[sid] = {"label": 0, "type": "real_code", "content": ln + "\n"}
    for ln in structural_negatives(N_NEG_STRUCT):
        idx += 1
        sid = f"s{idx:05d}"
        open(os.path.join(workdir, f"{sid}.py"), "w").write(ln + "\n")
        truth[sid] = {"label": 0, "type": "structural_nonsecret", "content": ln + "\n"}
    return truth


def run_autopilot(truth):
    flagged, t0 = set(), time.perf_counter()
    for sid, meta in truth.items():
        diff = "".join("+" + ln + "\n" for ln in meta["content"].splitlines())
        if scan_diff(diff, file_path="app/settings.py"):
            flagged.add(sid)
    return flagged, time.perf_counter() - t0


def run_gitleaks(workdir):
    out = os.path.join(workdir, "_gl.json")
    t0 = time.perf_counter()
    subprocess.run([GITLEAKS, "detect", "--source", workdir, "--no-git",
                    "--report-format", "json", "--report-path", out],
                   capture_output=True, timeout=900)
    el = time.perf_counter() - t0
    flagged = set()
    try:
        for f in json.load(open(out)) or []:
            m = re.match(r"(s\d{5})\.py", os.path.basename(f.get("File", "")))
            if m:
                flagged.add(m.group(1))
    except Exception:
        pass
    return flagged, el


def run_trufflehog(workdir):
    t0 = time.perf_counter()
    p = subprocess.run([TRUFFLEHOG, "filesystem", workdir, "--json",
                        "--no-verification", "--no-update"],
                       capture_output=True, timeout=1800, text=True)
    el = time.perf_counter() - t0
    flagged = set()
    for line in p.stdout.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        fp = (rec.get("SourceMetadata", {}).get("Data", {})
                 .get("Filesystem", {}).get("file", ""))
        m = re.match(r"(s\d{5})\.py", os.path.basename(fp))
        if m:
            flagged.add(m.group(1))
    return flagged, el


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - h), 4), round(min(1.0, c + h), 4))


def evaluate(name, flagged, truth, elapsed):
    tp = sum(1 for s, m in truth.items() if m["label"] == 1 and s in flagged)
    fn = sum(1 for s, m in truth.items() if m["label"] == 1 and s not in flagged)
    fp = sum(1 for s, m in truth.items() if m["label"] == 0 and s in flagged)
    tn = sum(1 for s, m in truth.items() if m["label"] == 0 and s not in flagged)
    prec = tp / (tp + fp) if (tp + fp) else float("nan")
    rec = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else float("nan")
    ptr = {}
    for t in sorted({m["type"] for m in truth.values() if m["label"] == 1}):
        ids = [s for s, m in truth.items() if m["type"] == t]
        ptr[t] = round(sum(1 for s in ids if s in flagged) / len(ids), 4)
    ptf = {}
    for t in sorted({m["type"] for m in truth.values() if m["label"] == 0}):
        ids = [s for s, m in truth.items() if m["type"] == t]
        ptf[t] = round(sum(1 for s in ids if s in flagged) / len(ids), 4)
    return {
        "tool": name,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "precision": round(prec, 4), "precision_ci95": wilson(tp, tp + fp),
        "recall": round(rec, 4), "recall_ci95": wilson(tp, tp + fn),
        "f1": round(f1, 4),
        "macro_recall": round(sum(ptr.values()) / len(ptr), 4),
        "per_type_recall": ptr,
        "per_negative_type_fp_rate": ptf,
        "wall_s": round(elapsed, 3),
        "samples_per_s": round(len(truth) / elapsed, 1) if elapsed > 0 else None,
    }


def run_corpus(label, types):
    wd = tempfile.mkdtemp(prefix=f"cmp_{label}_")
    try:
        truth = build_corpus(wd, types)
        ap = run_autopilot(truth)
        gl = run_gitleaks(wd)
        th = run_trufflehog(wd)
        return {
            "corpus_label": label,
            "n_total": len(truth),
            "n_positive": sum(1 for m in truth.values() if m["label"] == 1),
            "n_negative": sum(1 for m in truth.values() if m["label"] == 0),
            "results": [
                evaluate("GitHub Autopilot", *ap[:1], truth, ap[1]) if False else
                evaluate("GitHub Autopilot", ap[0], truth, ap[1]),
                evaluate("Gitleaks 8.21.2", gl[0], truth, gl[1]),
                evaluate("TruffleHog 3.82.13", th[0], truth, th[1]),
            ],
        }
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def main():
    a = run_corpus("A_format_spec", SPEC_TYPES)
    b = run_corpus("B_structural", STRUCTURAL_TYPES)

    delta = {}
    for ra, rb in zip(a["results"], b["results"]):
        delta[ra["tool"]] = {
            "recall_A_format_spec": ra["recall"],
            "recall_B_structural": rb["recall"],
            "delta_recall": round(rb["recall"] - ra["recall"], 4),
            "f1_A": ra["f1"], "f1_B": rb["f1"],
        }

    return {
        "design": ("Two corpora, identical negatives, differing only in how "
                   "positives are constructed. Corpus A uses published "
                   "prefix+alphabet+length specs; Corpus B additionally makes "
                   "each credential structurally well-formed. Tools that "
                   "validate beyond the prefix score higher on B; the gap "
                   "measures how much of a tool's detection is shape-only."),
        "negative_composition": {
            "real_source_lines_from_system_under_test": N_NEG_REAL,
            "structural_non_secrets": N_NEG_STRUCT,
        },
        "n_samples_per_positive_type": N_POS_PER_TYPE,
        "corpus_A": a,
        "corpus_B": b,
        "corpus_sensitivity": delta,
        "seed": 20260823,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
