"""
DEPRECATED: app/handlers/comments.py
V5: This file has been split into app/handlers/comments/ package.
    This shim exists only for backward compatibility.
    Import from app.handlers.comments (the package) directly.
"""
# Re-export the public API from the new package
from app.handlers.comments import handle_comment_event  # noqa: F401

__all__ = ["handle_comment_event"]
