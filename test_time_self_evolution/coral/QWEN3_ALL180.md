# Qwen3-Coder-Plus × CORAL all-180 run

The frozen production run is controlled by:

```bash
scripts/run_qwen3_coral_all.sh install
scripts/run_qwen3_coral_all.sh start
scripts/run_qwen3_coral_all.sh status
```

Run ID: `qwen3-coder-plus-coral-all180-a2-24g-20260806`.

The launcher loads the trusted repository `.env`, starts a detached process
group, and writes its controller log and progress under
`eval/coral_batches/<run-id>/`. Per-case CORAL artifacts remain under
`eval/coral/<run-id>/`.

The controller freezes an alphabetically sorted manifest of exactly 180 valid
tasks and a source/config fingerprint. Completed cases are never repeated.
An interrupted case resumes only when its native CORAL checkpoint passes an
integrity check; otherwise its partial directory is archived under the batch
run before a fresh attempt.

OpenCode uses an XDG cache/data/config/state root scoped to this batch run,
never the user's global mutable provider cache. The controller makes one tiny
live provider request before the first CORAL agent starts. Agent crashes with
no new finalized attempt use 5/10/20-second backoff and stop after three
restarts instead of looping forever.

Before that live request, the run-local provider cache is seeded with links to
the exact dependencies pinned by `tools/opencode/package-lock.json`. This
avoids Bun leaving a partial first-install cache while keeping every run's
mutable OpenCode state isolated.

Each agent worktree also resolves `frontier/qwen3-coder-plus` with
`opencode models frontier` immediately before every agent spawn. Custom
provider availability is workspace-scoped in OpenCode, so the run-level
prewarm alone does not initialize a new worktree.

Agent worktrees link `.opencode/node_modules` to the repository-local runtime,
including the CLI-matched `@opencode-ai/plugin@1.18.5`. This prevents a formal
agent request from racing OpenCode's first workspace plugin installation.

The OpenCode child environment explicitly sets `PWD` to the agent worktree.
Changing only `Popen(cwd=...)` is insufficient because OpenCode 1.18.5/Bun
uses the inherited `PWD` when selecting the session project and configuration.
CORAL's session extractor also accepts OpenCode's `sessionID` event field so
eval-driven restarts and interrupted cases resume the native session.

The custom provider sends `Connection: close`. The configured endpoint works
with the same OpenCode request bodies and streaming tool calls over fresh
HTTP/1.1 connections, but returned intermittent server errors when Bun reused
the direct streaming connection.

Candidate solvers run with Bubblewrap, one CPU, and a 24 GiB inherited
`RLIMIT_AS`. Admission requires at least 48 GiB `MemAvailable`; new cases also
stop safely below 32 GiB available memory or 50 GiB free disk. This host has no
usable aggregate cgroup/Docker guard, so 24 GiB is a per-process address-space
limit, not a whole-process-tree memory limit.
