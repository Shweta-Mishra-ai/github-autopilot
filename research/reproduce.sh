#!/usr/bin/env bash
# One-command reproduction of every number and figure in the paper.
#   ./reproduce.sh            full run
#   ./reproduce.sh --figures  regenerate figures from existing results/
#
# Everything external to this package is pinned and verified before use:
# the subject system by commit SHA, the two comparison scanners by SHA-256
# of the extracted binary, and each real-code corpus by commit SHA. Any
# mismatch aborts rather than silently measuring a different artefact.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUBJECT="${SUBJECT_REPO:-$HOME/github-autopilot}"
RESULTS="$HERE/results"; mkdir -p "$RESULTS"

SUBJECT_COMMIT=38b201375926b11ff94703f6257a01aa8723c23d
GITLEAKS_SHA256=50b742abd7daad8bbddb6301f3017efb680632d9a5b3b4d8f137b3aac250e359
TRUFFLEHOG_SHA256=6f3e79d4fdfc0c0707a28c76ca0d1990c80e3090ff750f5bd58d6794202875e9

log(){ printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
die(){ printf '\n\033[31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

verify_sha256(){ # <file> <expected> <label>
  local got; got="$(sha256sum "$1" | cut -d' ' -f1)"
  [[ "$got" == "$2" ]] || die "$3 checksum mismatch
  expected $2
  got      $got
This is not the binary the paper measured. Refusing to continue."
  echo "  verified $3 ($2)"
}

pin_repo(){ # <dir> <url> <commit> <label>
  if [[ ! -d "$1/.git" ]]; then
    git init -q "$1"
    git -C "$1" remote add origin "$2"
  fi
  git -C "$1" fetch -q --depth 1 origin "$3"
  git -C "$1" checkout -q --detach FETCH_HEAD
  local got; got="$(git -C "$1" rev-parse HEAD)"
  [[ "$got" == "$3" ]] || die "$4 is at $got, expected $3"
  echo "  pinned $4 at $3"
}

if [[ "${1:-}" != "--figures" ]]; then
  log "subject system"
  [[ -d "$SUBJECT/.git" ]] || die "subject repo not found at $SUBJECT"
  actual="$(git -C "$SUBJECT" rev-parse HEAD)"
  if [[ "$actual" != "$SUBJECT_COMMIT" ]]; then
    echo "  subject at $actual, pinning to $SUBJECT_COMMIT"
    git -C "$SUBJECT" fetch -q --depth 1 origin "$SUBJECT_COMMIT"
    git -C "$SUBJECT" checkout -q --detach FETCH_HEAD
    [[ "$(git -C "$SUBJECT" rev-parse HEAD)" == "$SUBJECT_COMMIT" ]] \
      || die "could not pin subject to $SUBJECT_COMMIT"
  fi
  echo "  subject pinned at $SUBJECT_COMMIT"

  log "environment"
  python3 -m venv "$HERE/.venv"; source "$HERE/.venv/bin/activate"
  pip install -q --upgrade pip
  pip install -q -r "$SUBJECT/requirements-dev.txt"
  pip install -q exrex matplotlib numpy scipy pymupdf

  log "redis (loopback, persistence disabled)"
  redis-server --daemonize yes --port 6399 --save "" --appendonly no \
               --logfile /tmp/redis_repro.log
  sleep 1; redis-cli -p 6399 ping

  log "comparison scanners (version- and checksum-pinned)"
  [[ -x "$HERE/gitleaks" ]] || {
    curl -sSL -o /tmp/gl.tgz \
      https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_x64.tar.gz
    tar xzf /tmp/gl.tgz -C "$HERE" gitleaks; }
  verify_sha256 "$HERE/gitleaks" "$GITLEAKS_SHA256" "gitleaks 8.21.2"
  [[ -x "$HERE/trufflehog" ]] || {
    curl -sSL -o /tmp/th.tgz \
      https://github.com/trufflesecurity/trufflehog/releases/download/v3.82.13/trufflehog_3.82.13_linux_amd64.tar.gz
    tar xzf /tmp/th.tgz -C "$HERE" trufflehog; }
  verify_sha256 "$HERE/trufflehog" "$TRUFFLEHOG_SHA256" "trufflehog 3.82.13"

  log "real-code corpora (commit-pinned)"
  mkdir -p "$HERE/realcorpus"
  pin_repo "$HERE/realcorpus/requests" https://github.com/psf/requests.git \
           8f8b212de8c2129d7954c6cd373762880375620a "psf/requests"
  pin_repo "$HERE/realcorpus/flask" https://github.com/pallets/flask.git \
           d318b683471101618febed18996405ad26462110 "pallets/flask"
  pin_repo "$HERE/realcorpus/redis-py" https://github.com/redis/redis-py.git \
           081923b8a202f9481e8f46357c8bbf99cc322cae "redis/redis-py"

  export REDIS_URL=redis://127.0.0.1:6399/0
  cd "$HERE/benchmarks"
  log "RQ1/RQ2  boundary mutation";     python bench_mutation.py       > "$RESULTS/mutation_bench.json"
  log "RQ1      BR / RLP / SP";         python metrics_rlp_sp.py       > "$RESULTS/metrics_br_rlp_sp.txt"
  log "RQ1      repeated-run stability";python bench_stability.py      > "$RESULTS/stability_bench.json"
  log "RQ2      marginal probability";  python analysis_probability.py > "$RESULTS/probability_analysis.json"
  log "RQ2      hyphen MC confirmation";python mc_hyphen_confirmation.py > "$RESULTS/mc_hyphen_confirmation.json"
  log "REPAIR   fix validation";        python bench_fixvalidation.py  > "$RESULTS/fixvalidation_bench.json"
  log "RQ3      cross-tool mutation";   python bench_crosstool.py      > "$RESULTS/crosstool_bench.json"
  log "RQ3      delimiter probe";       python bench_delimiter.py      > "$RESULTS/delimiter_bench.json"
  log "RQ4      corpus A/B comparison"; python bench_comparative2.py   > "$RESULTS/comparative_bench.json"
  log "RQ4      paired sign test";      python paired_stats.py         > "$RESULTS/paired_stats.txt"
  log "RQ4      co-occurrence";         python bench_cooccurrence.py   > "$RESULTS/cooccurrence_bench.json"
  log "RQ5      real-code corpus";      python bench_realworld.py      > "$RESULTS/realworld_bench.json"
  log "SUPP     systems + faults";      python bench_systems.py        > "$RESULTS/systems_bench.json"
  log "SUPP     concurrency scaling";   python bench_scaling.py        > "$RESULTS/scaling_bench.json"
else
  if [[ -d "$HERE/.venv" ]]; then
    source "$HERE/.venv/bin/activate"
  else
    log "environment (--figures on a fresh checkout: no prior full run found)"
    python3 -m venv "$HERE/.venv"; source "$HERE/.venv/bin/activate"
    pip install -q --upgrade pip
    pip install -q matplotlib numpy
  fi
fi

log "figures"
python "$HERE/benchmarks/gen_figs4.py"

log "paper (Springer/EMSE manuscript, canonical)"
cd "$HERE/paper" \
  && pdflatex -interaction=nonstopmode main.tex >/dev/null \
  && bibtex main >/dev/null \
  && pdflatex -interaction=nonstopmode main.tex >/dev/null \
  && pdflatex -interaction=nonstopmode main.tex >/dev/null

log "online resource 1 (operational characterisation)"
cd "$HERE/paper" \
  && pdflatex -interaction=nonstopmode online_resource_1.tex >/dev/null \
  && pdflatex -interaction=nonstopmode online_resource_1.tex >/dev/null

log "IEEE preprint (non-canonical, kept for arXiv/GitHub use only)"
cd "$HERE/paper_ieee_preprint" \
  && pdflatex -interaction=nonstopmode main.tex >/dev/null \
  && pdflatex -interaction=nonstopmode main.tex >/dev/null \
  && pdflatex -interaction=nonstopmode main.tex >/dev/null

log "verification"
# Re-derives every headline number in the manuscript from the raw JSON and
# diffs it against paper/main.tex + paper/online_resource_1.tex, the
# canonical Springer/EMSE submission. Non-zero exit means the paper and the
# data disagree, which is a defect in the paper.
python "$HERE/benchmarks/verify_paper.py" || die "manuscript/data mismatch"

log "done — results/ figures_pdf_vector/ paper/main.pdf paper/online_resource_1.pdf"
