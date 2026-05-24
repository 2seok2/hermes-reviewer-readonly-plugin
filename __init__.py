"""Read-only enforcement for the dedicated ``reviewer`` profile.

The reviewer profile needs enough local access to inspect repository files and
``review_artifacts/...`` evidence, but it must not be able to mutate code or
machine state in practice.  This plugin enforces that boundary at the Hermes
tool-call layer for the active profile named ``reviewer``.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_REVIEWER_PROFILE = "reviewer"
_BLOCK_PREFIX = "reviewer-readonly:"

# Direct Hermes tools that can write files, mutate persistent state, drive UI
# actions, or spawn arbitrary code.  Keep read/search/kanban review tools out
# of this set so reviewer can inspect evidence and complete review cards.
_BLOCKED_TOOLS = {
    "write_file",
    "patch",
    "execute_code",
    "skill_manage",
    "memory",
    "fact_store",
    "fact_feedback",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_scroll",
    "browser_back",
    # These browser tools are mutation-capable even without obvious click/type
    # actions: browser_console can evaluate arbitrary JavaScript in the page
    # context and browser_cdp exposes raw Chrome DevTools Protocol commands.
    "browser_console",
    "browser_cdp",
    # Confirm/prompt dialogs can commit page actions, so reviewer keeps passive
    # browser inspection only: navigate/snapshot/get_images/vision remain allowed.
    "browser_dialog",
}

# process is part of the terminal toolset.  These actions write to or terminate
# another process; read/status actions remain allowed for review diagnostics.
_BLOCKED_PROCESS_ACTIONS = {"kill", "write", "submit", "close"}

# Terminal access is default-deny.  Reviewers should use Hermes read tools for
# most inspection and only a narrow set of known read-only shell diagnostics.
_ALLOWED_COMMANDS = {
    "pwd",
    "ls",
    "cat",
    "wc",
    "head",
    "tail",
    "grep",
    "git",
}

_ALLOWED_GIT_SUBCOMMANDS = {
    "status",
    "diff",
    "show",
    "log",
    "rev-parse",
    "ls-files",
    "grep",
    "blame",
    "describe",
}

# Some allowed git subcommands have options that write files or hand execution
# to configured helpers/pagers.  Keep the subcommand allowlist usable for
# read-only inspection, but reject those option-level escape hatches before git
# runs.  Values are human-readable reasons appended to block messages.
_GIT_BLOCKED_LONG_OPTIONS = {
    "--output": "can write command output to a file",
}
_GIT_BLOCKED_LONG_OPTIONS_BY_SUBCOMMAND = {
    "diff": {
        "--ext-diff": "can execute configured external diff helpers",
        "--textconv": "can execute configured external textconv filters",
    },
    "show": {
        "--ext-diff": "can execute configured external diff helpers",
        "--textconv": "can execute configured external textconv filters",
    },
    "log": {
        "--ext-diff": "can execute configured external diff helpers",
        "--textconv": "can execute configured external textconv filters",
    },
    "grep": {
        "--open-files-in-pager": "can execute an arbitrary pager command",
        "--textconv": "can execute configured external textconv filters",
    },
}
_GIT_BLOCKED_SHORT_OPTIONS_BY_SUBCOMMAND = {
    "grep": {
        "O": "can execute an arbitrary pager command",
    },
}

# Raw shell syntax that can hide or compose mutating commands.  Keep this check
# before shlex splitting so substitutions inside quoted arguments are rejected.
_LINE_BREAK_RE = re.compile(r"[\r\n]")
_SHELL_BYPASS_RE = re.compile(r"`|\$\(|[<>]\(")
_REDIRECTION_RE = re.compile(r"(^|\s)(?:\d?>|&>|\d?>>|>>|<>)")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CONTROL_TOKENS = {"&&", "||", ";", "|", "&", "(", ")"}


def _active_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception as exc:  # pragma: no cover - fail-open for non-reviewer use
        logger.debug("reviewer-readonly could not resolve active profile: %s", exc)
        return ""


def _is_reviewer_profile() -> bool:
    return _active_profile_name() == _REVIEWER_PROFILE


def _block(message: str) -> Dict[str, str]:
    return {"action": "block", "message": f"{_BLOCK_PREFIX} {message}"}


def _tokenize(command: str) -> tuple[list[str], Optional[str]]:
    if _LINE_BREAK_RE.search(command):
        return [], "shell line breaks are not allowed in reviewer terminal commands"
    if _SHELL_BYPASS_RE.search(command):
        return [], "shell substitution and process substitution are not allowed in reviewer sessions"
    if _REDIRECTION_RE.search(command):
        return [], "shell redirection is not allowed in reviewer sessions"
    # Make common control operators standalone even when the command omits
    # spaces.  In particular, POSIX shells treat ``&`` as a background-command
    # separator even in strings such as ``pwd&rm`` or ``pwd &rm``; shlex alone
    # would otherwise keep some of those forms attached to neighboring words.
    normalized = command
    for sep in ("&&", "||", ";", "|", "&"):
        normalized = normalized.replace(sep, f" {sep} ")
    try:
        return shlex.split(normalized, posix=True), None
    except ValueError as exc:
        return [], f"could not parse shell command safely: {exc}"


def _classify_git_options(subcommand: str, args: list[str]) -> Optional[str]:
    """Return a block reason for unsafe options on an allowed git command."""
    blocked_long = dict(_GIT_BLOCKED_LONG_OPTIONS)
    blocked_long.update(_GIT_BLOCKED_LONG_OPTIONS_BY_SUBCOMMAND.get(subcommand, {}))
    blocked_short = _GIT_BLOCKED_SHORT_OPTIONS_BY_SUBCOMMAND.get(subcommand, {})

    for token in args:
        # Everything after ``--`` is a pathspec or object argument, not an
        # option.  A file literally named ``--output=...`` must not be treated
        # as the write-capable diff option.
        if token == "--":
            break
        if token.startswith("--"):
            option = token.split("=", 1)[0]
            reason = blocked_long.get(option)
            if reason:
                return f"git {subcommand} option '{option}' is not allowed in reviewer sessions because it {reason}"
            continue
        if token.startswith("-") and token != "-":
            # Short options may be grouped (for example ``-nO``).  Block if a
            # subcommand-specific mutating short option appears anywhere in the
            # group before the pathspec terminator.
            for option, reason in blocked_short.items():
                if option in token[1:]:
                    return f"git {subcommand} option '-{option}' is not allowed in reviewer sessions because it {reason}"

    return None


def classify_terminal_command(command: str) -> Optional[str]:
    """Return a block reason for unsafe reviewer shell commands, else None."""
    if not isinstance(command, str) or not command.strip():
        return "empty terminal command is not useful for review"

    tokens, parse_error = _tokenize(command)
    if parse_error:
        return parse_error
    if not tokens:
        return "empty terminal command is not useful for review"

    # Fast path for redirection that appeared as an ordinary token after
    # shlex splitting, e.g. ``2>`` or ``>>``.
    if any(">" in tok for tok in tokens):
        return "shell redirection is not allowed in reviewer sessions"
    if any(tok in _CONTROL_TOKENS for tok in tokens):
        return "shell command chaining, grouping, and pipes are not allowed in reviewer sessions"
    if _ASSIGNMENT_RE.match(tokens[0]):
        return "environment assignments are not allowed in reviewer terminal commands"

    command_name = tokens[0]
    if "/" in command_name:
        return (
            f"path-qualified terminal command '{command_name}' is not allowed in reviewer sessions; "
            "use a bare allowlisted command name"
        )
    if command_name not in _ALLOWED_COMMANDS:
        return f"terminal command '{command_name}' is not in the reviewer read-only allowlist"

    if command_name == "git":
        if len(tokens) < 2:
            return "git requires an explicitly allowed read-only subcommand in reviewer sessions"
        subcommand = tokens[1]
        if subcommand == "-c" or subcommand.startswith("-c"):
            return "git -c can inject aliases and is not allowed in reviewer sessions"
        if subcommand.startswith("-"):
            return "git global options are not allowed before the subcommand in reviewer sessions"
        if subcommand not in _ALLOWED_GIT_SUBCOMMANDS:
            return f"git {subcommand} is not allowed in reviewer sessions"
        option_reason = _classify_git_options(subcommand, tokens[2:])
        if option_reason:
            return option_reason

    return None


def _on_pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> Optional[Dict[str, str]]:
    if not _is_reviewer_profile():
        return None

    args = args if isinstance(args, dict) else {}
    if tool_name in _BLOCKED_TOOLS:
        return _block(f"tool '{tool_name}' can mutate state and is disabled for the reviewer profile")

    if tool_name == "process":
        action = str(args.get("action") or "")
        if action in _BLOCKED_PROCESS_ACTIONS:
            return _block(f"process action '{action}' can mutate process state and is disabled")
        return None

    if tool_name == "terminal":
        reason = classify_terminal_command(str(args.get("command") or ""))
        if reason:
            return _block(reason)

    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
