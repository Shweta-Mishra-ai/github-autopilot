<!-- DRAFT — not posted. Target: gitleaks/gitleaks -->

# Rules miss a secret followed by `)`, `,`, `&`, `]`, `}` or a backslash

## Summary

Most rules in the default `config/gitleaks.toml` end in a terminator group
that lists the characters allowed after the secret. In 8.30.1 it is

```
(?:[\x60'"\s;]|\\[nr]|$)
```

so a secret is only reported when it is followed by a backtick, a quote,
whitespace, `;`, an escaped `\n`/`\r`, or the end of input. A credential
written as a function argument (`Client(key=…)`), in a list or call
(`…, retries=3`), or in a query string (`?key=…&page=2`) is not reported.

151 of the 222 rules in 8.30.1 end in this group (static count of the
table). The same behaviour is present in 8.21.2, where the group was
written `(?:['|\"|\n|\r|\s|\x60|;]|$)`.

## Reproduction (8.30.1, default config)

Made-up values; random rather than sequential so that no placeholder or
entropy filter is involved.

```
$ cat a.txt
bittrex_key = "8b0e7153bf7c3706d85c524e44006655"
$ cat b.txt
client = Client(bittrex_key=8b0e7153bf7c3706d85c524e44006655)
$ cat c.txt
url = "https://api.example.com/v1?bittrex_key=8b0e7153bf7c3706d85c524e44006655&page=2"
$ cat d.txt
client = Client(bittrex_key=8b0e7153bf7c3706d85c524e44006655, retries=3)
$ cat g.txt
login(hf_<34 letters>)
$ gitleaks detect --source . --no-git --report-format json --report-path r.json
```

| file | 8.30.1 | 8.30.1 with the change below |
|---|---|---|
| a.txt (quoted) | found | found |
| b.txt (`)`) | **missed** | found |
| c.txt (`&`) | **missed** | found |
| d.txt (`,`) | **missed** | found |
| g.txt (Hugging Face, `)`)¹ | **missed** | found |

(8.21.2 behaves the same, and additionally misses a Hugging Face token
followed by a newline; 8.30.1 fixed that case.)

¹ `<34 letters>` stands for a random 34-letter value: a literal one here
would be flagged as a Hugging Face token by secret scanning (GitHub's push
protection rejected this draft for that reason). `reproduce_matrix.py` in
the replication package writes every case with seeded values.

## Scale

On 8.21.2 we generated 40 values per rule from each rule's own secret
pattern and embedded each value in a battery of source contexts (quoted,
unquoted, YAML, JSON, `.env`, URL query, call argument, list, …). Of the 183
rules analysed, 135 lose more than half the values in some context: 134 of
them when the secret is followed by `)`, 95 of those also when it is
followed by `,`; the remaining one is the Hugging Face organisation-token
rule, which loses values followed by a newline. With the same change applied
to the 8.21.2 table (in that release's form of the group), no provider rule
loses more than half the values in any context.
Method and data: <!-- link to the paper / replication package -->.

## Suggested change

Add the missing delimiters to the class, on every rule that ends in the
group except `generic-api-key`:

```diff
-([a-z0-9]{32})(?:[\x60'"\s;]|\\[nr]|$)
+([a-z0-9]{32})(?:[\x60'"\s;,)\]}>&<\\]|\\[nr]|$)
```

The change only adds accepted characters, so no existing match is lost; the
secret is still the first capture group and the terminator is still
consumed, as now. A script that applies it to the whole table is in the
replication package (`repair/repair_gitleaks.py --release 8.30`).

## False-positive check

Gitleaks 8.30.1 with the original and the changed table over the same
38,309 files (9,870,820 lines) of 31 open-source projects in nine languages:
1,057 findings each, **0 added, 0 removed**.

## Notes

- `generic-api-key` is deliberately left out. Applying the same change to it
  on 8.21.2 added 25 findings on the corpus above, every one a test fixture,
  mock or placeholder (19 of them before a backslash); for the provider
  rules the change added none.
- A secret inside an escaped JSON string,
  `payload = "{\"bittrex_key\": \"…\"}"`, is still missed with the change:
  the backslash is rejected on the *leading* side, between the keyword and
  the value. That is a separate issue and not addressed here.
