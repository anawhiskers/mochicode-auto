# Routing decision benchmark, 2026-09-02

Status: complete local paired evidence, not a public leaderboard score.

## Results

| Pair | Lean route | Added mechanism | Accepted-quality result |
| --- | ---: | ---: | --- |
| Delegation crossover | Direct Sol High: 335,605 tokens, 220.059 s | Sol High + two Luna Medium: 1,714,210 tokens, 482.649 s | Direct passed first attempt; workers required repair |
| Conditional debugging | Stock Sol High: 236,099 tokens, 89.065 s | Sol High + debug/TDD: 475,674 tokens, 107.529 s | Both passed 20/20; no quality gain |
| Consequential judge gate | No gate: 3,704,402 tokens, 327.088 s | Three judges + Sol adjudication: 8,231,527 tokens, 556.798 s | Gate uniquely caught two remaining technical defects |

## Decisions

- Keep stock direct Sol High as the base route.
- Independence is necessary but not sufficient for delegation. Require a concrete predicted critical-path saving from larger leaves, slow independent verification, external build latency, context isolation, or batch volume.
- Do not force systematic debugging or TDD merely because a bug is boundary-sensitive. Activate it for security, concurrency, or data-integrity consequence, after incomplete diagnosis or failed first-pass acceptance, or when no executable reproduction exists.
- Keep three fresh read-only judges for consequential mixed UI-and-logic work. Default to product hierarchy, accessibility and interaction, and state integration. Deduplicate findings, allow one Sol repair, rerun the same checks, and preserve human judgment for taste.

## Accounting

- Qualified comparison total: 14,697,517 tokens and 29.720 minutes of arm wall time.
- Actual lab total, including orchestration and invalidated protocol work: 41,819,087 tokens and 96.491 minutes.
- A contradictory hidden assertion was repaired once before Pair 1 was counted. The invalidated work remains included in actual usage.

## Limits

- The delegation result covers local code leaves of the measured size, not much larger leaves or work dominated by external builds.
- The debugging result does not cover nondeterministic races or bugs without stable executable reproduction.
- The judge result is one consequential mixed UI-and-logic candidate. It proves technical coverage, not human visual preference.
- No Astra result exists. A future model comparison remains fail-closed until an exact official slug, live supported efforts, and a real session receipt exist.
