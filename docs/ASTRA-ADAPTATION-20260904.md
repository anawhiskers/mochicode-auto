# GPT-6 Astra adaptation, 2026-09-04

Historical investigation record. The initial High/catalog assumptions below were
superseded during the audit: an already selected parent retains its model and
effort without a catalog gate. See [current v0.1.5 behavior](ASTRA-HYGIENE-20260905.md).

## Decision

Prepare MochiCode for Astra without replacing the active Sol route. Astra becomes a direct authority parent only when the installed Codex catalog lists `gpt-6-astra` with High reasoning and the user explicitly selects or installs the Astra profile. Automatic default promotion still requires matched evidence against direct Sol.

The local Codex 0.153.0 catalog snapshot taken for this change did not list Astra, so no live Astra task or quality claim was possible. This is a readiness change, not an Astra benchmark.

## Evidence that changes routing

Official OpenAI guidance describes Astra as stronger for long coherent work, instruction following, software engineering, browsing, and computer use. It also notes that Astra may ask more clarifying questions, delegate less than desired, and test more broadly than a small change needs. Astra supports Low through Max reasoning, but not None. The Responses API adds async tool calls, mid-turn steering, and `configuration_update` reasoning changes.

The supplied [Angel Brodin post](https://x.com/angelbrodin/status/2095882075412832380) says Astra follows tone-of-voice skills unusually well and avoids generic AI wording on the first pass. This is useful product feedback, not benchmark evidence. MochiCode therefore keeps a concise positive style contract and avoids adding a long phrase blacklist.

Official sources:

- [Astra model guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra)
- [Astra model card](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)

## Adapted behavior

- Direct Astra High owns substantial, coupled, visual, architectural, debugging, integration, and long sequential work when Astra is already the selected parent.
- Astra does not automatically add Manager Mode. Its long-task coherence reduces the benefit of another coordinator layer.
- Delegation is explicit and bounded because Astra may delegate less without clear triggers. Existing Luna and Terra roles remain cheaper support roles. Astra keeps product decisions and integration.
- The existing Sol High verifier remains the independent review fallback until a matched test proves that a second Astra call improves accepted quality enough to repay its cost.
- Tests start narrow and broaden only after a failure, weak oracle, changed boundary, or consequential risk.
- Astra may use browser or computer tools directly when available. No dedicated UI subagent is added.
- API-only async, steering, and effort-update features are used only when the active host advertises them. The plugin never simulates them with polling, config churn, or extra agents.
- Instruction conflicts are treated as a larger Astra risk because it follows skills and `AGENTS.md` more literally. The Astra reference requires minimum relevant skill loading and names the exact instruction behind a pause.

## Configuration gate

`--astra-first` and `-AstraFirst` are explicit profile switches. They fail closed unless the live Codex model catalog lists `gpt-6-astra` and High reasoning. A successful profile keeps the existing 1,000,000-token context and 850,000-token compaction settings, removes persistent Fast as a default, and retains Sol High as the review model.

This switch changes a stored model preference. The installer does not invoke it implicitly.

## Promotion gate

Before Astra becomes MochiCode's automatic installed default:

1. Capture a live catalog receipt from the target Codex version and account.
2. Run a matched direct Astra High versus direct Sol High task with the same repository state and acceptance criteria.
3. Compare accepted quality first, then uncached and total input, output, reasoning, wall time, rework, and defects.
4. Include human judgment for visual or interaction work.
5. Confirm the result on a second distinct task.

Until then, Sol High remains the installed default and Astra support remains capability-gated.
