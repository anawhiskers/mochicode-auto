# Manager Mode matched benchmark, 2026-09-04

## Decision

Keep Manager Mode as an explicit beta and keep automatic selection in shadow. One blind AI judge preferred its operator-facing result, but it used 3.33 times direct Sol's total input and took 1.48 times as long. That fails the frozen automatic-promotion limits, and one judge is not human quality evidence.

## Why this mechanism was tested

Matt Shumer's [Manager Loop experiment](https://somethingbig.ai/manager-loop-experiments) separates a coordinator from a long-running implementer after earlier long-run, multi-lane, and periodic-supervisor experiments stalled or created coordination tax. His [Astra review](https://somethingbig.ai/astra-review) also warns that many-agent operation can be unnecessarily expensive. OpenAI's [subagent guidance](https://learn.chatgpt.com/docs/agent-configuration/subagents) says subagents can add context isolation and parallelism but consume more tokens, while the [model guidance](https://developers.openai.com/api/docs/guides/latest-model) recommends tuning delegation against task evidence.

MochiCode tested the narrowest compatible form: the current Sol High parent managed one non-spawning Sol High implementer across three sequential phases. No periodic supervisor, recursive child tree, or always-on critic was added.

## Frozen task

Build a framework-free Signal Garden operations dashboard with a runnable wave-one vertical slice, six operational signals, filtering, pause and resume, acknowledgements, storage, responsive styling, accessibility semantics, and deterministic tests. Both arms started from equivalent committed repositories with seven production files across two components, three phases, and the same final oracle.

| Arm | Execution | Result |
|---|---|---|
| A | One direct Sol High run, no subagents | 6/6 tests passed |
| B | Sol High manager plus one reused non-spawning Sol High child | 6/6 tests passed; all three phases independently reverified and accepted |

The first setup attempt was invalidated before scoring because real Codex rejected unsupported JSON Schema keywords and the test repository was outside its trusted root. The schema was reduced to the supported output subset, strict checks stayed in deterministic Python, and both arms were rebuilt from fresh equivalent repositories.

## Usage and speed

| Metric | Direct Sol | Manager Mode | Manager / direct |
|---|---:|---:|---:|
| Input tokens reported by host | 598,212 | 1,992,380 | 3.33x |
| Cached input tokens | 540,928 | 1,875,712 | 3.47x |
| Uncached input tokens | 57,284 | 116,668 | 2.04x |
| Output tokens | 21,138 | 19,295 | 0.91x |
| Reasoning output tokens | 4,992 | 5,340 | 1.07x |
| Wall time | 518 sec | 766 sec | 1.48x |
| Child count | 0 | 1 reused child | n/a |
| Rework | 0 | 1 format-only receipt correction | n/a |

These are subscription-host telemetry values, not API dollar costs. Cached and uncached input are shown separately because total input alone hides how much context was reused.

## Blind judge

A fresh Sol High judge received anonymized source trees and equal-size screenshots. It was not told which workflow produced either result. Candidate A was direct Sol and Candidate B was Manager Mode.

| Criterion, 0 to 10 | Direct Sol | Manager Mode |
|---|---:|---:|
| Functional correctness and state behavior | 8.2 | 9.0 |
| Code quality and maintainability | 9.2 | 8.5 |
| Accessibility and interaction semantics | 7.8 | 9.1 |
| Visual hierarchy and polish | 8.8 | 9.2 |
| Integration coherence and defect risk | 8.5 | 8.1 |
| Overall product quality | 8.4 | 9.0 |

One blind AI judge chose Manager Mode. Its reported advantage was operational truth and interaction safety: acknowledging a critical signal did not relabel the system as contained, focus was restored after rerenders, and live-region updates were more reliable. Direct Sol had the cleaner internal model and more distinctive visual identity, but the judge found its acknowledgement behavior potentially misleading and several small-text colors weak. Manager Mode's main debt was a disconnected scoring function, hard-coded status values, and an unexplained snapshot constant.

## Promotion result

Automatic promotion failed. The frozen gate required no more than 1.25 times direct tokens and 1.15 times wall time, or at most 1.5 times tokens when Manager Mode uniquely prevented a material defect. Manager Mode reached 3.33 times total input, 2.04 times uncached input, and 1.48 times wall time. A second distinct matched task is also still required.

Explicit `[MOCHICODE_MANAGER]` remains available for deliberate trials because this run found a plausible benefit when whole-goal continuity and operator-facing correctness matter. Ordinary work remains direct Sol High. Automatic classification records only shadow candidates until a later matched benchmark satisfies every promotion gate.

## Verification evidence

- Both candidate repositories: `npm test`, 6/6 passing.
- Manager phase commits: `48038e9733bc789d78bf00cf637eb651017ea36e`, `dec138d112fc7bf1b9a5f52acb6e3ea28c187de0`, `6f73857a35a8fd62f0815d99af26e608d12c3d29`.
- Manager ledger: three accepted phases, one child identity, source-revision chaining, child receipt plus independent parent receipt for every phase.
- Live manager browser pass covered rendered content, controls, accessibility tree, and screenshot. The exact helper server process was stopped and verified absent.
- Direct and manager visual screenshots were captured from fresh local servers at the same viewport for the blind comparison.

## Limits

This is one short UI-and-state task, not proof for all repositories or true multi-hour work. The judge is an AI diagnostic, not human taste authority. No API dollar cost was measured. Automatic Manager Mode remains unpromoted until a second distinct matched task also passes quality, usage, speed, and negative-control gates.
