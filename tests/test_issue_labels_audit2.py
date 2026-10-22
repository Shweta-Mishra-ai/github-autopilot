"""Audit round 2: triage applied arbitrary model labels; _ensure_labels
POSTed twelve labels on every issue."""

from unittest.mock import MagicMock, patch

from app.handlers import issues
from app.handlers.issues import BOT_LABELS, triage_labels


class TestTriageLabels:
    def test_unknown_labels_are_dropped(self):
        out = triage_labels(["urgent-fix-asap", "wontfix"], "priority: low 💤")
        assert out == ["priority: low 💤"]

    def test_bare_names_map_to_the_bot_label(self):
        assert triage_labels(["bug", "Security"], "priority: high 🔥") == [
            "bug 🐛",
            "security 🔒",
            "priority: high 🔥",
        ]

    def test_the_model_cannot_add_a_second_priority(self):
        out = triage_labels(["priority: critical 🚨"], "priority: low 💤")
        assert out == ["priority: low 💤"]


class TestEnsureLabels:
    def test_only_missing_labels_are_created_and_the_repo_is_remembered(self):
        existing = [{"name": name} for name, _ in BOT_LABELS[1:]]
        redis = MagicMock()
        redis.get.return_value = None
        with (
            patch("app.core.redis_client.get_redis", return_value=redis),
            patch("app.github.client.gh_get_all", return_value=existing),
            patch.object(issues, "gh_post") as post,
        ):
            issues._ensure_labels("o/r", "t")
        assert post.call_count == 1
        assert post.call_args[0][2]["name"] == BOT_LABELS[0][0]
        redis.set.assert_called_once()

    def test_a_remembered_repo_makes_no_calls(self):
        redis = MagicMock()
        redis.get.return_value = "1"
        with (
            patch("app.core.redis_client.get_redis", return_value=redis),
            patch("app.github.client.gh_get_all") as get_all,
            patch.object(issues, "gh_post") as post,
        ):
            issues._ensure_labels("o/r", "t")
        get_all.assert_not_called()
        post.assert_not_called()
