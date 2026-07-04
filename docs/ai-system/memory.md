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

## The privacy rule (why this is safe)

Memory can contain source code and internal decisions, so it is treated as
**sensitive**. `recall_context()` injects memory into an LLM prompt **only** when
the model is local or you explicitly opt in:

| Config | Memory injected into prompt? |
|--------|------------------------------|
| default (cloud LLM) | ❌ No — stored & searchable, but never sent to a cloud provider |
| `LLM_LOCAL_ONLY=1` | ✅ Yes — runs on your Ollama, nothing leaves your infra |
| `LLM_PREFER_LOCAL=1` | ✅ Yes |
| `MEMORY_ALLOW_CLOUD=1` | ✅ Yes — you explicitly accept cloud egress |

This is the "smart brain, but sensitive code never leaves your infra" guarantee,
enforced in one place: `memory.injection_allowed()`.

## Durability — encrypted backup

Render's free tier can wipe Redis on restart. `app/core/memory_backup.py`
encrypts the **entire** memory dump client-side with Fernet (AES-128-CBC + HMAC)
*before* it leaves the process, so any durable store — a private GitHub repo,
object storage — only ever holds **ciphertext**. The key never leaves your env.

```bash
# 1. Generate a key once
python -c "from app.core.memory_backup import generate_key; print(generate_key())"

# 2. Set it
MEMORY_BACKUP_KEY=<that value>
```

Then, e.g. from a scheduled job:

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
memory.recall_context(repo, "queue")                 # -> prompt block, or "" if privacy disallows
memory.count(repo); memory.clear(repo); memory.known_repos()
```

`recall_context()` is already wired into the comment handler
(`augment_with_memory` in `app/handlers/comments/dispatcher.py`), so every
command automatically benefits when a local model is active.
