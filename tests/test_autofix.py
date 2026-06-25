"""
tests/test_autofix.py — V5
Tests for autofix path gating.

V5 changes:
  - .yml/.yaml removed from blanket ALLOWED_EXTENSIONS
  - ALLOWED_YAML_PATTERNS allowlist added for specific safe yaml files
  - _block_reason() updated to explain yaml restriction
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.handlers.autofix import _is_allowed, _block_reason, ALLOWED_YAML_PATTERNS


class TestIsAllowed:

    # ── Always blocked ─────────────────────────────────────────────────────

    def test_server_py_blocked(self):
        assert _is_allowed("server.py") is False

    def test_webhook_security_blocked(self):
        assert _is_allowed("app/core/webhook_security.py") is False

    def test_auth_module_blocked(self):
        assert _is_allowed("app/github/auth.py") is False

    def test_env_file_blocked(self):
        assert _is_allowed(".env") is False

    def test_github_workflow_blocked(self):
        assert _is_allowed(".github/workflows/ci.yml") is False

    def test_github_action_yml_blocked(self):
        assert _is_allowed(".github/workflows/deploy.yaml") is False

    def test_github_other_blocked(self):
        assert _is_allowed(".github/CODEOWNERS") is False

    def test_path_traversal_blocked(self):
        assert _is_allowed("../../etc/passwd") is False

    def test_absolute_path_blocked(self):
        assert _is_allowed("/etc/hosts") is False

    def test_requirements_txt_blocked(self):
        assert _is_allowed("requirements.txt") is False

    # ── YAML: blanket allow removed in V5 ─────────────────────────────────

    def test_arbitrary_yaml_blocked(self):
        """V5 FIX: .yaml files not in ALLOWED_YAML_PATTERNS must be blocked."""
        assert _is_allowed("config.yaml") is False

    def test_deploy_yaml_blocked(self):
        assert _is_allowed("deploy/api.yaml") is False

    def test_helm_values_blocked(self):
        assert _is_allowed("helm/values.yaml") is False

    def test_docker_compose_yaml_blocked(self):
        assert _is_allowed("docker-compose.yaml") is False

    def test_k8s_deployment_yaml_blocked(self):
        assert _is_allowed("k8s/deployment.yaml") is False

    # ── YAML: specific safe files allowed ─────────────────────────────────

    def test_mkdocs_yml_allowed(self):
        """Known safe: mkdocs.yml is documentation config."""
        assert _is_allowed("mkdocs.yml") is True

    def test_mkdocs_yaml_allowed(self):
        assert _is_allowed("mkdocs.yaml") is True

    def test_pre_commit_config_allowed(self):
        """Known safe: .pre-commit-config.yaml is dev tooling, not CI."""
        assert _is_allowed(".pre-commit-config.yaml") is True

    def test_codecov_yml_allowed(self):
        assert _is_allowed("codecov.yml") is True

    def test_codecov_yaml_allowed(self):
        assert _is_allowed("codecov.yaml") is True

    def test_readthedocs_yml_allowed(self):
        assert _is_allowed("readthedocs.yml") is True

    def test_markdownlint_yaml_allowed(self):
        assert _is_allowed(".markdownlint.yaml") is True

    # ── Regular files ──────────────────────────────────────────────────────

    def test_python_file_allowed(self):
        assert _is_allowed("app/handlers/comments.py") is True

    def test_readme_allowed(self):
        assert _is_allowed("README.md") is True

    def test_json_config_allowed(self):
        assert _is_allowed("package.json") is True

    def test_toml_allowed(self):
        assert _is_allowed("config.toml") is True

    def test_txt_allowed(self):
        assert _is_allowed("docs/notes.txt") is True

    def test_nested_python_allowed(self):
        assert _is_allowed("src/core/utils.py") is True

    # ── Edge cases ─────────────────────────────────────────────────────────

    def test_empty_path_blocked(self):
        assert _is_allowed("") is False

    def test_none_path_blocked(self):
        assert _is_allowed(None) is False

    def test_no_extension_blocked(self):
        assert _is_allowed("Makefile") is False

    def test_binary_extension_blocked(self):
        assert _is_allowed("lib/native.so") is False

    def test_dockerfile_blocked(self):
        assert _is_allowed("Dockerfile") is False


class TestBlockReason:

    def test_yaml_has_specific_message(self):
        """V5 FIX: yaml block reason should explain the restriction, not generic ext msg."""
        reason = _block_reason("config.yaml")
        assert "yaml" in reason.lower() or "yml" in reason.lower()

    def test_path_traversal_caught(self):
        assert _block_reason("../../etc/passwd") is not None

    def test_blocked_prefix_explains(self):
        reason = _block_reason(".github/workflows/ci.yml")
        assert reason is not None and len(reason) > 0

    def test_allowed_file_returns_none(self):
        assert _block_reason("app/utils.py") is None


class TestAllowedYamlPatterns:

    def test_patterns_list_is_non_empty(self):
        assert len(ALLOWED_YAML_PATTERNS) > 0

    def test_mkdocs_in_patterns(self):
        assert any("mkdocs" in p for p in ALLOWED_YAML_PATTERNS)

    def test_pre_commit_in_patterns(self):
        assert any("pre-commit" in p for p in ALLOWED_YAML_PATTERNS)

    def test_no_workflow_patterns(self):
        """Workflow files must never be in the allowlist."""
        assert not any("workflow" in p or "action" in p.lower() for p in ALLOWED_YAML_PATTERNS)

    def test_no_docker_compose_in_patterns(self):
        assert not any("docker" in p for p in ALLOWED_YAML_PATTERNS)
