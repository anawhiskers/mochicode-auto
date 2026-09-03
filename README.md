# MochiCode Auto

MochiCode Auto is an evidence-driven workflow router for Codex on Windows. You give Codex the real goal once. The workflow keeps strong tasks direct, delegates only work that can repay the handoff cost, and activates critics or durable state only when their trigger is present.

The main lesson from the benchmark work is simple: **more agents are not automatically better**. Direct GPT-5.6 Sol High was often faster, cheaper, and better than elaborate orchestration. MochiCode therefore preserves direct Sol behavior by default and adds bounded workers only when the task divides cleanly.

## Current routing

The 2026-09-02 decision benchmark confirmed direct Sol as the base route and rejected automatic fan-out plus always-on debug/TDD ceremony. Follow-up research and the v0.1.2 security pilot replaced the ordinary three-judge gate with one evidence-bound fresh verifier; three judges remain exceptional. See [the benchmark record](docs/ROUTING-BENCHMARK-20260902.md) and [research record](docs/AGENT-WORKFLOW-RESEARCH-20260902.md).

```text
trivial conversation or one obvious action
  -> current parent, no workflow ceremony

substantive, visual, architectural, coupled, or debugging work
  -> direct GPT-5.6 Sol High

sizable independent implementation leaf with executable checks
  -> one real GPT-5.6 Luna Medium child
  -> Luna Max only after failed acceptance or proven difficulty

two or more frozen, disjoint leaves with a concrete expected critical-path saving
  -> Sol-led fan-out, starting with two workers
  -> normally no more than three live workers per wave

observable risk or quality gate -> one fresh evidence-bound Sol verifier -> one adjudication -> at most one repair

experimental controller -> explicit selection only after promotion gates pass
```

Native work has one writer per file or shared state. Delegation depth is one, primary Sol parent to child, and children cannot delegate. Automatic fan-out starts with two and never exceeds three live workers; eight is only a host ceiling.

Delegated implementation leaves use a typed JSON completion receipt. The bundled validator rejects missing acceptance evidence, unowned or unsafe paths, contradictory command exits, truncation markers, and fake `COMPLETED` claims. One malformed receipt gets one format-only correction attempt. This makes handoffs inspectable, but it does not replace parent verification or a risk-triggered fresh verifier.

## What the measurements showed

These are local paired experiments, not OpenAI or SWE-bench leaderboard scores.

| Comparison | Direct route | Orchestrated route | Observed result |
| --- | ---: | ---: | --- |
| Tiny coding task | Sol High: 315,817 tokens, 67 sec | Sol + real Luna Medium: 574,391 tokens, 133 sec | Direct used 45% fewer tokens, took about half the time, and produced 7 tests versus 6 |
| Visual task | Sol High: 1,203,669 tokens, 6.34 min | Terra + Sol + Luna: 2,461,020 tokens, 13.22 min | Direct was the human visual winner |
| Game task | Sol High: 1,420,299 tokens, 9.28 min | Sol + eight Luna workers: 4,773,666 tokens, 11.14 min | Fan-out used 3.36 times the tokens, was 20% slower, and had worse integration defects |
| Routine isolated code | Luna Medium: 162,289 tokens, 0.93 min | Luna Max: 168,963 tokens, 1.64 min | Medium passed 8/8 tests; Max passed 7/7 with no quality gain |
| Final matched build | Stock Sol High: 2,672,996 tokens, 10.06 min | Automatic router: 2,722,738 tokens, 10.88 min | Router correctly stayed direct, but did not earn a quality promotion over stock |

The complete table, negative results, test counts, human judgments, and limitations are in [the benchmark record](docs/ADAPTIVE-ROUTING-BENCHMARK-20260901.md).

### September 2 decision benchmark

| Question | Lean route | Added mechanism | What happened |
| --- | ---: | ---: | --- |
| Two-worker crossover | Direct Sol: 335,605 tokens, 220 sec | Sol + 2 Luna: 1,714,210 tokens, 483 sec | Workers used 5.108× tokens, took 2.193× longer, and needed repair |
| Mandatory debugging/TDD | Stock Sol: 236,099 tokens, 89 sec | Debug/TDD: 475,674 tokens, 108 sec | Both passed 20/20; ceremony used 2.015× tokens with no quality gain |
| Consequential judge gate | No gate: 3,704,402 tokens, 327 sec | 3 judges + Sol: 8,231,527 tokens, 557 sec | Gate cost 2.222× tokens but uniquely caught two technical defects |

The [September 2 benchmark record](docs/ROUTING-BENCHMARK-20260902.md) contains the frozen methodology, limitations, and promotion decisions.

## Requirements

- Windows 11 or a supported Windows release
- Codex CLI and Codex desktop access
- Saved ChatGPT subscription authentication
- Python 3.13 or newer. Python 3.11 and 3.12 cannot satisfy the required Codex Windows verifier sandbox gate.
- Git
- PowerShell 7 recommended

MochiCode does not require an API key and does not add an MCP server. It does not fall back to paid API billing.

## Install

### From Git

```powershell
git clone https://github.com/anawhiskers/mochicode-auto.git
cd mochicode-auto
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -ConfirmInstall -DirectFirst
```

The installer creates a timestamped backup before changing an existing installation. Updating an existing copy is explicit:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -UpdateExisting -ConfirmInstall -DirectFirst
```

Run the doctor after installation:

```powershell
python .\scripts\mochicode.py doctor
```

Then start a fresh Codex task with **GPT-5.6 Sol at High** and enter your normal request. No routing phrase is needed.

## Other coding agents

The portable adapter can merge the model-neutral workflow into the documented Markdown instruction file for Codex, Claude, Kimi, Z.ai, or another coding agent. Audit first:

```powershell
pwsh -NoProfile -File .\portable\install\agent-sync.ps1 -Agent claude
pwsh -NoProfile -File .\portable\install\agent-sync.ps1 -Agent kimi
pwsh -NoProfile -File .\portable\install\agent-sync.ps1 -Agent zai
pwsh -NoProfile -File .\portable\install\agent-sync.ps1 -Agent generic -Target C:\path\to\AGENT-INSTRUCTIONS.md
```

Apply only after reviewing the proposed target and backup location:

```powershell
pwsh -NoProfile -File .\portable\install\agent-sync.ps1 -Agent generic -Target C:\path\to\AGENT-INSTRUCTIONS.md -Apply -Confirm
```

The adapter does not copy credentials, login state, hooks, MCP endpoints, local plugin paths, or provider-specific model selections.

## Safety and stopping rules

- One writer owns each file or shared state at a time.
- Children never spawn grandchildren.
- Workers receive bounded paths, acceptance checks, and evidence requirements.
- Repeated failures change method instead of looping forever.
- Human judgment is final for visual quality, usability, comprehension, and enjoyment.
- Internal tool or child failures trigger recovery, not a false whole-project blocker.
- Production, deployment, spending, destructive work, and difficult-to-reverse actions remain human gates.
- The experimental controller never automatically merges into the source branch.

## Validate locally

```powershell
python -m unittest tests.test_routing tests.test_adaptive_config tests.test_agent_adapter tests.test_capabilities
python -m unittest tests.test_package.PackageTests.test_portable_python_launchers_select_one_executable
python .\scripts\mochicode.py doctor
git diff --check
```

Official release ZIPs are built from the tagged commit by GitHub Actions and receive a Sigstore-backed GitHub artifact attestation. Verify the publisher and exact artifact before installation:

```powershell
gh attestation verify .\MochiCode-Auto-<version>.zip -R anawhiskers/mochicode-auto
```

The attestation proves which repository, workflow, and commit produced the ZIP. It does not prove the code is vulnerability-free, so the package manifest and local verifier remain required.

## Updating the evidence

Open a [workflow result](https://github.com/anawhiskers/mochicode-auto/issues/new?template=workflow-result.yml) when you test a new route. Include the original task, model and effort for every proven participant, acceptance result, total tokens, wall time, rework, integration defects, and human preference when applicable.

Routing changes should be promoted only after a paired comparison shows equal or better accepted quality. Cost and speed break ties after quality passes.

## Project run

Put the exact request in a temporary text file outside the target repository, then run:

```powershell
python scripts\mochicode.py run --project C:\path\to\repo --goal-file C:\path\to\goal.md --backend codex
```

Inspect or control it with:

```powershell
python scripts\mochicode.py status --run-root C:\path\to\run --verbose
python scripts\mochicode.py stop --run-root C:\path\to\run
python scripts\mochicode.py resume --run-root C:\path\to\run --continue-run --backend codex
```

## Learning

The learning store records bounded outcome metadata and lessons in separate append-only hash chains. Raw goals and prompts are rejected. A verified failure and recovery pair creates a fixed-taxonomy candidate. Candidates never enter ordinary model prompts; only an explicit trial can expose one, and the real runner records both the expected applicability and whether the provider actually applied it. Eligible trial evidence is copied into the trusted local learning store and bound to the SHA-256 of the Codex model-call receipt. Promotion reopens that receipt and verifies its bytes, backend, role, lesson state, exit status, timeout, and stop state; zero-cost stub or fabricated evidence cannot promote a lesson. Promotion requires a recorded positive recurrence, a separate successful negative-control task where the lesson stayed inactive, and either explicit human approval or two independent positive runs. Tampered chains are never retrieved or exported, and exports contain only redacted active lessons with known evidence references. Active lessons cannot change tests, criteria, verifier commands, budgets, retries, or safety gates. This is durable, evidence-gated prompt memory, not model-weight training.

```powershell
python scripts\mochicode.py lessons list --json
python scripts\mochicode.py run --project C:\path\to\repo --goal-file C:\path\to\trial.md --backend codex --lesson-trial LESSON_ID --lesson-expected true
python scripts\mochicode.py run --project C:\path\to\repo --goal-file C:\path\to\negative-control.md --backend codex --lesson-trial LESSON_ID --lesson-expected false
python scripts\mochicode.py lessons promote LESSON_ID --evidence POSITIVE_REF1 --evidence POSITIVE_REF2 --negative-control-evidence NEGATIVE_REF
python scripts\mochicode.py lessons retire LESSON_ID --reason "superseded"
```

Detailed design and research decisions are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/RESEARCH.md](docs/RESEARCH.md).

Private Google Drive package sync uses the boundary described in [docs/CLOUD-SYNC.md](docs/CLOUD-SYNC.md).

## Status

Version `0.1.3` is an experimental beta. Direct Sol, bounded native workers, typed child receipts, and selective verification are usable. The deterministic controller remains opt-in and unpromoted.

Licensed under the [MIT License](LICENSE).
