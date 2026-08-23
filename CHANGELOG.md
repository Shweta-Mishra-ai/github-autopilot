# Changelog

Every release of GitHub Autopilot, newest first.

This file is deliberately detailed. Most entries are not "added X" but "X was
broken in a way nothing could see" — the failure, the reason it stayed
invisible, and what now makes it visible. That detail belongs here rather than
in the README: someone evaluating the project needs three minutes, someone
debugging their deployment needs the specifics.

Versions follow [semantic versioning](https://semver.org). Upgrading from V6?
See [docs/MIGRATING.md](docs/MIGRATING.md).

---

### V7.2.0 — 2026-08-20

Full-codebase audit. The theme is features that were wired, tested, and shipped — and then silently did nothing in production.

**Seven commands were dead in the field**
- `check_command_permission()` treated *every* non-200 from GitHub's collaborator API as "this user has no permission". A 403 (the App lacks the `members` scope), a 5xx, or a dropped connection all cached `"none"` for an hour, so `/autofix`, `/apply`, `/merge`, `/rollback`, `/release`, `/ignore` and `/secfull` refused to run for the repository owner and blamed the *user's* access in the reply. Transport and authorization failures now return an `unknown` sentinel that is **never cached** and produces a reply naming the App installation as the suspect. A denial also costs one API call now instead of two.
- Notifications were off by default. `notifications.slack` and `notifications.discord` defaulted to `False`, so a correctly configured `SLACK_WEBHOOK_URL` delivered nothing. The tests passed because they patched the `SLACK_ENABLED` / `DISCORD_ENABLED` module constants — which is precisely why nobody noticed. Those constants were read once at import, so setting the env var after import had no effect either; they are call-time functions now, and Slack gained the rich-block sender Discord already had.
- `/notify` reported success for channels it had never contacted. It now sends per channel and reports delivered-vs-configured truthfully, including "configured but unreachable".

**`/rollback` could not roll back**
- The pre-rollback safety snapshot's return value was discarded, so a failed snapshot still let the rollback proceed with no way back. It now aborts.
- Undo ran **oldest-first**. Reverting action 1 before action 5 is not an undo; the order is now newest-first (LIFO), and an action type the handler does not recognise is reported as failed instead of counted as reverted.
- Four handlers crashed on real payload shapes: a null `user` on a ghosted comment, a null `patch` on a binary file, a null `body`, and a missing `filename`. All GitHub reads in `snapshot.py` are now shape-guarded.

**Scanner accuracy — no false positives, no silent misses**
- The dependency scanner matched versions by regex prefix, so `flask 3.10.0` matched the `3.1.x` advisory and `requests 2.30.0` matched `2.3.x`. Replaced with real version-range comparison. Findings report the *affected range*, not a "fixed in" version — the range ends are series boundaries, and printing `flask>=4.0.0` would be advertising a release that does not exist.
- Several secret patterns were mathematically unable to fire: a flat Shannon-entropy floor rejects short high-quality tokens because entropy is bounded by `log2(length)`. The gate is now a normalized ratio against that ceiling plus a distinct-character floor, and the Slack patterns were widened to the lengths Slack actually issues. Structural non-secrets (UUIDs, hashes, base64 blobs in lockfiles) are excluded explicitly rather than by luck.
- Detection turned out to be *probabilistic* — a randomly generated key passes or fails depending on the characters it happens to draw. The accuracy tests are statistical now, with thresholds set from a measured 5000-sample distribution instead of a lucky seed.
- `/security` scanned the base branch instead of the PR head, so it reported on code the PR had already changed.

**New: see the codebase, not a diagram**
- `app/intelligence/codegraph.py` extracts a real import graph with the `ast` module — top-level vs. runtime edges, iterative Tarjan cycle detection, orphan and hotspot detection — and `/graph` renders it as a force-directed graph in canvas, no CDN and no external asset.
- The graph found its own first bug: a five-module import cycle in `app/handlers/comments/`. Broken via a deferred-delegation shim; the repo now reports **zero cycles**, and CI fails if a new one appears.

**New: the bot writes on push**
- Professional commit-message suggestions on push (one LLM call, capped at five commits, merge/revert skipped, SHA-keyed dedup that fails closed).
- README managed regions between `<!-- autopilot:NAME:start -->` markers, regenerated as the project changes and delivered by pull request only — never a direct push to the default branch. Region replacement slices the string rather than using `re.sub`, so a `\g<1>` in generated content cannot corrupt the file.

**`/autofix`'s path guard could be walked around with `./`**
- `/autofix` opens a PR that rewrites a file, and the path comes from the model's `target_file` — derived from an issue body, which is attacker-controlled on a public repo. `BLOCKED_PATHS` and `BLOCKED_PREFIXES` are the only thing keeping it away from `server.py`, `requirements.txt`, the CI workflows and `app/core/authorization.py`, **the module that decides who may run destructive commands at all**.
- Those lists are exact strings and prefixes, so they only work if the path being tested is spelled the way they are. **Seven of fifteen probe paths walked straight through** — `./server.py`, `./requirements.txt`, `./pyproject.toml`, `./setup.py`, `app/core/./config.py`, `app//core//config.py`, `./app/core/authorization.py`. GitHub's Contents API resolves every one to the protected file, and `target` went verbatim into the `PUT`: the guard checked one spelling and the write used another.
- Fixed by normalising once, at the top, and using that spelling for every later decision *and* the write. `_block_reason` normalises identically, so the two can never disagree about which path they are judging. The tests generate their respelling variants **from the blocklists themselves**, so a path added tomorrow is covered without anyone remembering to add cases.

**The secret scanner reported placeholders as leaks**
- Run against **this repository's own source** it produced eight findings, every one a placeholder — including four `CRITICAL` "private key" hits on `app/security/enhanced_secrets.py`, where the matched text was the regex that *detects* private keys. A scanner that reports its own ruleset as a leak is not one anyone reads twice. It is now **zero**, with every real credential shape still firing.
- Three separate causes. `FALSE_POSITIVE_FILE_PATTERNS` contained `/tests/` **with a leading slash**, and GitHub reports repo-relative paths — so `tests/conftest.py` never matched and the exclusion had never worked for the commonest layout there is. The placeholder word list was five words and missed `replace`, `dummy`, `sample`, `not_real`, `${…}` and `{{ … }}`. And a PEM *header* matched on its own, when what makes a private key a leak is the key **material**.
- The material check looks at the following lines as well as the rest of the current one, because a real key is base64 across many lines — requiring the body on the same line would have missed every genuine multi-line leak, which is far worse than the noise it removes.

**Ollama earns its place: a local triage gate**
- The provider was reachable only through `LLM_LOCAL_ONLY` and `LLM_PREFER_LOCAL` — two all-or-nothing switches that route *everything* local. Neither is set in a normal deployment, so the integration existed and did nothing. As a **gatekeeper** it fits: a local call costs nothing, so it is affordable to ask about every diff, and *"is there anything here worth reviewing"* is a far easier question than reviewing. Version bumps, lockfile churn, formatting and comment edits now skip the cloud entirely.
- **It fails open, always.** Unreachable, slow, circuit open, empty answer, rambling answer, both words at once — every one of those means "review it", identical to having no gate. A gate that failed closed would silently stop reviewing pull requests while every test still passed, which is the exact failure this codebase has spent its history removing. There is deliberately no setting that makes it strict, and a test asserts only one code path in the whole function can skip a review.
- Inert unless `OLLAMA_HOST` is set, so nothing changes for anyone who does not want it.

**Webhook hardening — the only endpoint the internet is meant to reach**
- **An unauthenticated 30 MB request allocated 62 MB before being rejected.** `verify_webhook` checks `len(request.data)`, which cannot run until the whole body has been materialised — and the size check is *step one*, so no signature was needed to trigger it. A handful of concurrent requests exhausts a 512 MB instance. Werkzeug now refuses the body against the stream: the same request peaks at **0.2 MB**. The explicit length check stays as defence in depth for a chunked request that declares no `Content-Length`.
- **X-Forwarded-For was trusted whenever present.** With a proxy in front that is correct; with no proxy the entire header is attacker-supplied, so a flood could pick a fresh rate-limit bucket on every single request. How much of the chain is trustworthy is a property of the *deployment*, so it is now `TRUSTED_PROXY_HOPS` — default `1`, which is exactly Render's shape and changes nothing for the standard install. The module's own docstring had claimed spoofing was fixed; it was fixed for Render and nowhere else.
- `[]`, `"str"` and `123` are all valid JSON and none of them has `.get()`. A non-object payload raised `AttributeError` and surfaced as a 500 — an internal error for what is really a malformed request.

**The code review silently dropped findings when GitHub rejected them**
- A finding that anchors to a diff line is deliberately left **out** of the per-file markdown, which renders *"All findings posted as inline comments"* in its place. When the Reviews API returns 422 — a line it considers non-commentable, an outdated diff, a force-pushed head — `_post_inline_review` builds recovery markdown and returns it. **The caller discarded the return value**, so the finding existed in neither place and the sticky report asserted it had been posted as an inline comment that did not exist.
- There was already a test called `test_reviews_api_rejection_does_not_lose_findings`. It passed, because its harness dropped the return value the same way production did — it tested the function, not the wiring. Both are fixed, and a third test now asserts the caller reads the value.
- Found by `vulture`, which flagged the ignored `review_body` parameter next door. Of its six findings, five were false positives (signal-handler arguments, and a name used only inside a string annotation) — worth stating, because the tool's value here was one true positive that led to a different bug entirely.

**Performance: profiled, not guessed**
- The webhook path is 0.57 ms/request, so local CPU was never the bottleneck — but the profile found that **two `logging.warning` calls fired on every single event** when Redis was unavailable, and that was ~40% of the handler's own time. "Redis is unavailable" does not become more true the four-thousandth time it is logged; it becomes less readable, and it buries the warnings that are about one specific event. Both are logged on the *transition* now, in each direction, so an operator still learns when it starts and when it recovers.
- `_is_duplicate_local` filtered all 2000 dedup entries on every event to expire the handful that had aged out. It is an `OrderedDict` written in time order, so the oldest key is always first — popping from the front until the head is live is **2.2× faster** (measured: 418 µs → 186 µs) and drops the O(n)-per-event term entirely.
- The in-memory IP rate limiter **never freed an address**. The old code tried to delete empty windows but appended the current timestamp *before* testing for emptiness, so the window was never empty and the delete branch was unreachable — a comment describing a fix that could not fire, and one dict entry per source address forever on a public endpoint. It also appended while *over* the limit, so a flooding IP grew its own window for a full minute: the limiter paying for the flood it was refusing.
- `_perm_cache` and `_config_cache` checked their TTL **on read only**, so an entry for a user who never came back was invalid but never freed — one entry per `(repo, user)` pair, forever, in a process meant to run for weeks on 512 MB. Checking a TTL is not the same as honouring it. Both prune on write now, amortised so the sweep is not paid per request.
- That leak existed because `invalidate_permission_cache()` and `invalidate_config_cache()` had **zero callers**. They now have a real one: a push that edits `.ai-repo-manager.yml` drops both caches for that repo — the config one because it is stale, and the permission one because `commands.permissions.maintainer_only` lives in that same file. A maintainer fixing their config previously waited up to five minutes to learn whether the fix worked, which is long enough to conclude it had not and change something else.

**Licence declarations disagreed with each other**
- `plugin.json` and `marketplace.json` both said `MIT` while `pyproject.toml`, the README and the `LICENSE` files said `MIT OR Apache-2.0`; `mcp-manifest.json` declared nothing at all. Understating the grant is the harmless direction, but a licence that disagrees with itself is worse than none — someone reads the manifest, adopts under the narrower terms, and never learns the broader grant exists. All four now agree, pinned by a test that also checks both licence files actually ship.

**The licence scanner was wrong about three quarters of this repo's own dependencies**
- It read PyPI's `info.license` and nothing else. **Six of this repository's eight direct dependencies leave that field empty** — Flask, redis, gunicorn, cryptography, PyJWT and structlog all declare their licence in `license_expression` (PEP 639) or in trove classifiers — so all six were reported as "unknown", i.e. as something a maintainer must go and check. A scanner that is wrong about three quarters of a normal requirements file teaches people to skim past it. All three metadata sources are now read in order of authority, and the fixtures in the tests are the real payloads those packages publish, not shapes invented to make the parser pass.
- Matching was by substring, which got dual licences backwards: `"MIT" in "MIT AND GPL-3.0"` is true, so a package that genuinely imposes the GPL was reported safe — a false **negative** in the one direction that costs someone a licence violation. Expressions are parsed now: `OR` takes the most permissive branch (the consumer picks), `AND` the most restrictive (all apply). The first version of that parser split on `OR` without respecting parentheses and called `GPL-3.0 AND (MIT OR Apache-2.0)` safe; a test caught it, not a re-read.
- "Could not check" is now separate from "no licence declared". A private index, a git dependency or a network blip is not evidence of a licence problem, and reporting it as one is the false positive that makes the whole section ignorable.

**CI: measured, then cut**
- The three test legs each booted a Redis service container (~19s of startup apiece) that **nothing connected to** — `conftest.py` installs an in-process fake and its autouse fixture forces `REDIS_URL=""`, and there are zero tests marked `integration`, which is what the service existed for. Removed, with a note to give integration tests their own job rather than re-attaching it to the unit matrix.
- Coverage instrumentation costs 49% on this suite (measured: 23.2s → 34.6s). Only the 3.11 leg uploads a report, so the other two were paying it for a file nothing reads. The floor is still enforced, on the leg that measures it.
- The matrix no longer waits on lint: lint takes ~10s and the matrix spends longer than that on setup, so the gate delayed every green run by about what it saved on a red one.
- `-v` in `addopts` was also measured, at 22.7s vs 23.5s for `-q` — inside the noise, so it was left alone rather than changed on a hunch. The `integration`/`e2e` markers CI filters on are now registered, so `--strict-markers` catches a typo that would otherwise silently exclude nothing.

**Visualization is tested against the real payload, and rendered**
- `graphview.py` reported "100% coverage" on three statements, because the page is one string constant — a meaningless number. The gap that mattered was the contract between `codegraph.py` (which writes the JSON) and the page's JavaScript (which reads it): a rename on the Python side produces an **empty canvas, not an exception**. Tests now derive the field names the shipped page dereferences and assert the generator emits every one, plus that no edge endpoint dangles and the payload stays small enough for an O(n²) layout to animate.
- Verified by actually rendering it in headless Chromium against the live endpoint: 72,480 pixels painted, all eight layers in the legend with correct counts, hotspots populated, and zero external requests — the self-contained claim confirmed rather than asserted.

**User content could close its own prompt delimiter**
- The README advertises *"input sanitization + delimiter-wrapped user content"* as this app's prompt-injection defence. The sanitization half worked; the delimiter half did not. `wrap_user_content()` interpolated attacker-controlled text between `<PR_BODY>` and `</PR_BODY>` **without checking whether that text contained `</PR_BODY>` itself** — so a PR body could close the block early and land its own instructions *outside* the delimiters, where the model reads them as system text rather than as data. `sanitize_user_input()` never caught it: its XML patterns cover `<system>` and `</instructions>`, not the label names the module invents for itself.
- Worst on the PR path, where the body is written by whoever opened the pull request — an outside contributor could aim it at the risk assessment that decides whether a PR is safe to auto-merge. Reproduced end-to-end before fixing, and the test asserts on what escapes the block rather than on the presence of a string.
- Delimiter-shaped sequences are now escaped rather than deleted, so a legitimate `<TODO>` in a diff survives as `&lt;TODO&gt;` instead of vanishing — a scanner that silently eats content is its own bug. The escaping lives in `wrap_user_content`, **not** in `sanitize_user_input`: the router sanitizes the fully assembled prompt, which by then legitimately contains these delimiters, so defanging at that layer would destroy the markers it exists to protect.

**The memory index was a list used as a set**
- `_index_repo()` deduplicated by reading the entire index back on **every memory write** — O(n) in the number of repos, on the hottest path in the module whose own docstring says that complexity was removed from `remember()`. It was also not atomic: two concurrent writers for a new repo both saw "absent" and both pushed it. Now a Redis set: O(1), atomic, no duplicates, with the pre-7.2.0 list migrated on first read under a new key (reading a set with `LRANGE` raises `WRONGTYPE`, so changing the type in place would have broken every running worker until it restarted).
- `clear(repo)` dropped the memory list and the dedup hash but left the repo in the index, so `known_repos()` kept reporting a repo with nothing in it and the backup carried an empty record for it forever.

**Five more LLM fields requested, validated, and thrown away**
- A systematic sweep of `validator.py` against every reader in the codebase found that `pr_type` and `labels` (PR analysis) and `verdict`, `positives` and `refactor_opportunity` (code review) were sanitised and consumed by nothing. PRs are never labelled at all — only issues are — so `labels` had no destination even in principle. `verdict` duplicated `summary` under a comment claiming `app/mcp/handlers.py` and `evals/` read it; **neither ever did**. All five removed; `verdict` is still accepted as an *input* alias, which is the half that fixed the original blank-summary bug.
- This is the same defect the repo has shipped four times (`improved_title`, `verdict`/`summary`, `time_estimate`, `description`), and it was invisible every time because the validator's own tests pass — they assert the sanitising is correct, which says nothing about whether anyone consumes the result. `TestNoDeadValidatorFields` now checks the other half, so a field added without a reader fails the build.

**Six unguarded payload reads on the paths that matter most**
- `pr["user"]["login"]` in the PR handler and `issue["user"]["login"]` in the issue handler both run *before the EventLogger exists*. An issue or PR from a deleted account raised a `TypeError` that `server._run_handler`'s blanket handler swallowed, so the event vanished with a log line naming no cause. Also fixed: `/merge` read `pr["head"]["sha"]` and told the user "Merge failed" when a PR's source fork had been deleted, and the auto-merge guardrail raised on a change request from a deleted reviewer — turning "correctly blocked by a review" into a generic error.
- Guarded structurally rather than site-by-site: a test walks the AST for bare subscripts of `user`/`head`/`base`/`patch`, verified against the pre-fix tree to confirm it catches all six rather than passing vacuously.

**Durability and the quality gate — the two things nothing was watching**
- Memory backup now runs itself, every 15 days. The encrypted export existed and was correct; nothing invoked it, so a free-tier Redis wipe still lost every learned fact. **Export is scheduled, restore is not**: exporting writes ciphertext elsewhere, restoring *overwrites live memory*, so restore runs at boot and only when memory is empty — non-destructive by construction rather than by being careful about when it is called. If it cannot prove memory is empty, it does not restore. A test asserts the maintenance module cannot so much as name `restore_from_github`.
- **The schedule is a due time in Redis, not a `sleep()`.** This runs on a free tier that restarts on deploy and on idle, and every restart puts a `sleep(15 days)` back to zero — the timer would never have fired once. The due time survives restarts, is advanced *before* the work starts (so a pass that dies halfway costs one cycle instead of retrying every hour forever), and is claimed with `SET NX` so one of N gunicorn workers runs it rather than all of them.
- The same pass runs a **full security scan of every repository the app has seen**. That needed something new: an installation id arrives only on a webhook and nothing persisted it, so anything running on a schedule had no credential for any repository at all. `app/core/installations.py` is a small repo → installation-id registry, refreshed per event and expiring on its own; only the id is stored, never a token.
- The eval suite — the only check that can see the bot getting *worse* at reviewing code — failed loudly on a missing `GROQ_API_KEY`, but then filed an issue saying quality had regressed. That sends a maintainer to diff prompts and model versions when the fix is one repository secret; the two failures are now reported as the different things they are. A `push`-triggered CI job also reports when the nightly evals last ran, because GitHub disables scheduled workflows after 60 days of repository inactivity and a cron that stopped firing produces no failure and no issue.

**The PR description the bot generated but never wrote**
- The PR analysis prompt asks for a structured `## Summary / ## Changes / ## Testing` body, `validate_pr_analysis()` sanitises it to 5000 characters, `pull_requests.auto_fill_description` is documented as "Fills empty PR descriptions" and defaults to true, and `check_pr_description_update()` decides whether it is allowed — and **no code path ever wrote it**. Every PR analysis since the feature was written has been paying for a field it discarded. Same bug class as v7.0.0's `time_estimate`. Title and body now go in one PATCH, because GitHub emits a `pull_request.edited` webhook per write and this bot listens to those.
- The prelaunch "every config key is read" audit passed this, because the key *was* read — inside a function nothing called. That check is now generalised: every config-reading guardrail must be reachable from outside its own module. Verified by reverting the fix and watching it fail.
- `_analyze_pr` also subscripted `pr["user"]["login"]`, `pr["base"]["ref"]` and `pr["head"]["ref"]` directly. A PR from a deleted fork has a null `head` and one from a deleted account has a null `user`; either raised inside the function that decides the PR's risk level, losing the whole review.

**Hygiene, testing and CI**
- `app/handlers/pull_request.py` split into a package (classify / analysis / review / gaps / report); routing policy extracted from the LLM router; `/runtests` and `/notify` moved out of `publisher.py`, which had grown past the repo's own 600-line guard.
- Two existing tests were asserting on patch targets the code never resolved — they had been passing without testing anything. Two hardcoded MCP tool counts now derive from the registry, with handler↔catalog symmetry assertions.
- `record_latency()` had no callers, so `/health` and the health endpoint reported a 0% error rate no matter what the providers did. Wired into the circuit breaker and router.
- The dependency-free `Codebase map` CI job broke on a third-party import reached through a package `__init__`. The command registry moved to a pure-stdlib `app/core/commands.py`, and five subprocess tests now fail if any third-party import creeps back into that path.
- **Six unreachable modules resolved, none left.** `app/security/secrets.py` removed (superseded by `enhanced_secrets`). `app/security/licenses.py` — a complete copyleft-compliance scanner with green tests that nothing had ever imported, so the bot had never once reported a restrictive licence — is now part of `/secfull`, bounded to 20 packages and a 20-second budget, and omits packages it did not reach rather than reporting them as "unknown". `app/core/memory_backup.py` gained the operator CLI its documentation had been describing as `python -c` one-liners. `app/core/cache.py` was **deleted rather than wired**: every read it could have served either feeds a guardrail (`archived`, where a stale answer is precisely the bug v7.1.1 fixed) or picks a branch to write to (`default_branch`, where a stale answer targets the wrong ref) — and `load_config` already caches the one hot read that is safe to cache. Fixing bugs in unreachable code does not make it earn its place.
- Tests 1054 → 1855, coverage 79% → 84%, orphan modules 6 → **0**, import cycles 1 → **0**. Both are now CI gates rather than reports.

**`Retry-After` was parsed with a bare `int()` in three places**
- RFC 9110 allows this header to carry an HTTP-date (`Wed, 21 Oct 2015 07:28:00 GMT`) instead of a delay in seconds, and Groq sends *fractional* seconds (`7.66`). `int()` raises `ValueError` on both.
- In the GitHub client's **429** branch that `ValueError` was not caught at all. Every caller catches `GitHubError`, so a primary rate limit escaped as an unhandled exception rather than a rate-limit signal.
- In the **secondary** rate-limit branch it *was* caught — by the branch's own `except Exception`, which downgraded a transient, retryable limit into a permanent-looking `403 Forbidden`, discarding `retry_after` and the `GitHubSecondaryRateLimitError` type that `server.py` uses to decide "drop this event and let GitHub redeliver it".
- In the Groq provider it tripped the circuit breaker with the wrong failure reason, on the header shape Groq actually sends.
- All three now share `app/core/retry_after.py`, which parses delay-seconds and HTTP-dates, rounds fractional delays *up* so the wait is never shorter than asked, clamps absurd values, and is total — it never raises, and falls back to the caller's default on anything it cannot read. The regression tests were confirmed to fail against the previous code before the fix landed.

### V7.1.1 — 2026-08-03

- Removed `notifications.on_health_degraded` and the `notify_health_degraded` / `notify_ci_failure` / `notify_stale_closed` functions. Nothing could trigger any of them — the periodic health monitor was deleted in v6.1.0 and there is no stale-issue sweep — so these were alerts the product advertised and could never send. The v7.1.0 "every config key is read" check passed them because the toggle was wired even though the feature was unreachable; the check is now stricter.
- `notify_all_providers_down` is wired rather than removed, at most once per 15-minute window. A total outage affects every command at once, so an un-deduplicated alert would page the operator dozens of times for a single incident.
- `check_archived_repo()` had zero callers, so the bot commented and reviewed on archived repositories, which are read-only by intent. Now checked in the PR and issue handlers.

### V7.1.0 — 2026-08-03

Pre-launch audit. The theme is configuration the product documented and then ignored.

- **Thirteen dead config keys wired or removed.** `bot.enabled` — the documented master kill switch — had zero callers, so setting it to `false` left the bot fully active. `commands.enabled` was never enforced. `auto_merge.allowed_risk_levels` was never consulted, so a user restricting auto-merge to low-risk PRs still had high-risk ones merged. Every `notifications.on_*` toggle was ignored. `ai.primary_model` and friends sat in repo config where nothing could read them — model choice is a deployment concern (the router is a process-wide singleton), so they are now `LLM_PRIMARY_MODEL` / `LLM_FALLBACK_MODEL` env vars.
- **`/ignore` is now maintainer-only.** It writes to persistent repo memory, which V7 injects into every later prompt, but it was ungated: any commenter on a public repo could poison the context all subsequent commands saw — stored prompt injection that outlives the comment.
- **Per-repo AI budget is enforced.** `check_repo_rate_limit()` and `increment_repo_usage()` existed with zero callers, so `REPO_DAILY_AI_LIMIT` did nothing and one busy repository could drain the whole free-tier quota.
- **Review targets code, not licence files.** The review budget is spent by file kind first, then change size. Previously files were taken in GitHub's alphabetical order, so a PR touching `LICENSE`/`CONTRIBUTING`/`MANIFEST` exhausted the budget before reaching a single source file — and then reported a coverage score for code it had never read.
- **The command registry is no longer duplicated.** It lived in four places and had already drifted; `ALL_COMMANDS` is now the only source, and an absent `commands.enabled` means "no restriction" rather than "everything off".
- Config is documented as read from the default branch, never a PR head — a trust boundary, since config decides who may merge. Pinned by a test so it is not "fixed" into a privilege-escalation hole.
- New `tests/test_prelaunch_audit.py` checks these as classes rather than cases: every config key must be read, every `Config` helper must have a caller, any command reaching `remember()` must be gated, every command must be documented, and versions must agree across all manifests.

### V7.0.0 — 2026-07-27

**Correctness — the bot no longer fabricates output**
- Unparseable model responses (`{"raw": ...}`) fail closed instead of falling through to validator defaults. A non-JSON response used to render as "Score: 7/10 — ✅ No issues found" for a review that never happened.
- `validate_code_review` returned the assessment as `verdict` while the renderer read `summary` — **every** code review shipped with a blank summary. Second occurrence of this bug class after `improved_title`/`suggested_title`.
- `critical` was missing from `VALID_PRIORITIES`, so every critical issue was silently relabelled `medium` (this repo's own security issue #76 carries `priority: medium`). Same for type `refactor` and complexity `epic`. `time_estimate` was requested and discarded, so the Est. Effort row could never render.
- Hallucination detection guarded `/fix` and nothing else — 29 of ~30 output paths were unchecked. All commands now route through `app/ai/guarded.py`, with a structural test so a new command cannot skip it.

**Noise — comment volume cut hard**
- One sticky comment per PR, edited in place, replacing four on open plus two per push.
- Secret scanning switched to `enhanced_secrets` (the "drop-in replacement with false-positive reduction" that `push.py` never actually used) with a critical/high severity floor.
- Dedup now **fails closed**. `_already_reported` returned `False` on Redis errors — meaning "file it" — and the key hashed the *set of pattern names*, so different finding mixes bypassed each other. Evidence: issues #47/#50/#52/#54/#55/#59/#60 opened inside 73 seconds.
- CI had no dedup at all: a 5-job matrix failure produced 5 AI analyses and 5 comments. Now one per commit SHA.
- Code review batched into one LLM call instead of one per file (~7 calls per PR open → ~3).

**Intelligence — the subsystems are actually connected**
- Repo memory had **no write path**: nothing in the application called `remember()`. Added at `/merge`, `/apply` and triage.
- Recall was opt-in and therefore inert in every cloud deployment. Now on by default with write-time redaction; `MEMORY_ALLOW_CLOUD=0` opts out.
- `ConfidenceGate` compared every threshold against the model's *self-reported* confidence — a number it invents. Replaced with computed signals (field completeness, hallucination check, diff-anchor rate), with the model's claim at the lowest weight. `_review_code` was also passed the gate and never called it.

**Security (#76)**
- Zero-width stripping, whitespace collapse, and fail-closed rejection for critical-severity patterns.
- `wrap_user_content` had **zero production callers** — every handler interpolated raw user text into prompts. Now wired into every prompt site. See [docs/security/prompt-injection.md](docs/security/prompt-injection.md).

**Tests:** 908 → 1017. New tests assert on *rendered output* rather than validator return values — the gap that let all four correctness bugs survive the previous suite.

### V6.3.0 — 2026-07-16
- **CI security gate actually gates**: `pip-audit` had a trailing `|| true`, so the "Security" job could never fail even though `release` depends on it. 17 real CVEs across `flask`, `requests`, `PyJWT`, and `cryptography` (used for JWT signing and the encrypted memory backup) had gone silently unpatched as a result — all bumped, `pip-audit` now clean and blocking.
- **Gemini token-tracking bug fixed**: `_track()` used `incr()` (+1 per call) instead of `incrby(tokens)` — the identical V4 bug already fixed in `groq.py` but missed in `gemini.py`. `/budget` data for Gemini has been meaningless since it shipped. Caught by new tests (`gemini.py` coverage 23% → 90%).
- **Silent-failure audit**: all 26 bare `except Exception: pass` blocks in `app/` now log at debug/warning, so Redis and GitHub API degradation is observable instead of invisible.
- **Dead code removed**: `app/ai/prompt_builder.py` (297 lines, zero callers, zero tests) — a duplicate of prompt construction handlers already do inline. `learning.py` itself is confirmed wired (`record_fix_accepted`, `record_autofix_merged`).
- Local dev checkout re-synced (was 3+ weeks behind `main`) and MCP registration re-verified live against the deployed server.

### V6.2.0 — 2026-07-11
- **Inline PR reviews**: findings now post as a real GitHub Review with line-anchored comments, snapped onto actual diff lines, with committable ```suggestion blocks for safe single-line fixes. Automatic fallback to the classic issue comment if the Reviews API rejects a payload — a mapping bug can never lose a review.
- **AI evals** ([evals/](evals/)): golden issues + PR diffs with planted bugs (SQL injection, hardcoded secret, N+1, path traversal, plus a clean-diff over-flagging check), pushed through the *real* production code paths and scored deterministically. Manual `Evals` workflow in Actions.
- **Model disclosure**: every bot comment states which model produced it. **Quality floor** (`LLM_QUALITY_FLOOR=high`): reviews/fixes refuse to run on a basic-tier model instead of silently degrading to 8B.
- **Learning loop finally wired** (shipped unit-tested-but-unused in V6.0): `/apply` and merging a bot autofix branch now record acceptance; future `/fix` prompts inject the learned repo conventions.
- **Command rate limit enforced during Redis outages** (was fail-open) via a bounded in-memory window. **MCP named API keys** (`MCP_API_KEYS=laptop:tok1,ci:tok2`) with per-client revocation and an attributable audit log. **Redis memory watermark** on `/health` (the 25MB free tier fails writes when full — now visible before it bites).
- Honesty pass: durability claim corrected (Redis-down fallback is best-effort and now says so), demo labeled as simulated, `/` endpoint no longer reports the pre-rename app name.

### V6.1.1 — 2026-07-10
- **Honest badges**: the "tests: N passing" badge is now generated by CI itself — a `badges` job counts the passes from a real run on `main` and publishes the number; it can no longer drift from reality. New **Server Health** badge backed by a scheduled production ping.
- **No more cold-start surprises**: keep-alive workflow pings production every 10 minutes (Render free tier sleeps at 15 min idle) and turns red + emails the owner if the server is actually down. README now states the ~50 s cold-start worst case explicitly.
- **Event-queue fixes** (PR #69): eliminated constant "Timeout reading from socket" log spam, fixed a `TypeError` crash in confidence-gated `pull_request` handling, and a deadlock in `get_redis_blocking()`.
- Docker cleanup: removed a stale ChromaDB/SQLite `mkdir` from the Dockerfile and unused `SCHEDULED_*` env vars from docker-compose (that cron handler was deleted in V6.1.0).

### V6.1.0 — 2026-07-05
- **Live-validated, not just mock-tested**: booted the real app and drove it — real HMAC-signed webhooks through the full dispatch pipeline, `LLM_LOCAL_ONLY` refusing a genuinely unreachable network target, a full memory → encrypted-backup → restore round trip with an explicit no-plaintext-in-ciphertext assertion. Two real bugs found and fixed during this process: a duplicate/ungated release workflow, and the secret scanner flagging its own test fixtures.
- **+84 tests** (732 → 816): full integration coverage for the webhook pipeline, the local-LLM privacy guarantee, the comment-dispatch entry point (all 25 commands' routing verified), the GitHub Security API reader, and Slack/Discord notifications. Coverage 65% → 75%.
- **Two dead files removed** (verified via grep, not assumed): the pre-router V4 LLM client and an unwired V3 cron handler.
- Documentation corrected to match reality: the testing guide referenced a test file that no longer existed and a CI config that didn't match `.github/workflows/ci.yml`; both rewritten from verified values.

### V6.0.0 — 2026-07-04
- **Durable Redis event queue** — webhooks survive restarts; bounded, at-least-once, dead-letter, thread-pool fallback
- **Fail-closed MCP auth** + constant-time token compares + installation allowlist
- **Local-LLM privacy mode** (Ollama) — code never leaves your infra
- **Private repo memory** — explainable ("knows why") + encrypted backup
- **Live ops dashboard** (`/dashboard`) and **Claude Code plugin + marketplace**
- **Observability** — boot warnings for missing auth tokens; silent optional-path failures now instrumented
- **Maintainability** — `mcp_server.py` split into `tools.py` / `handlers.py` / dispatch
- Version single source of truth; config cross-tenant leak fixed; dead code purged
- Pro README, logo, animated demo, MCP setup guide, [reliability audit](docs/architecture/reliability-audit.md) + [roadmap](docs/architecture/roadmap.md)

### V5.0.0
- `comments.py` → `comments/` package (5 focused modules)
- Redis connection pooling, secret scanning on all branches
- LLM circuit breakers with automatic failover
- MCP server for IDE integrations · per-repo YAML config

---
