---
name: usage-aware-workflow
description: Check Codex App Server limits before and during long implementation, delegation, build, or review tasks.
---

# Usage-aware workflow

Use the `codex_usage_guard` MCP server for advisory usage checks. Map checkpoints to the exact `purpose` values: `task_start` when a task begins; `before_delegate` immediately before any delegation (including Luna); `before_build` before a large build/test; `before_external_cli` before an external CLI; `checkpoint` at phase transitions or after substantial work; and `manual` for an explicit user request. During a long phase, call `evaluate_usage_guard(purpose="checkpoint")` at least every 10 minutes. Call `get_usage_status` when exact windows, reset times, credits, or activity summaries are needed.

Treat the returned decision as policy: `proceed` (>40%), `watch` (>25-40%), `checkpoint` (>10-25%), and `critical` (<=10% or an explicit reached state). `unknown` is fail-closed when no fresh valid window or cache exists. Do not infer quota from account activity, missing credits, or a missing secondary window.

For `watch`, do not start new parallel delegation or a large expensive phase; finish only the current atomic operation, then checkpoint. For `checkpoint` or `critical`, do not start new expensive delegation/build work; save a concise state summary and ask the user whether to continue. The tool cannot cancel the currently running Codex request and hooks are advisory context only. Never read or request API keys, ChatGPT tokens, or config secrets.

The plugin polls the official Codex App Server (`account/rateLimits/read`) and merges `account/rateLimits/updated` notifications. `account/usage/read` is an optional activity summary and never changes the quota decision. Keep the product name and version visible: Codex Usage Guard v0.1.0.
