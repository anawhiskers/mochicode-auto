# Research decisions

## Current Codex capabilities

Official OpenAI documentation confirms that skills can trigger implicitly, plugins distribute skills to Codex, custom agents can select different models and sandboxes, worktrees isolate changes, and `codex exec` supports machine-readable JSONL, structured output schemas, explicit sandboxes, resumable sessions, and saved CLI authentication.

Sources:

- [Build skills](https://learn.chatgpt.com/docs/build-skills)
- [Build plugins](https://learn.chatgpt.com/docs/build-plugins)
- [Subagents and custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Git worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)
- [Non-interactive Codex](https://learn.chatgpt.com/docs/non-interactive-mode)
- [GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [GPT-6 Astra model guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra)

## Traycer

Traycer validates the value of subscription reuse, shared context, parallel agents, and agent-to-agent review. It is not used as a dependency. Its own repository states that the open-source tree contains clients, CLI, and protocol, while the signed Host and cloud backends are not in the repository. Depending on it would not produce the requested fully portable local controller.

Sources:

- [Traycer repository](https://github.com/traycerai/traycer)
- [Traycer repository boundary](https://github.com/traycerai/traycer/blob/main/AGENTS.md)

## LongHorizon-Harness

The useful transfer is explicit durable state, one bounded fresh-context executor step, independent read-only audit, and state updates only from verified facts. Its published paper reports meaningful benchmark gains, but those results do not prove this plugin. The package is not installed as a dependency because its documentation says Windows support exists but is not yet thoroughly tested, and its larger browser and computer-use surface is unnecessary here.

Sources:

- [LongHorizon-Harness repository](https://github.com/AMAP-ML/LongHorizon-Harness)
- [LongHorizon-Harness paper](https://arxiv.org/abs/2608.01964)

## Supplied video

The complete 17 minute 27 second auto-caption track for [AI Built This Space Game From Scratch, Here’s Exactly How](https://www.youtube.com/watch?v=Z9fyPTQVEdI) was retrieved and analyzed. Caption evidence supports a planner-only orchestrator, one bounded task per fresh session, parallel independent assets, separate critics, numeric checks, screenshot review, and final human judgment.

The video also shows why this plugin adds stricter controls: the planner drifted into implementation, the creator stopped counting more than 100 agent runs, a worker exhausted its budget without a documented handoff, independently produced assets failed to integrate, a partial spider smoke test replaced the dedicated laboratory, and the first end-to-end recording exposed an untested final bug. The caption file SHA-256 is `A8E68B280CF0339054A284FAD928C8B7BF169F29856D356B73F447DF984BD68D`.
