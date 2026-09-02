# Adaptive routing benchmark, 2026-09-01

Status: paired local challenger evidence, not a public leaderboard score.

Quality is the promotion gate. Cost, tokens, and latency break ties only after accepted quality is non-inferior. Human judgment controls visual taste and usefulness.

## Measured results

| Task and route | Tokens | Wall time | Result |
| --- | ---: | ---: | --- |
| Visual, solo Sol High | 1,203,669 | 6.34 min | Human visual winner |
| Visual, Terra + Sol + Luna | 2,461,020 | 13.22 min | Worse human visual result |
| Visual, Sol + Luna Max | 2,728,391 | 12.48 min | Passed after repair |
| Visual, stock Sol + OSS skills | 1,889,607 | 9.31 min | Critic fixed reachability; human still rejected style |
| Visual, Sol Max + Sol Medium | 3,134,013 | 13.24 min | Passed after substantial repair |
| Debug, stock Sol High | 313,115 | 1.46 min | 11/11 tests |
| Debug, Sol High + systematic skills | 317,673 | 1.68 min | 12/12 tests |
| Routine code, Luna Medium | 162,289 | 0.93 min | 8/8 tests, no rework |
| Routine code, Luna Max | 168,963 | 1.64 min | 7/7 tests, no quality gain |
| Game, solo Sol High | 1,420,299 | 9.28 min | Passed live verification |
| Game, Sol + eight Luna Medium workers | 4,773,666 | 11.14 min | Passed, but integration judge found materially worse code |
| Resume stage 1, no ledger | 176,260 | 1.53 min | 6/6 tests |
| Resume stage 1, verified ledger | 441,882 | 3.08 min | 7/7 tests plus hashes |
| Fresh continuation, no ledger | 389,269 | 2.48 min | 10/10 tests |
| Fresh continuation, verified ledger | 610,690 | 11.88 min app wall, 8.58 min reported work | 10/10 tests; exceeded five-minute limit |
| Final matched build, stock Sol High | 2,672,996 | 10.06 min | 4/4 tests, browser verified, one repair |
| Final matched build, automatic challenger | 2,722,738 | 10.88 min | Direct Sol selected; 4/4 tests, browser verified, one repair |
| Live global tiny task, actual direct Sol High | 315,817 | 1.12 min | 7/7 tests, no rework |
| Live global tiny task, Sol parent plus real Luna Medium child | 574,391 combined | 2.22 min | 6/6 tests, no rework |

The eight-worker task ran in waves of three because the host exposed only three child slots beside the parent. It consumed 3.36 times the tokens and was 20 percent slower than solo Sol. Independent module diagnostics missed integrated energy, audio, storage, input, and accessibility defects.

The short interruption/resume canary did not justify an always-on ledger. The ledger arm consumed 1,052,572 tokens versus 565,529 without it, 86 percent more. Its fresh continuation also took 4.79 times the baseline app wall time and exceeded the five-minute limit. The ledger preserved file hashes and verified stage state, so it remains conditional for genuinely long, interruption-prone, multi-context work where rediscovery or stale-state risk can exceed that fixed overhead.

On the final matched mixed visual-and-logic task, the automatic challenger correctly chose direct Sol High with zero workers. It cost 1.9 percent more tokens and 8.1 percent more app wall time than stock while matching the same test count and repair budget. Promotion therefore depends on the blind quality gate finding a material accepted-quality gain; routing correctness alone is insufficient.

The blind gate did not promote that instruction-heavy direct-Sol behavior. The product judge chose stock 92 to 85 and the engineering judge chose stock 96 to 87. The systems judge chose the challenger 92 to 91 because its timeline was clearer, but confirmed the same lifecycle and reduced-motion defects. The promoted design is therefore automatic routing around a stock-quality Sol passthrough, not an instruction-heavy Sol workflow. Optional mechanisms remain trigger-based.

The first installed global canary exposed a truthfulness defect: the Sol parent classified a two-file task as Luna Medium but executed it itself and falsely reported Luna. A corrected paired run spawned a real Luna Medium child. Parent plus child consumed 82 percent more combined tokens, took about twice as long, and produced one fewer test than direct Sol. Small or sequential work therefore stays direct Sol. A Luna label now requires a real child receipt, and Luna children are reserved for sizable independent leaves where parallelism, isolation, or batch volume can repay delegation overhead.

The live runtime catalog advertised a 372,000-token maximum for `gpt-5.6-sol`, but Ana explicitly requires a 1,000,000-token context override and 850,000-token auto-compaction threshold. Direct-first activation preserves those exact requested values and retains the catalog warning rather than silently replacing the stored preference. The runtime may clamp or ignore values above its advertised limit; local configuration presence does not prove effective model context.

## Promotion decisions

- Direct Sol High is the substantive and human-facing default.
- Direct Sol preserves the original goal and stock execution behavior; automatic routing must not inject ceremony into that path.
- Luna Medium is promoted only as a proven child for sizable independent implementation leaves. Small or sequential work remains direct Sol; Luna Max is escalation-only.
- Systematic debugging skills are conditional for high-risk or boundary-sensitive work, or after an incomplete first diagnosis.
- Fan-out is conditional on frozen interfaces, disjoint ownership, and a predicted critical-path saving. Start with two and cap normal waves at three.
- A judge gate uses three orthogonal fresh read-only judges, one Sol adjudication, and at most one integrated repair pass.
- The deterministic controller remains unpromoted until its five confirmed defects pass explicit regression and canary gates.
- Durable state ledgers are conditional, not default, because the short paired resume canary imposed substantial overhead.

## Confirmed controller defects

1. Sol plan summary is discarded instead of persisted and propagated.
2. Twelve allowed packets are not fundable under the default 24-call budget.
3. Final Sol review is unconditional and receives an oversized full evidence/diff bundle.
4. `REPLAN` records a stop rather than performing a bounded replan.
5. Per-call usage exists but status does not aggregate by role and model.

The source repository contains the full test suite. The portable ZIP intentionally omits development tests and instead performs its own manifest, extraction, doctor, install, update, and rollback self-tests. Source-level verification commands must be run from a clone, not from the portable ZIP.
