"""
app/core/safe_import.py
V4 Sprint 5: Defensive import wrapper.

PROBLEM:
  Every Sprint, one bad import crashes an entire handler.
  format_findings, _get_collection, etc — 1 broken import = bot goes silent.

SOLUTION:
  safe_import() — returns None instead of crashing on ImportError.
  Handlers check if module loaded before using it.

USAGE:
    # Instead of:
    from app.intelligence.retrieval import get_context_for_pr  # crashes if broken

    # Use:
    from app.core.safe_import import safe_import
    retrieval = safe_import("app.intelligence.retrieval")
    context   = retrieval.get_context_for_pr(repo, files) if retrieval else ""
"""

import importlib
import logging
from types import ModuleType

log = logging.getLogger(__name__)

# Cache: module_path → (module | None)
_cache: dict[str, ModuleType | None] = {}


def safe_import(module_path: str) -> ModuleType | None:
    """
    Import a module safely. Returns None on ImportError.
    Cached — won't re-import on every call.
    """
    if module_path in _cache:
        return _cache[module_path]

    try:
        mod = importlib.import_module(module_path)
        _cache[module_path] = mod
        return mod
    except ImportError as e:
        log.warning(f"safe_import.failed module={module_path} error={e}")
        _cache[module_path] = None
        return None
    except Exception as e:
        log.error(f"safe_import.unexpected_error module={module_path} error={e}")
        _cache[module_path] = None
        return None


def safe_call(module_path: str, func_name: str, *args, default=None, **kwargs):
    """
    Import module and call a function safely.
    Returns default if import fails or function raises.

    Example:
        context = safe_call(
            "app.intelligence.retrieval",
            "get_context_for_pr",
            repo, files,
            default=""
        )
    """
    mod = safe_import(module_path)
    if mod is None:
        return default

    func = getattr(mod, func_name, None)
    if func is None:
        log.warning(f"safe_call.func_missing module={module_path} func={func_name}")
        return default

    try:
        return func(*args, **kwargs)
    except Exception as e:
        log.error(f"safe_call.error module={module_path} func={func_name}: {e}")
        return default


def clear_cache():
    """Clear import cache — useful in tests."""
    _cache.clear()
