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
        dumped = _dump_repos(repos)
        # Nothing to back up is never worth uploading. known_repos() returns
        # [] when Redis errors, and a Redis wiped mid-life (no boot restore)
        # is empty too: either way the scheduled backup encrypted
        # {"repos": {}}, overwrote the good backup with it, reported success,
        # and the next boot restored zero repositories.
        if not any(dumped.values()):
            log.warning(
                "memory_backup.nothing_to_export — refusing to overwrite a backup with an empty one"
            )
            return None
        envelope = {
            "version": BACKUP_VERSION,
            "created_at": int(time.time()),
            "repos": dumped,
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

    overwrite=False MERGES: what is already in Redis is kept, newest first,
    and the backup's items follow it, minus any already present.
    """
    if not overwrite:
        return _merge_encrypted(token)
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


def _merge_encrypted(token: bytes) -> int:
    """import_encrypted(overwrite=False). Never deletes an item already held."""
    import hashlib

    from app.core.redis_client import get_redis
    from app.intelligence.memory import MEMORY_MAX_ITEMS, MemoryItem, _hash_key, _index_repo, _key

    envelope = json.loads(_fernet().decrypt(token))
    r = get_redis()
    merged_repos = 0
    for repo, items in (envelope.get("repos") or {}).items():
        key = _key(repo)
        current = [x.decode() if isinstance(x, bytes) else x for x in (r.lrange(key, 0, -1) or [])]
        held = set(current)
        combined = (current + [x for x in items if x not in held])[:MEMORY_MAX_ITEMS]
        r.delete(key)
        for raw in reversed(combined):  # newest-first, as remember() keeps it
            r.lpush(key, raw)
        # The write-dedup index, so a restored memory is not stored twice.
        for raw in combined:
            item = MemoryItem.from_json(raw)
            if item and item.text:
                r.sadd(_hash_key(repo), hashlib.sha256(item.text.encode()).hexdigest()[:16])
        _index_repo(r, repo)
        merged_repos += 1
    log.info(f"memory_backup.merged repos={merged_repos}")
    return merged_repos


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
    """
    Fetch the ciphertext from GitHub and restore it, REPLACING what is held
    for each repository in it. A manual operation; the automatic path is
    reconcile(), which merges. Returns repos restored, 0 on failure.
    """
    try:
        blob = _fetch_backup(target_repo, path, token)
        if blob is None:
            log.error(f"memory_backup.github_restore_failed: no backup at {target_repo}/{path}")
            return 0
        return import_encrypted(blob)
    except Exception as e:
        log.error(f"memory_backup.github_restore_failed: {e}")
        return 0


# ── Scheduled backup (primitives; the cadence lives in app/core/maintenance) ──
#
# Render's free Redis keeps nothing on disk: Render can restart it at any time
# and everything in it is gone — while this app keeps running. Restoring only
# at app boot, and only when memory was empty, missed exactly that case: the
# first memory written after the wipe made memory "not empty", the restore
# never ran, and the next backup replaced the good one with that single item.
#
# So a wipe is detected directly. WIPE_MARKER is set once this Redis's memory
# is accounted for (merged from the backup, or there is no backup yet). A
# wipe removes it with everything else. While it is missing:
#   - reconcile() merges the backup in (never deleting what is there) and sets it;
#   - run_backup_once() refuses, so a half-empty Redis is never uploaded.
# reconcile() runs at boot and on the hourly maintenance tick.

BACKUP_REPO_ENV = "MEMORY_BACKUP_REPO"
BACKUP_PATH_ENV = "MEMORY_BACKUP_PATH"
BACKUP_TOKEN_ENV = "MEMORY_BACKUP_TOKEN"

DEFAULT_BACKUP_PATH = "memory.bin"

_RESTORE_LOCK_KEY = "memory_backup:restore_lock"


def backup_destination() -> tuple[str, str, str] | None:
    """
    (repo, path, token) for the GitHub transport, or None if not configured.

    All three are required together with the key — a key with no destination is
    a backup that encrypts something and then drops it.
    """
    repo = os.environ.get(BACKUP_REPO_ENV, "").strip()
    token = os.environ.get(BACKUP_TOKEN_ENV, "").strip()
    if not (repo and token and is_configured()):
        return None
    path = os.environ.get(BACKUP_PATH_ENV, "").strip() or DEFAULT_BACKUP_PATH
    return repo, path, token


WIPE_MARKER = "memory_backup:accounted"


def _marker_present() -> bool | None:
    """True/False, or None when Redis cannot be read."""
    try:
        from app.core.redis_client import get_redis

        return bool(get_redis().exists(WIPE_MARKER))
    except Exception as e:
        log.debug(f"memory_backup.marker_unreadable: {e}")
        return None


def _set_marker() -> None:
    from app.core.redis_client import get_redis

    get_redis().set(WIPE_MARKER, str(int(time.time())))


def run_backup_once() -> bool:
    """
    One export+push. Returns False when unconfigured, when this Redis has
    not been reconciled with the backup since it was last wiped, or when the
    push failed.

    Scheduling and cross-process locking are the caller's job.
    """
    dest = backup_destination()
    if dest is None:
        return False
    if not _marker_present():
        log.warning(
            "memory_backup.backup_refused — Redis has not been reconciled with the "
            "existing backup since it was (re)started; uploading now could replace "
            "the good backup with partial memory"
        )
        return False
    repo, path, token = dest
    return backup_to_github(repo, path, token)


def _fetch_backup(target_repo: str, path: str, token: str) -> bytes | None:
    """The ciphertext, or None when no backup exists yet. Raises on any other failure."""
    from app.github.client import GitHubError, gh_get

    try:
        data = gh_get(f"/repos/{target_repo}/contents/{path}", token)
    except GitHubError as e:
        if e.status_code == 404:
            return None
        raise
    if not isinstance(data, dict):
        raise ValueError("contents response was not a file")
    b64 = (data.get("content") or "").replace("\n", "")
    if not b64 and data.get("size") and data.get("sha"):
        # The contents API returns an EMPTY `content` over 1 MB; the git blob
        # API serves the same bytes up to 100 MB.
        blob = gh_get(f"/repos/{target_repo}/git/blobs/{data['sha']}", token)
        b64 = (blob.get("content") or "").replace("\n", "") if isinstance(blob, dict) else ""
    if not b64:
        raise ValueError(f"{path} has no content")
    return base64.b64decode(b64)


def reconcile() -> int:
    """
    Merge the backup into Redis if this Redis has lost its memory since it
    was last reconciled. Returns repos merged; 0 for "nothing to do" and for
    every failure (logged). Never raises; never deletes a memory.
    """
    dest = backup_destination()
    if dest is None:
        return 0
    present = _marker_present()
    if present is None or present:
        return 0
    try:
        from app.core.redis_client import get_redis

        # One process at a time: two merges could interleave.
        if not get_redis().set(_RESTORE_LOCK_KEY, str(int(time.time())), ex=300, nx=True):
            return 0
    except Exception as e:
        log.debug(f"memory_backup.restore_lock_failed: {e}")
        return 0

    repo, path, token = dest
    try:
        blob = _fetch_backup(repo, path, token)
        merged = 0 if blob is None else import_encrypted(blob, overwrite=False)
        _set_marker()
    except Exception as e:
        log.error(f"memory_backup.reconcile_failed — backups paused until it succeeds: {e}")
        return 0
    if blob is None:
        log.info(f"memory_backup.no_backup_yet at {repo}/{path} — backups start now")
    else:
        log.warning(
            f"memory_backup.restored repos={merged} from={repo}/{path} (merged; nothing deleted)"
        )
    return merged


def maybe_restore_on_boot() -> int:
    """Boot-time reconcile(); kept under its old name for the boot path."""
    return reconcile()


def backup_status() -> dict:
    """Operator-facing configuration state for /health. Never raises."""
    dest = backup_destination()
    return {
        "key_set": is_configured(),
        "configured": dest is not None,
        "destination": f"{dest[0]}/{dest[1]}" if dest else "",
    }


# ── Operator CLI ──────────────────────────────────────────────────────────────
#
# Disaster recovery is not something an operator should have to reconstruct
# from a docstring at the moment they need it. docs/ai-system/memory.md used to
# document this module as a set of `python -c` one-liners; these are the same
# calls with argument validation and exit codes, so a restore can be scripted.
#
# There is deliberately no automatic trigger. A restore overwrites live memory,
# and nothing in the request path should be able to cause that.


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m app.core.memory_backup",
        description="Encrypted export/restore of per-repo memory.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("genkey", help="print a new MEMORY_BACKUP_KEY and exit")

    p_exp = sub.add_parser("export", help="write an encrypted backup to a file")
    p_exp.add_argument("--out", required=True, help="destination file (ciphertext)")
    p_exp.add_argument("--repo", action="append", default=None, help="repeatable; default: all")

    p_imp = sub.add_parser("restore", help="restore memory from an encrypted file")
    p_imp.add_argument("--in", dest="src", required=True, help="ciphertext file to restore")
    p_imp.add_argument(
        "--merge",
        action="store_true",
        help="keep existing entries instead of replacing them",
    )

    args = ap.parse_args(argv)

    if args.cmd == "genkey":
        print(generate_key())
        return 0

    if not is_configured():
        print("MEMORY_BACKUP_KEY is not set — backup is disabled.", file=sys.stderr)
        return 2

    if args.cmd == "export":
        blob = export_encrypted(args.repo)
        if blob is None:
            print("Export failed; see logs.", file=sys.stderr)
            return 1
        with open(args.out, "wb") as fh:
            fh.write(blob)
        print(f"Wrote {len(blob)} bytes of ciphertext to {args.out}")
        return 0

    # restore
    with open(args.src, "rb") as fh:
        blob = fh.read()
    try:
        count = import_encrypted(blob, overwrite=not args.merge)
    except Exception as e:
        # InvalidToken means wrong key or tampered file — say which, because
        # "decryption failed" sends the operator looking in the wrong place.
        print(f"Restore failed ({type(e).__name__}): wrong key or corrupt file.", file=sys.stderr)
        return 1
    print(f"Restored memory for {count} repo(s).")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
