# Manager Mode

Use Manager Mode for projects where keeping the whole goal moving is harder than implementing any one part. It separates direction from execution and uses deterministic phase state so a long run cannot silently spend its budget polishing one corner.

## Activation

Activate only for an explicit Manager Mode implementation request: the user uses `[MOCHICODE_MANAGER]` to request execution, asks to implement in Manager Mode, or explicitly requests separate manager and implementer execution. A generic implementation request, quoted marker, or research, review, or planning mention does not activate it. Automatic classification stays shadow-only.

Classify automatically only when all of these are observable, but keep the result shadow-only until benchmark promotion:

- the request authorizes implementation;
- the goal has 3 to 6 acceptance-distinct phases and a wave-one runnable vertical slice;
- the expected change touches at least 6 production files across at least 2 repository components;
- every phase has a declared write set and repository-backed executable or observable oracle, plus a final integrated oracle;
- decisions needed by the active phase are frozen;
- one implementer can own all writes sequentially and its identity, permissions, and descendant prohibition are confirmable;
- fan-out has no proven critical-path advantage and the heavier controller route is unnecessary.

When evidence is mixed, stay with the direct authority parent. Never use Manager Mode for conversation, one patch, one bug with a bounded reproduction, a small sequential change, or work the user asked to keep direct.

## Roles

- **Manager:** the current selected authority parent, preserving its model and effort. Sol High is the installed default; a running Astra parent retains authority. Use Max only for consequential whole-product architecture or a repeated quality failure. It owns the goal, phase order, human checkpoints, one replan, integration judgment, and final answer. It does not write production code while a separate implementer owns the active phase.
- **Implementer:** exactly one direct `mochicode_manager_implementer` Sol High child. Its custom agent sets `features.multi_agent = false` and `agents.enabled = false`; it must never spawn descendants. Confirm the active host honors these controls before claiming tool enforcement. Configuration alone is not runtime proof. It owns one phase at a time and stays available across phases when supported. Memory, vault, global-config, optional-tool, browser, and background-server limits are behavioral instructions plus parent evidence checks, not OS isolation; the parent must reject out-of-scope evidence or changes.
- **Fallback:** if the child cannot be created, constrained, or identified before any write, record `fallback_before_write` and continue directly in the parent. Never create an unrelated top-level task as a workaround. After writes, terminate the exact child before transferring ownership to the parent.
- **Verifier:** the ordinary MochiCode fresh-verifier rule. Manager Mode does not create a critic for every phase.

## Deterministic phase state

Store state outside the repository, normally under `%LOCALAPPDATA%\MochiCode\manager-runs\<run-id>`. Store only a goal hash, phase titles, dependencies, acceptance criteria, declared paths, attempts, fingerprints, receipts, and status. Do not store raw prompts, secrets, model reasoning, or transcripts.

The ledger snapshots receipt bytes once and rechecks stored receipt hashes on load. It validates reported evidence, not actual command execution, and does not authenticate actors. The parent must execute the checks independently. Hashes detect inconsistent edits, but cannot protect against someone who can rewrite the entire store and its hash chain.

Recovery v2 binds each pending transaction to its predecessor state. Completed v1 state is retained; in-flight v1 pending transactions are refused. Use the previous release to settle those transactions before upgrading. Finishing an active phase preserves a stopped run; stopping a completed run is a no-op. Status reports `usage_scope=accepted_phase_receipts_only`, not total run usage; it excludes manager, failed-attempt, and other unrecorded usage.

Create 3 to 12 phases for explicit mode; automatic candidates require 3 to 6. Wave one contains a runnable vertical slice. One implementer may revisit a path in later sequential phases, but only the active writer may edit it. Initialize the ledger with activation evidence, source revision, and decision hash:

```powershell
python scripts\mochicode.py manager init --run-root <state> --run-id <id> --goal-hash <sha256> --source-revision <git-sha> --decision-hash <sha256> --activation-mode explicit --activation-criterion explicit_manager_request --plan <plan.json> --json
```

For each phase:

1. Read `manager status`; select only `next_phase`.
2. Run `manager start --phase <id> --writer-id <id> --thread-id <id> --source-revision <git-sha>` before dispatch.
3. Give the implementer only that phase, frozen decisions, paths, non-background commands, and criterion IDs. Require receipt role exactly `manager_implementer`; the parent owns live UI and background-server verification.
4. Require `schemas/manager-child-completion.schema.json` from the direct implementer child, not the generic `schemas/child-completion.schema.json`. Its phase, thread, base revision, result revision, role, model, and effort must match active state exactly.
5. The parent must independently rerun the declared checks and create `schemas/manager-verification.schema.json`. The verifier identity must differ from the writer, all criteria must pass, protected inputs must remain unchanged, and changed paths must stay in scope.
6. On accepted evidence, run `manager finish --result accepted --receipt <child.json> --verification <parent.json>`.
7. On failure, restore the starting revision, hash the diff plus verifier result, and run `manager finish --result failed --fingerprint <sha256> --current-revision <starting-git-sha>`.

The scheduler rotates a first failure behind untouched ready phases. A second attempt or repeated fingerprint parks the phase. When no ready work remains, the current selected authority may use `manager replan --plan <replacement.json> --decision-hash <sha256>` once. The replacement must preserve accepted phases exactly. A second exhausted queue is terminal and must identify the concrete remaining human or external gate.

Use `manager stop` and `manager resume` at safe boundaries. Do not create periodic supervisor chatter. Wait for task completion or a meaningful state change, then act.

## Completion and fallback

Manager Mode is complete only when every phase is accepted, the integrated product passes its repository verification profile, any triggered fresh verifier is resolved, and the human receives one concise result. If child creation, handoff, or state tooling fails, diagnose it once and fall back to the direct authority parent without abandoning the goal.

Record manager and implementer models, efforts, phase wall time, child count, rework, accepted phases, integration defects, and tokens when the host exposes them. Do not claim savings without a matched comparison.
