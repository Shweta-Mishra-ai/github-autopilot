"""
bench_realworld_multilang.py — Real-code evaluation of all three scanners on
twenty commit-pinned open-source repositories in nine languages.

WHY THIS EXPERIMENT
  bench_realworld.py measures false positives on four Python projects, which
  the paper itself says "is not a sample of anything", and every positive
  in the rest of the study is generated. This harness widens the real-code
  corpus to nine languages and adds a measurement the paper does not have:
  how each scanner behaves on credential-shaped literals that occur in real
  source, in the syntax real source actually uses.

SAMPLING FRAME (purposive, stated, two strata)
  general: two repositories per language for Go, JavaScript, TypeScript,
  Java, Ruby, Rust, PHP and C#, and four for Python (the paper's three plus
  one); where a language has one, one of the two is an official SDK of a
  credential-issuing service.
  credential_dense: eleven SDKs and CLIs of credential-issuing services
  (GitHub, DigitalOcean, npm, OpenAI, Shopify, Hugging Face, GitLab, Slack,
  Anthropic, Stripe), added because the general stratum contains too few
  credential-shaped literals for Part B; their tests and docs carry the
  providers' token formats in real syntax.
  Strata are reported separately. This is a purposive sample chosen for
  language and context diversity, not a random sample of GitHub, and
  supports no population claim.

IDENTICAL FILE SET
  Every text file (no NUL byte in its first 8 KiB, at most 1 MiB, outside
  vendored and build directories) is copied into a staging tree, and all
  three tools scan that same tree. bench_realworld.py did not do this: it fed
  the subject only an extension allow-list while the other two tools scanned
  every file.

PART A: FINDINGS. Every finding of every tool is recorded with a category
  assigned by stated rules (see categorise()). No finding is asserted to be
  a live credential; public released code is assumed to contain none.

PART C: CREDENTIAL-SHAPED TOKENS OF ANY PROVIDER. Every token meeting a
  stated provider-independent definition (see _credential_shaped) is
  counted by the character that follows it, per language: how often real
  source puts such a token where Gitleaks' terminator group rejects it.

PART B: CREDENTIAL-SHAPED LITERALS IN REAL CODE. For every prefix-type
  credential pattern from bench_threetool_expanded.py whose secret starts
  with a literal of three or more characters (ghp_, AKIA, sk_live_, ...), all
  occurrences in the staged tree are located with a boundary-agnostic search.
  For each occurrence we record the character that follows it and whether
  each scanner reported, on that line, the rule that scanner itself fires for
  that credential type canonically (the expected rule frozen in
  threetool_expanded_bench.json). This measures, on real text, whether the
  context defects found synthetically occur and matter.

Run: python bench_realworld_multilang.py > results/realworld_multilang_bench.json
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import sre_parse
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_gitleaks_fulltable as gl  # noqa: E402
import bench_threetool_expanded as tt  # noqa: E402

from app.security.enhanced_secrets import scan_diff  # noqa: E402  (path set by tt)

logging.disable(logging.CRITICAL)

_ROOT = gl._ROOT
CORPUS_ROOT = os.environ.get("CORPUS2_ROOT", os.path.join(_ROOT, "realcorpus2"))
EXPANDED = os.environ.get(
    "EXPANDED_RESULTS", os.path.join(_ROOT, "results", "threetool_expanded_bench.json"))

CORPUS = [
    # stratum "general": two projects per language, one an SDK where one exists
    ("go", "gin-gonic/gin", "0f09c3a9b4626d9fe9979cf4d0521d6ae32ab646", "general"),
    ("go", "sendgrid/sendgrid-go", "0f2bb79e71b9d73e40cb19a827089ed1252ab1d1", "general"),
    ("javascript", "expressjs/express", "0dee2c52c7c873498f8ed7d8ee4ddd0a04d3ada8", "general"),
    ("javascript", "stripe/stripe-node", "62458fe75f83b800707f06f077abe107213b0db0", "general"),
    ("typescript", "nestjs/nest", "62647d3ec95e816a4556b3e04106eb4af002b4b8", "general"),
    ("typescript", "slackapi/node-slack-sdk", "8730b5e3a7cee08a0ccfa44b7f792814f939a9cf", "general"),
    ("java", "google/gson", "e2292f9c9f2e07a4791827e8eea34aa7d80545d6", "general"),
    ("java", "square/retrofit", "f51e456b06fa99d78f896257a0d6ca78ab8e1e58", "general"),
    ("ruby", "sinatra/sinatra", "cb22afd7902b566b6eaba6c4ea89739494a65d12", "general"),
    ("ruby", "jekyll/jekyll", "97c3bdc822df9d5bea4c269b29a28de5a5e29d70", "general"),
    ("rust", "tokio-rs/axum", "86aa6153e47b7862737acdcfe3a0eb3a162b939a", "general"),
    ("rust", "BurntSushi/ripgrep", "3fce3b5bb0236da2df6d99672afb8a719642eca7", "general"),
    ("php", "guzzle/guzzle", "b920e5fcaf81d761b8fabe600ca90999beaefdb7", "general"),
    ("php", "twilio/twilio-php", "0cb66196f3c017dae395661e0cf1fc8197cb6d34", "general"),
    ("csharp", "JamesNK/Newtonsoft.Json", "52fa3aef1f2cadcd3a3f874251eddc98d3efbbaa", "general"),
    ("csharp", "App-vNext/Polly", "3f3adad9d5af85658fc3585018f02a2d8c2173e8", "general"),
    ("python", "openai/openai-python", "26e5d926966fc5c7e688790b21d66dc08d35eceb", "general"),
    ("python", "psf/requests", "8f8b212de8c2129d7954c6cd373762880375620a", "general"),
    ("python", "pallets/flask", "d318b683471101618febed18996405ad26462110", "general"),
    ("python", "redis/redis-py", "081923b8a202f9481e8f46357c8bbf99cc322cae", "general"),
    # stratum "credential_dense": SDKs and CLIs of credential-issuing services,
    # chosen because their tests and docs carry the provider's token formats
    ("go", "cli/cli", "7948e8056e45ac3c729b829e0eafe2d0797a95c6", "credential_dense"),
    ("go", "digitalocean/godo", "8a0153dc2f8986ec197fed5c06dc5c642ed6fffb", "credential_dense"),
    ("javascript", "npm/cli", "ae98c62586b6fea44114f72e249e246a2981de70", "credential_dense"),
    ("typescript", "openai/openai-node", "e35205033764ec2a2adc7fc71f3d2df9b0ea8679", "credential_dense"),
    ("typescript", "Shopify/shopify-app-js", "2a9a3a3b5a390a589618a6a5f753982c3b569215", "credential_dense"),
    ("python", "huggingface/huggingface_hub", "e1f595dd7db4c993837e320a6d62ccdd1717dc2d", "credential_dense"),
    ("python", "python-gitlab/python-gitlab", "51eb17e6e67a25157e04b0abc649221bd8ecc7fe", "credential_dense"),
    ("python", "slackapi/python-slack-sdk", "ab4e4983ba02789f5854e711450f9340d4cd53f2", "credential_dense"),
    ("python", "anthropics/anthropic-sdk-python", "ecfee6cd504cfbf634919bdbd058fb74afdbd533", "credential_dense"),
    ("java", "stripe/stripe-java", "f2c7e627e9d3ee3b09097c053f56fcc697f6eb64", "credential_dense"),
    ("ruby", "stripe/stripe-ruby", "082af3fc6505e3a2dbee4b3a52814e0a1b2483f8", "credential_dense"),
]

SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", "bin",
             "obj", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache"}
MAX_BYTES = 1 << 20


def repo_dir(repo):
    return os.path.join(CORPUS_ROOT, repo.replace("/", "_"))


def verify_pins():
    for _lang, repo, sha, _stratum in CORPUS:
        got = subprocess.run(["git", "-C", repo_dir(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        if got != sha:
            raise SystemExit(f"{repo} is at {got!r}, expected {sha}")


def stage(repo, dest):
    root = repo_dir(repo)
    n_files = n_lines = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            src = os.path.join(dirpath, fn)
            if os.path.islink(src) or os.path.getsize(src) > MAX_BYTES:
                continue
            with open(src, "rb") as fh:
                data = fh.read()
            if b"\x00" in data[:8192]:
                continue
            rel = os.path.relpath(src, root)
            out = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(data)
            n_files += 1
            n_lines += data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
    return n_files, n_lines


# ── scanners, all over the same staged tree ─────────────────────────────────
def scan_autopilot(root):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root)
            with open(path, encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().splitlines()
            diff = "".join("+" + ln + "\n" for ln in lines)
            for f in scan_diff(diff, file_path=rel):
                out.append({"file": rel, "line": f.line_number, "rule": f.pattern_name})
    return out


def scan_gitleaks(root, rep):
    subprocess.run([gl.GITLEAKS, "detect", "--source", root, "--no-git",
                    "--config", gl.GITLEAKS_TOML, "--report-format", "json",
                    "--report-path", rep, "--no-banner", "--log-level", "error"],
                   capture_output=True, timeout=3600)
    return [{"file": os.path.relpath(f["File"], root) if os.path.isabs(f["File"]) else f["File"],
             "line": f["StartLine"], "rule": f["RuleID"], "raw": f.get("Secret") or ""}
            for f in (json.load(open(rep)) or [])]


TRUFFLEHOG_RUNS = 3


def _trufflehog_once(root):
    p = subprocess.run([tt.TRUFFLEHOG, "filesystem", root, "--json",
                        "--no-verification", "--no-update"],
                       capture_output=True, timeout=7200, text=True)
    out = Counter()
    for line in p.stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "DetectorName" not in rec:
            continue
        fs = rec.get("SourceMetadata", {}).get("Data", {}).get("Filesystem", {})
        out[(os.path.relpath(fs.get("file", ""), root), fs.get("line"),
             rec["DetectorName"], rec.get("Raw") or "")] += 1
    return out


def scan_trufflehog(root):
    """TruffleHog 3.82.13 does not return the same findings for the same
    input every time: on cli/cli it reported two findings in one file in one
    run of six, with or without --concurrency=1. It is therefore run
    TRUFFLEHOG_RUNS times, the findings present in every run are the ones
    reported, and the rest are counted as unstable rather than dropped
    silently."""
    runs = [_trufflehog_once(root) for _ in range(TRUFFLEHOG_RUNS)]
    stable = runs[0]
    union = Counter(runs[0])
    for r in runs[1:]:
        stable = stable & r
        union = union | r
    out = [{"file": f, "line": ln, "rule": rule, "raw": raw}
           for (f, ln, rule, raw), n in sorted(stable.items(), key=lambda kv: str(kv[0]))
           for _ in range(n)]
    return out, sum((union - stable).values())


def relocate(root, findings):
    """TruffleHog's filesystem line numbers are not reliable: on
    python-gitlab it reported a token on line 34 of a file as line 33, and
    other findings on blank lines. A finding's line is therefore taken from
    its file, as the line holding the reported secret, when the tool reports
    one. The same secret can occur on several lines (one token, four lines of
    a docs page, all reported on one line), so each finding takes the nearest
    line holding its secret that no earlier finding with the same rule and
    secret has taken. Returns the number of findings moved and the number not
    located."""
    moved = missing = 0
    cache, taken = {}, defaultdict(set)
    for f in findings:
        raw = f.get("raw", "")
        if not raw:
            continue
        if f["file"] not in cache:
            try:
                with open(os.path.join(root, f["file"]), encoding="utf-8",
                          errors="ignore") as fh:
                    cache[f["file"]] = fh.read().splitlines()
            except OSError:
                cache[f["file"]] = []
        lines = cache[f["file"]]
        first = raw.splitlines()[0] if raw.splitlines() else raw
        key = (f["file"], f["rule"], first)
        here = f["line"] or 0
        spots = [i for i, ln in enumerate(lines, 1) if first in ln and i not in taken[key]]
        if not spots:
            missing += 1
            continue
        best = min(spots, key=lambda i: (abs(i - here), i))
        taken[key].add(best)
        if best != f["line"]:
            f["line"] = best
            moved += 1
    return moved, missing


# ── Part A: categorising findings ───────────────────────────────────────────
_PLACEHOLDER = re.compile(
    r"(?i)(x{4,}|\*{3,}|0{6,}|1234|abcd|example|sample|dummy|fake|placeholder|"
    r"your[_-]?|<[^>]*>|\{\{|\$\{|%s|changeme|redacted|test)")
_TEST_PATH = re.compile(r"(?i)(^|/)(tests?|spec|specs|__tests__|fixtures?|testdata|"
                        r"examples?|samples?|docs?|benchmarks?)(/|$)|(_test|\.test|\.spec|"
                        r"_spec|test_)[^/]*$|readme|changelog|\.md$|\.rst$")


def categorise(f, text_line):
    """Stated rules, in order, applied to the whole source line (the subject
    exposes only a redacted match, so the line is the one input all three
    tools share). A finding is never labelled a live credential."""
    if _PLACEHOLDER.search(text_line):
        return "placeholder_or_test_value"
    if _TEST_PATH.search(f["file"]):
        return "in_test_doc_or_example_path"
    if re.search(r"(?i)(sha|md5|hash|checksum|integrity|digest|nonce|uuid|guid)", text_line):
        return "hash_or_identifier_context"
    return "other"


def _line(root, rel, n):
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="ignore") as fh:
            for i, ln in enumerate(fh, 1):
                if i == n:
                    return ln.rstrip("\n")
    except OSError:
        pass
    return ""


# ── Part B: credential-shaped literals ──────────────────────────────────────
def _lead(seq, cap=64):
    """(strings, complete): the literal strings a match of `seq` must start
    with, expanding alternations; `complete` when all of `seq` is literal, so
    the caller may keep appending what follows it."""
    outs = [""]
    for op, av in seq:
        if op is sre_parse.LITERAL:
            outs = [o + chr(av) for o in outs]
            continue
        if op is sre_parse.SUBPATTERN:
            alts = [_lead(av[3], cap)]
        elif op is sre_parse.BRANCH:
            alts = [_lead(br, cap) for br in av[1]]
        else:
            return outs, False
        outs = [o + x for o in outs for strs, _ in alts for x in strs]
        if len(outs) > cap:
            return outs[:cap], False
        if not all(done for _, done in alts):
            return outs, False
    return outs, True


def literal_prefixes(pattern):
    """All literal prefixes a match must begin with, or [] when some match
    can begin with fewer than three fixed characters."""
    try:
        lead, _ = _lead(sre_parse.parse(pattern))
    except Exception:
        return []
    if not lead or any(len(x) < 3 for x in lead):
        return []
    return sorted(set(lead))


def anchor_types(expanded):
    """Prefix types with a distinctive literal start, and each tool's frozen
    expected rule for them."""
    cfg = tomllib.loads(open(gl.GITLEAKS_TOML, "rb").read().decode())
    th, _ = tt.types_trufflehog(tt.TRUFFLEHOG_SRC)
    out = []
    for t in tt.types_gitleaks(cfg) + th + tt.types_autopilot():
        if t["kw_pattern"] or t["id"] not in expanded["per_type"]:
            continue
        if re.search(r"(?i)private[ -]?key", t["id"]):
            continue                    # multi-line PEM blocks: no terminator
        pres = literal_prefixes(t["secret"])
        if not pres:
            continue
        rec = expanded["per_type"][t["id"]]
        out.append({"id": t["id"], "prefixes": pres,
                    "search": re.compile(r"(?<![A-Za-z0-9_])(" + t["secret"] + r")", re.ASCII),
                    # only a label-matched rule counts: a detection under
                    # another provider's rule is not detection of this type
                    "expected": {tool: rec[tool]["expected_rule"]
                                 if rec[tool]["label_matched"] else None
                                 for tool in tt.TOOLS}})
    return out


# ── Part C: credential-shaped tokens of any provider ────────────────────────
# Part B needs a known provider prefix and so sees few literals. Part C asks
# the wider question the defects turn on -- in real source, what character
# follows a credential-shaped token? -- using a provider-independent,
# stated definition of "credential-shaped" (see _credential_shaped).
_TOKEN = re.compile(r"(?<![A-Za-z0-9_+/-])([A-Za-z0-9_+/-]{20,100}={0,2})(?![A-Za-z0-9_+/=-])")
# Characters in Gitleaks' explicit terminator group, plus end of line.
GITLEAKS_ACCEPTED = {"EOL", "quote", "whitespace", ";", "|"}


def _shannon(s):
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _credential_shaped(tok):
    core = tok.rstrip("=")
    return (any(c.isupper() for c in core) and any(c.islower() for c in core)
            and any(c.isdigit() for c in core)
            and core.count("/") < 2
            and not re.fullmatch(r"[0-9a-fA-F]+", core)
            and _shannon(core) > 4.0)


def _repo_weighted(by_repo, min_tokens=20):
    """Unweighted mean and median over repositories of the share of tokens
    followed by a character Gitleaks' terminator group rejects; repositories
    with fewer than min_tokens tokens are left out as too small to rate."""
    shares = sorted(
        sum(v for k, v in c.items() if k not in GITLEAKS_ACCEPTED) / sum(c.values())
        for c in by_repo.values() if sum(c.values()) >= min_tokens)
    if not shares:
        return None
    n = len(shares)
    med = shares[n // 2] if n % 2 else (shares[n // 2 - 1] + shares[n // 2]) / 2
    return {"n_repositories": n, "min_tokens": min_tokens,
            "mean_share_rejected": round(sum(shares) / n, 4),
            "median_share_rejected": round(med, 4),
            "min_share_rejected": round(shares[0], 4),
            "max_share_rejected": round(shares[-1], 4)}


def follow_class(ch):
    if ch == "":
        return "EOL"
    if ch in "'\"`":
        return "quote"
    if ch in " \t":
        return "whitespace"
    return ch


def main():
    verify_pins()
    expanded = json.load(open(EXPANDED))
    anchors = anchor_types(expanded)
    by_prefix = defaultdict(list)
    for a in anchors:
        for p in a["prefixes"]:
            by_prefix[p].append(a)
    prefix_re = re.compile("|".join(re.escape(p) for p in
                                    sorted(by_prefix, key=len, reverse=True)))
    # Optional local dump of every finding with its source line, for manual
    # review. Never written into results/: the lines are third-party text.
    dump = open(os.environ["FINDINGS_DUMP"], "w") if os.environ.get("FINDINGS_DUMP") else None
    tmp = tempfile.mkdtemp(prefix="rw2_")
    projects, cands = {}, []
    part_c = defaultdict(Counter)       # (stratum, language) -> follow class
    part_c_repo = defaultdict(Counter)  # repo -> follow class
    part_c_path = defaultdict(Counter)  # under a test/doc/fixture path?
    part_c_hyphen = Counter()
    try:
        for lang, repo, sha, stratum in CORPUS:
            root = os.path.join(tmp, repo.replace("/", "_"))
            os.makedirs(root)
            n_files, n_lines = stage(repo, root)
            t0 = time.perf_counter()
            fnd = {"autopilot": scan_autopilot(root),
                   "gitleaks": scan_gitleaks(root, os.path.join(tmp, "_gl.json")),
                   "trufflehog": None}
            fnd["trufflehog"], th_unstable = scan_trufflehog(root)
            el = time.perf_counter() - t0
            for fs in fnd.values():     # tool output order is not stable
                fs.sort(key=lambda f: (f["file"], f["rule"], f.get("raw", ""), f["line"] or 0))
            relocated = {tool: relocate(root, fs) for tool, fs in fnd.items()}
            hit = defaultdict(set)          # (tool, file, line) -> rules
            per_tool = {}
            for tool, fs in fnd.items():
                cats = Counter()
                for f in fs:
                    hit[(tool, f["file"], f["line"])].add(f["rule"])
                    text = _line(root, f["file"], f["line"])
                    cat = categorise(f, text)
                    cats[cat] += 1
                    if dump:
                        dump.write(json.dumps({"repo": repo, "tool": tool,
                                               **{k: v for k, v in f.items() if k != "raw"},
                                               "category": cat, "text": text[:300]}) + "\n")
                per_tool[tool] = {
                    "n_findings": len(fs),
                    **({"unstable_across_runs": th_unstable} if tool == "trufflehog" else {}),
                    "lines_relocated_by_secret": relocated[tool][0],
                    "secret_not_found_in_file": relocated[tool][1],
                    "findings_per_kloc": round(len(fs) / (n_lines / 1000), 4) if n_lines else None,
                    "by_rule": dict(Counter(f["rule"] for f in fs).most_common()),
                    "by_category": dict(cats.most_common()),
                }
            projects[repo] = {"language": lang, "stratum": stratum, "commit": sha,
                              "n_files": n_files,
                              "n_lines": n_lines, "scan_wall_s": round(el, 2),
                              "tools": per_tool}

            # Part B: the unit is one literal occurrence (file, line, start,
            # end). Types from several generators can match the same literal;
            # they are merged, and a tool counts as covering the occurrence if
            # any of its label-matched expected rules applies to any of them.
            occ = {}
            for dirpath, _d, files in os.walk(root):
                for fn in files:
                    path = os.path.join(dirpath, fn)
                    rel = os.path.relpath(path, root)
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        lines = fh.read().splitlines()
                    for ln_no, text in enumerate(lines, 1):
                        present = set(prefix_re.findall(text))
                        if not present:
                            continue
                        # findall is non-overlapping, so also admit shorter
                        # prefixes contained in a longer one that matched
                        present |= {p for p in by_prefix if any(p in q for q in present)}
                        todo = {a["id"]: a for p in present for a in by_prefix.get(p, [])}
                        for a in todo.values():
                            for m in a["search"].finditer(text):
                                key = (rel, ln_no, m.start(1), m.end(1))
                                o = occ.setdefault(key, {
                                    "types": set(), "expected": defaultdict(set),
                                    "follow": follow_class(text[m.end(1):m.end(1) + 1]),
                                    "last_char_hyphen": m.group(1).endswith("-")})
                                o["types"].add(a["id"])
                                for tool, rule in a["expected"].items():
                                    if rule:
                                        o["expected"][tool].add(rule)
                    for text in lines:
                        for m in _TOKEN.finditer(text):
                            if _credential_shaped(m.group(1)):
                                fc = follow_class(text[m.end(1):m.end(1) + 1])
                                part_c[(stratum, lang)][fc] += 1
                                part_c_repo[repo][fc] += 1
                                part_c_path[bool(_TEST_PATH.search(rel))][fc] += 1
                                part_c_hyphen[m.group(1).endswith("-")] += 1
            for (rel, ln_no, _s, _e), o in sorted(occ.items()):
                cands.append({
                    "repo": repo, "language": lang, "stratum": stratum,
                    "types": sorted(o["types"]),
                    "follow": o["follow"],
                    "last_char_hyphen": o["last_char_hyphen"],
                    "test_or_doc_path": bool(_TEST_PATH.search(rel)),
                    "detected": {
                        tool: bool(o["expected"][tool] & hit[(tool, rel, ln_no)])
                        if o["expected"][tool] else None
                        for tool in tt.TOOLS},
                })
            shutil.rmtree(root, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if dump:
            dump.close()

    # Part B summary
    def rate(rows, tool):
        rs = [r["detected"][tool] for r in rows if r["detected"][tool] is not None]
        return {"n": len(rs), "detected": sum(rs),
                "rate": round(sum(rs) / len(rs), 4) if rs else None}
    by_follow = defaultdict(list)
    for c in cands:
        by_follow[c["follow"]].append(c)
    part_b = {
        "n_occurrences": len(cands),
        "n_types_occurring": len({t for c in cands for t in c["types"]}),
        "by_language": dict(Counter(c["language"] for c in cands)),
        "by_repo": dict(Counter(c["repo"] for c in cands)),
        "in_test_or_doc_path": sum(c["test_or_doc_path"] for c in cands),
        "following_character": dict(Counter(c["follow"] for c in cands).most_common()),
        "detection_overall": {tool: rate(cands, tool) for tool in tt.TOOLS},
        # the subject suppresses findings under test and fixture paths by
        # design, so its rate is reported split by path
        "detection_by_path": {
            label: {tool: rate([c for c in cands if c["test_or_doc_path"] == flag], tool)
                    for tool in tt.TOOLS}
            for label, flag in (("test_doc_or_example_path", True), ("other_path", False))},
        "detection_by_following_character": {
            f: {tool: rate(rows, tool) for tool in tt.TOOLS}
            for f, rows in sorted(by_follow.items(), key=lambda kv: -len(kv[1]))},
        "gitleaks_terminator_split": {
            "accepted_by_class": rate([c for c in cands if c["follow"] in
                                       ("EOL", "quote", "whitespace", ";")], "gitleaks"),
            "rejected_by_class": rate([c for c in cands if c["follow"] not in
                                       ("EOL", "quote", "whitespace", ";")], "gitleaks"),
        },
        "hyphen_terminated": sum(c["last_char_hyphen"] for c in cands),
        "occurrences": cands,
    }
    part_b["by_stratum"] = {
        st: {"n_occurrences": len(rows),
             "following_character": dict(Counter(c["follow"] for c in rows).most_common()),
             "detection_overall": {tool: rate(rows, tool) for tool in tt.TOOLS}}
        for st in ("general", "credential_dense")
        for rows in [[c for c in cands if c["stratum"] == st]]}
    def c_summary(counter):
        n = sum(counter.values())
        rej = sum(v for k, v in counter.items() if k not in GITLEAKS_ACCEPTED)
        return {"n_tokens": n,
                "share_followed_by_char_gitleaks_terminator_rejects":
                    round(rej / n, 4) if n else None,
                "share_by_following_character": {
                    k: round(v / n, 4) for k, v in counter.most_common(15)} if n else {}}
    langs = sorted({lg for _st, lg in part_c})
    part_c_out = {
        "definition": ("20-100 chars of [A-Za-z0-9_+/-] (optional = padding), "
                       "upper+lower+digit, Shannon entropy > 4.0, not hex-only, "
                       "fewer than two '/'"),
        "gitleaks_accepted_terminators": sorted(GITLEAKS_ACCEPTED),
        "overall": c_summary(sum(part_c.values(), Counter())),
        "by_stratum": {st: c_summary(sum((v for (s2, _l), v in part_c.items() if s2 == st),
                                         Counter()))
                       for st in ("general", "credential_dense")},
        "by_language": {lg: c_summary(sum((v for (_s, l2), v in part_c.items() if l2 == lg),
                                          Counter()))
                        for lg in langs},
        "tokens_ending_in_hyphen": part_c_hyphen[True],
        # one repository can dominate the pooled count (npm/cli's registry
        # mock fixtures hold ~237k of the tokens), so the share is also given
        # per repository, as an unweighted mean and median over
        # repositories, and split by test/doc/fixture path
        "by_repository": {r: c_summary(c) for r, c in sorted(part_c_repo.items())},
        "repository_weighted": _repo_weighted(part_c_repo),
        "by_path": {"test_doc_or_fixture_path": c_summary(part_c_path[True]),
                    "other_path": c_summary(part_c_path[False])},
    }
    tot_lines = sum(p["n_lines"] for p in projects.values())

    def totals(ps):
        lines = sum(p["n_lines"] for p in ps)
        return {"n_projects": len(ps), "n_lines": lines, **{tool: {
            "n_findings": sum(p["tools"][tool]["n_findings"] for p in ps),
            "findings_per_kloc": round(sum(p["tools"][tool]["n_findings"] for p in ps)
                                       / (lines / 1000), 4) if lines else None}
            for tool in tt.TOOLS}}
    return {
        "design": __doc__.strip().splitlines()[0],
        "n_projects": len(projects),
        "n_languages": len({p["language"] for p in projects.values()}),
        "n_files": sum(p["n_files"] for p in projects.values()),
        "n_lines": tot_lines,
        "totals": {tool: {
            "n_findings": sum(p["tools"][tool]["n_findings"] for p in projects.values()),
            "findings_per_kloc": round(sum(p["tools"][tool]["n_findings"]
                                           for p in projects.values()) / (tot_lines / 1000), 4),
        } for tool in tt.TOOLS},
        "totals_by_stratum": {st: totals([p for p in projects.values() if p["stratum"] == st])
                              for st in ("general", "credential_dense")},
        "totals_by_language": {lg: totals([p for p in projects.values() if p["language"] == lg])
                               for lg in sorted({p["language"] for p in projects.values()})},
        "projects": projects,
        "part_b_credential_shaped_literals": part_b,
        "part_c_credential_shaped_tokens": part_c_out,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
