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


# ── Scheduled backup (primitives; the cadence lives in app/core/maintenance) ──
#
# Export is safe to automate; restore is not. Exporting reads memory and writes
# ciphertext elsewhere — worst case it wastes a request. Restoring *overwrites*
# live memory, so it runs on boot only when there is nothing to overwrite (see
# maybe_restore_on_boot), which makes it non-destructive by construction rather
# than by carefulness.

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


def run_backup_once() -> bool:
    """
    One export+push. Returns False when unconfigured or the push failed.

    Scheduling and cross-process locking are the caller's job — this function
    does exactly what it is asked, every time it is asked.
    """
    dest = backup_destination()
    if dest is None:
        return False
    repo, path, token = dest
    return backup_to_github(repo, path, token)


def maybe_restore_on_boot() -> int:
    """
    Restore memory at startup, but only when there is none.

    This is the whole safety argument: `known_repos()` is empty exactly when
    Redis has been wiped, which is the only situation a restore is for. If any
    memory exists the process leaves it alone, so a restart during normal
    operation, a second worker booting, or a partially-warm instance can never
    lose data to this path.

    Returns repos restored — 0 for "nothing to do" and for every failure, since
    a boot must not depend on a backup being reachable.
    """
    dest = backup_destination()
    if dest is None:
        return 0
    try:
        from app.intelligence.memory import known_repos

        if known_repos():
            log.debug("memory_backup.restore_skipped — memory is not empty")
            return 0
    except Exception as e:
        # Cannot prove memory is empty → do not overwrite it.
        log.warning(f"memory_backup.restore_skipped — could not read memory: {e}")
        return 0

    # Fails closed: a Redis problem means another worker may already be
    # restoring, and two concurrent restores can interleave writes.
    try:
        from app.core.redis_client import get_redis

        if not get_redis().set(_RESTORE_LOCK_KEY, str(int(time.time())), ex=300, nx=True):
            return 0
    except Exception as e:
        log.debug(f"memory_backup.restore_lock_failed: {e}")
        return 0

    repo, path, token = dest
    restored = restore_from_github(repo, path, token)
    if restored:
        log.warning(f"memory_backup.restored_on_boot repos={restored} from={repo}/{path}")
    return restored


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
