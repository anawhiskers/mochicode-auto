# Apply and verify

These assets have two different targets: ChatGPT account-level text and local Codex configuration. Do not treat account text as local configuration.

## ChatGPT account layer

1. Open ChatGPT Settings and the current **Custom Instructions** control.
2. Paste the contents of `CUSTOM-INSTRUCTIONS-COMPACT.txt`, then save.
3. Reload the settings and start a fresh chat. Confirm that the text is still present and that a simple request receives a conclusion-first response.
4. If account sync is part of the goal, inspect the same setting on another signed-in device. This verifies visible account text only, not Codex setup.

## Local Codex layer

1. Provide `NEW-PC-HANDOFF-PROMPT.md` together with the package to the local Codex workflow.
2. Confirm that a backup was made before any local change. Review the changed-file list and configuration validation result.
3. Run the supported local status or configuration check, such as `codex features list` when that command is available. Record its exit code and output summary.

A local installer cannot carry over login state, active sessions, plugin installation state, connector state, or browser permissions. Reconnect those items on the computer where they will be used. Do not claim that account sync, a copied file, or a successful syntax check proves a live integration works.

**Pass:** account text is present, local changes are backed up and validated, and every live integration has its own observed result. Mark anything not tested as unknown.
