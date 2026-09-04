# MochiCode hybrid routing contract

This portable contract defines how project work enters MochiCode Auto. It keeps small work fast while
reserving deterministic state and evidence handling for work that can actually benefit from it.

## Automatic routes

| Route | Select it when | Execution boundary |
| --- | --- | --- |
| Direct current parent | Trivial or one-step. | No child. |
| Direct Sol High | Substantive visual, product, architecture, tightly coupled, ambiguous, or debugging work. | Sol owns the complete quality loop. |
| Bounded Luna Medium worker | Sizable independent implementation leaf with a concrete expected saving from leaf size, slow verification, external build latency, isolation, or batch volume. Independence alone is insufficient. | A real Luna Medium child implements; parent verifies; Max is escalation. |
| Sol-led fan-out | Frozen interfaces, at least two disjoint independent leaves, and a concrete expected critical-path saving. | Start with two, cap normal waves at three, integrate under Sol. |
| Manager Mode beta | Explicit execution request. Automatic classification is shadow-only until matched promotion. | Sol manages one direct non-spawning Sol High implementer child across phases; parent verification, deterministic rotation, parking, stop/resume, and one replan are required. |
| Experimental controller | Explicit experiment only after promotion criteria are met. | Never selected automatically. |

MochiCode Auto is the only top-level workflow. The user supplies only the real goal; routing, models, effort, skills, workers, checkpoints, and judge gates are automatic.

## Core roles

- Sol High is the default substantive parent and owns decisions, implementation when appropriate, integration, debugging, live verification, and final judgment. Max is consequential-only.
- Small or sequential routine work remains direct Sol. Luna Medium is a real child only for sizable independent leaves with hard checks; Max is escalation after failed acceptance.
- Terra is absent from default native routing and optional only in explicit controller experiments.
- Manager Mode separates direction from execution. The manager does not edit production code while exactly one direct Sol High child owns the active phase. That child cannot spawn descendants. Child failure before a write falls back to direct Sol.
- A warranted quality gate uses one fresh evidence-bound read-only authority verifier, one parent adjudication, one repair at most, then reruns all checks and stops. Three judges are exceptional.
- The deterministic controller owns persistent workflow state and every retry, evidence, and merge
  decision only on an explicitly selected experimental route.

The complete archive-role mapping is machine-readable in `config/role-dispositions.json`. The four
active core agent definitions are in `config/agents/`.

## Invariants

- Keep the existing plan and contract packet schemas unchanged.
- Exactly one writer owns each file, module, configuration area, migration, or shared state.
- Maximum orchestration depth is one, primary Sol parent to child. Children do not spawn descendants.
- Eight active child threads is a host ceiling. Automatic waves start with two and cap at three.
- Total waves are bounded by the goal, declared budgets, stop conditions, and host limits.
- Manager Mode keeps 3 to 12 explicit phases, one active phase, two attempts per phase, one replan, a wave-one vertical slice, and independent child plus parent receipts. Automatic candidates require 3 to 6 phases, at least 6 production files across 2 components, complete phase/final oracles, frozen decisions, one sequential writer, confirmed child controls, rejected fan-out, and no heavier-controller need.
- Repository files do not widen host permissions, replace user approvals, change the host context
  limit, or add recursive agent configuration.
