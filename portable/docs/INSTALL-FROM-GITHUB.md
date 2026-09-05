# Install this release on another computer

Use the v0.1.6 release ZIP, not a copied personal config directory. Official
release assets are built by GitHub Actions from the tag after validation.

On Windows, use PowerShell 7, Python 3.13+, Git, Codex CLI, and local ChatGPT
subscription sign-in. The Windows installer is not a cross-platform installer.
Other agents/platforms use the documented portable policy adapter as supported.

Download into a new local directory:

```powershell
gh release download v0.1.6 --repo anawhiskers/mochicode-auto --pattern 'MochiCode-Auto-0.1.6.zip'
gh attestation verify .\MochiCode-Auto-0.1.6.zip --repo anawhiskers/mochicode-auto
```

If provenance verification fails, stop installation rather than bypassing it.
Extract to a fresh folder, inspect the README, and run the extracted
`verify-package.ps1 -PackageRoot <extracted-folder> -Quiet` in PowerShell 7.

Inspect the destination's existing Codex setup. Back up the config, global
AGENTS, affected roles and skills. Preserve local authentication, endpoints,
permissions, project trust, and unrelated instructions. Do not copy any such
data from another computer.

While Codex desktop is closed, run the extracted `install.ps1 -ConfirmInstall`
for a new installation, or `update.ps1 -ConfirmUpdate` for an existing MochiCode
installation. Preserve the destination's selected model/effort unless its user
explicitly requests a change. Run the doctor and verify source/cache version and
the next session's loaded skill, not just file presence.

Experimental context management is separate and opt-in. Verify that the host
accepts `-c features.context_management.experimental_mode=true` first. The
Windows `plugin/scripts/context_trial.py` helper previews by default; use
`--config <private-config.toml> --apply` only after the desktop closes. It
preserves unrelated model, context, and compaction values.

Cleanup is scoped to confirmed obsolete MochiCode artifacts. Archive before
retiring, preserve working specialist skills and project-specific instructions,
and verify the replacement on real work. Never blanket-delete skills or alter
another assistant's configuration without explicit scope.
