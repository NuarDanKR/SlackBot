"""Keep the Hermes source import exceptions narrower than the secret and data guards."""

from __future__ import annotations

import re
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "pre-commit"
PREFIX = ":(exclude)subbots/hermes/"


def _exclusions(text: str, variable: str) -> set[str]:
    match = re.search(rf"^{variable}='([^']*)'", text, re.MULTILINE)
    assert match is not None
    return {line for line in match.group(1).splitlines() if line.startswith(PREFIX)}


def test_hermes_keyword_exceptions_are_exact_source_files():
    hook = HOOK.read_text(encoding="utf-8")
    assert _exclusions(hook, "KEYWORD_ALLOWED") == {
        f"{PREFIX}src/archive-health/doc-filters.js",
        f"{PREFIX}scripts/check-shared-rules.js",
        f"{PREFIX}.claude/skills/doc-archive/SKILL.md",
        *(f"{PREFIX}.claude/skills/doc-archive/scripts/{name}.py" for name in (
            "xlsx_to_blocks", "test_xlsx_to_blocks", "test_screen_personal",
            "test_review_batch", "test_prune_cache", "test_fetch_filters",
            "apply_approvals", "decide", "screen_personal", "review_batch",
        )),
    }
    assert _exclusions(hook, "RRN_ALLOWED") == {
        f"{PREFIX}.claude/skills/doc-archive/scripts/test_review_batch.py",
        f"{PREFIX}.claude/skills/doc-archive/scripts/test_xlsx_to_blocks.py",
    }


def test_hermes_exceptions_do_not_skip_secret_or_runtime_data_checks():
    hook = HOOK.read_text(encoding="utf-8")
    assert "if git diff --cached -U0 | grep -nEq" in hook
    assert "subbots/hermes/(archive|qa-log|documents|slack-export|state|logs)" in hook
