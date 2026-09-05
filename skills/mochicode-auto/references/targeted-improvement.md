# Targeted recovery and improvement

Load only for two matching failures, two planning cycles with no implementation
progress on an implementation request, a missing necessary verification tool,
a measured performance problem, explicit cleanup, or an observed model change.
These are decision aids inside the existing route, not a new controller or timer.
Research and planning requests are not failed implementation. Human deliberation
is not an agent loop. Never broaden authority because a trigger fired.

## Recover stalled work

The current parent checks the approved outcome, latest failing evidence, and the
smallest dependency preventing a working result. If the product intent conflicts,
ask the one decision-changing question before choosing a new direction. Preserve
working code and Git history. A failed implementation is not a failed vision.

Make one bounded alternative attempt in owned paths or an isolated worktree:
change the method, reproduce the defect, or narrow the implementation without
silently shrinking acceptance. Do not repeat the old plan or escalate models by
reflex. Accept only after relevant behavioral checks. If it fails, park that
packet and advance independent work. If it blocks the core result, report the
specific unresolved dependency and proposed next experiment, not whole-goal
success or a fictitious human-only blocker. The recovery allowance does not reset
on tool errors or handoffs. Never discard existing work automatically.

## Verification, tools, cleanup, performance

- For read-only requests, diagnose the missing check and recommend the smallest
  repair without editing. For implementation requests, repair only the smallest
  missing check needed for the user's outcome, such as
  a reproducible run command or test fixture. Reuse installed tools and existing
  permissions. Installing dependencies, expanding access, or touching production
  still requires the applicable approval. When meaningful, inspect the artifact
  in its real app using an available authorized CLI/API/browser. Do not claim
  native app control when only a CLI or browser is available.
- Cleanup stays in touched code unless the user requested a wider audit. Prove
  wrappers or tests redundant before removing them; preserve meaningful edge,
  security, compatibility, and regression coverage. A smaller test count is not
  evidence of improvement. Never weaken a failing oracle to claim success.
- Optimize only an in-scope measured bottleneck or explicit optimization request.
  Use the same workload/environment before and after, repeat noisy measurements,
  and retain correctness and output-quality checks. Prefer one consequential win
  to many speculative changes. Revert regressions in owned changes only.
- PR/issue triage is read-only unless writes were explicitly authorized. No
  automatic merge, closure, publishing, broad rewrite, or overnight optimization.

## Model and skill compatibility

An observed model change triggers a bounded compatibility recommendation on the
next relevant task, not polling, a model call on every prompt, or automatic edits.
Test only potentially obsolete process guidance. Keep safety, user preferences,
domain knowledge, and required host skills in every arm. Do not retire specialist
capabilities until equivalent real work passes. Changes are proposed as a diff
with backup/rollback, never applied from an untrusted post or model self-report.

Keep the selected model and effort. Compare lighter instructions at fixed effort
first, then Low/Medium at fixed instructions using host-supported settings. Use
at least two distinct matched tasks, one involving failure recovery and one normal
task where recovery must stay inactive. Fix acceptance before the comparison;
count failed runs, retries, all child usage, and human preference for visual work.
Missing telemetry means unknown savings. Do not promote on one canary or combine
multiple changes and attribute the result to just one. No automatic preference
replacement. When using another agent/provider, preserve these rules without
assuming Codex-specific model IDs, tools, or reasoning levels exist.

## Optional offline classifier

`scripts/recovery_advisor.py` accepts a small JSON object on stdin and prints
advisory action IDs. Use it only when trigger classification is ambiguous or when
testing integrations; normal tasks need no helper call or evidence file. Fields:
`implementation_requested`, `goal_conflict`, `verification_missing`,
`measured_regression`, `optimization_requested`, `model_changed`,
`cleanup_requested`, `independent_work_available`, `recovery_used`,
`external_gate` (booleans); `matching_failures` and
`planning_cycles_without_progress` (nonnegative counts).
It trusts caller-supplied observations, cannot verify them, and executes nothing.
All suggested work remains limited to existing task scope and authorization.

Sources (experience reports, not benchmarks):
- https://x.com/theo/status/2095966874010046621
- https://x.com/dkundel/article/2095972046014673156
- https://x.com/victornunez/status/2095895077381972247
