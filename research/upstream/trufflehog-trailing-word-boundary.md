<!-- DRAFT — not posted. Target: trufflesecurity/trufflehog -->

# Detectors miss a token that ends in `-` (or `.`) when a quote, space or line end follows

## Summary

Many detectors end their pattern with `(<secret>)\b` where the secret's
character class includes `-` (or `.`, `/`, `+`, `=`). `\b` needs a word
character on one side, so a token whose last character is `-` and which is
followed by a quote, whitespace or the end of the line has no word boundary
after it, and the detector does not fire. Conversely, a *longer* string
`…-x` is reported, truncated.

In v3.99.3, 403 patterns in 342 detector files end in `\b` after a class
that contains a non-word character (static count over `pkg/detectors`,
by the script below); in 3.82.13 it was 381 in 324 files.

## Reproduction (Replicate detector, `\b(r8_[0-9A-Za-z-_]{37})\b`)

`<36 chars>` is a random alphanumeric value (seeded; a literal token-shaped
value would trip secret scanning on this page).

```
a.txt  token = "r8_<36 chars>x"
b.txt  token = "r8_<36 chars>-"
c.txt  REPLICATE_API_TOKEN=r8_<36 chars>-
d.txt  token = "r8_<36 chars>-x"        # 41 characters: not a valid token
$ trufflehog filesystem . --json --no-verification --no-update
```

| file | 3.82.13 | 3.99.3 | 3.99.3 with the change below |
|---|---|---|---|
| a.txt (ends in a letter) | found | found | found |
| b.txt (ends in `-`, then `"`) | **missed** | **missed** | found |
| c.txt (ends in `-`, then end of line) | **missed** | **missed** | found |
| d.txt (`-` then a letter) | found (truncated to 40) | found (truncated) | found (truncated) |

## Scale

We generated 20 values for each of 1,238 credential types drawn from five
scanners' rule tables and embedded each in a battery of source contexts.
On 3.82.13 (rebuilt from the tag with go1.23.2; its detections are
identical to the release binary's on all 745,840 test files), a value
ending in `-` defeats 97 of the 132 types whose format allows a final
hyphen and that TruffleHog detects under the right detector. With the
change below: 0 of 132, no type newly failing in any other context, and
the same 688 types detected. Method and data:
<!-- link to the paper / replication package -->.

## Suggested change

For exactly those patterns, replace the final `\b` with `(?:\b|\W|$)`:

```diff
-	keyPat = regexp.MustCompile(`\b(r8_[0-9A-Za-z-_]{37})\b`)
+	keyPat = regexp.MustCompile(`\b(r8_[0-9A-Za-z-_]{37})(?:\b|\W|$)`)
```

- It accepts everything `\b` accepts, so no current match is lost
  (case d is unchanged).
- The group does not capture, so `match[1]` is still the secret.
- Patterns whose class is all word characters are left alone; there `\b`
  already behaves correctly.
- `repair/repair_trufflehog.py SRC_TREE REPORT.json` in the replication
  package makes the edit across `pkg/detectors` and lists every pattern it
  changed.
- The mirror-image case, a token *starting* with `-` after a quote, is not
  addressed.

## False-positive check

Over 38,309 files (9,870,820 lines) of 31 open-source projects in ten
languages, each binary run three times and only findings present in every
run counted:

| | unmodified | with the change | added | removed |
|---|---|---|---|---|
| 3.99.3 (source build) | 239 | 239 | 0 | 0 |
| 3.82.13 (source build) | 389 | 389 | 0 | 0 |

## Build note

A go1.24 build of v3.82.13 panics at start-up inside wazero 1.8.0
(`wasm error: invalid table access` while compiling a detector pattern);
go1.23.2, the toolchain of the 3.82.13 release, works. v3.99.3 was built
with go1.25.0.
