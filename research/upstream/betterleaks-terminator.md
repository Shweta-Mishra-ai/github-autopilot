<!-- DRAFT — not posted. Target: betterleaks/betterleaks -->

# Rules miss a secret followed by `)`, `,`, `&`, `]`, `}` or a backslash

## Summary

336 of the 463 rules in `config/betterleaks.toml` at v1.9.0 (static count)
end in the terminator group

```
(?:\\?['"\x60]|[\s;]|\\[nr]|$)
```

carried over, in a revised form, from Gitleaks. A secret is only reported
when followed by an (optionally escaped) quote or backtick, whitespace,
`;`, an escaped `\n`/`\r` or the end of input, so a credential passed as a
call argument, followed by a comma, or followed by `&` in a query string is
not reported. Kingfisher imports this table, and shows the same behaviour
(2.11.1 and 2.14.0 tested).

## Reproduction (betterleaks 1.9.0, default config)

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
$ betterleaks dir . --report-format json --report-path r.json
```

| file | 1.9.0 | 1.9.0 with the change below |
|---|---|---|
| a.txt (quoted) | found | found |
| b.txt (`)`) | **missed** | found |
| c.txt (`&`) | **missed** | found |
| d.txt (`,`) | **missed** | found |
| g.txt (Hugging Face, `)`)¹ | **missed** | found |

¹ `<34 letters>` stands for a random 34-letter value: a literal one here
would be flagged as a Hugging Face token by secret scanning (GitHub's push
protection rejected this draft for that reason). `reproduce_matrix.py` in
the replication package writes every case with seeded values.

The default embedded config and `--config config/betterleaks.toml` give
identical results here.

## Suggested change

On every rule ending in the group except the `generic-*` rules (335 rules):

```diff
-([a-z0-9]{32})(?:\\?['"\x60]|[\s;]|\\[nr]|$)
+([a-z0-9]{32})(?:\\?['"\x60]|[\s;,)\]}>&<\\]|\\[nr]|$)
```

It only adds accepted characters, so no existing match is lost.
`repair/repair_gitleaks.py --release betterleaks-1.9` in the replication
package applies it to the table.

## False-positive check

betterleaks 1.9.0 with the original and the changed table over 38,309
files (9,870,820 lines) of 31 open-source projects in nine languages: 1,358
findings each, **0 added, 0 removed**.

## Notes

- The `generic-*` rules are left out for the reason given in the companion
  Gitleaks report: on Gitleaks 8.21.2 the same change to `generic-api-key`
  added 25 findings on this corpus, all test fixtures or placeholders.
- `payload = "{\"bittrex_key\": \"…\"}"` is still missed with the change;
  the backslash is rejected between the keyword and the value, a separate
  issue.
