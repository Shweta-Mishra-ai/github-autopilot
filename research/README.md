# Replication package
**Boundary-Mutation Testing for Pattern-Based Secret Detection: A Rule-Level Method and Cross-Scanner Evaluation**

Every number and figure in the paper is produced by the code in this package
from the data in `results/`. Nothing is estimated or extrapolated.

Archived on Zenodo: [10.5281/zenodo.22114221](https://doi.org/10.5281/zenodo.22114221)

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
paper_ieee_preprint/   non-canonical IEEEtran-format preprint (arXiv/GitHub use only;
                       not the submitted manuscript, not covered by verify_paper.py)
metadata/              subject commit, corpus commits, tool versions + digests
environment/           interpreter and service versions, checksums
```

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
