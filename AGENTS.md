# Multi-Agent Coordination

These instructions apply to every agent working anywhere in this repository.

## Shared progress log

The canonical coordination log is the repository-root file 全局记录.txt. It is an append-only event log. Do not edit, truncate, rewrite, or delete it directly.

Use the repository helper for every progress event:

    pwsh -NoProfile -Command ".\.venv\Scripts\python.exe scripts/progress_log.py --agent <unique-agent-id> --status START --task <task-id> --file <repo-relative-file> --message '<short message>'"

Use python instead of the virtual-environment path when that is the available interpreter. To inspect current ownership before starting or when a claim is rejected:

    pwsh -NoProfile -Command ".\.venv\Scripts\python.exe scripts/progress_log.py --show-active"

Required event rules:

- Choose an agent id that is unique for this session, for example api-worker-01 or ui-orders-fix-02. Use one task id per coherent task.
- Emit START before editing any file and claim every file you will immediately modify.
- Emit UPDATE at meaningful milestones, when adding a new file claim, and at least every 10 minutes during long work.
- Emit BLOCKED with the concrete blocker as soon as work cannot proceed. A blocked task retains its claims until handoff or completion.
- Emit HANDOFF when another agent must continue, including the exact remaining work in the message.
- Emit DONE after verification, including the relevant test or validation result. DONE releases all claims for that task.
- Keep messages short and single-line. Never put secrets, tokens, or large command output in the log.

## File ownership

The helper serializes writers with a cross-process lock and rejects overlapping live claims. If a claim is rejected, do not edit the disputed file. Read 全局记录.txt, narrow the scope, or hand off after coordinating with the owner. A claim is a coordination signal, not permission to overwrite another agent's changes.

Before editing a file that was already modified in the working tree, inspect the diff and preserve changes you did not make. Never reset, checkout, or otherwise discard another agent's work.

The progress log is for coordination, not source-of-truth project state. Keep implementation decisions and durable project documentation in their appropriate source files.
