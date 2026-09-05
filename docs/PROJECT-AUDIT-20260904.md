# MochiCode project audit

Historical pre-release audit, now included in v0.1.5. Repairs were isolated from the original checkout and installed plugin during testing.

## Findings and repairs

| Area | Confirmed problem | Repair |
| --- | --- | --- |
| Model switching | Explicit context/compaction/effort were overwritten along with the model; empty config defaults could end up inside a table | Preserve existing context and compatible effort, insert root settings before tables, refuse unsafe multiline replacements |
| Model capability reporting | Disposable-home catalog was described as account access; malformed/duplicate records and numeric limits could mislead callers | Expose catalog provenance and unverified account access, reject ambiguous/malformed data, preserve the actual selected session |
| Adapter | Line-based parsing could report nested settings or comments as selected values; duplicate managed markers were ambiguous | Parse root TOML, sanitize reported settings, refuse ambiguous markers, handle unavailable catalog processes |
| Routing | Astra authority returned to Sol in subordinate instructions; generic and Manager schemas conflicted | Use current selected authority, keep intentionally pinned child roles, select schema by route |
| Manager resume | Stored receipt deletion/modification was ignored; input files were reread after validation | Reopen and verify stored hashes; archive exactly the validated byte snapshot |
| Manager stop | Finishing an active phase during a pause could turn the run into needs_replan; completed runs could be reopened by stop/resume | Preserve pause across completion/failure, derive status from remaining work, keep complete terminal |
| Crash recovery | Pending transactions did not record which evidence record they extended | Bind v2 pending transactions to the predecessor and reject stale replay |
| Reporting | Accepted-phase token totals could be mistaken for whole-run usage; impossible token subtotals were allowed | Expose usage_scope and reject cached/reasoning subtotals above totals |
| Package and release | A self-consistent package could lack core skills; an unvalidated tag could publish an attested archive | Require core files and run reusable validation on the tag before the publish job |

## What this does not prove

Manager receipts remain reports supplied by a trusted parent. Hashes detect changes relative to retained evidence; they do not authenticate authors or protect against someone rewriting the entire local store. Parent-side command execution and actual Git/protected-file verification are still necessary. Native limits remain behavioral unless the host actually enforces tool denial.

Existing settled Manager v1 state remains readable. An in-flight v1 pending transaction should be settled with the previous version before upgrading; it lacks the predecessor binding required by the new recovery format. Do not delete pending evidence to force recovery.

This audit does not promote Manager Mode, Astra as a default, or any cost-saving model split. Historical benchmark numbers remain historical. The cost recommendation and rate arithmetic are in [Astra cost controls](ASTRA-COST-CONTROLS.md); no new model-quality benchmark was run for these repairs.

## Verification

- Manager, child receipts, output schemas, learning, scheduler, state/evidence, process safety, contracts, backend configuration, and CLI help: 74 tests passed.
- Combined routing, role, config, capability, adapter, receipt, and instruction checks: 89 tests passed.
- Package unit fixture: build, ZIP/folder integrity, and refusal of a missing core skill despite a consistent rewritten manifest passed. One earlier expanded fixture timed out; no timeout was counted as a pass.

- Installer integration: catalog-missing refusal with rollback, catalog-present explicit Astra selection, clean installation, role delivery, and preservation of surrounding AGENTS content: both tests passed in 91.267 seconds.

GitHub workflow changes are locally inspectable but require a future authorized remote run to demonstrate CI execution. No publication, production change, API spending, or global model preference change is part of these local repairs.
