# Replication package
**Boundary-Mutation Testing for Pattern-Based Secret Detection: A Rule-Level Method and Cross-Scanner Evaluation**

Every number and figure in the paper is produced by the code in this package
from the data in `results/`. Nothing is estimated or extrapolated.

Archived on Zenodo: [10.5281/zenodo.22114221](https://doi.org/10.5281/zenodo.22114221)

This package lives in the `research/` directory of the subject repository:
<https://github.com/Shweta-Mishra-ai/github-autopilot/tree/main/research>

The manuscript is prepared for *Empirical Software Engineering* (Springer)
using the `sn-jnl` document class. `paper/` is the single, canonical
manuscript; no other journal format is maintained in this package.

## Quick start

```bash
docker build -t bmt .
docker run --rm -v "$PWD:/work" bmt
```

`reproduce.sh` writes its outputs in place (`results/`, `figures_pdf_vector/`, `paper/main.pdf`,
`paper/online_resource_1.pdf`), so mounting this whole directory as `/work` is what makes those
outputs land back on the host, at the same paths, once the container exits.

or, on a host with Python 3.11, Redis and a TeX installation:

```bash
./reproduce.sh              # full run: benchmarks, figures, paper
./reproduce.sh --figures    # figures and paper only, from existing results/
```

## What is pinned, and what happens if it drifts

Reproducibility here means measuring *the same artefacts*, so each external
input is pinned and **verified**, and `reproduce.sh` aborts on any mismatch
rather than silently measuring something else.

| Input | Pin | Verified by |
|---|---|---|
| Subject system | commit `38b2013` (full SHA in `metadata/subject_commit.txt`) | `git rev-parse` compared to the expected SHA |
| Gitleaks | 8.21.2 | SHA-256 of the extracted binary |
| TruffleHog | 3.82.13 | SHA-256 of the extracted binary |
| `psf/requests` | commit `8f8b212` | `git rev-parse` after a pinned fetch |
| `pallets/flask` | commit `d318b68` | `git rev-parse` after a pinned fetch |
| `redis/redis-py` | commit `081923b` | `git rev-parse` after a pinned fetch |
| Gitleaks rule table | `config/gitleaks.toml` at v8.21.2 | SHA-256 of the file |
| TruffleHog detector source | commit `1aa1871` (tag v3.82.13) | `git rev-parse` after a pinned fetch |
| 31-repository multi-language corpus | one commit each, `metadata/corpus_commits.txt` | `git rev-parse` after a pinned fetch |
| `exrex` (generates every credential) | 0.12.0, `requirements.txt` | exact pin |

Full values: `metadata/`, `environment/checksums.txt`.

## Layout

```
benchmarks/            harnesses, one per research question, plus analyses
  bench_mutation.py      RQ1/RQ2  boundary-mutation battery, 24 rules x 12 embeddings
                         (seeded, -- reproduces exactly; a fixed-but-never-
                         called seed was the third harness defect we found and
                         disclose in Threats to Validity)
  metrics_rlp_sp.py      RQ1      BR / RLP / SP from the recorded outcomes
  bench_stability.py     RQ1      10 independent repetitions of the hyphen cell,
                         separating deterministic rules from variable-rate ones
  analysis_probability.py RQ2     marginal (not conditional) failure rates,
                         reporting the exact 1/64 rather than a simulated one
  mc_hyphen_confirmation.py RQ2   independent Monte Carlo check that 1/64 is
                         not being misapplied (not itself the reported number)
  bench_fixvalidation.py REPAIR   applies two candidate fixes in memory, re-runs
                         the full battery, and probes for regression -- finds
                         one in the naive first-attempt fix
  bench_crosstool.py     RQ3      same battery, Gitleaks + TruffleHog
  bench_delimiter.py     RQ3      single-character terminator probe
  bench_comparative2.py  RQ4      corpora A and B, identical negatives
  paired_stats.py        RQ4      exact paired sign test at the type level
  bench_cooccurrence.py  RQ4      four deployment conditions
  bench_realworld.py     RQ5      four pinned open-source projects
  -- extended study (full rule tables, more types,
     more languages, encodings, real-world exposure) --
  bench_gitleaks_fulltable.py    every rule in Gitleaks' table (195), credentials
                         generated from each rule's own secret group
  bench_threetool_expanded.py    774 credential types generated from all three
                         tools' rule tables, run through all three tools;
                         label-matched coverage, neutral-generator pairwise
                         tests, 17 encoding/escaping/interpolation contexts,
                         hyphen marginal probability for every type
  bench_realworld_multilang.py   31 pinned repositories, 9 languages, 9.9M lines,
                         one identical file set for all tools: false positives
                         (A), real credential literals by following character
                         (B), credential-shaped tokens of any provider (C)
  bench_systems.py       SUPP     queueing, fault injection
  bench_scaling.py       SUPP     concurrency scaling, synchronised start
  gen_figs4.py           all figures, vector PDF + 600 dpi PNG
  verify_paper.py        re-checks every headline number in paper/main.tex +
                         paper/online_resource_1.tex against the raw JSON;
                         exits non-zero on mismatch
results/               unmodified raw JSON from every run above
figures_pdf_vector/    all fourteen figures (ten in the main manuscript, four in Online Resource 1)
figures_png_600dpi/    the same figures at 600 dpi
paper/                 canonical manuscript: Springer/EMSE submission
  main.tex               main manuscript source (sn-jnl class, author-year refs)
  online_resource_1.tex  supplementary operational characterisation
  sn-jnl.cls, sn-basic.bst, sn-bibliography.bib   Springer Nature template + bibliography
metadata/              subject commit, corpus commits, tool versions + digests
environment/           interpreter and service versions, checksums
```

## Continuous verification

`.github/workflows/research.yml` runs whenever this directory changes, apart
from the application's CI:

- every harness compiles, and `verify_paper.py` re-derives every number in
  the manuscript from `results/`;
- `bench_gitleaks_fulltable.py` is re-run from scratch against the
  checksum-verified Gitleaks binary and rule table, and the job fails if its
  output differs from the committed result in anything but wall time.

The seeded harnesses give byte-identical output on CPython 3.11 and 3.13 with
`exrex` 0.12.0.

## Harness defects found during the extended study

Each was found by checking a surprising number, fixed before any reported
result, and is recorded in the commit that fixed it:

- `exrex` draws Python's Unicode classes for `\w` and line breaks for `\s`,
  which are outside Go's ASCII classes; samples were outside the rule's
  language. Classes are now translated to Go's before generation.
- Gitleaks' keyword prefilter and path-restricted rules were not modelled,
  so filtered samples were scored as misses.
- TruffleHog's filesystem line numbers are unreliable; joining its findings
  to source lines by number scored it 0/14 on real literals. Findings are
  now located by their reported secret.
- One literal matched by several generators' types was counted once per type.

## A harness defect worth knowing about

An early version of the mutation harness used the identifier `SECRET` in its
embeddings and measured a uniform 100% detection rate, apparently exonerating
every rule. The identifier was itself triggering the scanner's *generic*
keyword rules, masking the failure of the specific rule under test. All
results use deliberately neutral identifiers (`cfg_value`). This is the
failure mode a replication is most likely to reproduce, so it is documented
here and in the paper rather than quietly fixed.

## Licence

MIT — see `LICENSE`.
