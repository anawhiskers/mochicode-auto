# Before and after benchmark protocol

## Current status

This document defines a frozen public benchmark protocol, not a benchmark result. The public before/after benchmark is not yet scored. Selection, adapter, setup, or gold-patch smoke checks must not be reported as a score.

## Claim boundary

The release comparison uses a paired 20-task pilot from the public SWE-bench Verified test split. It is reported as a local `SWE-bench Verified 20-task pilot`, never as an official full-benchmark or leaderboard score.

Both arms use the same frozen dataset revision, harness revision, task IDs, base commits, problem statements, Luna model, Max reasoning, standard service tier, sandbox, and wall-time ceiling. Gold patches, test patches, and known solutions are never exposed to either arm.

## Frozen selection

- Dataset: `SWE-bench/SWE-bench_Verified`
- Split: `test`
- Count: 20
- Selection seed: `mochicode-auto-sbv-pilot-v1`
- Execution seed: `mochicode-auto-sbv-execution-v1`

For each allowed row, calculate:

```text
SHA256("mochicode-auto-sbv-pilot-v1\0" + instance_id)
```

Group by repository, sort repositories lexicographically, sort each repository by the hash and then `instance_id`, and take one unused task per repository per pass until 20 are selected. Save the dataset SHA, harness SHA, ordered IDs, and SHA-256 of the ordered ID list before generation.

Execution order uses:

```text
SHA256("mochicode-auto-sbv-execution-v1\0" + instance_id)
```

Alternate arm order by task position so one arm does not consistently benefit from service load or warmed caches.

## Before arm

Run one vanilla Codex session per task with:

```text
model: gpt-5.6-luna
reasoning: max
service tier: standard
sandbox: workspace-write
multi-agent: disabled
automatic retries: none
```

Use an isolated Codex profile that cannot load MochiCode skills, rules, agents, lessons, or routing. Export the tracked Git diff even when it is empty.

## After arm

Run the identical task through the pinned MochiCode controller:

```text
Sol: binding product/architecture plan and final read-only judgment
Terra: orchestration, mechanically derived acceptance checks, integration, and immutable commit evidence review
Luna: gpt-5.6-luna, Max reasoning and standard service tier by default, at most two attempts
Controller: hash-bound plan receipt, durable reservations, worktrees, retries, sandboxed verifiers, receipts, stop conditions, and merge eligibility
```

Use a fresh learning store per task for the primary cold-start comparison. Export only the reviewed integration-branch diff. Never merge into the source branch.

## Primary score

```text
Before = resolved_before / 20
After  = resolved_after / 20
Lift   = (resolved_after - resolved_before) * 5 percentage points
```

Also report all paired outcomes, strict resolution including infrastructure failures in the denominator, graded-only resolution as a diagnostic, exact McNemar results, and a paired bootstrap 95 percent interval. One task equals five percentage points, so the pilot is a feasibility signal, not a leaderboard claim.

## Required diagnostics

- Resolved, unresolved, error, infrastructure-failure, ambiguous-failure, empty-patch, and patch-application counts.
- `FAIL_TO_PASS`, `PASS_TO_PASS`, patch-apply, clean-patch, and unrelated-file rates.
- Wall time, model calls, Luna calls, retries, replans, parked packets, repeated fingerprints, and available token usage.
- Evidence-chain, receipt-hash, learning-chain, source-head, integration-head, and protected-input verification.
- Resolved tasks per model call, per Luna call, and per hour.

Do not convert subscription calls or credits into dollar savings unless actual billing evidence exists.

## Infrastructure gate

Docker grading is approval-gated. Before any large Docker image, isolated dependency, or other large dependency download, obtain explicit approval. Until that approval is granted, paired generation and Docker grading remain pending and produce no benchmark score. After approval, pin the dataset and harness revisions, confirm Docker works, and run one gold-patch evaluator smoke test. Regrade an infrastructure failure once using the identical already-generated patch and a new run ID. Never regenerate a patch during an infrastructure-only regrade.

## Scale gate

The 20-task pilot requires roughly 40 generation runs and can consume substantial ChatGPT credits. Do not expand to all 500 Verified tasks until the 20-task adapter, patch extraction, evaluator accounting, and contamination checks are proven.

Authoritative public sources:

- https://github.com/SWE-bench/SWE-bench
- https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/datasets.md
- https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/evaluation.md
- https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/docker_setup.md
- https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified
