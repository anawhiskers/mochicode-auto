# Retaining Astra capabilities while reducing usage

Recommendation: use Standard Astra where its decisions or visual/computer verification matter, direct Sol for ordinary tasks, and a bounded cheaper worker only when its saved work exceeds the handoff cost. This is a candidate strategy, not a promoted replacement for the installed model preference.

## Current public rates, checked 2026-09-04

| Credits per million tokens | Input | Cached input | Output |
| --- | ---: | ---: | ---: |
| Astra Standard | 250 | 25 | 1,250 |
| Sol Standard | 100 | 10 | 500 |
| Terra Standard | 50 | 5 | 300 |
| Luna Standard | 5 | 0.5 | 30 |

[OpenAI's ChatGPT/Codex pricing page](https://learn.chatgpt.com/docs/pricing) lists these credit rates and Astra Fast at 2.5 times Standard. Included plan allowance estimates are ranges, not fixed per-message limits or a guaranteed dollar conversion. API rates are separate: the [Astra API model card](https://developers.openai.com/api/docs/models/gpt-6-astra) lists different Fast billing and long-input multipliers. Do not apply API dollar calculations directly to subscription usage.

At equal token counts, Standard instead of Fast uses 60% fewer credits while retaining the same model. Cached input has a 90% lower listed rate than uncached input; that is not a 90% saving on the whole task because output, misses, and orchestration still cost usage.

## Controls that retain capability

1. Avoid persistent Fast unless waiting time justifies the premium. Respect an explicit user speed preference.
2. Preserve the selected parent and use the lowest adequate effort for the next task. Benchmark Medium versus High for routine work; reserve Max/Ultra for demonstrated need and supported hosts. Do not silently alter an active task's effort or stored preference.
3. Keep difficult product and interface decisions with Astra. For one sizable frozen leaf, send only its contract and necessary files to Sol or Luna, then review the result. Do not clone the entire conversation or create a team for a small patch.
4. Preserve the 1M context and 850K compaction settings. Available capacity is not a target to fill: use narrow file reads, short test receipts, and stable instruction prefixes. Start unrelated work in a fresh task; keep coherent work in its current task so useful cache reuse and continuity survive.
5. Run required checks once per relevant candidate. Cache verification evidence by source/configuration identity, invalidate it after edits, and poll only when progress can change the next action. Do not use a model to repeatedly narrate a quiet test runner.
6. Review with one fresh evidence-bound critic when risk warrants it. No automatic Astra reviewer panel or always-on Manager loop.

The plugin cannot turn a Sol parent into Astra or make a lower model inherit Astra's reasoning and vision. A real model switch needs a supported host control. Responses API async tools and dynamic reasoning updates remain provider features, not settings this CLI plugin can fabricate.

## Small next comparison

Freeze one coupled UI/state task and one routine coding task. Compare direct Astra Standard with a single cheaper implementation handoff, using equal acceptance criteria and one repair maximum. Record all participants' input/cache/output tokens, wall time, failed attempts, technical defects, and human visual preference. Preserve the baseline unless accepted quality is maintained and total usage or time improves on both tasks. No large paid benchmark campaign is required to reject a losing setup.

Illustration only: if 70% of an equal-token Astra workload moved to Sol, its model-rate cost would be 58% of the original, a 42% reduction before handoffs, cache changes, and rework. This is arithmetic, not a measured MochiCode saving. Prior local tests showed orchestration can cost more than direct work, so the workflow must count every call.
