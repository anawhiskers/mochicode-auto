# Coding-agent workflow research, 2026-09-02

Status: research complete; one retrospective verifier pilot recorded; candidate safeguards implemented but not globally promoted.

## External-agent outcomes

- ChatGPT GPT-5.6 Sol Pro completed a public-source and web research review of MochiCode Auto v0.1.2.
- Claude Opus 5 Extra accepted the same brief but reached its shared usage limit before producing evidence.
- Gemini could not accept the brief because the selected Google Workspace account reports a cancelled subscription.
- No credits were purchased, no subscription was changed, and failed or partial responses are not counted as evidence.
- A separate native GPT-5.6 Sol High child reviewed the supplied X post and its linked Hermes Agent v0.21 claims. It recommended adopting only typed child receipts, one correction for malformed receipts, and per-child telemetry, not Hermes itself or a permanent agent society.

## Primary-source findings

- OpenAI reports that subagents consume more tokens than comparable single-agent runs because each performs its own model and tool work. This supports delegation only when parallel work can repay coordination cost: https://learn.chatgpt.com/docs/agent-configuration/subagents
- OpenAI's harness-engineering report recommends a short AGENTS.md as a map with progressively disclosed repository knowledge, not a monolithic instruction manual: https://openai.com/index/harness-engineering/
- Anthropic recommends the simplest workable agent design and evaluator loops only when criteria are clear and improvement is measurable: https://www.anthropic.com/engineering/building-effective-agents
- Anthropic's context-engineering report treats context as a finite attention budget with diminishing returns: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Agentless showed that a bounded localization, repair, and validation pipeline could outperform more complex open-source agents on its measured SWE-bench Lite setup. That result does not generalize to product or UI work: https://arxiv.org/html/2407.01489v2
- LongHorizon-Harness reports task-dependent gains with 2.3x baseline tokens on WeaveBench, 3.6x baseline output tokens on OSWorld 2.0, and 24 percent fewer tokens on Terminal-Bench 2.1. Its auditor consumed 19.4 to 38.1 percent of tokens: https://arxiv.org/html/2608.01964v1
- CriticGPT supports fresh review as a possible quality mechanism, but it does not prove a generic coding-model reviewer will reproduce a specialized critic: https://arxiv.org/html/2407.00215v1
- OpenAI now warns that public SWE-bench Verified is flawed and contaminated. Future MochiCode quality tests should prefer new private fixtures or SWE-bench Pro rather than report the old public split as frontier evidence: https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/
- Hermes Agent v0.21 documents structured child-output schemas, live steer or stop controls, per-delegation cost reporting, truncation markers, detected verification recipes, and split-brain ownership protection. These are useful mechanism examples, not evidence that its default ten-child society improves MochiCode: https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.31

## Candidate ranking

1. One fresh evidence-bound verifier after direct Sol, triggered only by security, authorization, data integrity, compatibility, cross-system integration, repaired first attempts, weak oracles, or consequential interactive behavior.
2. A bounded same-agent acceptance brief for ambiguous or cross-cutting work, with no separate planning call.
3. Canonical compact instructions with lazy evidence retrieval and stable prefixes.
4. Run-owned named verification profiles discovered from the repository instead of model-invented command arrays.
5. Sol-owned vertical slice before exactly two Luna Medium workers on large frozen leaves.
6. UI state matrices plus one browser evidence pass and one repair.
7. Failure-memory promotion only after a positive recurrence and a negative-control task.
8. Machine-validated child completion receipts with exactly one format correction, explicit truncation rejection, and per-child telemetry.

## Hermes and X-post advisory

The supplied X page could not be fetched directly in the research environment, so the recommendation does not rely on its prose. The linked public Hermes v0.21 release and implementation references were inspectable.

Adopted in the candidate branch:

- A strict completion schema records role, model, effort, owned paths, acceptance evidence, commands and exit codes, evidence locations, unresolved risks, stop reason, and token, tool, retry, duration, and termination telemetry.
- The validator rejects omitted criteria, commandless completion, unsafe or unowned paths, cross-directory `*` glob escapes, truncated evidence, extra fields, and a `COMPLETED` claim that contains a failed command or non-passing criterion.
- A malformed result gets one format-only correction attempt. It does not start a critic loop.
- Candidate lessons remain absent from ordinary prompts. Explicit positive and negative-control runs record whether the provider actually applied the selected candidate, copy the Codex model-call receipt into the trusted learning store, and bind the outcome to its hash. Promotion reopens and verifies the receipt; stub, fabricated, missing, or tampered evidence is rejected.

Rejected:

- Installing Hermes as another runtime.
- Permanent named-agent societies, group chats, peer-to-peer architecture decisions, ten-child defaults, or persistent memory by default.
- Treating schema validation or automated verification as authority over repository checks or human product judgment.

The receipt mechanism remains a candidate until a short paired benchmark shows fewer incomplete handoffs without quality regression, with both negative controls caught, token overhead no more than 5 percent, and wall-time overhead no more than 10 percent.

## Retrospective H2 pilot

The v0.1.2 security-hardening change provides one real but non-matched pilot:

- The same-context implementation and local test pass did not identify two residual gaps: trusted Codex environment propagation into model-generated shell commands, and incomplete direct-source install exclusions.
- A fresh read-only Codex reviewer found both gaps with exact mechanisms.
- Reviewer trace: `tr_089fd269-bf80-4fd6-9a60-394ee9a9fe74`.
- Reviewer model: GPT-5.5.
- Reviewer usage: 1,731,658 input tokens and 4,300 output tokens.
- Reviewer wall time: 133.252 seconds, no retry.
- Both findings were confirmed, repaired, regression-tested, and included in v0.1.2.

This is positive evidence that fresh context can add quality, but it is not a matched self-review comparison and cannot promote the route by itself.

## Frozen matched benchmark

Run three newly prepared tasks, one each for cross-layer correctness, authorization or data safety, and interactive UI state.

For each task:

1. Stock direct Sol High creates one base patch and runs frozen normal verification.
2. Clone the exact patch and receipts.
3. Control receives one same-executor self-review.
4. Challenger receives one fresh Sol High review containing only the objective, acceptance manifest, changed paths, diffstat, verification receipts, and risk-specific files fetched on demand.
5. Each arm gets one repair and one full verification rerun.
6. A blind evaluator scores the repository and running behavior, not the transcript or route name.

Record uncached input, cached input, output and reasoning tokens, calls, wall time, verification time, repair count, changed files, post-completion churn, and P0/P1/P2 defects.

Promotion requires all three tasks to complete, no challenger-only P0 or P1 defect, and at least one material evidence-backed defect caught by the fresh verifier that self-review missed. If quality ties, lower total tokens and wall time win. Speculative or P2-only findings do not justify promotion.

## Current decision

The candidate branch now contains the compact dispatcher, selective verifier, named verification profile, vertical-slice fan-out limits, UI state matrix, negative-control-gated learning, and typed child receipt safeguard. Do not change the installed dispatcher or global AGENTS.md until local verification and independent review pass. The fresh-verifier route still needs the frozen three-task H2 benchmark, and the receipt route still needs its short paired handoff benchmark before either claim is described as fully promoted evidence.
