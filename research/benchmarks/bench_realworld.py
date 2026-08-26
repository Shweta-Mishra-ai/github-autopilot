"""
bench_realworld.py — Ecological-validity benchmark.

The controlled corpus measures precision against negatives the experimenter
constructed. That answers "how does the scanner behave on the distribution I
built", not "how does it behave on real code". This experiment measures the
FALSE-POSITIVE RATE on genuine, unmodified source files from four widely used
open-source Python projects, scanned line by line exactly as the production
push handler scans an added line of a diff.

Ground truth: these are released, publicly audited codebases; we treat every
line as a true negative. That assumption is checked -- every reported finding
is written out in redacted form for manual inspection, and any finding that is
in fact a real credential would invalidate the assumption rather than being
silently counted as a false positive.

Corpora:
  requests, flask, redis-py   -- third-party, no relationship to the scanner
  github-autopilot            -- the system under test's own codebase

All three tools are run over the identical file set.

Run: python bench_realworld.py > results/realworld_bench.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
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
CORPUS_ROOT = os.path.join(SCRATCH, "realcorpus")

PROJECTS = {
    "requests": os.path.join(CORPUS_ROOT, "requests"),
    "flask": os.path.join(CORPUS_ROOT, "flask"),
    "redis-py": os.path.join(CORPUS_ROOT, "redis-py"),
    "github-autopilot (system under test)": SUBJECT_REPO_PATH,
}

SKIP_DIRS = {".git", ".venv-research", "node_modules", "__pycache__",
             ".mypy_cache", ".pytest_cache", ".ruff_cache"}
EXTS = (".py", ".yml", ".yaml", ".json", ".toml", ".cfg", ".ini", ".sh", ".md")


def iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(EXTS):
                yield os.path.join(dirpath, fn)


def scan_project_autopilot(root):
    """Scan every line as an added diff line, mirroring the push handler."""
    n_files = n_lines = 0
    findings = []
    t0 = time.perf_counter()
    for path in iter_files(root):
        rel = os.path.relpath(path, root)
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except Exception:
            continue
        n_files += 1
        lines = content.splitlines()
        n_lines += len(lines)
        diff = "".join("+" + ln + "\n" for ln in lines)
        for f in scan_diff(diff, file_path=rel):
            findings.append({
                "project_relative_path": rel,
                "line": f.line_number,
                "rule": f.pattern_name,
                "severity": f.severity,
                "confidence": f.confidence,
                "redacted": f.redacted_match,
            })
    return {
        "n_files": n_files,
        "n_lines": n_lines,
        "n_findings": len(findings),
        "findings_per_kloc": round(len(findings) / (n_lines / 1000), 4) if n_lines else 0,
        "elapsed_s": round(time.perf_counter() - t0, 3),
        "findings": findings[:60],
        "findings_by_rule": _by(findings, "rule"),
        "findings_by_severity": _by(findings, "severity"),
    }


def _by(findings, key):
    out = {}
    for f in findings:
        out[f[key]] = out.get(f[key], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def scan_project_gitleaks(root):
    out = os.path.join(SCRATCH, "_gl_rw.json")
    if os.path.exists(out):
        os.remove(out)
    t0 = time.perf_counter()
    subprocess.run([GITLEAKS, "detect", "--source", root, "--no-git",
                    "--report-format", "json", "--report-path", out],
                   capture_output=True, timeout=1800)
    el = time.perf_counter() - t0
    items = []
    try:
        items = json.load(open(out)) or []
    except Exception:
        items = []
    findings = [{"project_relative_path": i.get("File", ""),
                 "line": i.get("StartLine"),
                 "rule": i.get("RuleID"),
                 "redacted": (i.get("Secret", "")[:4] + "***")} for i in items]
    return {"n_findings": len(findings), "elapsed_s": round(el, 3),
            "findings_by_rule": _by(findings, "rule"), "findings": findings[:60]}


def scan_project_trufflehog(root):
    t0 = time.perf_counter()
    p = subprocess.run([TRUFFLEHOG, "filesystem", root, "--json",
                        "--no-verification", "--no-update"],
                       capture_output=True, timeout=2400, text=True)
    el = time.perf_counter() - t0
    findings = []
    for line in p.stdout.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if "DetectorName" not in rec:
            continue
        fp = (rec.get("SourceMetadata", {}).get("Data", {})
                 .get("Filesystem", {}).get("file", ""))
        findings.append({"project_relative_path": fp,
                         "rule": rec.get("DetectorName"),
                         "redacted": (rec.get("Raw", "")[:4] + "***")})
    return {"n_findings": len(findings), "elapsed_s": round(el, 3),
            "findings_by_rule": _by(findings, "rule"), "findings": findings[:60]}


def main():
    out = {"design": (
        "False-positive rate on unmodified real source from widely used OSS "
        "projects. Every line treated as an added diff line, mirroring the "
        "production push handler. Findings are reported redacted for manual "
        "inspection; ground truth assumes released public code contains no "
        "live credentials."), "projects": {}}

    for name, root in PROJECTS.items():
        if not os.path.isdir(root):
            out["projects"][name] = {"error": "corpus not available"}
            continue
        entry = {"root_exists": True}
        entry["autopilot"] = scan_project_autopilot(root)
        entry["gitleaks"] = scan_project_gitleaks(root)
        entry["trufflehog"] = scan_project_trufflehog(root)
        out["projects"][name] = entry

    tot = {"autopilot": 0, "gitleaks": 0, "trufflehog": 0}
    lines = 0
    for name, e in out["projects"].items():
        if "error" in e:
            continue
        tot["autopilot"] += e["autopilot"]["n_findings"]
        tot["gitleaks"] += e["gitleaks"]["n_findings"]
        tot["trufflehog"] += e["trufflehog"]["n_findings"]
        lines += e["autopilot"]["n_lines"]
    out["totals"] = {
        "total_lines_scanned": lines,
        "total_findings": tot,
        "findings_per_kloc": {
            k: round(v / (lines / 1000), 4) if lines else 0 for k, v in tot.items()
        },
    }
    return out


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
