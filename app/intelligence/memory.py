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

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict

log = logging.getLogger(__name__)

MEMORY_MAX_ITEMS = int(os.environ.get("MEMORY_MAX_ITEMS", "500"))
MAX_TEXT_CHARS = 2000
DEFAULT_TOP_K = 4
MAX_CONTEXT_CHARS = 3000

VALID_KINDS = {"fix", "decision", "pattern", "preference", "fact"}

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_INDEX_KEY = "mem:__index__"  # list of repos that have memory (for backup enumeration)


def _key(repo: str) -> str:
    return f"mem:{repo}"


def _index_repo(r, repo: str) -> None:
    """Record `repo` in the index list (dedup) so known_repos() can enumerate."""
    try:
        if repo.encode() not in (
            v.encode() if isinstance(v, str) else v for v in (r.lrange(_INDEX_KEY, 0, -1) or [])
        ):
            r.lpush(_INDEX_KEY, repo)
    except Exception:
        pass


def known_repos() -> list[str]:
    """Repos that have stored memory — used by backup to enumerate."""
    try:
        from app.core.redis_client import get_redis

        return list(get_redis().lrange(_INDEX_KEY, 0, -1) or [])
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
    True only when it is safe to inject memory into the LLM prompt for this
    process's configuration:
      - LLM_LOCAL_ONLY / LLM_PREFER_LOCAL  → local model, sensitive text stays in
      - MEMORY_ALLOW_CLOUD=1               → operator explicitly accepts cloud egress
    Otherwise memory is stored/searchable but NOT sent to any cloud provider.
    """

    def _truthy(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")

    return _truthy("LLM_LOCAL_ONLY") or _truthy("LLM_PREFER_LOCAL") or _truthy("MEMORY_ALLOW_CLOUD")


def remember(repo: str, text: str, kind: str = "fact", meta: dict | None = None) -> bool:
    """
    Store one memory. Deduplicates on exact text. Returns True if stored.
    Never raises — memory is an enhancement, not a critical path.
    """
    text = (text or "").strip()[:MAX_TEXT_CHARS]
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
        # Dedup: skip if identical text already present.
        for raw in r.lrange(key, 0, MEMORY_MAX_ITEMS - 1) or []:
            existing = MemoryItem.from_json(raw)
            if existing and existing.text == text:
                return False
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
        raws = r.lrange(_key(repo), 0, MEMORY_MAX_ITEMS - 1) or []
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
    try:
        from app.core.redis_client import get_redis

        get_redis().delete(_key(repo))
    except Exception:
        pass
