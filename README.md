# hermes-reviewer-readonly-plugin

Hermes Agent user plugin that keeps the dedicated `reviewer` profile read-only while still allowing local evidence inspection for review-gated Kanban workflows.

## What it does

When the active Hermes profile is named `reviewer`, the plugin blocks mutation-capable tool calls before dispatch:

- direct write tools such as `write_file`, `patch`, `execute_code`, `skill_manage`, memory writes, and fact-store writes
- mutating `process` actions (`kill`, `write`, `submit`, `close`)
- terminal commands outside a small read-only allowlist
- shell chaining, redirection, command substitution, process substitution, line breaks, background operators, and environment assignment
- mutating browser actions and browser escape hatches including `browser_click`, `browser_type`, `browser_console`, `browser_cdp`, and `browser_dialog`
- mutating or helper-spawning git forms such as `git add`, `git checkout`, `git diff --output`, `--ext-diff`, `--textconv`, and `git grep -O`

It allows normal review reads through `read_file`, `search_files`, `kanban_*` review operations, status/log process reads, passive browser inspection (`browser_navigate`, `browser_snapshot`, `browser_get_images`, `browser_vision`), and narrowly allowlisted shell diagnostics like `pwd`, `ls`, `cat`, `grep`, and read-only `git status/diff/show/log/rev-parse/ls-files/grep/blame/describe`.

## Install

Clone this repository and link it into the active Hermes home as a user plugin:

```bash
git clone https://github.com/2seok2/hermes-reviewer-readonly-plugin.git /Users/yeonseoklee/.hermes/plugins-src/reviewer-readonly
ln -sfn /Users/yeonseoklee/.hermes/plugins-src/reviewer-readonly /Users/yeonseoklee/.hermes/plugins/reviewer-readonly
```

Enable it in the reviewer profile config:

```yaml
plugins:
  enabled:
    - reviewer-readonly
```

Then start a fresh reviewer session or restart the relevant gateway/worker process.

## Notes

This is a guardrail for trusted review workflows, not a hostile-code sandbox. Keep reviewer prompts read-only and use it alongside review-gated Kanban tasks that provide local `review_artifacts/<task_id>/review_packet.md` and `review.diff` evidence.

## Optional Kanban Review Bridge Backup

This repository also keeps backup copies of the local Kanban reviewer ops scripts:

```text
ops/review_required_bridge.py
ops/kanban_watchdog.py
```

They are not part of the plugin runtime. They are ops companion scripts for installations that want the official Hermes Kanban `review-required:` blocked-task convention to route into a dedicated `reviewer` profile while keeping recoverable blocked work moving without unnecessary human intervention. In particular, the watchdog can reconcile a completed reviewer bridge back into the original source task by auto-unblocking and redispatching it when the review outcome is a normal approve/request-changes result rather than a human-gated approval/credential decision. It also escalates silent `RECOVERY:` loops more aggressively: if a recovery task itself runs stale, the watchdog now creates a separate `DECOMPOSE:` orchestration task and emits a user-visible warning instead of waiting quietly for another long timeout cycle.

Restore them after a fresh clone with:

```bash
mkdir -p /Users/yeonseoklee/.hermes/scripts
cp ops/review_required_bridge.py /Users/yeonseoklee/.hermes/scripts/review_required_bridge.py
cp ops/kanban_watchdog.py /Users/yeonseoklee/.hermes/scripts/kanban_watchdog.py
chmod +x /Users/yeonseoklee/.hermes/scripts/review_required_bridge.py /Users/yeonseoklee/.hermes/scripts/kanban_watchdog.py
hermes cron create "every 1m" --name "Kanban review-required bridge" --deliver local --script review_required_bridge.py --no-agent
hermes cron create "every 5m" --name "Kanban watchdog — no silent blocked work" --deliver telegram:-5133663775 --script kanban_watchdog.py --no-agent
```
