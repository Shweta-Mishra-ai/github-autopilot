<!-- DRAFT — not posted. Target: mongodb/kingfisher -->

# Imported Betterleaks rules miss a secret followed by `)`, `,` or `&`

## Summary

Most of Kingfisher's built-in rules are generated from Betterleaks'
`config/betterleaks.toml` (461 of 488 rules in 2.11.1, by
`generated/provenance.json`). Those rules end in Betterleaks' terminator
group `(?:\\?['"\x60]|[\s;]|\\[nr]|$)`, so a secret is reported only when
followed by an (optionally escaped) quote or backtick, whitespace, `;`, an
escaped `\n`/`\r` or the end of input. A credential passed as a call
argument, followed by a comma, or followed by `&` in a query string is not
reported. The defect is in the upstream table (a companion report is
drafted for Betterleaks); this report is so that Kingfisher users and the
importer know about it.

## Reproduction (kingfisher 2.11.1 and 2.14.0)

Same files as the Betterleaks report (random made-up value):

```
a.txt  bittrex_key = "<32 hex>"
b.txt  client = Client(bittrex_key=<32 hex>)
c.txt  url = "https://api.example.com/v1?bittrex_key=<32 hex>&page=2"
d.txt  client = Client(bittrex_key=<32 hex>, retries=3)
$ kingfisher scan . --format jsonl --no-validate --no-dedup
```

| file | 2.11.1 | 2.14.0 |
|---|---|---|
| a.txt (quoted) | found | found |
| b.txt (`)`) | **missed** | **missed** |
| c.txt (`&`) | **missed** | **missed** |
| d.txt (`,`) | **missed** | **missed** |

Values must be random: a sequential value such as `0123456789abcdef…`
is discarded by a placeholder filter, which is a different (and correct)
behaviour.

## Scale (2.11.1)

We generated 20 values for each of 1,238 credential types drawn from five
scanners' rule tables, embedded each in a battery of source contexts, and
ran Kingfisher with `--no-validate --no-dedup`. Of the 616 types Kingfisher
detects under a rule for the same provider, a trailing `)` defeats every
value for 436 (71%; 95% interval 0.60–0.81, resampling providers), `&` in
a query string for 436, and a trailing `, …` for 268. Gitleaks 8.21.2
shows the same pattern (302 of 427 for `)`).
Method and data: <!-- link to the paper / replication package -->.

## Suggested change

Picking up a fix in the Betterleaks table would carry over on the next
import. The change proposed there, applied to every rule ending in the
group except the `generic-*` rules, is

```diff
-(?:\\?['"\x60]|[\s;]|\\[nr]|$)
+(?:\\?['"\x60]|[\s;,)\]}>&<\\]|\\[nr]|$)
```

With it, Betterleaks 1.9.0 finds b, c and d above and, over 38,309 files
(9,870,820 lines) of 31 open-source projects, adds no finding and removes
none. We did not rebuild Kingfisher with the changed rules; the false-
positive check is Betterleaks'.

## Note on `--no-dedup`

Running without `--no-dedup` reports an identical secret once across
files, so a test corpus that repeats one value in many contexts measures
the deduplication, not the rules. Not a defect; mentioned because it is
easy to trip over when evaluating rules.
