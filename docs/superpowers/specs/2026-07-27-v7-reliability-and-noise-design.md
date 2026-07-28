# V7 — Reliability, Noise Reduction, and a Working Brain

**Date:** 2026-07-27
**Status:** Approved for implementation
**Baseline:** `main` @ `6a238c9` (PR #75), 908 tests passing

---

## Problem

Three user-visible failures, each with a distinct root cause:

1. **The bot posts wrong answers.** When the LLM returns non-JSON, the validators
   substitute defaults and the bot publishes a confident review it never performed.
2. **The bot is too loud.** One PR generates four comments on open and two more on
   every push. Secret scanning has filed seven duplicate issues in 73 seconds.
3. **The bot does not learn.** The memory subsystem has no write path and its read
   path is disabled by default.

Underneath all three: the subsystems built to prevent exactly these problems —
`hallucination.check_response`, `ConfidenceGate`, `intelligence.memory` — are
present in the tree but not connected to the paths that matter.

---

## Non-goals

- Rewriting `router.py` (PR #78's approach). The router's provider selection,
  circuit breaking, and privacy modes are sound. Changes stay surgical.
- Repo-wide semantic indexing / embeddings upgrade. Deferred.
- Replacing the memory store with a graph database. This pass gives the existing
  store a working write path; the graph redesign is a separate spec.

---

## Phase 1 — Correctness

### 1.1 Fail closed on unparseable model output

`app/ai/providers/base.py::_extract_json` returns `{"raw": text}` when no JSON is
found. Every validator must treat that as a hard failure, not as absent fields.

Add to `app/ai/validator.py` a shared guard:

```python
def is_unusable(raw) -> bool:
    return (not isinstance(raw, dict)) or bool(raw.get("error")) or ("raw" in raw)
```

`validate_code_review`, `validate_issue_triage`, and `validate_pr_analysis` return
their degraded shape when `is_unusable(raw)`. The degraded shape gains an explicit
`"_degraded": True` marker.

Callers must not render a degraded result as a normal result. `_review_code` skips
the file entirely; `issues.handle` posts a plain acknowledgement with no fabricated
type/priority/complexity table.

**Rationale:** silence is correct here. A blank comment is recoverable; a fake
"Score: 7/10 — no issues found" is not.

### 1.2 Field-name contract between validator and renderer

`validate_code_review` returns `verdict`; `pull_request.py` reads `summary`. Fix by
returning **both** keys from the validator (`summary` as the canonical name,
`verdict` retained for the existing MCP/eval consumers), and add a test that asserts
the rendered markdown contains the model's text.

This is the second occurrence of this bug class (`improved_title` was the first).
The regression test asserts on **rendered output**, not on validator return values —
that is what the existing 908 tests failed to do.

### 1.3 Restore dropped triage vocabulary

`validate_issue_triage` must accept the values its own prompt requests:

| Field | Add |
|---|---|
| `VALID_PRIORITIES` | `critical` |
| `VALID_TYPES` | `refactor` |
| `VALID_COMPLEXITY` | `epic` |

Pass through `time_estimate` (validated against the prompt's enum) so the
"Est. Effort" row renders. Drop `is_duplicate_risk`, `similar_search_terms`, and
`auto_close_reason` **from the prompt** — nothing consumes them, and requesting
unused fields wastes tokens on every issue.

Add `priority: critical 🚨` to the `_ensure_labels` list (already present) and
verify the mapping in `issues.py` handles it (already present — only the validator
was blocking it).

### 1.4 Score default

`validate_code_review`'s degraded path returns `score: None`. Renderers use
`r.get("score", 8)`, which returns `None` because the key exists. Under 1.1 the
degraded result is never rendered, which fixes this structurally. Renderers
additionally coerce: `score = r.get("score") or 8`.

### 1.5 Wire hallucination detection everywhere

`check_response` currently guards `/fix` only. Extend to every LLM output path:
code review, issue triage, CI analysis, PR analysis, `/impact`, `/arch`, `/improve`,
`/perf`, `/gaps`, `/refactor`, `/test`, `/docs`.

Introduce `app/ai/guarded.py` with a single wrapper so the check cannot be forgotten
on a future command:

```python
def guarded_ask(system, user, task, response_type, context=None, **kw):
    """router.ask + validation + hallucination check. Returns (result, meta, verdict)."""
```

New commands route through `guarded_ask`; a test enumerates the command registry and
asserts every LLM-calling command uses it.

---

## Phase 2 — Noise

### 2.1 Sticky PR comment

**Current:** PR open posts 4 comments (`PR Analysis`, `PR Summary`, `AI Code Review`,
`Test Coverage Analysis`). Each `synchronize` posts 2 more. Nothing is ever edited.

**New:** one bot comment per PR, identified by a hidden HTML marker
(`<!-- github-autopilot:pr-report -->`), **edited in place** on every subsequent event.

Structure:

```markdown
## 🤖 Autopilot — PR #123

**Risk:** 🟡 Medium · **Files:** 7 · **+240 −18**

<summary text>

<details><summary>🔍 Code review — 3 findings</summary>…</details>
<details><summary>🧪 Test coverage — 6/10</summary>…</details>

<!-- github-autopilot:pr-report -->
*Updated <timestamp> · model: `llama-3.3-70b-versatile`*
```

New module `app/github/sticky.py`:

- `find_sticky(repo, issue_number, token, marker) -> int | None` — locates an
  existing bot comment by marker (paginated search of issue comments).
- `upsert_sticky(repo, issue_number, token, marker, body)` — PATCH when found,
  POST when not.

Line-anchored inline review comments continue to use the Reviews API unchanged —
those are genuinely useful and land on the diff, not in the conversation.

### 2.2 Silence on empty findings

The bot must not comment when it has nothing to say:

- No code-review findings **and** no test gaps **and** risk is `low` → no comment
  at all on `synchronize`. (On `opened` the summary is still worth posting once.)
- `_detect_test_gaps` already returns early when `has_gaps` is false — keep.
- CI analysis posts only when the failure is new (see 2.4).

### 2.3 Secret scanning: use the scanner that was built for this

`app/handlers/push.py` imports the legacy `app.security.secrets`. Switch to
`app.security.enhanced_secrets`, which the codebase itself documents as a
*"drop-in replacement … FALSE POSITIVE REDUCTION … entropy + pattern combined
scoring — reduces noise."*

Required adjustments:

- `enhanced_secrets.scan_diff(patch, file_path=...)` takes a file path — pass
  `f["filename"]` so its per-path false-positive suppression engages.
- Its findings carry a `severity` of `critical|high|medium|low`. **Only
  `critical` and `high` open a GitHub issue.** `medium`/`low` are logged, matching
  the policy already applied to dependency findings.
- Fix line numbers: `secrets.scan_diff` reports the index within the patch string,
  not the file line. Use `patch_parser` to map patch offsets to real file lines so
  alerts point at the right place.

### 2.4 Dedup that actually deduplicates

Three defects, three fixes:

**Fails open.** `_already_reported` returns `False` (= "not yet reported") on any
Redis exception, so an outage produces duplicates. Invert to fail **closed**:
return `True` on error, and increment a `dedup.redis_unavailable` metric. A missed
alert during an outage is strictly better than seven duplicates.

**Wrong key.** The current key hashes the set of pattern *names*, so pushes with
different finding mixes bypass each other. Replace with a per-repo window key
`secret_alert:{repo}` (24h TTL) holding the **issue number** of the open alert.
Within the window, new findings post a comment on that existing issue instead of
opening a new one; a fresh issue is opened only when the key is absent or the
recorded issue has been closed.

**No CI dedup.** `ci.py` has none. Add `ci_alert:{repo}:{pr}:{head_sha}` so a
matrix of N failing jobs on one commit produces one comment, not N. Use the sticky
comment on the PR so a re-push edits rather than appends.

Also fix `_track_failure_pattern`: `int(count) == 3` fires exactly once and never
again; change to `>= 3` with a separate `alerted` flag key, and stop resetting the
TTL on every increment so the 24h window actually rolls.

### 2.5 Cost

`_review_code` makes one LLM call per file (up to `max_files_reviewed`). Batch all
reviewable files into a single call with a per-file JSON structure. Reduces a PR
open from ~7 LLM calls to ~3.

---

## Phase 3 — A brain that works

### 3.1 Give memory a write path

Nothing in the application calls `remember()`. Add writes at the points where a real
signal exists:

| Trigger | Kind | Stored |
|---|---|---|
| `/apply` opens a PR from a bot fix | `fix` | issue title + accepted fix summary |
| `/merge` merges a `fix/bot-issue-*` branch | `fix` | strongest acceptance signal |
| PR merged with review findings unaddressed | `preference` | what this repo tolerates |
| Issue triaged | `pattern` | recurring issue shapes |
| `/arch`, `/impact` output | `decision` | via `remember_decision(why=...)` |

### 3.2 Enable recall by default, with redaction

`injection_allowed()` currently returns `False` unless a local model is configured,
which disables the brain in every standard cloud deployment.

Replace the binary switch with a redaction pipeline:

- Memory text is scrubbed through `enhanced_secrets` before storage; anything
  matching a secret pattern is replaced with `[REDACTED]`.
- Code bodies are not stored — only file paths, symbol names, and prose rationale.
- `MEMORY_ALLOW_CLOUD` is retained as an **opt-out** (`=0` restores current
  behavior) rather than an opt-in.

Documented in `docs/ai-system/memory.md` with an explicit statement of what leaves
the deployment.

### 3.3 Bounded recall cost

`remember()` currently scans and JSON-parses the entire list to dedup on every
write; `recall()` does the same on every read. Add a Redis set of content hashes
(`mem:hashes:{repo}`) for O(1) dedup, and cap `recall()`'s scan at the most recent
`MEMORY_RECALL_SCAN` (default 200) items.

**Graph structure is explicitly deferred** to a follow-up spec. This phase makes the
existing store function; the entity/edge redesign is a larger change that should not
ride along with a bugfix release.

### 3.4 Confidence that measures something

`ConfidenceGate` reads `ai_response["confidence"]` — a number the model invents.
Replace with computed signals in `app/core/confidence.py`:

```
score = w1·json_valid          # did it return parseable, schema-conforming JSON
      + w2·anchor_rate         # fraction of findings that map to real diff lines
      + w3·hallucination_conf  # check_response confidence
      + w4·field_completeness  # required fields present and non-trivial
```

The model's self-reported value is retained as a **weak** input (low weight), not
the sole input. `_review_code` must actually call the gate — it currently receives
`gate` and never uses it.

Auto-apply thresholds stay as configured; what changes is that the number they
compare against is now derived from evidence.

---

## Phase 4 — Issue #76 (prompt injection)

The report cites v4.1.0 and quotes code that no longer exists — `main` already has
NFKC normalization and 19 compiled patterns in `app/core/sanitizer.py`. Three of the
six requested defenses are genuinely missing:

**4.1 Structural separation.** `wrap_user_content()` is defined, documented, and
unit-tested — and called from **zero** production call sites. Every handler
interpolates raw user text into prompt f-strings. Wire it into every handler that
embeds webhook-derived text (issue bodies, PR titles/descriptions, commit messages,
CI logs, comment args).

**4.2 Whitespace collapse + zero-width stripping.** Add to `sanitize_user_input`
before pattern matching, so `ignore​previous\ninstructions` is caught.

**4.3 Fail-closed on critical severity.** Classify patterns by severity. Patterns
indicating a deliberate override attempt (system-prompt exfiltration, delimiter
injection) reject the input entirely and return a "content rejected" comment,
rather than substituting `[INSTR_INJ]` and proceeding.

Then: close #76 with a summary of what was stale vs. what was fixed, and reply to
PR #77 and PR #78 crediting the report.

---

## Testing

Every phase ships with tests that assert on **rendered output**, not on internal
return values — the gap that let all four Phase 1 bugs survive 908 tests.

- Phase 1: a degraded/unparseable LLM response produces no fabricated review;
  rendered markdown contains the model's summary text; `critical` survives triage.
- Phase 2: two events on one PR produce one comment (PATCH, not POST); N failing
  matrix jobs on one SHA produce one CI comment; Redis-down produces zero duplicate
  issues.
- Phase 3: `/merge` on a bot branch writes a memory; recall returns it on a later
  related command; secrets never reach the store.
- Phase 4: each documented bypass technique from #76 is rejected.

Full suite must stay green (908 baseline + new).

---

## Risks

| Risk | Mitigation |
|---|---|
| Sticky comment changes what existing users see | Documented in release notes; marker-based lookup degrades to POST if no sticky found |
| Fail-closed dedup could suppress a real alert during a Redis outage | Metric + `WARN` log on every suppression; outage is visible on `/health` |
| Memory-by-default is a privacy posture change | Redaction pipeline + opt-out env var + explicit docs |
| Batched file review may reduce per-file depth | Eval suite (`evals/run.py`) gates the change on measured pass-rate |
