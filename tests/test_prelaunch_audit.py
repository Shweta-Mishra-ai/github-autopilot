"""
tests/test_prelaunch_audit.py — pre-launch correctness guarantees.

Every assertion here derives its expected value from the code itself rather
than from a copied constant, so these cannot go stale the way the documented
command list and the DEFAULTS enabled-list both did.
"""

import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).parent.parent


# ── /ignore authorization ─────────────────────────────────────────────────────


class TestIgnoreIsGated:
    """
    /ignore writes to persistent repo memory, and V7 injects that memory into
    every later prompt. Ungated, any drive-by commenter could poison the
    context every subsequent command sees.
    """

    def test_ignore_is_in_restricted_commands(self):
        from app.core.authorization import RESTRICTED_COMMANDS

        assert "/ignore" in RESTRICTED_COMMANDS

    def test_non_collaborator_cannot_invoke_ignore(self):
        from app.core.authorization import check_command_permission

        cfg = MagicMock()
        cfg.is_maintainer_only.return_value = False
        with patch("app.core.authorization.get_user_permission", return_value="none"):
            allowed, _reason = check_command_permission(
                "/ignore", "o/r", "drive-by", "tok", cfg
            )
        assert allowed is False

    def test_maintainer_can_invoke_ignore(self):
        from app.core.authorization import check_command_permission

        cfg = MagicMock()
        cfg.is_maintainer_only.return_value = False
        with patch("app.core.authorization.get_user_permission", return_value="write"):
            allowed, _reason = check_command_permission(
                "/ignore", "o/r", "maintainer", "tok", cfg
            )
        assert allowed is True

    def test_every_memory_writing_command_is_gated(self):
        """
        Structural guard: any cmd_* that reaches remember() must be gated, or
        it is a memory-poisoning vector.

        Uses AST rather than text scanning so a mention of remember() in a
        docstring or comment cannot produce a false positive, and a nested
        helper cannot hide a real call from a line-based search.
        """
        import ast

        from app.core.authorization import RESTRICTED_COMMANDS
        from app.handlers.comments import generator, publisher, reviewer, security

        offenders = []
        for mod in (generator, publisher, reviewer, security):
            tree = ast.parse(inspect.getsource(mod))
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef) or not node.name.startswith("cmd_"):
                    continue
                calls = {
                    n.func.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                if "remember" in calls or "remember_decision" in calls:
                    cmd = "/" + node.name[len("cmd_") :]
                    if cmd not in RESTRICTED_COMMANDS:
                        offenders.append(cmd)

        assert offenders == [], (
            f"these commands write to repo memory but are not gated: {offenders}. "
            "Memory is injected into every later prompt, so an ungated writer "
            "is a stored prompt-injection vector."
        )


# ── commands.enabled is enforced ──────────────────────────────────────────────


class TestEnabledListIsEnforced:
    """
    The enabled list was dead config: Config.command_enabled() existed with
    zero callers, so a maintainer who removed a command from the list still
    had it fully working.

    The root cause was not the missing call — it was that the command registry
    was duplicated in four places (ALL_COMMANDS, DEFAULTS, the shipped YAML,
    and the README table). Four copies of one list drift by construction. The
    fix removes the duplicates: ALL_COMMANDS is the only registry, and an
    unset enabled list means "no restriction configured" rather than
    "everything off".
    """

    def test_command_enabled_helper_has_callers(self):
        from app.handlers.comments import service

        assert "command_enabled" in inspect.getsource(service)

    def test_unset_list_enables_everything(self):
        """
        An absent key must not disable the product. _deep_merge REPLACES
        lists, so any DEFAULTS copy would silently become the ceiling.
        """
        from app.core.config import Config
        from app.handlers.comments.constants import ALL_COMMANDS

        cfg = Config({})
        for cmd in ALL_COMMANDS:
            assert cfg.command_enabled(cmd) is True, cmd

    def test_explicit_list_is_enforced(self):
        from app.core.config import Config

        cfg = Config({"commands": {"enabled": ["fix"]}})
        assert cfg.command_enabled("/fix") is True
        assert cfg.command_enabled("/autofix") is False

    def test_explicit_empty_list_disables_everything(self):
        """An operator who writes `enabled: []` means it — distinct from unset."""
        from app.core.config import Config

        cfg = Config({"commands": {"enabled": []}})
        assert cfg.command_enabled("/fix") is False

    def test_registry_is_not_duplicated_in_defaults(self):
        """
        The duplicate is the bug. DEFAULTS must not carry its own copy of the
        command list — that copy went stale (missing ignore, notify, report)
        and would have disabled three working commands the moment the list
        started being enforced.
        """
        from app.core.config import DEFAULTS

        assert "enabled" not in DEFAULTS.get("commands", {}), (
            "DEFAULTS must not duplicate the command registry — "
            "app.handlers.comments.constants.ALL_COMMANDS is the only source"
        )

    def test_disabled_command_is_not_dispatched(self):
        """End-to-end: a disabled command must produce no dispatch."""
        from app.handlers.comments import service

        cfg = MagicMock()
        cfg.command_enabled.return_value = False
        cfg.footer = ""
        payload = {
            "action": "created",
            "comment": {"body": "/fix please"},
            "issue": {"number": 1, "title": "t", "body": "b"},
            "repository": {"full_name": "o/r"},
            "installation": {"id": 1},
            "sender": {"login": "dev"},
        }
        with (
            patch.object(service, "get_installation_token", return_value="tok"),
            patch.object(service, "load_config", return_value=cfg),
            patch.object(service, "check_user_rate_limit", return_value=True),
            patch.object(service, "check_command_permission", return_value=(True, "")),
            patch.object(service, "_dispatch") as dispatch,
            patch.object(service, "_post_comment"),
        ):
            service.handle_comment_event(payload)
        dispatch.assert_not_called()


# ── No dead config ────────────────────────────────────────────────────────────


class TestNoDeadConfig:
    """
    The recurring defect class in this codebase is config that looks enforced
    and is not: command_enabled() had zero callers, ConfidenceGate was passed
    into _review_code and never called, wrap_user_content was written and
    never used. Each was found by hand, one at a time.

    This checks the whole class at once.
    """

    def test_bot_enabled_is_a_real_kill_switch(self):
        """
        `bot.enabled` ships in the sample config every user copies and is the
        documented way to turn the app off. It had zero callers: setting it to
        false left the bot fully active.
        """
        from app.core.config import Config

        off = Config({"bot": {"enabled": False}})
        assert off.bot_enabled() is False
        assert off.pr_enabled() is False, "bot.enabled=false must disable PR handling"
        assert off.issues_enabled() is False, "bot.enabled=false must disable issues"
        assert off.command_enabled("/fix") is False, "bot.enabled=false must disable commands"
        assert off.push_enabled() is False, "bot.enabled=false must disable push handling"

        on = Config({})
        assert on.bot_enabled() is True
        assert on.pr_enabled() is True
        assert on.command_enabled("/fix") is True

    def test_kill_switch_stops_every_webhook_handler(self):
        """End-to-end: no handler may act while the bot is switched off."""
        from app.core.config import Config
        from app.handlers import ci, issues, pull_request
        from app.handlers.comments import service

        off = Config({"bot": {"enabled": False}})

        cases = [
            (pull_request, "handle", {
                "action": "opened",
                "pull_request": {"number": 1, "title": "t", "body": "b",
                                 "user": {"login": "dev"},
                                 "head": {"ref": "f", "sha": "s"}, "base": {"ref": "main"}},
                "repository": {"full_name": "o/r"}, "installation": {"id": 1},
            }),
            (issues, "handle", {
                "action": "opened",
                "issue": {"number": 1, "title": "t", "body": "b", "user": {"login": "dev"}},
                "repository": {"full_name": "o/r"}, "installation": {"id": 1},
            }),
        ]
        for mod, fn, payload in cases:
            with (
                patch.object(mod, "get_installation_token", return_value="tok"),
                patch.object(mod, "load_config", return_value=off),
                patch.object(mod, "gh_get", return_value=[]),
                patch.object(mod, "gh_post") as post,
                patch.object(mod.router, "ask") as ask,
            ):
                getattr(mod, fn)(payload)
            ask.assert_not_called()
            post.assert_not_called()

        # Commands
        with (
            patch.object(service, "get_installation_token", return_value="tok"),
            patch.object(service, "load_config", return_value=off),
            patch.object(service, "_dispatch") as dispatch,
            patch.object(service, "_post_comment"),
        ):
            service.handle_comment_event({
                "action": "created",
                "comment": {"body": "/fix"},
                "issue": {"number": 1, "title": "t", "body": "b"},
                "repository": {"full_name": "o/r"},
                "installation": {"id": 1},
                "sender": {"login": "dev"},
            })
        dispatch.assert_not_called()

        # Push
        with (
            patch.object(ci, "get_installation_token", return_value="tok"),
            patch.object(ci, "load_config", return_value=off),
        ):
            from app.ai import guarded

            with patch.object(guarded, "safe_router_ask") as ask:
                ci.handle({
                    "action": "completed",
                    "check_run": {"name": "pytest", "conclusion": "failure",
                                  "output": {}, "pull_requests": [{"number": 1}],
                                  "head_sha": "s"},
                    "repository": {"full_name": "o/r"}, "installation": {"id": 1},
                })
            ask.assert_not_called()

    def test_every_documented_config_key_is_read(self):
        """
        The broadest form of the recurring defect: a key in DEFAULTS that
        nothing reads is a setting the product advertises and ignores.

        Thirteen keys were dead when this was written — including
        auto_merge.allowed_risk_levels (a safety control), ai.primary_model
        (the user's model choice), and every notifications.on_* toggle.
        Finding them one at a time is how they accumulated; this finds them
        all, every run.
        """
        from app.core.config import DEFAULTS

        def leaves(d, path=()):
            for k, v in d.items():
                if isinstance(v, dict):
                    yield from leaves(v, path + (k,))
                else:
                    yield path + (k,)

        corpus = "".join(
            p.read_text(encoding="utf-8")
            for p in Path("app").rglob("*.py")
            if p.name != "config.py"
        ) + Path("server.py").read_text(encoding="utf-8")

        dead = [
            ".".join(path)
            for path in leaves(DEFAULTS)
            if not re.search(rf"\b{re.escape(path[-1])}\b", corpus)
        ]
        assert dead == [], (
            f"config keys nothing reads: {dead}. Each is a setting the product "
            "documents and then ignores. Wire it up or remove it."
        )

    def test_no_config_key_backs_an_unreachable_feature(self):
        """
        Stricter than "is the key read". A key can be read by a filter that
        nothing ever reaches — notifications.on_health_degraded passed the
        read-check while notify_health_degraded() had no caller, so the
        notification could never fire under any circumstance.

        For each notification toggle, the function it governs must be
        reachable from somewhere other than its own definition.
        """
        import ast

        from app.github.notifications import _CONFIG_EVENT_KEYS

        called: set[str] = set()
        for path in list(Path("app").rglob("*.py")) + [Path("server.py")]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Name):
                        called.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        called.add(fn.attr)

        unreachable = [
            f"notify_{event}"
            for event in _CONFIG_EVENT_KEYS
            if f"notify_{event}" not in called
        ]
        assert unreachable == [], (
            f"these notifications are configurable but can never fire: {unreachable}. "
            "A toggle for an unreachable feature is a promise the product cannot keep."
        )

    def test_every_config_reading_guardrail_has_a_caller(self):
        """
        The generalisation of the check above, and the one that was missing.

        check_pr_description_update() read `pull_requests.auto_fill_description`
        — a key that is documented ("Fills empty PR descriptions"), defaults to
        true, and ships in .ai-repo-manager.yml. The read-check passed it
        because the key *was* read. It was read inside a function nothing
        called, so the PR body was never filled: the model was asked for one,
        the validator sanitised it, and the value was dropped.

        A guardrail exists to gate an action. One with no caller gates nothing,
        so every config-reading guardrail must be reached from outside its own
        module.
        """
        import ast

        guardrails = Path("app/core/guardrails.py")
        tree = ast.parse(guardrails.read_text(encoding="utf-8"))

        # Module-level functions whose body reads a config key.
        config_readers = set()
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "config"
                ):
                    config_readers.add(node.name)
                    break

        assert config_readers, "no config-reading guardrails found — has the file moved?"

        called: set[str] = set()
        for path in list(Path("app").rglob("*.py")) + [Path("server.py"), Path("worker.py")]:
            if path == guardrails:
                continue  # a guardrail calling itself proves nothing
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Name):
                        called.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        called.add(fn.attr)

        dead = sorted(config_readers - called)
        assert dead == [], (
            f"guardrails that read config but nothing calls: {dead}. "
            "Each gates a documented setting that therefore has no effect."
        )

    def test_archived_repositories_are_not_acted_on(self):
        """
        check_archived_repo() had zero callers, so the bot commented on,
        labelled and reviewed archived repositories — which are read-only by
        intent.
        """
        import ast

        called: set[str] = set()
        for path in Path("app/handlers").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Name):
                        called.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        called.add(fn.attr)
        assert "check_archived_repo" in called, (
            "no handler checks whether the repository is archived"
        )

    def test_every_config_helper_is_called_somewhere(self):
        import ast

        from app.core.config import Config

        helpers = {
            name
            for name in vars(Config)
            if not name.startswith("_") and callable(getattr(Config, name, None))
        } - {"get"}  # get() is the generic accessor, called everywhere

        used: set[str] = set()
        for path in Path("app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    used.add(node.attr)

        dead = sorted(helpers - used)
        assert dead == [], (
            f"Config helpers with no caller in app/: {dead}. "
            "A config key nothing reads is a promise the product does not keep."
        )


# ── Config is a trust boundary ────────────────────────────────────────────────


class TestConfigTrustBoundary:
    """
    Config is fetched from the DEFAULT BRANCH, never from a PR head.

    This looks like a missing `?ref=` and has been mistaken for a bug. It is a
    security control: config decides who may merge, whether auto-merge runs,
    whether secrets are scanned, and whether the bot runs at all. Honouring it
    from a PR head would let any outside contributor escalate privileges by
    editing the YAML inside their own pull request.
    """

    def test_config_fetch_is_not_ref_pinned_to_a_branch(self):
        import inspect

        from app.core import config as config_mod

        src = inspect.getsource(config_mod)
        fetch_line = next(
            line for line in src.splitlines() if "contents/.ai-repo-manager.yml" in line
        )
        assert "ref=" not in fetch_line, (
            "config must be read from the default branch. Adding ?ref= to honour "
            "a PR head would let a contributor grant themselves merge rights by "
            "editing config inside their own PR."
        )

    def test_the_reason_is_documented_at_the_call_site(self):
        """A future reader must not 'fix' this into a vulnerability."""
        import inspect

        from app.core import config as config_mod

        src = inspect.getsource(config_mod)
        idx = src.index("contents/.ai-repo-manager.yml")
        preceding = src[max(0, idx - 1200) : idx]
        assert "default branch" in preceding.lower()
        assert "auto_merge" in preceding or "maintainer_only" in preceding


# ── Review targets the code, not the licence files ────────────────────────────


class TestReviewFilePrioritisation:
    """
    max_files_reviewed used to take files in the order GitHub returned them,
    which is alphabetical. On a PR touching LICENSE/CONTRIBUTING/MANIFEST the
    budget was spent before reaching a single source file — and the bot then
    reported a test-coverage score for code it had never read.
    """

    def test_source_files_outrank_licences_and_config(self):
        from app.handlers.pull_request import _file_review_priority as prio

        assert prio("app/core/config.py") > prio("tests/test_x.py")
        assert prio("tests/test_x.py") > prio("pyproject.toml")
        assert prio("pyproject.toml") > prio("LICENSE")
        assert prio("app/main.py") > prio("MANIFEST.in")

    def test_the_pr_79_file_set_now_selects_source_files(self):
        """Regression against the exact case that exposed this."""
        from app.handlers.pull_request import _file_review_priority as prio

        files = [
            ".claude-plugin/marketplace.json",
            "CONTRIBUTING.md",
            "LICENSE",
            "LICENSE-APACHE",
            "LICENSE-MIT",
            "MANIFEST.in",
            "app/ai/guarded.py",
            "app/core/config.py",
            "app/handlers/pull_request.py",
            "tests/test_v7_noise.py",
        ]
        top = sorted(files, key=prio, reverse=True)[:4]
        assert all(f.startswith(("app/", "tests/")) for f in top), top
        assert "LICENSE" not in top

    def test_larger_changes_win_within_the_same_tier(self):
        """A 200-line change matters more than a 2-line one of the same kind."""
        from app.handlers.pull_request import _review_sort_key

        big = {"filename": "app/b.py", "additions": 200, "deletions": 10}
        small = {"filename": "app/a.py", "additions": 2, "deletions": 0}
        assert sorted([small, big], key=_review_sort_key, reverse=True)[0] is big


# ── Published numbers are true ────────────────────────────────────────────────


class TestPublishedNumbersAreTrue:
    def _readme(self) -> str:
        return (_ROOT / "README.md").read_text(encoding="utf-8")

    def test_mcp_tool_count_is_derived_not_hardcoded(self):
        """server.py advertised a literal 8 — it lies the moment a tool is added."""
        import server
        from app.mcp.tools import MCP_TOOLS

        src = inspect.getsource(server)
        assert '"tools": 8' not in src, "MCP tool count must be derived from MCP_TOOLS"
        assert "len(MCP_TOOLS)" in src or "MCP_TOOLS" in src

        with server.app.test_client() as c:
            assert c.get("/mcp").get_json()["tools"] == len(MCP_TOOLS)

    def test_every_command_is_documented(self):
        """
        /ignore shipped undocumented.

        The pattern matches the command name inside a code span and ignores
        any argument placeholder that follows, so `/rollback N` counts as
        documenting /rollback. An earlier version anchored on a closing
        backtick and reported /rollback as missing — a false positive, which
        is worse than no test at all.
        """
        from app.handlers.comments.constants import ALL_COMMANDS

        section = self._readme().split("## Commands")[1].split("## Architecture")[0]
        documented = {"/" + m for m in re.findall(r"`/([a-z]+)\b[^`]*`", section)}
        missing = set(ALL_COMMANDS) - documented
        assert missing == set(), f"undocumented commands: {sorted(missing)}"

    def test_the_documentation_check_cannot_pass_vacuously(self):
        """A parser that finds nothing would make the test above meaningless."""
        from app.handlers.comments.constants import ALL_COMMANDS

        section = self._readme().split("## Commands")[1].split("## Architecture")[0]
        documented = {"/" + m for m in re.findall(r"`/([a-z]+)\b[^`]*`", section)}
        assert len(documented) >= len(ALL_COMMANDS)
        assert "/rollback" in documented, "argument placeholders must still count"

    def test_headline_command_count_is_correct(self):
        """
        The "N slash commands" claim at the top of the README is the first
        number a reader checks. It said 26 while the registry held 27.
        """
        from app.handlers.comments.constants import ALL_COMMANDS

        claims = re.findall(r"\*\*(\d+) slash commands\*\*", self._readme())
        assert claims, "the headline command-count claim has moved or been removed"
        for claimed in claims:
            assert int(claimed) == len(ALL_COMMANDS), (
                f"README claims {claimed} slash commands, registry has {len(ALL_COMMANDS)}"
            )

    def test_version_is_consistent_everywhere(self):
        """A stale version string in any manifest is a launch-day embarrassment."""
        import json

        from app import __version__

        assert json.loads((_ROOT / "mcp-manifest.json").read_text(encoding="utf-8"))[
            "version"
        ] == __version__
        assert json.loads(
            (_ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8")
        )["version"] == __version__
        pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert f'version = "{__version__}"' in pyproject
