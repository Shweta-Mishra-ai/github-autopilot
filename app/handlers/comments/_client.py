"""
app/handlers/comments/_client.py
Deferred delegation to the package-level GitHub wrappers and LLM router.

Why this module exists
──────────────────────
publisher.py, reviewer.py, security.py and integrations.py each re-exported the
package's `gh_*` helpers so that `patch("app.handlers.comments.gh_get")` reaches
them. Each did it with a module-level `import app.handlers.comments as hc`,
while the package's own __init__ imports all four at module level — a genuine
top-level import cycle:

    app.handlers.comments  ->  .publisher  ->  app.handlers.comments

It worked only because the wrappers look attributes up at CALL time, so Python's
partially-initialised module was never actually read during import. That is a
narrow ledge to stand on: any future module-level use of `hc.something` in those
files turns an AttributeError into an import-time crash, and none of the four
can be imported on its own.

The import here is deferred into each function body. sys.modules makes the
repeat cost a dict lookup, the cycle disappears from the import graph, and the
patch targets are unchanged — the attribute is still resolved on the package
module object at call time.
"""

from __future__ import annotations


def gh_get(*a, **kw):
    import app.handlers.comments as hc

    return hc.gh_get(*a, **kw)


def gh_post(*a, **kw):
    import app.handlers.comments as hc

    return hc.gh_post(*a, **kw)


def gh_put(*a, **kw):
    import app.handlers.comments as hc

    return hc.gh_put(*a, **kw)


def gh_delete(*a, **kw):
    import app.handlers.comments as hc

    return hc.gh_delete(*a, **kw)


class RouterProxy:
    """Attribute access forwarded to the package's router, resolved per call."""

    def __getattr__(self, name):
        import app.handlers.comments as hc

        return getattr(hc.router, name)


router = RouterProxy()
