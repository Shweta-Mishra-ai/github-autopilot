"""
repair_gitleaks.py — Two repairs of the Gitleaks 8.21.2 rule table's
boundary handling, written as text edits of the pinned config/gitleaks.toml
so that each changed rule can be diffed against the original.

THE DEFECT
  134 of the table's 195 rules end with the group
      (?:['|\\"|\\n|\\r|\\s|\\x60|;]|$)
  a character class written as if it were an alternation. It admits a
  quote, '|', whitespace, a backtick, ';' or the end of input after the
  secret, and nothing else, so a credential followed by ')', ',', '&', ']',
  '}', '>' or a backslash is not reported. The two Hugging Face rules end
  with (?:$|[\\'"` <]) without (?m), so '$' is the end of the scanned
  fragment and a token followed by a newline is missed; the access-token
  rule's leading class also lacks the '(' and ',' its organisation-token
  sibling has, so hf_... inside parentheses is missed at the other end.

R1 (minimal)  Keep the delimiter-list design; add the delimiters of source
              code it omits: , ) ] } > < & and backslash; for the Hugging
              Face rules also the line break.
R1P           R1 on the provider rules only: generic-api-key, the fallback
              rule, keeps its original terminator. On 9.87M lines of real
              code every finding R1 adds comes from generic-api-key
              (validate_gitleaks_repair.py), so R1P is the repair whose
              false-positive cost there is measured as zero.
R2 (alphabet) The secret ends where its own alphabet ends: the terminator
              is any character the secret's pattern cannot contain, or the
              end of input -- what a word boundary would do for an alphabet
              of word characters, written without look-around, which RE2
              does not have. Every character the original terminator
              accepts stays accepted (hashicorp-tf-password's alphabet
              contains '"'), so R2, like R1, matches everything the original
              matches.

Both repairs only widen the set of accepted terminators, so neither can
lose a detection the original makes; validate_gitleaks_repair.py checks
that on real code (findings removed: 0).

Go's RE2 has no look-ahead, so both repairs consume the terminating
character, as the original group does; the secret is the first capture
group in every changed rule, so the reported secret is unchanged.

LATER RELEASES
  Gitleaks 8.30.1 rewrote the group as (?:[\\x60'"\\s;]|\\\\[nr]|$): a real
  class now, and '\\s' takes in the line break, which fixes the Hugging
  Face newline case, but ')', ',', '&', ']', '}', '>', '<' and a backslash
  are still rejected. --release 8.30 applies R1P's addition to that form
  (the 151 rules there ending in it, less generic-api-key: 150);
  --release betterleaks-1.9 to Betterleaks 1.9.0's
  (?:\\\\?['"\\x60]|[\\s;]|\\\\[nr]|$), which Kingfisher imports.

Run: python repair_gitleaks.py gitleaks.toml OUTDIR
     python repair_gitleaks.py --release {8.30|betterleaks-1.9} TABLE OUTDIR
     writes OUTDIR/gitleaks.{R1,R1P,R2}.toml and
     OUTDIR/repair_gitleaks.json (what changed, per rule)
"""
from __future__ import annotations

import json
import os
import re
import string
import sys
import tomllib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "benchmarks"))
import bench_gitleaks_fulltable as gl  # noqa: E402

TERM = gl.TERM
R1_TERM = r"(?:['|\"|\n|\r|\s|\x60|;,)\]}>&<\\]|$)"
HF_TERMS = {
    r'''(?:$|[\\'"` <])''': r'''(?:$|[\\'"` <\n\r,;)\]}>&])''',
    r'''(?:$|[\\'"` <\),])''': r'''(?:$|[\\'"` <\),\n\r;\]}>&])''',
}
# what the original terminators accept, kept accepted by R2
TERM_CHARS = set("'|\"\n\r \t`;")
HF_TERM_CHARS = {r'''(?:$|[\\'"` <])''': set("\\'\"` <"),
                 r'''(?:$|[\\'"` <\),])''': set("\\'\"` <),")}
HF_LEAD_OLD = r'''(?:^|[\\'"` >=:])'''
HF_LEAD_NEW = r'''(?:^|[\\'"` >=:\(,])'''      # as its organisation-token sibling
HF_RULES = ("huggingface-access-token", "huggingface-organization-api-token")
PRINTABLE = [c for c in string.printable if c not in "\x0b\x0c"]


def _atoms(p, quantifiers=True):
    """Top-level atoms of a Go pattern, each with its quantifier unless
    quantifiers=False (bench_multitool._atoms, kept here so the repair does
    not import the six-scanner harness)."""
    out, i = [], 0
    while i < len(p):
        if p[i] == "\\":
            j = i + (4 if p[i + 1:i + 2] == "x" else 2)
        elif p[i] == "[":
            j = i + 1
            if p[j:j + 1] == "^":
                j += 1
            if p[j:j + 1] == "]":
                j += 1
            while p[j] != "]":
                j += 2 if p[j] == "\\" else 1
            j += 1
        elif p[i] == "(":
            j = gl._group_end(p, i)
        else:
            j = i + 1
        base = p[i:j]
        m = re.match(r"(?:[*+?]|\{\d*,?\d*\})\??", p[j:])
        if m:
            j += m.end()
        out.append(p[i:j] if quantifiers else base)
        i = j
    return out


def alphabet(secret, icase):
    """Every printable ASCII character the secret's pattern can contain."""
    chars = set()
    for a in _atoms(secret, quantifiers=False):
        flag = re.fullmatch(r"\(\?([a-zA-Z-]+)\)", a)
        if flag:
            # a mid-pattern flag such as p8e-(?i)[a-z0-9]{32}; treating the
            # whole secret as case-insensitive only widens the alphabet,
            # which makes the terminator stricter, never looser
            icase = icase or "i" in flag.group(1).split("-")[0]
            continue
        if a.startswith("("):
            scoped = re.match(r"\(\?([a-zA-Z]*)(?:-[a-zA-Z]*)?:", a)
            inner = re.sub(r"^\((?:\?[a-zA-Z-]*:|\?P<\w+>)?", "", a)[:-1]
            # '|' is skipped below; (?i:...) scopes case-insensitivity
            chars |= alphabet(inner, icase or bool(scoped and "i" in scoped.group(1)))
            continue
        if a == "|":
            continue
        rx = re.compile(gl._ascii_classes(a), re.I if icase else 0)
        chars |= {c for c in PRINTABLE if rx.fullmatch(c)}
    if icase:
        chars |= {c.swapcase() for c in chars}
    return chars


def negated_class(chars):
    """[^...] over `chars`, escaped for Go and Python alike."""
    body = "".join("\\" + c if c in "\\]^-[" else
                   {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(c, c)
                   for c in sorted(chars))
    return f"[^{body}]"


def secret_of(regex):
    span = gl._first_capture(regex)
    inner = regex[span[0] + 1:span[1] - 1]
    if inner.startswith("?P<"):
        inner = inner[inner.index(">") + 1:]
    return inner


def _replace_regex(text, rid, old, new):
    lines = text.split("\n")
    i = lines.index(f'id = "{rid}"')
    j = next(j for j in range(i + 1, len(lines)) if lines[j].startswith("regex = "))
    assert lines[j] == f"regex = '''{old}'''", rid
    lines[j] = f"regex = '''{new}'''"
    return "\n".join(lines)


def main(src, outdir):
    text = open(src).read()
    cfg = tomllib.loads(text)
    r1, r1p, r2, report = text, text, text, {}
    for rule in cfg["rules"]:
        rx, rid = rule["regex"], rule["id"]
        if TERM in rx:
            assert rx.endswith(TERM), rid
            icase = rx.startswith("(?i)")
            alpha = alphabet(secret_of(rx[:-len(TERM)]), icase)
            new1 = rx[:-len(TERM)] + R1_TERM
            new2 = rx[:-len(TERM)] + f"(?:{negated_class(alpha - TERM_CHARS)}|$)"
        elif rid in HF_RULES:
            old = next(t for t in HF_TERMS if rx.endswith(t))
            alpha = alphabet(secret_of(rx), False)
            body = rx[:-len(old)].replace(HF_LEAD_OLD, HF_LEAD_NEW)
            new1 = body + HF_TERMS[old]
            new2 = body + f"(?:$|{negated_class(alpha - HF_TERM_CHARS[old])})"
        else:
            continue
        # two rules (bittrex-access-key, -secret-key) share one regex, so the
        # edit is anchored to the rule's own id line
        r1 = _replace_regex(r1, rid, rx, new1)
        r2 = _replace_regex(r2, rid, rx, new2)
        if rid != gl.GENERIC:
            r1p = _replace_regex(r1p, rid, rx, new1)
        report[rid] = {"original": rx, "R1": new1, "R2": new2,
                       "R1P": new1 if rid != gl.GENERIC else rx}
    os.makedirs(outdir, exist_ok=True)
    for name, body in (("gitleaks.R1.toml", r1), ("gitleaks.R1P.toml", r1p),
                       ("gitleaks.R2.toml", r2)):
        tomllib.loads(body)                       # still valid TOML
        with open(os.path.join(outdir, name), "w") as fh:
            fh.write(body)
    with open(os.path.join(outdir, "repair_gitleaks.json"), "w") as fh:
        json.dump({"n_rules": len(cfg["rules"]), "n_changed": len(report),
                   "rules": report}, fh, indent=2)
    print(f"{len(report)} of {len(cfg['rules'])} rules changed")


# Later tables: the terminator in its new form -> the same added delimiters
RELEASES = {
    # Gitleaks 8.30.1
    "8.30": (r"""(?:[\x60'"\s;]|\\[nr]|$)""",
             r"""(?:[\x60'"\s;,)\]}>&<\\]|\\[nr]|$)""", "gitleaks-8.30.R1P.toml"),
    # Betterleaks 1.9.0, whose table Kingfisher imports
    "betterleaks-1.9": (r"""(?:\\?['"\x60]|[\s;]|\\[nr]|$)""",
                        r"""(?:\\?['"\x60]|[\s;,)\]}>&<\\]|\\[nr]|$)""",
                        "betterleaks-1.9.R1P.toml"),
}


def main_release(release, src, outdir):
    """R1P for a later table: the same added delimiters, in its own form,
    on every rule but the generic ones (generic-api-key, and Betterleaks'
    generic-password, -username and -credential-uri)."""
    term, new_term, name = RELEASES[release]
    text = open(src).read()
    cfg = tomllib.loads(text)
    out, report = text, {}
    for rule in cfg["rules"]:
        rx, rid = rule.get("regex", ""), rule["id"]
        if rid.startswith("generic-") or not rx.endswith(term):
            continue
        new = rx[:-len(term)] + new_term
        out = _replace_regex(out, rid, rx, new)
        report[rid] = {"original": rx, "R1P": new}
    tomllib.loads(out)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, name), "w") as fh:
        fh.write(out)
    with open(os.path.join(outdir, name.replace(".toml", ".json")), "w") as fh:
        json.dump({"n_rules": len(cfg["rules"]), "n_changed": len(report),
                   "rules": report}, fh, indent=2)
    print(f"{len(report)} of {len(cfg['rules'])} rules changed")


if __name__ == "__main__":
    if sys.argv[1] == "--release":
        main_release(*sys.argv[2:5])
    else:
        main(*sys.argv[1:3])
