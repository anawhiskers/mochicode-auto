# v0.1.5: Astra instruction and configuration cleanup

This release combines the Astra adaptation, Manager/configuration audit,
bounded recovery, and instruction-hygiene work completed on September 4-5, 2026.

## What changed

- Preserve the selected parent model and effort, including Astra. No forced
  model switch, mandatory manager, or recursive delegation.
- Main skill reduced from 8929 to 6785 bytes, about 24%, while retaining privacy,
  authority, bounded recovery, review, and human-acceptance constraints.
- Task-relevant reading and reuse of unchanged context. Affected checks plus
  required repository gates, without repeating green checks unnecessarily.
- Corrected the workflow-upgrader's stale effort/catalog assumptions and removed
  the requirement to introduce a five-role pipeline into direct work.
- Shortened a starter prompt that exceeded Codex's 128-character metadata limit.
- Added an optional, offline recovery advisor and opt-in context-setting helper.
- Fixed Manager pause/resume, stored receipt verification, and pending-record
  binding; configuration migration preserves unrelated and compatible settings.
- Verified-package requirements include the new helpers and core instructions.
  Release publication depends on validation of the exact tag.

## Experimental context setting

The official setting is:

```toml
[features.context_management]
experimental_mode = true
```

It remains opt-in. In a Git checkout:

```powershell
python scripts/context_trial.py --config <private-config.toml>
```

In an extracted release ZIP, the helper is under `plugin/scripts/`.
Preview is read-only. Apply with `--apply` only after Codex desktop closes.
The Windows helper verifies that only this setting changes, holds an exclusive
file handle to prevent competing writes, retains a backup, verifies the write,
and restores on a caught write failure. It refuses ambiguous existing forms.
Abrupt power loss during a write still requires recovery from the retained backup.
Do not redistribute your configuration or its backup.

## Evidence and limits

- 99 focused tests passed in the pre-release local pass, including actual
  competing-process file locking and a clean-install integration test.
- Changed skills, metadata, and PowerShell passed validation.
- Independent Sol High review identified a concurrent-edit race and a dropped
  failure-lesson privacy restriction. Both were repaired and tests rerun.
- A real child run completed with the experimental context flag supplied as a
  temporary override. This is a startup smoke, not a long-context quality test.
- The release's GitHub Actions run validates the tagged code independently.
- No new claim of coding-quality improvement, token savings, or reduced billing.
  The instruction-size reduction is a byte measurement, not a usage benchmark.
- No automatic retirement of specialist skills, expansion of permissions,
  production deployment, or model-preference replacement.
- Low/Medium reasoning comparisons remain separate experiments; the existing
  historical benchmark results were not relabeled as Astra results.

## Sources

- [Original skills/prompt guidance](https://x.com/pvncher/article/2095991462416490862)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Astra model guidance](https://developers.openai.com/api/docs/guides/latest-model)

See the GitHub release for the public source, CI result, verified ZIP, and
artifact provenance. Private machine paths, configuration hashes, credentials,
and local activation logs are deliberately not part of this release.
