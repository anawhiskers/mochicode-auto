---
name: repository-workflow-upgrader
description: Audit and upgrade an existing repository's instruction chain, native agent configuration, and skills while preserving local knowledge, concurrent work, and host safety.
---

# Repository Workflow Upgrader

Use this skill when a repository already has `AGENTS.md`, `AGENTS.override.md`, fallback instruction
files, custom agents, a project configuration, or workflow skills that must be kept and reconciled.
The upgrade is a reversible repository migration, not a clean-install replacement.

## Scope and route

1. Work from the repository root and identify the complete instruction chain before editing.
2. Read-only inventory may stay direct. Every repository-workflow migration that writes files uses
   the deterministic controller. Terra defines the migration contract and protected paths first.
   The controller then assigns each declared write set to a bounded Luna worktree and serializes
   integration. Native delegation does not perform migration writes.
3. Keep MochiCode Auto as the only top-level workflow. This skill is a repository-migration skill,
   not a second orchestration loop.

## Required migration procedure

1. Record the repository root, current branch or revision, status, instruction files, configuration
   files, custom agent files, skills, and the commands used to discover them.
2. Before changing any file, make a timestamped local backup of every candidate target. Do not
   overwrite a concurrent edit. If a target changes after the inventory, stop and re-read it.
3. Parse project configuration with the available TOML parser. Identify duplicate tables, unsupported
   keys, conflicting values, stale machine-specific paths, and settings that would widen host access.
4. Preserve repository-specific commands, architecture, invariants, deployment rules, ownership
   boundaries, and recurring review feedback. Do not replace local instructions with generic prose.
5. Keep the repository root instruction file concise. Move repeatable migration steps into skills and
   keep nested instructions limited to facts that are true for that directory.
6. Reconcile agent roles against `config/role-dispositions.json` when this plugin is the source of the
   upgrade. Consolidate duplicate planning, contract, review, and implementation roles into the four
   core agents. Retain an optional native specialist only when its domain or coordination boundary is
   materially distinct, and document its fallback.
7. Preserve the packet schema. Each packet must retain its identity, goal, wave, priority, vertical
   slice, dependencies, acceptance criteria, and verification hints. Contracts must retain their
   execution mode, verification class, executable argument arrays, protected patterns, allowed paths,
   and evidence requirements.
8. Enforce one writer per file or shared state. Keep orchestration depth at one, primary Sol parent
   to child. Children never spawn descendants. Treat eight as a host ceiling, start with two, and
   cap normal live waves at three until larger waves earn benchmark promotion.
9. Validate the final instruction discovery, parse every changed TOML and JSON file, run the focused
   repository checks, and record exact commands, exit codes, and material output.
10. Write a migration report named `CODEX-WORKFLOW-MIGRATION.md` unless the repository already has a
    different established report location. Include before and after inventories, role mappings,
    preserved rules, changed paths, validation evidence, risks, and rollback steps.

## Core role contract

- Sol High is the default migration parent and owns repository architecture, instruction hierarchy, role consolidation, migration decisions, implementation when tightly coupled, integration, verification, and final judgment. Use Max only for consequential whole-repository migrations.
- Luna Medium may implement a complete localized migration packet with hard checks. Luna Max is escalation after failed acceptance.
- Terra is optional only inside an explicitly selected experimental controller run.
- The deterministic controller owns persistence, queues, retries, budgets, process identity, evidence,
  and integration only when the user explicitly selects the unpromoted controller experiment.

## Safety boundaries

- Preserve the host's existing approval and sandbox policy. Do not add repository settings that grant
  broad access, suppress review, change the host context limit, or enable recursive agent blocks.
- Never copy tokens, credentials, cookies, private endpoints, local runtime paths, trust records, or
  machine-specific plugin state into the repository.
- Do not delete redundant roles during migration. Preserve them in the timestamped backup or a clearly
  documented archive, and make the active mapping explicit.
- Do not commit during the upgrade unless the user explicitly requested a commit as part of the task.
- If a backup cannot be made, a concurrent edit cannot be reconciled, or a validation command would
  change protected state, stop and report the exact blocker.

## Report shape

Return a compact report with:

```text
STATUS: complete | blocked | refused
FACTS: repository identity, instruction chain, and role inventory
CHANGES: exact paths and preserved local rules
TESTS: commands, exit codes, and salient output
RISKS: unresolved conflicts, assumptions, and rollback location
NEXT: one concrete follow-up action
```
