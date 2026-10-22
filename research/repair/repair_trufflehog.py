"""
repair_trufflehog.py — A repair of TruffleHog 3.82.13's trailing word
boundaries, written as edits of the detector sources at the pinned commit
(1aa1871f9ae24a8c8a3a48a9345514acf42beb39), to be rebuilt with `go build`.

THE DEFECT
  Many detectors end their pattern with `(<secret>)\\b`. When the secret's
  alphabet includes a character that is not a word character ('-', '.',
  '/', '+', '='), a value ending in such a character and followed by a
  quote, a space or the end of a line has no word boundary after it, and
  the detector does not fire: the hyphen defect of the paper's RQ2.

THE REPAIR
  For exactly those patterns -- the trailing group's alphabet contains a
  non-word character -- the final `\\b` becomes `(?:\\b|\\W|$)`: a word
  boundary as before, or any non-word character, or the end of input. It
  accepts everything `\\b` accepts, so no detection the original makes is
  lost, and it accepts a value ending in '-' or '.' followed by a quote, a
  space or the end of a line, which `\\b` does not. (The complement of the
  secret's alphabet, repair_gitleaks.py's R2, would reject a hyphen after a
  value ending in a letter, where `\\b` accepts, and so is not monotone
  here.) Patterns whose alphabet is all word characters are left alone,
  since there the two are the same. The added group does not capture, so
  the detectors' submatch indices are unchanged. The alphabet is computed
  case-insensitively for patterns built with detectors.PrefixRegex, which
  prepends (?i). A leading `\\b` before a secret that may begin with a
  non-word character has the mirror-image defect; the battery does not
  mutate the first character, so it is not repaired here (and not claimed).

Run: python repair_trufflehog.py SRC_TREE REPORT.json   (edits SRC_TREE in place)
"""
from __future__ import annotations

import glob
import json
import os
import re
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "benchmarks"))
import bench_gitleaks_fulltable as gl  # noqa: E402
from repair_gitleaks import alphabet  # noqa: E402

WORD = set(string.ascii_letters + string.digits + "_")
_LIT = re.compile(r"`([^`]*)`")


def trailing_group(body):
    """Start of the top-level group that ends right before the final \\b."""
    i, end = 0, len(body) - 2
    while i < end:
        if body[i] == "\\":
            i += 2
            continue
        if body[i] == "[":
            j = i + 1
            if body[j:j + 1] == "^":
                j += 1
            if body[j:j + 1] == "]":
                j += 1
            while body[j] != "]":
                j += 2 if body[j] == "\\" else 1
            i = j + 1
            continue
        if body[i] == "(":
            j = gl._group_end(body, i)
            if j == end:
                return i
            i = j
            continue
        i += 1
    return None


def repair_literal(body, icase):
    if not body.endswith(")\\b"):
        return None
    start = trailing_group(body)
    if start is None:
        return None
    inner = re.sub(r"^\((?:\?[a-zA-Z-]*:|\?P<\w+>)?", "", body[start:-2])[:-1]
    try:
        alpha = alphabet(inner, icase or body.startswith("(?i)"))
    except (re.error, ValueError, IndexError):
        return "untranslatable"
    if not alpha - WORD:
        return None
    return body[:-2] + r"(?:\b|\W|$)", sorted(alpha - WORD)


def main(tree, report_path):
    root = os.path.join(tree, "pkg", "detectors")
    report, n_trailing, untranslatable = {}, 0, []
    for f in sorted(glob.glob(os.path.join(root, "**", "*.go"), recursive=True)):
        if f.endswith("_test.go"):
            continue
        src = open(f).read()
        out, pos, changed = [], 0, []
        for m in _LIT.finditer(src):
            before = src[max(0, m.start() - 200):m.start()]
            call = before[before.rfind("MustCompile("):] if "MustCompile(" in before else ""
            if not call or "\n\n" in call:
                continue
            body = m.group(1)
            n_trailing += body.endswith(")\\b")
            r = repair_literal(body, "PrefixRegex(" in call)
            if r == "untranslatable":
                untranslatable.append({"file": os.path.relpath(f, root), "pattern": body})
                continue
            if r is None:
                continue
            new, nonword = r
            out.append(src[pos:m.start()] + f"`{new}`")
            pos = m.end()
            changed.append({"original": body, "repaired": new,
                            "non_word_chars_in_alphabet": nonword})
        if changed:
            out.append(src[pos:])
            with open(f, "w") as fh:
                fh.write("".join(out))
            report[os.path.relpath(f, root)] = changed
    with open(report_path, "w") as fh:
        json.dump({"patterns_ending_in_group_then_word_boundary": n_trailing,
                   "n_patterns_repaired": sum(len(v) for v in report.values()),
                   "n_files_changed": len(report),
                   "not_analysable": untranslatable, "files": report}, fh, indent=2)
    print(f"{sum(len(v) for v in report.values())} patterns in {len(report)} files "
          f"repaired, of {n_trailing} ending in a group and \\b")


if __name__ == "__main__":
    main(*sys.argv[1:3])
