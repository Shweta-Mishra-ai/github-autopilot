"""
app/ai/prompt_builder.py
V4 Sprint 7: Dynamic prompt builder using repo-specific patterns.

Builds better prompts by injecting:
  - Repo language/framework context
  - Historical patterns (what fixes were accepted)
  - Coding style from existing codebase
  - Team conventions from .ai-repo-manager.yml
"""

import logging

log = logging.getLogger(__name__)


def build_fix_prompt(
    issue_title: str,
    issue_body: str,
    context: str = "",
    repo: str = "",
    history: str = "",
) -> tuple[str, str]:
    """
    Build system + user prompt for /fix command.
    Returns (system_prompt, user_prompt).
    """
    repo_context = _get_repo_context(repo) if repo else ""
    patterns     = _get_accepted_patterns(repo) if repo else ""

    system = (
        "You are a principal engineer with 15+ years experience. "
        "Give precise, production-ready fixes with complete working code. "
        f"{repo_context}"
    )

    user = f"""Fix this GitHub issue completely:

Issue: {issue_title}
Details: {issue_body[:1500]}

{f"Repo patterns (previously accepted fixes):{patterns}" if patterns else ""}
{f"Prior conversation:{history}" if history else ""}
{f"Codebase context:{context[:1000]}" if context else ""}

Return JSON:
{{
  "root_cause": "specific technical reason",
  "fix": "complete working code",
  "explanation": "why this fixes it",
  "test": "pytest test that verifies the fix",
  "affected_files": ["file1.py"],
  "breaking_change": false,
  "confidence": 0.85
}}"""

    return system, user


def build_review_prompt(
    filename: str,
    patch: str,
    context: str = "",
    repo: str = "",
) -> tuple[str, str]:
    """Build prompt for code review of a single file."""
    lang = _detect_language(filename)

    system = (
        f"You are a senior {lang} engineer doing a thorough code review. "
        "Give precise, actionable feedback with exact fixes. JSON only."
    )

    user = f"""Review this code change:

File: {filename}
```{lang}
{patch[:2000]}
```

{f"Related codebase:{context[:600]}" if context else ""}

Return JSON:
{{
  "score": 8,
  "issues": [
    {{
      "severity": "critical|major|minor|nit",
      "line": "~42",
      "issue": "what is wrong",
      "fix": "exact fix code"
    }}
  ],
  "positives": ["what is good"],
  "summary": "overall assessment",
  "confidence": 0.85
}}"""

    return system, user


def build_perf_prompt(
    code: str,
    filename: str = "",
    context: str = "",
) -> tuple[str, str]:
    """Build prompt for /perf command — performance analysis."""
    lang = _detect_language(filename)

    system = (
        f"You are a {lang} performance engineer. "
        "Identify bottlenecks, complexity issues, and optimization opportunities. JSON only."
    )

    user = f"""Analyze this code for performance issues:

{f"File: {filename}" if filename else ""}
```{lang}
{code[:3000]}
```

{f"Context:{context[:500]}" if context else ""}

Return JSON:
{{
  "overall_complexity": "O(n) or description",
  "performance_score": 7,
  "bottlenecks": [
    {{
      "location": "function_name or line ~N",
      "issue": "what causes slowdown",
      "impact": "high|medium|low",
      "fix": "optimized code",
      "improvement": "estimated speedup e.g. 10x"
    }}
  ],
  "quick_wins": ["easy optimizations"],
  "summary": "overall performance assessment"
}}"""

    return system, user


def build_arch_prompt(
    code: str,
    filename: str = "",
    context: str = "",
    repo: str = "",
) -> tuple[str, str]:
    """Build prompt for /arch command — architecture review."""
    system = (
        "You are a software architect reviewing for SOLID principles, "
        "separation of concerns, and dependency violations. JSON only."
    )

    user = f"""Architectural review:

{f"File: {filename}" if filename else ""}
{f"Repository: {repo}" if repo else ""}
```
{code[:3000]}
```

{f"Codebase context:{context[:600]}" if context else ""}

Return JSON:
{{
  "arch_score": 7,
  "violations": [
    {{
      "principle": "SRP|OCP|LSP|ISP|DIP|separation_of_concerns",
      "location": "class/function name",
      "issue": "what is violated",
      "fix": "how to fix",
      "severity": "high|medium|low"
    }}
  ],
  "good_patterns": ["what is well structured"],
  "refactor_priority": "high|medium|low",
  "summary": "architectural health summary"
}}"""

    return system, user


def _get_repo_context(repo: str) -> str:
    """Get cached repo context string for prompt injection."""
    try:
        from app.core.redis_client import get_redis
        r   = get_redis()
        ctx = r.get(f"repo_context:{repo}")
        return ctx.decode() if ctx else ""
    except Exception:
        return ""


def _get_accepted_patterns(repo: str) -> str:
    """Get summary of accepted fix patterns for this repo."""
    try:
        from app.core.learning import get_pattern_summary
        return get_pattern_summary(repo)
    except Exception:
        return ""


def _detect_language(filename: str) -> str:
    """Detect language from filename for prompt context."""
    ext_map = {
        ".py": "Python", ".ts": "TypeScript", ".js": "JavaScript",
        ".go": "Go", ".rs": "Rust", ".java": "Java",
        ".rb": "Ruby", ".cs": "C#", ".cpp": "C++",
        ".yml": "YAML", ".yaml": "YAML", ".json": "JSON",
    }
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1]
        return ext_map.get(ext, "code")
    return "code"


# ==== Unified functions expected by tests ====

def build_prompt(task: str, context: str = "", repo: str = "", history: str = "") -> str:
    """Unified prompt builder for all task types."""
    task = task.lower()
    if task == "fix":
        system, user = build_fix_prompt(
            issue_title="",
            issue_body=context,
            context="",
            repo=repo,
            history=history,
        )
        return system + "\n\n" + user
    elif task == "review":
        system, user = build_review_prompt(
            filename="",
            patch=context[:2000],
            context="",
            repo=repo,
        )
        return system + "\n\n" + user
    elif task == "perf":
        system, user = build_perf_prompt(
            code=context,
            filename="",
            context="",
        )
        return system + "\n\n" + user
    elif task == "arch":
        system, user = build_arch_prompt(
            code=context,
            filename="",
            context="",
            repo=repo,
        )
        return system + "\n\n" + user
    else:
        system = get_system_prompt(task)
        user = f"""{task.capitalize()} this:

{context[:1000]}

{f"Prior conversation: {history}" if history else ""}
{f"Repository: {repo}" if repo else ""}

Provide a detailed response."""
        return system + "\n\n" + user


def get_system_prompt(task: str) -> str:
    """Get system prompt for a given task type."""
    task = task.lower()
    prompts = {
        "fix": "You are a principal engineer with 15+ years experience. Give precise, production-ready fixes with complete working code.",
        "explain": "You are a senior software engineer. Explain code clearly and concisely.",
        "improve": "You are a code quality expert. Suggest improvements with clear rationale.",
        "test": "You are a QA engineer. Write comprehensive tests that cover edge cases.",
        "review": "You are a senior engineer doing thorough code reviews with actionable feedback.",
        "docs": "You are a technical writer. Write clear, concise documentation.",
        "refactor": "You are a software architect. Refactor code for clarity and maintainability.",
        "ci": "You are a DevOps engineer. Write efficient CI/CD pipelines.",
        "security": "You are a security expert. Identify vulnerabilities and suggest fixes.",
        "perf": "You are a performance engineer. Identify bottlenecks and optimization opportunities.",
        "arch": "You are a software architect. Review for design patterns and SOLID principles.",
    }
    return prompts.get(task, f"You are a helpful assistant for {task} tasks.")
