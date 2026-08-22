# Repository Memory ("the brain")

Per-repo memory that makes the bot sharper the more a repository is used — while
keeping sensitive code **private by default**.

## What it stores

Short learned facts, one per entry, each tagged with a `kind`:

| kind | example |
|------|---------|
| `fix` | `fix: null-check in parser resolved the crash in #142` |
| `decision` | `decision: queue uses Redis lists, not Celery (free tier)` |
| `pattern` | `pattern: all handlers return Markdown, never raise` |
| `preference` | `preference: maintainer wants conventional-commit titles` |
| `fact` | anything else worth remembering |

Storage is one capped Redis list per repo (`mem:{repo}`), trimmed to
`MEMORY_MAX_ITEMS` (default 500) so it never grows unbounded on a 25 MB free
Redis. Retrieval is deterministic lexical similarity (set-cosine over tokens) —
**no 350 MB ML models**, works on the free tier, good enough to surface the
right memory. Semantic embeddings (via local Ollama) are a future drop-in.

## What writes to it

Memory is written where a real signal exists — not on every event:

| Trigger | kind | Stored |
|---------|------|--------|
| `/merge` on a `fix/bot-issue-*` branch | `fix` | the strongest acceptance signal available |
| `/apply` opens a PR from a bot branch | `pattern` | a maintainer chose to act on a bot fix |
| Issue triaged | `pattern` | the shape of issues this repo receives |

> Before V7 **nothing in the application called `remember()`** — only the backup
> module touched the store. The brain could not learn.

## The privacy rule (V7)

Memory can contain source code and internal decisions, so protection is applied
**at write time**: everything passes through `app/core/redaction.py` before
storage.

| Removed before storage | Kept |
|------------------------|------|
| Fenced and indented code blocks | Prose and rationale |
| Anything matching a secret pattern | File paths |
| | Symbol and function names |

Recall is then **on by default**:

| Config | Memory injected into prompt? |
|--------|------------------------------|
| default | ✅ Yes — redacted at write time |
| `LLM_LOCAL_ONLY=1` / `LLM_PREFER_LOCAL=1` | ✅ Yes — and nothing leaves your infra at all |
| `MEMORY_ALLOW_CLOUD=0` | ❌ No — stored & searchable, never sent to a provider |

**This inverts the V6 behaviour.** Recall used to require `LLM_LOCAL_ONLY`,
`LLM_PREFER_LOCAL` or `MEMORY_ALLOW_CLOUD=1`, which meant that in every standard
cloud deployment `recall_context()` returned `""` and the brain never recalled
anything. The "gets sharper the more the repo is used" promise did not apply to
most users. Redacting at the boundary makes "on" defensible; set
`MEMORY_ALLOW_CLOUD=0` to restore the old behaviour.

The guarantee is enforced in one place: `memory.injection_allowed()`.

## Cost

| Operation | V6 | V7 |
|-----------|----|----|
| `remember()` dedup | O(n) — full list deserialised as JSON per write | O(1) — per-repo hash set |
| `recall()` scan | entire list (up to `MEMORY_MAX_ITEMS`) | bounded by `MEMORY_RECALL_SCAN` (default 200) |

## Durability — encrypted backup

Render's free tier can wipe Redis on restart. `app/core/memory_backup.py`
encrypts the **entire** memory dump client-side with Fernet (AES-128-CBC + HMAC)
*before* it leaves the process, so any durable store — a private GitHub repo,
object storage — only ever holds **ciphertext**. The key never leaves your env.

```bash
# 1. Generate a key once
python -m app.core.memory_backup genkey

# 2. Set it
MEMORY_BACKUP_KEY=<that value>

# 3. Back up / restore
python -m app.core.memory_backup export --out memory.bin
python -m app.core.memory_backup restore --in memory.bin
```

`export` writes ciphertext only; `restore` replaces existing memory unless you
pass `--merge`. Both exit non-zero on failure, so they can be driven from a
scheduled job without the caller having to parse output.

### Automatic backup

Set these and the app backs itself up on a schedule, no cron needed:

```bash
MEMORY_BACKUP_KEY=<from genkey>
MEMORY_BACKUP_REPO=you/private-backup    # use a PRIVATE repo
MEMORY_BACKUP_TOKEN=<PAT with contents:write on that repo>
MEMORY_BACKUP_PATH=memory.bin            # optional
MAINTENANCE_INTERVAL_DAYS=15             # optional, default 15
```

All of `KEY`, `REPO` and `TOKEN` are required together — a key with no
destination encrypts something and drops it, so a partial configuration counts
as unconfigured rather than half-working.

**Export is scheduled; restore is not.** Exporting reads memory and writes
ciphertext elsewhere — worst case it wastes a request. Restoring *overwrites*
live memory, so it runs at boot and **only when memory is empty**, which is
exactly the situation a restore is for. That makes it non-destructive by
construction: a restart during normal operation, a second worker booting, or a
partially-warm instance are all no-ops. If the app cannot *prove* memory is
empty (a Redis error), it does not restore.

The schedule lives in Redis as a due time, not as a `sleep()`. On a free tier
that restarts on deploy and on idle, a thread sleeping for 15 days would never
fire once; storing the deadline means a restart cannot reset the clock. Check
it on `/health` under `maintenance` — `next_run_at` and `overdue` tell you
whether the schedule is actually alive.

Manual export and restore remain available for a deliberate migration, and the
GitHub transport can be called directly:

```python
from app.core.memory_backup import backup_to_github, restore_from_github
backup_to_github("you/private-backup", "memory.bin", token)   # push ciphertext
restore_from_github("you/private-backup", "memory.bin", token) # restore on boot
```

Wrong key or tampered bytes → `InvalidToken` is raised on restore (authenticated
encryption detects corruption). Unset key → backup disabled, memory stays
local-only and still works.

## API

```python
from app.intelligence import memory

memory.remember(repo, "decision: use Redis lists for the queue", kind="decision")
memory.recall(repo, "how is the queue built")        # -> [MemoryItem, ...]
memory.recall_context(repo, "queue")                 # -> prompt block, or "" if MEMORY_ALLOW_CLOUD=0
memory.count(repo); memory.clear(repo); memory.known_repos()
```

### Explainable decisions — the brain knows *why*

Decisions can carry their rationale, so the recalled context tells the model not
just *what* was decided but *why*. This is what makes the brain reason instead of
assert:

```python
memory.remember_decision(
    repo,
    "use Redis lists for the event queue",
    why="Celery is too heavy for the 512MB free tier",
)
```

`recall_context()` then renders:

```
## Repository Memory (learned context)
- [decision] use Redis lists for the event queue
    ↳ why: Celery is too heavy for the 512MB free tier
```

The rationale is stored in `meta["why"]` and is subject to the same privacy guard
as everything else — it only reaches a prompt on a local model (or explicit
`MEMORY_ALLOW_CLOUD=1`).

`recall_context()` is already wired into the comment handler
(`augment_with_memory` in `app/handlers/comments/dispatcher.py`), so every
command automatically benefits when a local model is active.
