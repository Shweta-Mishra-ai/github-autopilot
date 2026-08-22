"""
app/intelligence/memory.py — Per-repo private memory ("the brain").

WHAT IT IS
  A lightweight, per-repository memory of facts the bot learns: accepted fixes,
  architecture decisions, recurring patterns, maintainer preferences. On the
  next command the most relevant memories are recalled and injected as context,
  so answers get sharper the more the repo is used.

PRIVACY (the load-bearing rule)
  Memory text can contain source code and internal decisions. It is therefore
  treated as SENSITIVE and, by default, is ONLY injected into prompts that run
  on a LOCAL model (LLM_LOCAL_ONLY / LLM_PREFER_LOCAL). It is never sent to a
  cloud provider unless the operator explicitly opts in with MEMORY_ALLOW_CLOUD=1.
  See injection_allowed(). This implements "smart brain, but sensitive code
  never leaves your infra."

STORAGE (free-tier safe)
  One Redis list per repo: mem:{repo}. Capped at MEMORY_MAX_ITEMS (LTRIM), so it
  can never grow unbounded on a 25MB free Redis. No heavy ML deps — retrieval is
  deterministic lexical similarity (set-cosine over tokens), which is local,
  free, and good enough to surface the right memory. Semantic embeddings are an
  optional future upgrade (see docs/ai-system/memory.md).

DURABILITY
  Redis on the free tier can be wiped on restart. app/core/memory_backup.py
  encrypts the whole memory dump client-side (Fernet) and can push the
  ciphertext to durable storage — the cloud only ever sees encrypted bytes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict

log = logging.getLogger(__name__)

MEMORY_MAX_ITEMS = int(os.environ.get("MEMORY_MAX_ITEMS", "500"))
# Upper bound on how many items recall() deserialises per query. The whole list
# was scanned and JSON-parsed on every single read; capping it keeps recall
# cost flat as a repo's memory grows.
MEMORY_RECALL_SCAN = int(os.environ.get("MEMORY_RECALL_SCAN", "200"))
MAX_TEXT_CHARS = 2000
DEFAULT_TOP_K = 4
MAX_CONTEXT_CHARS = 3000

VALID_KINDS = {"fix", "decision", "pattern", "preference", "fact"}

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
# Repos that have memory, for backup and the scheduled sweep to enumerate.
#
# A SET, not a list. It was a list with dedup done by reading the whole thing
# back on every write: O(n) in the number of repos on the hottest path in this
# module — the same complexity this file's docstring says was removed from
# remember() — and not atomic, so two concurrent writers for a new repo both
# saw "absent" and both pushed it. Redis has a type for this.
_REPO_SET_KEY = "mem:repos"
_LEGACY_INDEX_KEY = "mem:__index__"  # pre-7.2.0 list; migrated on first read
# Dedup hashes outlive nothing in particular — 90 days is long enough that the
# same fact isn't re-stored, short enough that the keys expire on a free tier.
_HASH_TTL = 90 * 86400


def _key(repo: str) -> str:
    return f"mem:{repo}"


def _hash_key(repo: str) -> str:
    """Set of content hashes for this repo — the O(1) write-dedup index."""
    return f"mem:hashes:{repo}"


def _index_repo(r, repo: str) -> None:
    """Record `repo` in the index so known_repos() can enumerate. O(1)."""
    try:
        r.sadd(_REPO_SET_KEY, repo)
    except Exception as e:
        log.debug(f"memory.index_repo_failed repo={repo}: {e}")


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _migrate_legacy_index(r) -> None:
    """
    Fold the pre-7.2.0 list into the set, once.

    Renaming rather than reusing the key is deliberate: reading a set with
    LRANGE raises WRONGTYPE, so a deploy that changed the type in place would
    have broken every running worker until it restarted.
    """
    try:
        legacy = r.lrange(_LEGACY_INDEX_KEY, 0, -1) or []
        if not legacy:
            return
        for value in legacy:
            r.sadd(_REPO_SET_KEY, _decode(value))
        r.delete(_LEGACY_INDEX_KEY)
        log.info(f"memory.index_migrated repos={len(set(map(_decode, legacy)))}")
    except Exception as e:
        log.debug(f"memory.index_migration_skipped: {e}")


def known_repos() -> list[str]:
    """
    Repos with stored memory — read by the backup and the scheduled sweep.

    Sorted so callers are deterministic: the maintenance pass takes a bounded
    slice of this, and an arbitrary Redis set order would silently scan a
    different subset every cycle.
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        _migrate_legacy_index(r)
        return sorted(_decode(v) for v in (r.smembers(_REPO_SET_KEY) or set()))
    except Exception:
        return []


def _tokens(text: str) -> set[str]:
    """Lowercase word/identifier tokens, length ≥ 2 (drops noise like 'a', 'x')."""
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2}


def _similarity(q_tokens: set[str], d_tokens: set[str]) -> float:
    """Set-cosine: |q∩d| / sqrt(|q|·|d|). Range 0..1, deterministic, no deps."""
    if not q_tokens or not d_tokens:
        return 0.0
    overlap = len(q_tokens & d_tokens)
    if overlap == 0:
        return 0.0
    return overlap / ((len(q_tokens) * len(d_tokens)) ** 0.5)


@dataclass
class MemoryItem:
    id: str
    kind: str
    text: str
    meta: dict
    ts: int

    @classmethod
    def from_json(cls, raw: str) -> "MemoryItem | None":
        try:
            d = json.loads(raw)
            return cls(
                id=d["id"],
                kind=d.get("kind", "fact"),
                text=d.get("text", ""),
                meta=d.get("meta") or {},
                ts=int(d.get("ts", 0)),
            )
        except Exception:
            return None


def injection_allowed() -> bool:
    """
    True unless the operator explicitly opts out with MEMORY_ALLOW_CLOUD=0.

    This was an opt-IN gate: recall_context() returned "" unless a local model
    was configured, which meant the brain was inert in every standard cloud
    deployment — it never recalled anything, so the "gets sharper the more the
    repo is used" promise never applied to most users.

    Content is now redacted at write time (app/core/redaction.py): code bodies
    are stripped and secret-shaped strings replaced, so what can leave the
    deployment is prose, file paths and symbol names. That makes "on" a
    defensible default. Operators who want the old behaviour set
    MEMORY_ALLOW_CLOUD=0.
    """
    raw = os.environ.get("MEMORY_ALLOW_CLOUD", "").strip().lower()
    return raw not in ("0", "false", "no")


def remember(repo: str, text: str, kind: str = "fact", meta: dict | None = None) -> bool:
    """
    Store one memory. Deduplicates on exact text. Returns True if stored.
    Never raises — memory is an enhancement, not a critical path.
    """
    from app.core.redaction import redact

    # Redact BEFORE anything else: nothing secret-shaped and no code body
    # should ever reach the store, regardless of who called us.
    text = redact((text or "").strip())[:MAX_TEXT_CHARS].strip()
    if not text or not repo:
        return False
    if kind not in VALID_KINDS:
        kind = "fact"

    item = MemoryItem(
        id=f"{int(time.time() * 1000):x}",
        kind=kind,
        text=text,
        meta=meta or {},
        ts=int(time.time()),
    )
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        key = _key(repo)

        # O(1) dedup via a per-repo set of content hashes. The old loop
        # deserialised the ENTIRE list as JSON on every single write just to
        # compare strings. A set (rather than one key per hash) keeps clear()
        # able to drop the whole thing in one delete.
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        if r.sadd(_hash_key(repo), digest) == 0:
            return False  # identical text already stored
        r.expire(_hash_key(repo), _HASH_TTL)

        r.lpush(key, json.dumps(asdict(item), separators=(",", ":")))
        r.ltrim(key, 0, MEMORY_MAX_ITEMS - 1)  # keep newest N, bound memory
        _index_repo(r, repo)
        log.info(f"memory.remember repo={repo} kind={kind}")
        return True
    except Exception as e:
        log.debug(f"memory.remember_failed repo={repo}: {e}")
        return False


def remember_decision(repo: str, decision: str, why: str = "", meta: dict | None = None) -> bool:
    """
    Store a decision together with its rationale — the "why" behind it.

    This is what makes the brain *explainable*: when the decision is later
    recalled, its reasoning comes with it, so the bot can justify what it does
    instead of asserting it blindly. The rationale is kept in meta["why"] and
    surfaced by recall_context().
    """
    m = dict(meta or {})
    if why:
        m["why"] = why.strip()[:MAX_TEXT_CHARS]
    return remember(repo, decision, kind="decision", meta=m)


def recall(repo: str, query: str, top_k: int = DEFAULT_TOP_K) -> list[MemoryItem]:
    """
    Return the top_k memories most relevant to `query`, most-relevant first.
    Ties broken by recency. Returns [] on any error or empty store.
    """
    if not repo or not query:
        return []
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        raws = r.lrange(_key(repo), 0, MEMORY_RECALL_SCAN - 1) or []
        q_tokens = _tokens(query)
        scored: list[tuple[float, int, MemoryItem]] = []
        for raw in raws:
            item = MemoryItem.from_json(raw)
            if not item:
                continue
            score = _similarity(q_tokens, _tokens(item.text))
            if score > 0.0:
                scored.append((score, item.ts, item))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [item for _s, _ts, item in scored[:top_k]]
    except Exception as e:
        log.debug(f"memory.recall_failed repo={repo}: {e}")
        return []


def recall_context(repo: str, query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """
    Formatted memory block for prompt injection — but ONLY when injection_allowed()
    (privacy guard). Returns "" when disallowed or nothing relevant, so callers
    can unconditionally concatenate the result.
    """
    if not injection_allowed():
        return ""
    items = recall(repo, query, top_k=top_k)
    if not items:
        return ""

    lines, total = ["## Repository Memory (learned context)"], 0
    for it in items:
        line = f"- [{it.kind}] {it.text}"
        # Surface the rationale so the model reasons *with the why*, not just the what.
        why = (it.meta or {}).get("why")
        if why:
            line += f"\n    ↳ why: {why}"
        if total + len(line) > MAX_CONTEXT_CHARS:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines) if len(lines) > 1 else ""


def count(repo: str) -> int:
    try:
        from app.core.redis_client import get_redis

        return int(get_redis().llen(_key(repo)) or 0)
    except Exception:
        return 0


def clear(repo: str) -> None:
    """
    Drop this repo's memory, its write-dedup index, and its index entry.

    Clearing only the list would leave the hash set behind, so re-storing a
    previously-known fact after a clear would silently no-op. Leaving the
    repo-set entry behind is the same mistake one level up: known_repos() would
    keep reporting a repo with nothing in it, and the backup would carry an
    empty record for it forever.
    """
    try:
        from app.core.redis_client import get_redis

        r = get_redis()
        r.delete(_key(repo), _hash_key(repo))
        r.srem(_REPO_SET_KEY, repo)
    except Exception as e:
        log.warning(f"memory.clear_failed repo={repo}: {e} — repo memory may not have been deleted")
