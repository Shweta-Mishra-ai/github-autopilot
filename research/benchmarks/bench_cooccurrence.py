"""
bench_cooccurrence.py — Architecture-fair comparison across four deployment
conditions.

MOTIVATION
  Our earlier single-credential-per-file corpus disadvantaged TruffleHog,
  whose detectors are built around verification and CO-OCCURRENCE: probing
  showed a lone AWS key ID yields no finding, while the same key ID beside a
  secret access key is detected. Measuring only condition A therefore reports
  a property of our corpus, not of the tool.

  This experiment evaluates all three scanners under four conditions that span
  the range from adversarial-to-co-occurrence to favourable-to-co-occurrence:

    A  single credential, alone in a file          (architecture-neutral floor)
    B  credential paired with its companion secret (co-occurrence satisfied)
    C  several credentials of different types in one file
    D  realistic repository layout: a settings module with imports, comments,
       unrelated configuration, and the credential in situ

  Reporting all four separates "the tool cannot detect this shape" from
  "the tool declines to report this shape without corroborating context".

Run: python bench_cooccurrence.py > results/cooccurrence_bench.json
"""
from __future__ import annotations

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
N = 30


def r(a, n):
    return "".join(_secrets.choice(a) for _ in range(n))


def aws_id():
    while True:
        k = r(UPPER_NUM, 16)
        if not re.search(r"(.)\1{3,}", k):
            return "AKIA" + k


GEN = {
    "aws_access_key_id": aws_id,
    "github_pat":        lambda: "ghp_" + r(ALNUM, 36),
    "gcp_api_key":       lambda: "AIza" + r(ALNUM + "_-", 35),
    "stripe_secret_key": lambda: "sk_live_" + r(ALNUM, 24),
    "sendgrid_api_key":  lambda: f"SG.{r(ALNUM+'_-',22)}.{r(ALNUM+'_-',43)}",
    "npm_token":         lambda: "npm_" + r(ALNUM, 36),
}

# The companion value each provider issues alongside the primary credential.
COMPANION = {
    "aws_access_key_id": lambda: ("aws_secret_access_key",
                                  r(ALNUM + "/+", 40)),
    "github_pat":        lambda: ("github_username", "deploy-bot"),
    "gcp_api_key":       lambda: ("gcp_project_id", "prod-analytics-42"),
    "stripe_secret_key": lambda: ("stripe_publishable_key",
                                  "pk_live_" + r(ALNUM, 24)),
    "sendgrid_api_key":  lambda: ("sendgrid_sender", "noreply@example.com"),
    "npm_token":         lambda: ("npm_registry",
                                  "https://registry.npmjs.org/"),
}


def render_A(t):
    return f'cfg_value = "{GEN[t]()}"\n'


def render_B(t):
    k, v = COMPANION[t]()
    return f'{t} = "{GEN[t]()}"\n{k} = "{v}"\n'


def render_C(t):
    others = [x for x in GEN if x != t]
    picks = [t] + list(_secrets.SystemRandom().sample(others, 2))
    return "".join(f'{p} = "{GEN[p]()}"\n' for p in picks)


def render_D(t):
    k, v = COMPANION[t]()
    return (
        '"""Application settings loaded at start-up."""\n'
        "import os\n"
        "from pathlib import Path\n\n"
        "BASE_DIR = Path(__file__).resolve().parent\n"
        "DEBUG = os.environ.get('DEBUG', '0') == '1'\n"
        "TIMEOUT_SECONDS = 30\n"
        "RETRY_ATTEMPTS = 3\n\n"
        "# --- third-party integration -------------------------------------\n"
        f'{t} = "{GEN[t]()}"\n'
        f'{k} = "{v}"\n'
        "REGION = 'us-east-1'\n\n"
        "LOG_LEVEL = 'INFO'\n"
        "CACHE_TTL = 300\n"
    )


CONDITIONS = {
    "A_single_credential": render_A,
    "B_paired_with_companion": render_B,
    "C_multiple_credentials": render_C,
    "D_realistic_settings_module": render_D,
}


def build(wd):
    meta, idx = {}, 0
    for cond, render in CONDITIONS.items():
        for t in GEN:
            for _ in range(N):
                idx += 1
                sid = f"c{idx:06d}"
                open(os.path.join(wd, f"{sid}.py"), "w").write(render(t))
                meta[sid] = (cond, t)
    return meta


def scan_ap(wd, meta):
    out = {}
    for sid in meta:
        content = open(os.path.join(wd, f"{sid}.py")).read()
        diff = "".join("+" + ln + "\n" for ln in content.splitlines())
        out[sid] = bool(scan_diff(diff, file_path="app/settings.py"))
    return out


def scan_gl(wd, meta):
    rep = os.path.join(wd, "_g.json")
    subprocess.run([GITLEAKS, "detect", "--source", wd, "--no-git",
                    "--report-format", "json", "--report-path", rep],
                   capture_output=True, timeout=3600)
    out = {s: False for s in meta}
    try:
        for f in json.load(open(rep)) or []:
            m = re.match(r"(c\d{6})\.py", os.path.basename(f.get("File", "")))
            if m and m.group(1) in out:
                out[m.group(1)] = True
    except Exception:
        pass
    return out


def scan_th(wd, meta):
    p = subprocess.run([TRUFFLEHOG, "filesystem", wd, "--json",
                        "--no-verification", "--no-update"],
                       capture_output=True, timeout=7200, text=True)
    out = {s: False for s in meta}
    for line in p.stdout.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if "DetectorName" not in rec:
            continue
        fp = (rec.get("SourceMetadata", {}).get("Data", {})
                 .get("Filesystem", {}).get("file", ""))
        m = re.match(r"(c\d{6})\.py", os.path.basename(fp))
        if m and m.group(1) in out:
            out[m.group(1)] = True
    return out


def analyse(tool, det, meta):
    by_cond, per_type = {}, {}
    for cond in CONDITIONS:
        ids = [s for s, (c, t) in meta.items() if c == cond]
        by_cond[cond] = round(sum(det[s] for s in ids) / len(ids), 4)
        per_type[cond] = {}
        for t in GEN:
            tid = [s for s, (c, tt) in meta.items() if c == cond and tt == t]
            per_type[cond][t] = round(sum(det[s] for s in tid) / len(tid), 4)
    return {
        "tool": tool,
        "detection_rate_by_condition": by_cond,
        "per_type_by_condition": per_type,
        "condition_A_to_D_delta": round(
            by_cond["D_realistic_settings_module"] - by_cond["A_single_credential"], 4),
    }


def main():
    wd = tempfile.mkdtemp(prefix="cooc_")
    try:
        meta = build(wd)
        res = {
            "design": ("Four deployment conditions spanning adversarial to "
                       "favourable for co-occurrence-based detectors. "
                       "Detection is measured at file level (did the tool "
                       "report anything for this file)."),
            "n_samples_per_cell": N,
            "n_conditions": len(CONDITIONS),
            "n_types": len(GEN),
            "n_files": len(meta),
            "results": {},
        }
        for tool, fn in [("GitHub Autopilot", scan_ap),
                         ("Gitleaks 8.21.2", scan_gl),
                         ("TruffleHog 3.82.13", scan_th)]:
            res["results"][tool] = analyse(tool, fn(wd, meta), meta)
        return res
    finally:
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
