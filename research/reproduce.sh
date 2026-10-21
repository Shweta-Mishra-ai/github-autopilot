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
GITLEAKS_TOML_SHA256=2ce9d818ed5aac0d9a36638a317284bd733c26d5069c980829335183397430bb

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
  [[ -f "$HERE/gitleaks.toml" ]] || curl -sSL -o "$HERE/gitleaks.toml" \
    https://raw.githubusercontent.com/gitleaks/gitleaks/v8.21.2/config/gitleaks.toml
  verify_sha256 "$HERE/gitleaks.toml" "$GITLEAKS_TOML_SHA256" "gitleaks.toml v8.21.2"

  log "real-code corpora (commit-pinned)"
  mkdir -p "$HERE/realcorpus"
  pin_repo "$HERE/realcorpus/requests" https://github.com/psf/requests.git \
           8f8b212de8c2129d7954c6cd373762880375620a "psf/requests"
  pin_repo "$HERE/realcorpus/flask" https://github.com/pallets/flask.git \
           d318b683471101618febed18996405ad26462110 "pallets/flask"
  pin_repo "$HERE/realcorpus/redis-py" https://github.com/redis/redis-py.git \
           081923b8a202f9481e8f46357c8bbf99cc322cae "redis/redis-py"

  log "TruffleHog detector source (commit-pinned, read for its regexes)"
  pin_repo "$HERE/trufflehog-src" https://github.com/trufflesecurity/trufflehog.git \
           1aa1871f9ae24a8c8a3a48a9345514acf42beb39 "trufflesecurity/trufflehog v3.82.13"

  log "multi-language real-code corpus (commit-pinned)"
  mkdir -p "$HERE/realcorpus2"
  pin_repo "$HERE/realcorpus2/gin-gonic_gin" https://github.com/gin-gonic/gin.git \
           0f09c3a9b4626d9fe9979cf4d0521d6ae32ab646 "gin-gonic/gin"
  pin_repo "$HERE/realcorpus2/sendgrid_sendgrid-go" https://github.com/sendgrid/sendgrid-go.git \
           0f2bb79e71b9d73e40cb19a827089ed1252ab1d1 "sendgrid/sendgrid-go"
  pin_repo "$HERE/realcorpus2/expressjs_express" https://github.com/expressjs/express.git \
           0dee2c52c7c873498f8ed7d8ee4ddd0a04d3ada8 "expressjs/express"
  pin_repo "$HERE/realcorpus2/stripe_stripe-node" https://github.com/stripe/stripe-node.git \
           62458fe75f83b800707f06f077abe107213b0db0 "stripe/stripe-node"
  pin_repo "$HERE/realcorpus2/nestjs_nest" https://github.com/nestjs/nest.git \
           62647d3ec95e816a4556b3e04106eb4af002b4b8 "nestjs/nest"
  pin_repo "$HERE/realcorpus2/slackapi_node-slack-sdk" https://github.com/slackapi/node-slack-sdk.git \
           8730b5e3a7cee08a0ccfa44b7f792814f939a9cf "slackapi/node-slack-sdk"
  pin_repo "$HERE/realcorpus2/google_gson" https://github.com/google/gson.git \
           e2292f9c9f2e07a4791827e8eea34aa7d80545d6 "google/gson"
  pin_repo "$HERE/realcorpus2/square_retrofit" https://github.com/square/retrofit.git \
           f51e456b06fa99d78f896257a0d6ca78ab8e1e58 "square/retrofit"
  pin_repo "$HERE/realcorpus2/sinatra_sinatra" https://github.com/sinatra/sinatra.git \
           cb22afd7902b566b6eaba6c4ea89739494a65d12 "sinatra/sinatra"
  pin_repo "$HERE/realcorpus2/jekyll_jekyll" https://github.com/jekyll/jekyll.git \
           97c3bdc822df9d5bea4c269b29a28de5a5e29d70 "jekyll/jekyll"
  pin_repo "$HERE/realcorpus2/tokio-rs_axum" https://github.com/tokio-rs/axum.git \
           86aa6153e47b7862737acdcfe3a0eb3a162b939a "tokio-rs/axum"
  pin_repo "$HERE/realcorpus2/BurntSushi_ripgrep" https://github.com/BurntSushi/ripgrep.git \
           3fce3b5bb0236da2df6d99672afb8a719642eca7 "BurntSushi/ripgrep"
  pin_repo "$HERE/realcorpus2/guzzle_guzzle" https://github.com/guzzle/guzzle.git \
           b920e5fcaf81d761b8fabe600ca90999beaefdb7 "guzzle/guzzle"
  pin_repo "$HERE/realcorpus2/twilio_twilio-php" https://github.com/twilio/twilio-php.git \
           0cb66196f3c017dae395661e0cf1fc8197cb6d34 "twilio/twilio-php"
  pin_repo "$HERE/realcorpus2/JamesNK_Newtonsoft.Json" https://github.com/JamesNK/Newtonsoft.Json.git \
           52fa3aef1f2cadcd3a3f874251eddc98d3efbbaa "JamesNK/Newtonsoft.Json"
  pin_repo "$HERE/realcorpus2/App-vNext_Polly" https://github.com/App-vNext/Polly.git \
           3f3adad9d5af85658fc3585018f02a2d8c2173e8 "App-vNext/Polly"
  pin_repo "$HERE/realcorpus2/openai_openai-python" https://github.com/openai/openai-python.git \
           26e5d926966fc5c7e688790b21d66dc08d35eceb "openai/openai-python"
  pin_repo "$HERE/realcorpus2/psf_requests" https://github.com/psf/requests.git \
           8f8b212de8c2129d7954c6cd373762880375620a "psf/requests"
  pin_repo "$HERE/realcorpus2/pallets_flask" https://github.com/pallets/flask.git \
           d318b683471101618febed18996405ad26462110 "pallets/flask"
  pin_repo "$HERE/realcorpus2/redis_redis-py" https://github.com/redis/redis-py.git \
           081923b8a202f9481e8f46357c8bbf99cc322cae "redis/redis-py"
  pin_repo "$HERE/realcorpus2/cli_cli" https://github.com/cli/cli.git \
           7948e8056e45ac3c729b829e0eafe2d0797a95c6 "cli/cli"
  pin_repo "$HERE/realcorpus2/digitalocean_godo" https://github.com/digitalocean/godo.git \
           8a0153dc2f8986ec197fed5c06dc5c642ed6fffb "digitalocean/godo"
  pin_repo "$HERE/realcorpus2/npm_cli" https://github.com/npm/cli.git \
           ae98c62586b6fea44114f72e249e246a2981de70 "npm/cli"
  pin_repo "$HERE/realcorpus2/openai_openai-node" https://github.com/openai/openai-node.git \
           e35205033764ec2a2adc7fc71f3d2df9b0ea8679 "openai/openai-node"
  pin_repo "$HERE/realcorpus2/Shopify_shopify-app-js" https://github.com/Shopify/shopify-app-js.git \
           2a9a3a3b5a390a589618a6a5f753982c3b569215 "Shopify/shopify-app-js"
  pin_repo "$HERE/realcorpus2/huggingface_huggingface_hub" https://github.com/huggingface/huggingface_hub.git \
           e1f595dd7db4c993837e320a6d62ccdd1717dc2d "huggingface/huggingface_hub"
  pin_repo "$HERE/realcorpus2/python-gitlab_python-gitlab" https://github.com/python-gitlab/python-gitlab.git \
           51eb17e6e67a25157e04b0abc649221bd8ecc7fe "python-gitlab/python-gitlab"
  pin_repo "$HERE/realcorpus2/slackapi_python-slack-sdk" https://github.com/slackapi/python-slack-sdk.git \
           ab4e4983ba02789f5854e711450f9340d4cd53f2 "slackapi/python-slack-sdk"
  pin_repo "$HERE/realcorpus2/anthropics_anthropic-sdk-python" https://github.com/anthropics/anthropic-sdk-python.git \
           ecfee6cd504cfbf634919bdbd058fb74afdbd533 "anthropics/anthropic-sdk-python"
  pin_repo "$HERE/realcorpus2/stripe_stripe-java" https://github.com/stripe/stripe-java.git \
           f2c7e627e9d3ee3b09097c053f56fcc697f6eb64 "stripe/stripe-java"
  pin_repo "$HERE/realcorpus2/stripe_stripe-ruby" https://github.com/stripe/stripe-ruby.git \
           082af3fc6505e3a2dbee4b3a52814e0a1b2483f8 "stripe/stripe-ruby"

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
  log "RQ3      Gitleaks full table";   python bench_gitleaks_fulltable.py > "$RESULTS/gitleaks_fulltable_bench.json"
  log "RQ3/RQ4  three tools, 774 types"; python bench_threetool_expanded.py > "$RESULTS/threetool_expanded_bench.json"
  log "RQ5      multi-language corpus"; python bench_realworld_multilang.py > "$RESULTS/realworld_multilang_bench.json"
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

log "verification"
# Re-derives every headline number in the manuscript from the raw JSON and
# diffs it against paper/main.tex + paper/online_resource_1.tex, the
# canonical Springer/EMSE submission. Non-zero exit means the paper and the
# data disagree, which is a defect in the paper.
python "$HERE/benchmarks/verify_paper.py" || die "manuscript/data mismatch"

log "done — results/ figures_pdf_vector/ paper/main.pdf paper/online_resource_1.pdf"
