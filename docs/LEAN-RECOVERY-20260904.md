# Lean recovery candidate, 2026-09-04

Historical pre-release test record, incorporated into v0.1.5.

## Changes

- Conditional, one-attempt recovery after repeated implementation failure or
  planning without implementation progress. Product conflicts require resolution.
- Scope-aware verification repairs, measured performance work, and preservation
  of useful tests and working code. No automatic rewrite, merge, PR closure,
  publishing, dependency installation, or background optimization.
- Model changes invite a compatibility comparison, not an automatic preference
  change. Removed the blanket High recommendation for selected Astra parents.
- Reduced managed global rule text from 3422 to 2645 characters (about 23%).
  This measures instruction characters, not token use, latency, or quality.
- Added optional offline recovery_advisor.py. It classifies caller-supplied
  observations; it does not execute work, verify evidence, or grant permission.
- Updated portable provider-neutral policy and ChatGPT copyable instructions.
  No changes to other assistants' live configuration or the ChatGPT website.

## Evidence

Final focused suite: 91 tests passed, exit 0, 31.679 seconds. Modules:
test_recovery_advisor, test_routing, test_agent_adapter, test_chatgpt_assets,
test_adaptive_config, test_manager_state, test_child_receipts,
test_capabilities, test_cli_smoke, plus
test_installer_adaptive.AdaptiveInstallerTests.test_clean_install_contains_catalog_and_plugin_only_upgrader_with_byte_preserved_marker_context.

The existing 9000-byte skill-entrypoint limit remains enforced. Plugin and skill
validators passed before the final narrow review repairs; rerun for packaging.

Independent read-only Sol High review found three issues: an overly broad global
planning trigger, external-gate advice assuming independent work, and an unclear
read-only verification route. The parent accepted all three, fixed them, and ran
the final suite above. The reviewer made no edits. No second full critic loop.

Tests include real CLI invocation for valid/invalid input and research-versus-
implementation, exhausted recovery, product conflict, authority gates, and model
change cases. These are regression and scenario checks, not a controlled coding
quality benchmark. No claim of cost savings or universal quality improvement.

## Activation and rollback

Use the verified portable update wrapper with PowerShell 7 after Codex exits.
The installer creates timestamped backups and records its rollback manifest.
Keep selected model/effort and context/compaction unchanged. Do not edit a running
app's config. Installation and fresh-session pickup must be reported separately.

Retain the previously installed release and its backup for rollback. Do not
delete the old configuration during an update.

## Remaining limits

Native workflow rules remain behavioral, not OS-enforced scheduling. The helper
does not persist a retry budget; its caller must carry recovery_used forward.
No automatic default/skill retirement was promoted. Low/Medium and lighter-skill
quality comparisons remain separate experiments requiring at least two matched
tasks and unchanged safety, project knowledge, and acceptance. Public posting and
live changes outside Codex were not performed.
