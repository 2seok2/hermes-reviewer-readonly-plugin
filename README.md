# hermes-reviewer-readonly-plugin

Hermes Agent user plugin that keeps the dedicated `reviewer` profile read-only while still allowing local evidence inspection for review-gated Kanban workflows.

## What it does

When the active Hermes profile is named `reviewer`, the plugin blocks mutation-capable tool calls before dispatch:

- direct write tools such as `write_file`, `patch`, `execute_code`, `skill_manage`, memory writes, and fact-store writes
- mutating `process` actions (`kill`, `write`, `submit`, `close`)
- terminal commands outside a small read-only allowlist
- shell chaining, redirection, command substitution, process substitution, line breaks, background operators, and environment assignment
- mutating or helper-spawning git forms such as `git add`, `git checkout`, `git diff --output`, `--ext-diff`, `--textconv`, and `git grep -O`

It allows normal review reads through `read_file`, `search_files`, `kanban_*` review operations, status/log process reads, and narrowly allowlisted shell diagnostics like `pwd`, `ls`, `cat`, `grep`, and read-only `git status/diff/show/log/rev-parse/ls-files/grep/blame/describe`.

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
