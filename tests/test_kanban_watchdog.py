from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_watchdog_module():
    module_path = Path(__file__).resolve().parents[1] / "ops" / "kanban_watchdog.py"
    spec = importlib.util.spec_from_file_location("kanban_watchdog", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_block_context_dedupes_identical_reason_and_summary() -> None:
    watchdog = _load_watchdog_module()
    reason = (
        "scope-blocked: merge is done and evidence was commented, "
        "but scoped completion was refused"
    )

    assert watchdog.block_context(reason, reason) == reason


def test_block_context_dedupes_whitespace_only_variants() -> None:
    watchdog = _load_watchdog_module()

    assert watchdog.block_context("scope-blocked:\n  merge done", "scope-blocked: merge done") == "scope-blocked:\n  merge done"


def test_block_context_keeps_distinct_reason_and_summary() -> None:
    watchdog = _load_watchdog_module()

    assert watchdog.block_context("missing credential", "latest worker summary") == "missing credential\nlatest worker summary"
