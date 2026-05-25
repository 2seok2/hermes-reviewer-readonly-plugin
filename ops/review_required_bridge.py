#!/usr/bin/env python3
"""Bridge Hermes `review-required:` blocked tasks into reviewer work.

This is a local overlay, not a patch to Hermes core. It preserves the
default Kanban convention where implementation tasks block with
`review-required: ...`, then creates a separate reviewer task so the
`reviewer` profile can inspect and unblock/comment on the original task.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path


HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()


@dataclass
class BlockedReviewTask:
    board: str
    task_id: str
    title: str
    assignee: str
    body: str
    reason: str
    block_event_id: int


def board_dbs() -> list[tuple[str, Path]]:
    dbs: list[tuple[str, Path]] = []
    default_db = HERMES_HOME / "kanban.db"
    if default_db.exists():
        dbs.append(("default", default_db))
    boards_dir = HERMES_HOME / "kanban" / "boards"
    if boards_dir.exists():
        for db in sorted(boards_dir.glob("*/kanban.db")):
            dbs.append((db.parent.name, db))
    return dbs


def latest_block_reason(conn: sqlite3.Connection, task_id: str) -> tuple[int, str] | None:
    row = conn.execute(
        """
        SELECT id, payload
        FROM task_events
        WHERE task_id = ? AND kind = 'blocked'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if not row:
        return None
    event_id = int(row[0])
    try:
        payload = json.loads(row[1] or "{}")
    except json.JSONDecodeError:
        payload = {}
    reason = str(payload.get("reason") or "")
    return event_id, reason


def find_blocked_review_tasks(board: str, db: Path) -> list[BlockedReviewTask]:
    out: list[BlockedReviewTask] = []
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, title, assignee, body
            FROM tasks
            WHERE status = 'blocked'
            ORDER BY priority DESC, created_at ASC
            """
        ).fetchall()
        for row in rows:
            latest = latest_block_reason(conn, str(row["id"]))
            if not latest:
                continue
            event_id, reason = latest
            if reason.lower().startswith("review-required:"):
                task_id = str(row["id"])
                if active_bridge_exists(conn, task_id):
                    continue
                out.append(
                    BlockedReviewTask(
                        board=board,
                        task_id=task_id,
                        title=str(row["title"] or ""),
                        assignee=str(row["assignee"] or ""),
                        body=str(row["body"] or ""),
                        reason=reason,
                        block_event_id=event_id,
                    )
                )
    finally:
        conn.close()
    return out


def active_bridge_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    pattern = f"%{task_id}%"
    row = conn.execute(
        """
        SELECT 1
        FROM tasks
        WHERE created_by = 'review-required-bridge'
          AND status NOT IN ('done', 'archived')
          AND body LIKE ?
        LIMIT 1
        """,
        (pattern,),
    ).fetchone()
    return bool(row)


def is_batch_candidate(task: BlockedReviewTask) -> bool:
    text = "\n".join([task.reason, task.title, task.body]).lower()
    markers = [
        "review_policy=batch",
        "review_policy: batch",
        '"review_policy": "batch"',
        '"review_policy":"batch"',
        "review policy: batch",
        "review-policy: batch",
    ]
    return any(marker in text for marker in markers)


def review_body(task: BlockedReviewTask) -> str:
    return f"""# Review-required bridge task

You are assigned as `reviewer`.

The original implementation task is blocked with `review-required:` using the default Hermes Kanban convention.

Original board: `{task.board}`
Original task: `{task.task_id}`
Original assignee: `{task.assignee}`
Original title: {task.title}
Block reason: {task.reason}

## Review protocol

- Do not edit files, apply patches, run implementation commands, commit, push, merge, or change configuration.
- Inspect the original task with `kanban_show(task_id="{task.task_id}")`.
- Read its comments, result metadata, changed files, validation evidence, and any review artifact paths.
- Use local read-only inspection only if available and safe.
- This review task intentionally has no parent link. The original task is blocked, not done; using it as a parent would deadlock.

## If approved

1. Add a comment to `{task.task_id}` with approval, evidence reviewed, and residual risks.
2. Unblock `{task.task_id}` so the original worker can resume and complete or merge according to its task body.
3. Complete this review task with metadata:

```json
{{"approved": true, "reviewed_task": "{task.task_id}", "bridge": "review-required"}}
```

## If changes or evidence are needed

1. Add a comment to `{task.task_id}` with concrete findings or missing evidence.
2. Unblock `{task.task_id}` so the original assignee can address the findings in the default Kanban flow.
3. Complete this review task with metadata:

```json
{{"approved": false, "reviewed_task": "{task.task_id}", "bridge": "review-required", "blocking_findings": []}}
```

Create separate coder/xhigh tasks only if the original task cannot reasonably continue or repeated reviewed failures justify escalation.
"""


def batch_review_body(tasks: list[BlockedReviewTask]) -> str:
    lines = "\n".join(
        f"- `{task.task_id}`: {task.title} (assignee: `{task.assignee}`, reason: {task.reason})"
        for task in tasks
    )
    task_ids = ", ".join(f'"{task.task_id}"' for task in tasks)
    return f"""# Batch review-required bridge task

You are assigned as `reviewer`.

These original implementation tasks are blocked with `review-required:` using the default Hermes Kanban convention.

Original board: `{tasks[0].board}`
Original blocked tasks:

{lines}

## Review protocol

- Do not edit files, apply patches, run implementation commands, commit, push, merge, or change configuration.
- Inspect each original task with `kanban_show`.
- Read comments, result metadata, changed files, validation evidence, and any review artifact paths.
- Fully review risky or unusual items.
- Sample repetitive low-risk items only when the pattern is consistent and evidence is strong.
- Check cross-task consistency across naming, registration, docs, tests, schema/tool exposure, permissions, and release readiness.
- This batch review task intentionally has no parent links. The original tasks are blocked, not done; using them as parents would deadlock.

## For each original task

1. Add an approval comment or concrete findings/missing evidence.
2. Unblock the original task so its assignee can complete or address findings.

Then complete this batch review task with metadata:

```json
{{"approved": true, "review_type": "batch-review-required-bridge", "reviewed_tasks": [{task_ids}], "blocking_findings": []}}
```

Use `approved=false` if any source task has blocking findings.
"""


def create_review_task(task: BlockedReviewTask, *, dry_run: bool) -> str:
    key = f"review-required-bridge:{task.board}:{task.task_id}:{task.block_event_id}"
    title = f"Review blocked task {task.task_id}: {task.title[:60]}"
    if dry_run:
        return f"would create reviewer bridge for {task.board}/{task.task_id}"
    cmd = [
        "hermes",
        "kanban",
        "--board",
        task.board,
        "create",
        title,
        "--assignee",
        "reviewer",
        "--created-by",
        "review-required-bridge",
        "--idempotency-key",
        key,
        "--skill",
        "kanban-review-gated-orchestration",
        "--body",
        review_body(task),
        "--json",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        return f"error creating reviewer bridge for {task.board}/{task.task_id}: {proc.stderr.strip() or proc.stdout.strip()}"
    try:
        data = json.loads(proc.stdout or "{}")
        created = data.get("task_id") or data.get("id") or proc.stdout.strip()
    except json.JSONDecodeError:
        created = proc.stdout.strip()
    return f"reviewer bridge {created} for {task.board}/{task.task_id}"


def create_batch_review_task(tasks: list[BlockedReviewTask], *, dry_run: bool) -> str:
    event_ids = ",".join(str(task.block_event_id) for task in tasks)
    digest = hashlib.sha256(event_ids.encode("utf-8")).hexdigest()[:16]
    key = f"review-required-bridge-batch:{tasks[0].board}:{digest}"
    title = f"Batch review {len(tasks)} blocked review-required tasks"
    task_labels = ", ".join(task.task_id for task in tasks)
    if dry_run:
        return f"would create batch reviewer bridge for {tasks[0].board}/{task_labels}"
    cmd = [
        "hermes",
        "kanban",
        "--board",
        tasks[0].board,
        "create",
        title,
        "--assignee",
        "reviewer",
        "--created-by",
        "review-required-bridge",
        "--idempotency-key",
        key,
        "--skill",
        "kanban-review-gated-orchestration",
        "--body",
        batch_review_body(tasks),
        "--json",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        return f"error creating batch reviewer bridge for {tasks[0].board}/{task_labels}: {proc.stderr.strip() or proc.stdout.strip()}"
    try:
        data = json.loads(proc.stdout or "{}")
        created = data.get("task_id") or data.get("id") or proc.stdout.strip()
    except json.JSONDecodeError:
        created = proc.stdout.strip()
    return f"batch reviewer bridge {created} for {tasks[0].board}/{task_labels}"


def chunks(items: list[BlockedReviewTask], size: int) -> list[list[BlockedReviewTask]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=25)
    args = parser.parse_args()

    messages: list[str] = []
    for board, db in board_dbs():
        tasks = find_blocked_review_tasks(board, db)
        batch_candidates = [task for task in tasks if is_batch_candidate(task)]
        single_tasks = [task for task in tasks if not is_batch_candidate(task)]
        for batch in chunks(batch_candidates, max(args.batch_size, 2)):
            if len(batch) == 1:
                single_tasks.extend(batch)
            else:
                messages.append(create_batch_review_task(batch, dry_run=args.dry_run))
        for task in single_tasks:
            messages.append(create_review_task(task, dry_run=args.dry_run))

    if messages:
        print("\n".join(messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
