# Google Drive and device sync

The only supported cloud target is a private user-owned Google Drive folder. Sync is limited to a redacted device bundle and promoted lessons. It is not live run-state synchronization, and OneDrive is not used as a release target.

Upload only the versioned device-bundle artifacts:

- Current plugin source and guarded installer.
- Role configuration files.
- Current documentation.
- Redacted active lessons.
- Version and SHA-256 file manifest.

Never bundle or sync raw logs, prompts or goals, outcomes, diffs, process or PID data, unredacted candidates, secrets, credentials, tokens, or cookies.

Keep these local per device:

- Active run state and STOP files.
- Raw goals and prompts.
- Process logs and PIDs.
- Source diffs and verifier output.
- Unredacted outcomes and lesson candidates.
- Git packet worktrees and integration branches.
- Verifier-sandbox runtime metadata.
- Secrets, credentials, tokens, and cookies.

Each device installs from a local copy downloaded from Google Drive. Do not run the plugin from a partially downloaded cloud folder and do not share one live run-state directory between devices. MochiCode provides no concurrent state merge.

## New device

1. Confirm Google Drive has finished downloading the complete ZIP or folder.
2. Verify every `MANIFEST.json` byte count and SHA-256 entry.
3. Extract the bundle to a local non-synchronized folder.
4. Open PowerShell in the bundle root.
5. Run `pwsh -NoProfile -File .\plugin\install.ps1 -Source .\plugin`.
6. Start a fresh Codex task and use a normal substantive project prompt.
7. Run the installed controller's `doctor` and zero-cost `demo` before a real project.

## Lesson sync

Only promoted lessons are exported. Scope, tags, raw goals, outcomes, diffs, logs, secrets, and credentials are omitted. Evidence references must resolve to known hash-chained outcome records. From the device-bundle root, `Update-RedactedLessons.ps1` writes `learning/active-lessons.redacted.json`.

The current CLI exports but does not automatically import lessons. Another device may inspect the verified redacted export, but no text should be trusted if either learning hash chain or the bundle manifest fails validation.
