"""
reproduce_matrix.py — The minimal reproducers behind the upstream issue
drafts in this directory, run against every scanner version named on the
command line.

Each reproducer is one line with one made-up credential. Values are drawn
from a seeded generator, not typed: Gitleaks, Betterleaks and Kingfisher
discard sequential or low-entropy values such as 0123456789abcdef..., and a
reproducer built on one reports that filter instead of the boundary.

Run: python reproduce_matrix.py OUTDIR NAME=KIND:BINARY[:CONFIG] ...
     KIND is gitleaks, betterleaks, kingfisher or trufflehog
e.g. python reproduce_matrix.py /tmp/m "gitleaks 8.30.1=gitleaks:/opt/gitleaks" \
         "trufflehog 3.99.3=trufflehog:/opt/trufflehog"
"""
from __future__ import annotations

import json
import os
import random
import string
import subprocess
import sys
import tempfile

def _draw(seed, alphabet, n):
    rng = random.Random(seed)              # one generator per value
    return "".join(rng.choice(alphabet) for _ in range(n))


HEX = _draw(5, "0123456789abcdef", 32)
HF = _draw(7, string.ascii_letters, 34)
R8 = _draw(11, string.ascii_letters + string.digits, 36)

CASES = {
    # Gitleaks family: a keyword rule (Bittrex) and a prefix rule (Hugging Face)
    "gl_a_quoted":        f'bittrex_key = "{HEX}"',
    "gl_b_parens":        f"client = Client(bittrex_key={HEX})",
    "gl_c_amp":           f'url = "https://api.example.com/v1?bittrex_key={HEX}&page=2"',
    "gl_d_comma":         f"client = Client(bittrex_key={HEX}, retries=3)",
    "gl_e_escaped":       f'payload = "{{\\"bittrex_key\\": \\"{HEX}\\"}}"',
    "gl_f_hf_newline":    f"token = hf_{HF}\nnext = 1",
    "gl_g_hf_parens":     f"login(hf_{HF})",
    "gl_h_hf_quoted":     f'token = "hf_{HF}"',
    # TruffleHog: Replicate's r8_ token, whose alphabet includes '-'
    "th_a_letter":        f'token = "r8_{R8[:36]}x"',
    "th_b_hyphen":        f'token = "r8_{R8[:36]}-"',
    "th_c_hyphen_eol":    f"REPLICATE_API_TOKEN=r8_{R8[:36]}-",
    "th_d_hyphen_letter": f'token = "r8_{R8[:36]}-x"',
}


def run(kind, binary, config, d):
    out = set()
    if kind in ("gitleaks", "betterleaks"):
        rep = os.path.join(os.path.dirname(d), os.path.basename(d) + "_report.json")
        cmd = ([binary, "detect", "--source", d, "--no-git"] if kind == "gitleaks"
               else [binary, "dir", d])
        cmd += ["--report-format", "json", "--report-path", rep, "--no-banner",
                "--log-level", "error"] + (["--config", config] if config else [])
        subprocess.run(cmd, capture_output=True)
        out = {os.path.basename(f["File"]) for f in json.load(open(rep)) or []}
    elif kind == "kingfisher":
        p = subprocess.run([binary, "scan", d, "--format", "jsonl", "--no-validate",
                            "--no-dedup", "-q", "--no-update-check"],
                           capture_output=True, text=True)
        for line in p.stdout.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "finding" in r:
                out.add(os.path.basename(r["finding"]["path"]))
    elif kind == "trufflehog":
        p = subprocess.run([binary, "filesystem", d, "--json", "--no-verification",
                            "--no-update"], capture_output=True, text=True)
        for line in p.stdout.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "DetectorName" in r:
                out.add(os.path.basename(
                    r["SourceMetadata"]["Data"]["Filesystem"]["file"]))
    return out


def main(outdir, *specs):
    d = tempfile.mkdtemp(prefix="repro_")    # the scanned tree: case files only
    for name, text in CASES.items():
        with open(os.path.join(d, f"{name}.txt"), "w") as fh:
            fh.write(text + "\n")
    cols = {}
    for spec in specs:
        name, rest = spec.split("=", 1)
        kind, binary, *cfg = rest.split(":")
        cols[name] = run(kind, binary, cfg[0] if cfg else None, d)
    prefixes = {"gitleaks": "gl_", "betterleaks": "gl_", "kingfisher": "gl_",
                "trufflehog": "th_"}
    os.makedirs(outdir, exist_ok=True)
    matrix = {}
    for spec, (name, found) in zip(specs, cols.items()):
        kind = spec.split("=", 1)[1].split(":")[0]
        matrix[name] = {c: (f"{c}.txt" in found) for c in CASES
                        if c.startswith(prefixes[kind])}
    with open(os.path.join(outdir, "reproducer_matrix.json"), "w") as fh:
        json.dump({"cases": CASES, "found": matrix}, fh, indent=2)
    for name, row in matrix.items():
        print(name.ljust(26), " ".join(f"{c}:{'found' if v else 'MISSED'}"
                                        for c, v in row.items()))


if __name__ == "__main__":
    main(*sys.argv[1:])
