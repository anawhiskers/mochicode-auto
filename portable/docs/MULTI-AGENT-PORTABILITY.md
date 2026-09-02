# Multi-agent portability

This release has one model-neutral workflow core, first-class adapters for Codex, Claude, Kimi, and Z.ai, plus a generic adapter for any coding agent that documents a Markdown instruction file. The core delegates by responsibility, not provider branding: authority parent, bounded worker, optional reviewer panel, and deterministic controller.

## Use on this PC

Run a read-only audit first:

```powershell
pwsh -NoProfile -File .\agent-sync.ps1 -Agent codex
pwsh -NoProfile -File .\agent-sync.ps1 -Agent claude
```

The audit returns only public model-choice fields and executable/version availability. It never reads credentials, environments, browser state, or provider session data.

To merge the managed workflow block into an existing instruction file, first review the audit, then use a backup-backed explicit write:

```powershell
pwsh -NoProfile -File .\agent-sync.ps1 -Agent claude -Apply -Confirm
```

For Kimi or Z.ai, first install the client and consult its current instruction-file documentation. Then provide the exact project instruction file:

```powershell
pwsh -NoProfile -File .\agent-sync.ps1 -Agent kimi -Target <project-root>/AGENTS.md -Apply -Confirm
```

For any other coding agent, use the generic adapter with the exact Markdown instruction file documented by that client:

```powershell
pwsh -NoProfile -File .\agent-sync.ps1 -Agent generic -Target <path-to-agent-instructions.md>
pwsh -NoProfile -File .\agent-sync.ps1 -Agent generic -Target <path-to-agent-instructions.md> -Apply -Confirm
```

The generic adapter refuses non-Markdown targets, creates a timestamped backup, preserves existing guidance outside its managed markers, and does not invent provider model IDs. If the target agent cannot prove a child model or does not support child routing, the workflow remains direct with the parent.

## Future model releases

The workflow block is model-neutral, so a new provider model does not require rewriting every agent file. Use a release audit to discover the current catalog and selected settings. A release audit writes no configuration. Change a selected model only after a representative canary verifies task success, required evidence, total turns, latency, and cost or subscription usage. This keeps a new release from silently degrading a working workflow.

Codex can provide a local catalog through `codex debug models`. Claude, Kimi, and Z.ai use only their installed executable/version until their documented catalog API is available. Authentication is never copied between providers.
