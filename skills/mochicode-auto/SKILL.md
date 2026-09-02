---
name: mochicode-auto
description: Automatically route each new Codex goal through the smallest quality-proven direct, Sol-led, Luna, fan-out, critic, or resumable workflow. The user supplies only the real goal. Skip prompts marked MOCHICODE_CHILD.
---

# MochiCode Auto

MochiCode Auto is the only automatic top-level workflow for project work. The user supplies only the real goal once. MochiCode classifies task shape, authority, risk, coupling, verification, and duration, then selects models, effort, skills, children, checkpoints, critics, and stopping rules automatically. Manual overrides remain available but are never required.

## Routing boundary

Choose exactly one route automatically before doing project work:

1. **Direct current parent**: Trivial lookups, tiny rewrites, one obvious reversible action, one-step navigation, and ordinary conversation stay direct with no child.
2. **Direct Sol**: The quality-first default for substantive visual, product, architecture, tightly coupled, ambiguous, cross-cutting, or debugging work. Sol High owns judgment, implementation, live verification, correction, and final handoff. Use Sol Max only for consequential whole-product, architecture, security, release, or repeated quality-failure decisions.
3. **Bounded Luna Medium worker**: Use a real Luna child only for a sizable independent implementation leaf whose context isolation, parallel wall-time saving, or batch volume is expected to repay parent handoff and review overhead. The Sol parent freezes the contract, spawns `gpt-5.6-luna` at Medium, does not edit the child's files, and verifies the receipt. Escalate the same valid contract to Luna Max only after failed acceptance or proven difficulty.
4. **Bounded Sol-led fan-out**: At least two independent leaves with frozen interfaces and disjoint ownership. Start with two workers and use no more than three live children per wave. Continue only when the first wave demonstrates critical-path savings without integration regressions.
5. **Experimental controller**: Manual and unpromoted. Never enter it automatically until decision persistence, packet budgeting, real replanning, conditional compact final review, and usage aggregation pass their promotion gates.

If a direct or bounded route becomes too large, preserve a checkpoint and escalate only to the smallest proven route that can handle the remaining work. A long task or failed child does not by itself justify the experimental controller.

Direct Sol is a stock-quality passthrough, not a workflow ceremony. After selecting it, preserve the user's original goal and the repository instruction chain, then let Sol work directly. Do not rewrite the goal into a packet, force a planning document, activate optional process skills, spawn workers, create a ledger, or add a critic merely because MochiCode was triggered. Add one of those mechanisms only when its specific trigger below becomes observable during the task.

Small or sequential routine work also stays with the current Sol parent. A skill cannot change the already-running parent model. Never label parent-executed work as Luna. Use a Luna route only when a real child receipt proves the child model and effort. If the user started the task on Luna manually, direct Luna is truthful and may proceed without a Sol wrapper.

If the prompt contains `[MOCHICODE_CHILD]`, perform only the assigned child role. Never invoke this workflow recursively.

## Skill coexistence

Treat `config/skill-dispositions.json` as the portable registry for user-owned skill routing. MochiCode Auto remains the sole automatic top-level project workflow. Guardrails may constrain a selected route, narrow leaf skills may perform one specialist job, and explicit-only utilities run only when named. No other skill may start a competing project loop, choose a separate orchestrator, force a routine confirmation pause, or recursively delegate.

Do not edit OpenAI-managed system or plugin-cache skills. Retire stale user-owned skills only after the exact candidate version is installed and a fresh-task canary passes with a manifest-bound raw receipt; until then keep them narrow and explicit-only. Retirement moves complete folders into a dated recoverable archive outside active discovery roots. Keep legacy document fallbacks explicit-only until their managed replacements pass representative read and write canaries. Read [skill-system policy](references/skill-system.md) when auditing, installing, retiring, or restoring skills.

## Visible task naming

Name every new parent task with its automatically selected role, model, and reasoning effort first: `[ROLE | MODEL | EFFORT] concise objective`. Examples: `[DIRECT | Sol | High] Weather Window`, `[CODER | Luna | Medium] allocator`, and `[ORCH | Sol | High] game subsystem integration`.

The native child UI may assign its own generated label. When a custom child title is unavailable, begin the child packet and the parent handoff with `ROLE`, `MODEL`, and `EFFORT` fields instead. Never rename an existing user task just to conform, never hide an override, and never claim a model or effort that was not selected.

## Required role split

- Sol is the default substantive parent and final authority. Sol owns product, architecture, curriculum, visual design, UI/UX, interaction, motion, major planning, tightly coupled implementation, integration, debugging, live verification, and final judgment. Sol may implement directly. Use High normally and Max only when measured consequence or complexity justifies it.
- Luna Medium is a bounded worker for sizable independent leaves, not the default for tiny sequential tasks. Luna writes only declared paths and receives complete acceptance checks. Luna Max is an escalation, not the default.
- A Sol parent may delegate genuinely independent grunt leaves to bounded Luna workers. Sol freezes shared interfaces first, retains integration ownership, and closes children before changing their files.
- Terra is not part of the default native path. Terra remains available only as an optional read-only evidence or contract specialist inside an explicitly selected experimental controller run. Terra never owns product/design judgment or the user-facing result.
- The deterministic controller owns state, queues, dependencies, retries, budgets, fingerprints, process identity, worktree isolation, evidence, and integration only after explicit experimental selection.

## Effort escalation

- The Sol parent selects and records every child model and effort automatically. Escalate only from observed difficulty, ambiguity, failure, consequence, or verification risk.
- Sol High is the substantive default. Sol Max is reserved for consequential whole-product, architecture, security/release, or repeated quality-failure decisions. Ultra remains exceptional and must earn its latency and usage cost.
- When a Luna worker is justified, Medium is the first implementation effort. Retry one transient tool failure without changing effort. Escalate to Luna Max only when the specification remains valid and the failure is implementation depth rather than missing authority.
- Fast remains opt-in and independent of reasoning effort.

## Human and Sol authority gate

- Before a worker edits product behavior, architecture, curriculum, layout, styling, visuals, interaction, motion, navigation, learner flow, or user-facing language, the Sol parent binds the packet to a current concise decision and acceptance bar.
- Product and UX acceptance belongs to Sol plus human observation. Human judgment is final for usefulness, taste, comprehension, enjoyment, legibility, and AI-slop concerns. Automated judges may prove reachability, accessibility, and fidelity, but cannot overrule the human.
- For consequential interactive behavior, verify applicable transitional and re-entrant states: actions while running or paused, editing during execution, rapid repeated controls, keyboard focus after insertion, narrow layout, and actual reduced-motion behavior. Do not generalize these UI checks to noninteractive work.
- A Luna implementation may make local code-level choices inside the approved design, but any visible or architectural deviation returns to Sol. Do not let an implementation worker silently become the designer.

The retained controller agent names are `mochicode_sol`, `mochicode_terra_contract`, `mochicode_terra_review`, and `mochicode_luna`. Native direct routes select the explicit model and effort above rather than treating every retained controller role as active.

## Bounded critic panel

Run a judge gate only for consequential human-facing work, architecture/security/release decisions, cross-subsystem integration, repeated failure, or an explicitly requested quality audit. When a judge gate is due, automatically use three fresh read-only judges with distinct task-relevant perspectives. They see the fixed bar and candidate, not the implementer's self-assessment. Judges never edit or spawn.

The Sol parent adjudicates evidence, allows at most one integrated repair pass, reruns the same checks, and stops. Never loop until perfect. Correlated opinions without new failing evidence do not justify another round. Park a stubborn local packet and continue breadth-first work instead of polishing one subsystem while the larger product remains incomplete.

## Long-task state and selective skills

For interruption-prone or multi-context work, keep a compact verified state ledger outside production paths: original goal, accepted facts, completed stages, current hashes or commit identities, checks that passed, remaining work, blockers, and the exact next action. Executors receive one bounded next step. Update the ledger only from observed files, commands, receipts, or human judgments; never promote a model guess into durable fact.

Activate specialized skills only when their mechanism matches the task:

- systematic debugging and verification-before-completion for security, concurrency, state-machine, data-integrity, boundary-sensitive failures, or after an incomplete first diagnosis;
- focused TDD when behavior has a stable executable oracle;
- file-backed plans and dependency maps only when real dependencies or context pressure justify them;
- no always-on brainstorming, mandatory planning ceremony, unbounded Gauntlet/Ralph loops, or automatic self-written lessons.

Preserve passing checks as regression obligations. Promote a reusable lesson only after the same failure mechanism is observed twice and the lesson survives one negative-control task where it must not apply.

## Packet and concurrency invariants

- Preserve the existing packet schema. Plan packets retain `id`, `title`, `goal`, `wave`, `priority`, `vertical_slice`, `dependencies`, `acceptance_criteria`, and `verification_hints`. Contract packets retain `packet_id`, `goal`, `execution_mode`, `verification_class`, `acceptance_criteria`, `baseline_argv`, `final_argvs`, `expected_failure_codes`, `protected_patterns`, `allowed_paths`, and `evidence_requirements`.
- Exactly one writer owns a file, module, configuration area, migration, or shared state at a time. Use disjoint paths or isolated worktrees for independent writers.
- Maximum orchestration depth is one: primary Sol parent to child. Children never spawn descendants.
- Eight active child threads is a host ceiling, never a target or promise. Use zero for direct work, start fan-out with two, and use no more than three live children per wave until a representative benchmark proves a larger wave improves accepted quality per token and wall time. Close completed children before successive waves.
- Give children only the objective, relevant paths and symbols, constraints, the bound Sol decision when applicable, acceptance criteria, allowed commands, required evidence, and output format. Results should normally stay under about 1,200 tokens and use `STATUS`, `FACTS`, `CHANGES`, `TESTS`, `RISKS`, and `NEXT`.
- Native delegation does not bypass host policy, project instructions, protected inputs, or the one-writer rule.

## Progress, blockers, and human checkpoints

- Do not mark a task blocked merely because a future human test, an incomplete check, a scheduling hiccup, a missing optional capability, or an unresolved reversible choice exists. First identify and advance every independent reversible packet.
- Before reporting a blocker, state the exact missing authority or evidence and confirm that no useful reversible work remains. A failed attempt changes method after two tries; repeated failure fingerprints are parked while independent work continues.
- Internal failures are work, not blockers. A missing controller state file, failed child, child without a handoff, timeout, tool error, incomplete Sol decision, failed verifier, dirty worktree, checkout confusion, integration conflict, unsupported optional tool, or model refusal must trigger recovery. None permits the parent to mark the goal blocked.
- The parent remains responsible when orchestration machinery fails. Use this recovery ladder: inspect current files and receipts; change method after two matching failures; narrow the packet; retry with a fresh child or higher justified effort; switch between native and deterministic execution when safe; perform direct read-only diagnosis; repair the prerequisite; then park only the failed packet and continue independent work.
- A valid blocked goal requires a specific human-only action, unavailable external authority or credential, spending, production/deployment approval, destructive or irreversible permission, required unavailable hardware/service, or another external condition the parent cannot change. Every independent reversible path must already be exhausted. Name the exact unblock action and preserve resumable state.
- Never convert "I do not know yet," "the controller failed," "the agent returned nothing," "tests are red," or "the architecture is unresolved" into `blocked`. Investigate, escalate, or re-route instead.
- A human checkpoint is a readiness state, not an automatic stop. Prepare the testable slice, the question people must answer, feedback capture, and success/failure criteria. Continue all unrelated technical work until a real person must operate or judge the experience.
- Progress updates are milestones, not heartbeats. Do not emit repeated waiting messages or empty completed turns. Report only a meaningful result, a changed failure condition, a needed human-only decision, or the final verified handoff.
- The parent records each child’s role, model and effort, owned paths, result status, tests, and stop reason in the task artifact or handoff. Keep the child’s returned summary concise.

## Invocation

Resolve the plugin root as two directories above this `SKILL.md`. Run the controller from `<plugin-root>/scripts/mochicode.py`.

Use the deterministic invocation only for the controller route:

1. Run `doctor` once for the target environment. Do not silently fall back to another provider.
2. For a new controller-routed request, start `run` with the target project and exact user goal. Keep the goal out of committed project files.
3. For a continuing controller run, use `status` and resume the existing run instead of replanning from scratch.
4. Report only concise milestones, a needed decision, or the final verified result. Keep internal packet IDs, hashes, and model usage behind `status --verbose` unless actionable.

Read [workflow details](references/workflow.md) for controller-routed multi-packet work, [safety boundaries](references/safety.md) before code-changing work, and [command reference](references/commands.md) when invoking or recovering the controller.

## Completion

For direct and native work, report the selected route, actual parent model, every proven child model and effort, changed paths, checks, tokens when available, wall time, rework, and remaining uncertainty. Never claim that a model performed work without current session evidence or a child receipt. For experimental controller work, do not claim completion until the original acceptance criteria are satisfied by current evidence, protected checks are unchanged, and the end-to-end path is exercised. Never merge controller integration into the user’s current branch automatically.
