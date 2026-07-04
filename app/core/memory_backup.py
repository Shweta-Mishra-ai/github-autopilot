"""
app/core/memory_backup.py — Encrypted, client-side backup of repo memory.

THE PROBLEM
  Render's free tier can wipe Redis on restart. Per-repo memory
  (app/intelligence/memory.py) would be lost — the "brain" forgets everything.

THE CONSTRAINT (operator's rule)
  We may use the cloud for durability, but sensitive data/code must never sit
  in the cloud in the clear.

THE SOLUTION
  Encrypt the entire memory dump client-side with Fernet (AES-128-CBC + HMAC)
  BEFORE it leaves the process. Whatever durable store holds the backup — a
  private GitHub repo, object storage — only ever sees ciphertext. The key
  (MEMORY_BACKUP_KEY) stays with the operator and is never uploaded. Decryption
  happens only in-process on restore.

  export_encrypted()/import_encrypted() are pure and fully tested. The GitHub
  transport is a thin, optional helper on top of them.

SETUP
  Generate a key once:  python -c "from app.core.memory_backup import generate_key; print(generate_key())"
  Set it:               MEMORY_BACKUP_KEY=<that value>
  Unset key → backup is disabled (memory stays local-only, still works).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time

log = logging.getLogger(__name__)

BACKUP_VERSION = 1


def generate_key() -> str:
    """Generate a new Fernet key (base64 str). Store as MEMORY_BACKUP_KEY."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def is_configured() -> bool:
    return bool(os.environ.get("MEMORY_BACKUP_KEY", "").strip())


def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get("MEMORY_BACKUP_KEY", "").strip()
    if not key:
        raise RuntimeError("MEMORY_BACKUP_KEY is not set — encrypted backup disabled.")
    return Fernet(key.encode())


def _dump_repos(repos: list[str]) -> dict:
    """Collect raw memory items for the given repos into a plain dict."""
    from app.core.redis_client import get_redis
    from app.intelligence.memory import _key, MEMORY_MAX_ITEMS

    r = get_redis()
    data = {}
    for repo in repos:
        items = r.lrange(_key(repo), 0, MEMORY_MAX_ITEMS - 1) or []
        # Store newest-first exactly as Redis holds them.
        data[repo] = list(items)
    return data


def export_encrypted(repos: list[str] | None = None) -> bytes | None:
    """
    Encrypt all memory for `repos` (default: every known repo) into a single
    Fernet token. Returns ciphertext bytes, or None if backup isn't configured.
    """
    if not is_configured():
        return None
    try:
        from app.intelligence.memory import known_repos

        repos = repos if repos is not None else known_repos()
        envelope = {
            "version": BACKUP_VERSION,
            "created_at": int(time.time()),
            "repos": _dump_repos(repos),
        }
        plaintext = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        token = _fernet().encrypt(plaintext)
        log.info(f"memory_backup.exported repos={len(repos)} bytes={len(token)}")
        return token
    except Exception as e:
        log.error(f"memory_backup.export_failed: {e}")
        return None


def import_encrypted(token: bytes, overwrite: bool = True) -> int:
    """
    Decrypt a backup token and restore memory into Redis. Returns the number of
    repos restored. Raises cryptography.fernet.InvalidToken on a wrong key /
    tampered ciphertext (authenticated encryption — corruption is detected).
    """
    from app.core.redis_client import get_redis
    from app.intelligence.memory import _key, _index_repo

    plaintext = _fernet().decrypt(token)  # raises on wrong key / tamper
    envelope = json.loads(plaintext)
    repos = envelope.get("repos", {})

    r = get_redis()
    restored = 0
    for repo, items in repos.items():
        key = _key(repo)
        if overwrite:
            r.delete(key)
        # items are newest-first; re-lpush in reverse so order is preserved.
        for raw in reversed(items):
            r.lpush(key, raw)
        _index_repo(r, repo)
        restored += 1
    log.info(f"memory_backup.imported repos={restored}")
    return restored


# ── Optional GitHub transport (ciphertext only) ───────────────────────────────


def backup_to_github(
    target_repo: str, path: str, token: str, repos: list[str] | None = None
) -> bool:
    """
    Upload the encrypted memory blob to `target_repo` at `path` via the GitHub
    Contents API. Only ciphertext is transmitted. Use a PRIVATE repo. Returns
    True on success. Never raises.
    """
    blob = export_encrypted(repos)
    if blob is None:
        return False
    try:
        from app.github.client import gh_get, gh_put

        b64 = base64.b64encode(blob).decode("ascii")
        # Need the existing file SHA to update in place (if present).
        sha = None
        try:
            existing = gh_get(f"/repos/{target_repo}/contents/{path}", token)
            sha = existing.get("sha") if isinstance(existing, dict) else None
        except Exception:
            sha = None

        body = {"message": "chore: encrypted memory backup", "content": b64}
        if sha:
            body["sha"] = sha
        gh_put(f"/repos/{target_repo}/contents/{path}", token, body)
        log.info(f"memory_backup.pushed target={target_repo} path={path}")
        return True
    except Exception as e:
        log.error(f"memory_backup.github_push_failed: {e}")
        return False


def restore_from_github(target_repo: str, path: str, token: str) -> int:
    """Fetch the ciphertext blob from GitHub and restore. Returns repos restored, 0 on failure."""
    try:
        from app.github.client import gh_get

        data = gh_get(f"/repos/{target_repo}/contents/{path}", token)
        b64 = (data.get("content") or "").replace("\n", "") if isinstance(data, dict) else ""
        if not b64:
            return 0
        token_bytes = base64.b64decode(b64)
        return import_encrypted(token_bytes)
    except Exception as e:
        log.error(f"memory_backup.github_restore_failed: {e}")
        return 0
