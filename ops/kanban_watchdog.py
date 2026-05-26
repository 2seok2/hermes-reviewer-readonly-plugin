#!/usr/bin/env python3
"""Hermes Kanban watchdog: dispatch obvious ready work and alert on stalls.

No-agent cron contract: print nothing when healthy; print concise Korean report when
there is user intervention needed, a non-blocking warning, or an automated action worth surfacing.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

HOME = "/Users/yeonseoklee"
HERMES_HOME = Path(HOME) / ".hermes"
ORG_ROOT = Path("/Users/yeonseoklee/.hermes-org")
STATE_PATH = ORG_ROOT / "ops" / "kanban-watchdog-state.json"
CHAT_ID = "-5133663775"
REMINDER_SECONDS = 30 * 60
STALE_RUNNING_SECONDS = 30 * 60
RECOVERY_STALE_RUNNING_SECONDS = 15 * 60
RECOVERY_REMINDER_SECONDS = 15 * 60
DISPATCH_MAX: str | None = "10"
AUTO_UNBLOCK_LIMIT = 1

HUMAN_REQUIRED_RE = re.compile(
    r"token|credential|secret|api key|password|2fa|oauth|login|paywall|"
    r"missing.*(key|token|credential|secret|password|env)|"
    r"approval|approve|human decision|user decision|destructive|irreversible|"
    r"production approval|permission grant|"
    r"토큰|승인|자격|비밀|로그인|권한 부여|결제|유료|인증키|환경변수",
    re.IGNORECASE,
)

REVIEW_COMPLETION_HUMAN_REQUIRED_RE = re.compile(
    r"token|credential|secret|api key|password|2fa|oauth|login|paywall|"
    r"missing.*(key|token|credential|secret|password|env)|"
    r"explicit approval|approval required|human decision|user decision|destructive|irreversible|"
    r"production approval|permission grant|"
    r"토큰|자격|비밀|로그인|권한 부여|결제|유료|인증키|환경변수|명시적 승인|사용자 결정|사람 판단",
    re.IGNORECASE,
)

RECOVERABLE_RE = re.compile(
    r"iteration budget|protocol violation|rebase|merge conflict|conflict|timeout|timed out|"
    r"crashed|stale|validation|test failure|failed after|exhausted|"
    r"insufficient evidence|missing evidence|review packet|artifact|"
    r"profile.*not|permission denied|충돌|재시도|검증|반복|증거 부족",
    re.IGNORECASE,
)

MAX_REMINDERS = 3
REVIEW_BRIDGE_SCRIPT = HERMES_HOME / "scripts" / "review_required_bridge.py"


def run(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = HOME
    return subprocess.run(args, env=env, text=True, capture_output=True, check=check)


def board_slugs() -> list[str]:
    proc = run(["hermes", "kanban", "boards", "list"])
    slugs: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip() or line.strip().startswith(("SLUG", "Current", "Switch")):
            continue
        line = line.replace("●", " ").strip()
        m = re.match(r"([a-zA-Z0-9_.-]+)\s+", line)
        if m:
            slug = m.group(1)
            if slug not in {"SLUG"}:
                slugs.append(slug)
    if not slugs:
        slugs = ["default"]
    return sorted(set(slugs))


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))


def task_lines(board: str, task: dict[str, Any]) -> str:
    return f"- [{board}] {task['id']} {task['title']} — {task['status']}"


def latest_summary(board: str, task_id: str) -> str:
    show = run(["hermes", "kanban", "--board", board, "show", task_id])
    lines = show.stdout.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("Latest summary:"):
            value = line.strip().replace("Latest summary:", "", 1).strip()
            if value:
                return value
            if i + 1 < len(lines):
                return lines[i + 1].strip()
    return ""


def board_db_path(board: str) -> Path:
    if board == "default":
        return HERMES_HOME / "kanban.db"
    return HERMES_HOME / "kanban" / "boards" / board / "kanban.db"


def latest_block_reason(board: str, task_id: str) -> str:
    db = board_db_path(board)
    if not db.exists():
        return ""
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(
            """
            SELECT payload
            FROM task_events
            WHERE task_id = ? AND kind = 'blocked'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
    except Exception:
        return ""
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not row:
        return ""
    try:
        payload = json.loads(row[0] or "{}")
    except Exception:
        return ""
    return str(payload.get("reason") or "")


def _normalize_report_text(text: str) -> str:
    """Normalize report text enough to suppress duplicate reason/summary lines."""
    return re.sub(r"\s+", " ", (text or "").strip())


def block_context(block_reason: str, summary: str) -> str:
    parts: list[str] = []
    seen_normalized: set[str] = set()
    for part in (block_reason, summary):
        cleaned = (part or "").strip()
        if not cleaned:
            continue
        normalized = _normalize_report_text(cleaned)
        if normalized in seen_normalized:
            continue
        seen_normalized.add(normalized)
        parts.append(cleaned)
    return "\n".join(parts).strip()


def is_review_required(text: str) -> bool:
    return (text or "").lower().startswith("review-required:")


def active_review_bridge_exists(board: str, task_id: str) -> bool:
    db = board_db_path(board)
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(
            """
            SELECT 1
            FROM tasks
            WHERE created_by = 'review-required-bridge'
              AND status NOT IN ('done', 'archived')
              AND body LIKE ?
            LIMIT 1
            """,
            (f"%{task_id}%",),
        ).fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def latest_review_bridge_info(board: str, task_id: str) -> dict[str, str] | None:
    db = board_db_path(board)
    if not db.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        task_row = conn.execute(
            """
            SELECT id, status, title
            FROM tasks
            WHERE created_by = 'review-required-bridge'
              AND body LIKE ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (f"%{task_id}%",),
        ).fetchone()
        if not task_row:
            return None
        completed_row = conn.execute(
            """
            SELECT payload
            FROM task_events
            WHERE task_id = ? AND kind = 'completed'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (str(task_row["id"]),),
        ).fetchone()
        completed_summary = ""
        if completed_row:
            try:
                payload = json.loads(completed_row[0] or "{}")
            except Exception:
                payload = {}
            completed_summary = str(payload.get("summary") or "")
        return {
            "task_id": str(task_row["id"]),
            "status": str(task_row["status"] or ""),
            "title": str(task_row["title"] or ""),
            "completed_summary": completed_summary,
        }
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def bridge_action_line(board: str, bridge_task_id: str, task_id: str) -> str:
    return f"reviewer bridge {bridge_task_id} for {board}/{task_id}"


def review_bridge_context(bridge_info: dict[str, str] | None) -> str:
    if not bridge_info:
        return ""
    return "\n".join(
        part
        for part in [bridge_info.get("title", ""), bridge_info.get("completed_summary", "")]
        if part
    ).strip()


def review_bridge_outcome_label(bridge_info: dict[str, str] | None) -> str:
    text = review_bridge_context(bridge_info).lower()
    if not text:
        return "review complete"
    if "did not approve" in text or "approved=false" in text or '"approved": false' in text:
        return "changes requested"
    if "approved=true" in text or '"approved": true' in text:
        return "approved"
    if "approve" in text and "did not approve" not in text:
        return "approved"
    return "review complete"


def is_human_required(text: str) -> bool:
    return bool(HUMAN_REQUIRED_RE.search(text or ""))


def is_human_required_review_completion(text: str) -> bool:
    return bool(REVIEW_COMPLETION_HUMAN_REQUIRED_RE.search(text or ""))


def is_recoverable(text: str) -> bool:
    if not text:
        # Empty block reasons are usually protocol or worker failures. Give
        # them one automatic retry before paging the user.
        return True
    return bool(RECOVERABLE_RE.search(text)) and not is_human_required(text)


def profile_exists(name: str) -> bool:
    if not name:
        return False
    if name == "default":
        return True
    return (HERMES_HOME / "profiles" / name).is_dir()


def recovery_assignee(task: dict[str, Any]) -> str:
    assignee = str(task.get("assignee") or "")
    if assignee in {"reviewer", "xhigh"}:
        return "coder"
    if profile_exists(assignee):
        return assignee
    return "coder"


def extract_recovery_origin_task_id(task: dict[str, Any]) -> str:
    title = str(task.get("title") or "")
    body = str(task.get("body") or "")
    for text in (title, body):
        match = re.search(r"\bt_[0-9a-f]{8}\b", text)
        if match:
            return match.group(0)
    return ""


def create_recovery_task(board: str, task: dict[str, Any], summary: str, attempt: int) -> str | None:
    tid = task.get("id", "")
    title = f"RECOVERY: keep {tid} moving"
    body = (
        f"Watchdog recovery for blocked task {tid}: {task.get('title', '')}\n"
        f"Board: {board}\n"
        f"Original blocked summary: {summary}\n\n"
        "Goal: keep the work moving without user involvement if no secret/approval/production-sensitive decision is required.\n"
        "Required steps:\n"
        "1. Inspect `hermes kanban --board <board> show/runs/log <task_id>` and the workspace/artifacts.\n"
        "2. If the original task is effectively complete, reconcile it with `hermes kanban complete`.\n"
        "3. If it is recoverable, unblock/re-dispatch or finish it directly using branch/worktree + PR discipline.\n"
        "4. If it is too large/repeatedly failing, decompose into smaller Kanban tasks and link/sequence them.\n"
        "5. Only block/escalate if a token, credential, explicit approval, destructive action, or genuine legal/security decision is needed.\n"
        "Keep final report under 50 lines."
    )
    proc = run([
        "hermes", "kanban", "--board", board, "create", title,
        "--assignee", recovery_assignee(task),
        "--workspace", "scratch",
        "--idempotency-key", f"watchdog-recovery-{board}-{tid}-{attempt}",
        "--max-runtime", "45m",
        "--created-by", "kanban-watchdog",
        "--body", body,
        "--json",
    ])
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
        return data.get("id") or data.get("task_id")
    except Exception:
        m = re.search(r"t_[0-9a-f]+", proc.stdout)
        return m.group(0) if m else None


def create_decomposition_task(
    board: str,
    recovery_task: dict[str, Any],
    source_task_id: str,
    running_age_minutes: int,
) -> str | None:
    recovery_task_id = str(recovery_task.get("id") or "")
    title = f"DECOMPOSE: recover {source_task_id} after stalled recovery"
    body = (
        f"Watchdog escalation for stalled recovery task {recovery_task_id}.\n"
        f"Board: {board}\n"
        f"Original source task: {source_task_id}\n"
        f"Recovery task age: {running_age_minutes} minutes\n\n"
        "Goal: do the decomposition that the stalled recovery task did not finish, and stop silent long-running loops.\n"
        "Required steps:\n"
        f"1. Inspect both `{source_task_id}` and `{recovery_task_id}` with show/runs/log/context plus any existing worktree diff/artifacts.\n"
        "2. Preserve any in-progress implementation evidence; do NOT restart broad QA from scratch if validated work already exists.\n"
        "3. If the original task can be finished with a narrow handoff, create/route the minimum follow-up (for example review-handoff-only or deploy-only).\n"
        "4. If the scope is still too large, create smaller Kanban tasks with real dependencies instead of another monolithic recovery loop.\n"
        "5. Comment on the original task with the decomposition/recovery plan and unblock or reassign follow-up work as needed.\n"
        "Keep the result concise and execution-oriented."
    )
    proc = run([
        "hermes", "kanban", "--board", board, "create", title,
        "--assignee", "default",
        "--workspace", "scratch",
        "--idempotency-key", f"watchdog-decompose-{board}-{source_task_id}-{recovery_task_id}",
        "--max-runtime", "30m",
        "--created-by", "kanban-watchdog",
        "--body", body,
        "--json",
    ])
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
        return data.get("id") or data.get("task_id")
    except Exception:
        m = re.search(r"t_[0-9a-f]+", proc.stdout)
        return m.group(0) if m else None


def main() -> int:
    now = int(time.time())
    state = load_state()
    seen = state.setdefault("seen", {})
    intervention_reports: list[str] = []
    warning_reports: list[str] = []
    complete_reports: list[str] = []
    actions: list[str] = []
    raw_bridge_actions: list[str] = []
    suppressed_bridge_actions: set[str] = set()

    if REVIEW_BRIDGE_SCRIPT.exists():
        bridge = run([str(REVIEW_BRIDGE_SCRIPT)])
        if bridge.returncode == 0 and bridge.stdout.strip():
            raw_bridge_actions.extend(line for line in bridge.stdout.strip().splitlines() if line.strip())
        elif bridge.returncode != 0:
            warning_reports.append(
                "- review-required bridge 실행 실패: "
                + (bridge.stderr.strip() or bridge.stdout.strip() or f"exit {bridge.returncode}")
            )

    for board in board_slugs():
        dispatch_args = ["hermes", "kanban", "--board", board, "dispatch", "--json"]
        if DISPATCH_MAX is not None:
            dispatch_args.extend(["--max", DISPATCH_MAX])
        dispatch = run(dispatch_args)
        if dispatch.returncode == 0:
            try:
                data = json.loads(dispatch.stdout or "{}")
                spawned = data.get("spawned") or []
                reclaimed = data.get("reclaimed") or 0
                auto_blocked = data.get("auto_blocked") or []
                if spawned:
                    actions.append(f"[{board}] dispatch spawned: " + ", ".join(x.get("task_id", "?") for x in spawned))
                if reclaimed:
                    actions.append(f"[{board}] dispatch reclaimed stale claims: {reclaimed}")
                if auto_blocked:
                    actions.append(f"[{board}] dispatch auto-blocked: {auto_blocked}")
            except Exception:
                actions.append(f"[{board}] dispatch ran but JSON parse failed")
        else:
            warning_reports.append(f"- [{board}] dispatch 실패: {dispatch.stderr.strip() or dispatch.stdout.strip()}")

        listed = run(["hermes", "kanban", "--board", board, "list", "--json"])
        if listed.returncode != 0:
            warning_reports.append(f"- [{board}] list 실패: {listed.stderr.strip() or listed.stdout.strip()}")
            continue
        try:
            tasks = json.loads(listed.stdout or "[]")
        except Exception as exc:
            warning_reports.append(f"- [{board}] list JSON parse 실패: {exc}")
            continue

        active_tasks = [t for t in tasks if t.get("status") not in {"done", "archived"}]
        board_sig = ",".join(sorted(f"{t.get('id')}:{t.get('status')}" for t in active_tasks))
        complete_key = f"{board}:batch_complete_signature"
        if tasks and not active_tasks:
            done_sig = ",".join(sorted(t.get("id", "") for t in tasks if t.get("status") == "done"))
            if seen.get(complete_key) != done_sig:
                complete_reports.append(
                    f"- [{board}] 배치 완료: active/blocked/todo 작업 0개, done {sum(1 for t in tasks if t.get('status') == 'done')}개. 다음 Kanban 배치 지시 가능."
                )
                seen[complete_key] = done_sig
        elif active_tasks:
            if seen.get(complete_key) == board_sig:
                pass
            else:
                seen[complete_key] = "__active__:" + board_sig

        for task in tasks:
            tid = task.get("id")
            status = task.get("status")
            key = f"{board}:{tid}:{status}"
            last = seen.get(key, 0)

            if status not in {"done", "archived"}:
                sub_key = f"{board}:{tid}:subscribed"
                if not seen.get(sub_key):
                    run([
                        "hermes", "kanban", "--board", board, "notify-subscribe", tid,
                        "--platform", "telegram", "--chat-id", CHAT_ID, "--notifier-profile", "default",
                    ])
                    seen[sub_key] = now

            if status == "blocked":
                summary = latest_summary(board, tid)
                block_reason = latest_block_reason(board, tid)
                blocked_text = block_context(block_reason, summary)
                review_required = is_review_required(block_reason) or is_review_required(summary)
                if review_required:
                    bridge_info = latest_review_bridge_info(board, tid)
                    if bridge_info and bridge_info.get("status") == "done":
                        bridge_id = bridge_info.get("task_id", "")
                        if bridge_id:
                            suppressed_bridge_actions.add(bridge_action_line(board, bridge_id, tid))
                        review_text = review_bridge_context(bridge_info)
                        if is_human_required_review_completion(review_text):
                            if now - int(last or 0) >= REMINDER_SECONDS:
                                intervention_reports.append(
                                    task_lines(board, task)
                                    + "\n  reviewer bridge 완료 후에도 human-gated 상태로 판단되어 자동 재개하지 않음: "
                                    + (review_text or bridge_id or "review outcome requires human intervention")
                                )
                                seen[key] = now
                            continue

                        resume_key = f"{board}:{tid}:bridge-resume:{bridge_id}"
                        if not seen.get(resume_key):
                            run([
                                "hermes", "kanban", "--board", board, "comment", "--author", "kanban-watchdog", tid,
                                (
                                    f"Auto-recovery: reviewer bridge {bridge_id} completed; "
                                    "unblocking and redispatching the original task so the assignee can continue from the review outcome."
                                ),
                            ])
                            unblocked = run(["hermes", "kanban", "--board", board, "unblock", tid])
                            seen[resume_key] = now
                            seen[key] = now
                            if unblocked.returncode == 0:
                                seen[f"{board}:{tid}:running"] = now
                                actions.append(
                                    f"[{board}] resumed {tid} after completed reviewer bridge {bridge_id} ({review_bridge_outcome_label(bridge_info)})"
                                )
                                redispatch = run(["hermes", "kanban", "--board", board, "dispatch", "--max", "1", "--json"])
                                if redispatch.returncode == 0:
                                    try:
                                        rd = json.loads(redispatch.stdout or "{}")
                                        spawned = rd.get("spawned") or []
                                        if spawned:
                                            actions.append(
                                                f"[{board}] review follow-up dispatch spawned: "
                                                + ", ".join(x.get("task_id", "?") for x in spawned)
                                            )
                                    except Exception:
                                        pass
                            else:
                                warning_reports.append(
                                    task_lines(board, task)
                                    + f"\n  reviewer bridge 완료 후 자동 unblock 실패: {unblocked.stderr.strip() or unblocked.stdout.strip()}"
                                )
                        else:
                            seen[key] = now
                        continue

                    if active_review_bridge_exists(board, tid):
                        seen[key] = now
                        continue
                    if now - int(last or 0) >= REMINDER_SECONDS:
                        warning_reports.append(
                            task_lines(board, task)
                            + "\n  사유: "
                            + (block_reason or summary or "review-required")
                            + "\n  reviewer bridge task가 아직 감지되지 않음 — bridge/dispatcher 확인 필요."
                        )
                        seen[key] = now
                    continue

                retry_key = f"{board}:{tid}:auto_unblock_count"
                recovery_key = f"{board}:{tid}:recovery_task"
                retry_count = int(seen.get(retry_key, 0) or 0)

                if is_recoverable(blocked_text) and retry_count < AUTO_UNBLOCK_LIMIT:
                    run([
                        "hermes", "kanban", "--board", board, "comment", "--author", "kanban-watchdog", tid,
                        "Auto-recovery: block appears recoverable and not human-gated; unblocking and dispatching once instead of waiting for user intervention.",
                    ])
                    unblocked = run(["hermes", "kanban", "--board", board, "unblock", tid])
                    seen[retry_key] = retry_count + 1
                    seen[key] = now
                    if unblocked.returncode == 0:
                        actions.append(f"[{board}] auto-unblocked {tid} ({task.get('title')})")
                        redispatch = run(["hermes", "kanban", "--board", board, "dispatch", "--max", "1", "--json"])
                        if redispatch.returncode == 0:
                            try:
                                rd = json.loads(redispatch.stdout or "{}")
                                spawned = rd.get("spawned") or []
                                if spawned:
                                    actions.append(f"[{board}] recovery dispatch spawned: " + ", ".join(x.get("task_id", "?") for x in spawned))
                            except Exception:
                                pass
                    else:
                        warning_reports.append(task_lines(board, task) + f"\n  자동 unblock 실패: {unblocked.stderr.strip() or unblocked.stdout.strip()}")

                elif is_recoverable(blocked_text) and not seen.get(recovery_key):
                    # Only create recovery tasks for non-RECOVERY original tasks.
                    # RECOVERY tasks that re-block are runaway chains — log as warning, do not recurse.
                    is_recovery_task = task.get("title", "").startswith("RECOVERY:")
                    if is_recovery_task:
                        # RECOVERY task re-blocked: this needs human attention, not just a warning.
                        reminder_key = f"{board}:{tid}:reminder_count"
                        reminder_count = int(seen.get(reminder_key, 0) or 0) + 1
                        seen[reminder_key] = reminder_count
                        if reminder_count <= MAX_REMINDERS:
                            intervention_reports.append(
                                task_lines(board, task) +
                                f"\n  사유: {blocked_text}" +
                                f"\n  RECOVERY 태스크가 다시 block됨 ({reminder_count}/{MAX_REMINDERS}) — 수동 확인 필요."
                            )
                            seen[key] = now
                        # After MAX_REMINDERS: suppress (silently counted but not reported)
                    else:
                        recovery_id = create_recovery_task(board, task, blocked_text, retry_count + 1)
                        seen[recovery_key] = recovery_id or "failed"
                        if recovery_id:
                            actions.append(f"[{board}] created recovery/decomposition task {recovery_id} for blocked {tid}")
                            run([
                                "hermes", "kanban", "--board", board, "notify-subscribe", recovery_id,
                                "--platform", "telegram", "--chat-id", CHAT_ID, "--notifier-profile", "default",
                            ])
                            run(["hermes", "kanban", "--board", board, "dispatch", "--max", "1", "--json"])
                        else:
                            warning_reports.append(task_lines(board, task) + "\n  자동 recovery task 생성 실패")

                elif now - int(last or 0) >= REMINDER_SECONDS:
                    # Rate-limit repeated reports for the same blocked task.
                    reminder_key = f"{board}:{tid}:reminder_count"
                    reminder_count = int(seen.get(reminder_key, 0) or 0) + 1
                    seen[reminder_key] = reminder_count
                    if reminder_count > MAX_REMINDERS:
                        # Suppress after MAX_REMINDERS reports for this task.
                        pass
                    else:
                        line = task_lines(board, task) + (f"\n  사유: {blocked_text}" if blocked_text else "")
                        line += f" ({reminder_count}/{MAX_REMINDERS})"
                        # Truly human-gated blocks (approval/credential/permission) are intervention-needed.
                        # Recoverable blocks that already exhausted retries/recovery are warnings, not 🚨.
                        if is_recoverable(blocked_text):
                            warning_reports.append(line)
                        else:
                            intervention_reports.append(line)
                    seen[key] = now

            elif status == "running":
                started = int(task.get("started_at") or now)
                age = now - started
                is_recovery_task = str(task.get("title") or "").startswith("RECOVERY:")
                if is_recovery_task and age >= RECOVERY_STALE_RUNNING_SECONDS:
                    source_task_id = extract_recovery_origin_task_id(task)
                    decompose_key = f"{board}:{tid}:decomposition_task"
                    decompose_id = str(seen.get(decompose_key) or "")
                    age_minutes = age // 60
                    if source_task_id and not decompose_id:
                        created = create_decomposition_task(board, task, source_task_id, age_minutes)
                        seen[decompose_key] = created or "failed"
                        if created:
                            actions.append(
                                f"[{board}] created decomposition task {created} after stalled recovery {tid} for {source_task_id}"
                            )
                            run([
                                "hermes", "kanban", "--board", board, "notify-subscribe", created,
                                "--platform", "telegram", "--chat-id", CHAT_ID, "--notifier-profile", "default",
                            ])
                            run(["hermes", "kanban", "--board", board, "dispatch", "--max", "1", "--json"])
                            decompose_id = created
                        else:
                            intervention_reports.append(
                                task_lines(board, task)
                                + f"\n  경과: {age_minutes}분 running — 자동 decomposition task 생성 실패, 수동 확인 필요"
                            )
                            seen[key] = now
                    if now - int(last or 0) >= RECOVERY_REMINDER_SECONDS:
                        suffix = f"\n  자동 decomposition task: {decompose_id}" if decompose_id and decompose_id != "failed" else ""
                        warning_reports.append(
                            task_lines(board, task)
                            + f"\n  경과: {age_minutes}분 running — RECOVERY 태스크가 오래 지속되어 자동 decomposition을 트리거함."
                            + suffix
                        )
                        seen[key] = now
                    continue
                if age >= STALE_RUNNING_SECONDS and now - int(last or 0) >= REMINDER_SECONDS:
                    warning_reports.append(task_lines(board, task) + f"\n  경과: {age//60}분 running — heartbeat/log 확인 필요")
                    seen[key] = now

    for action in raw_bridge_actions:
        if action in suppressed_bridge_actions:
            continue
        actions.append(action)

    state["seen"] = seen
    state["last_run_at"] = now
    save_state(state)

    if not intervention_reports and not warning_reports and not complete_reports and not actions:
        return 0

    if intervention_reports:
        title = "🚨 Kanban Watchdog: Intervention Needed"
    elif warning_reports:
        title = "⚠️ Kanban Watchdog: Warning"
    elif actions:
        title = "🤖 Kanban Watchdog: Automated Action"
    else:
        title = "✅ Kanban Watchdog: Batch Complete"

    body_lines: list[str] = []
    if intervention_reports:
        body_lines.append("사용자 개입 필요:")
        body_lines.extend(intervention_reports)
    if warning_reports:
        if body_lines:
            body_lines.append("")
        body_lines.append("주의 필요:")
        body_lines.extend(warning_reports)
    if complete_reports:
        if body_lines:
            body_lines.append("")
        body_lines.append("배치 완료:")
        body_lines.extend(complete_reports)
    if actions:
        if body_lines:
            body_lines.append("")
        body_lines.append("자동 조치:")
        body_lines.extend(f"- {action}" for action in actions)

    body = "\n".join(body_lines).strip().replace("```", "''' ")
    print(f"{title}\n\n```text\n{body}\n```")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
