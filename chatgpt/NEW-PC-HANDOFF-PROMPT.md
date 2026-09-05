# New-computer handoff for ChatGPT and Codex

Use this prompt after signing in to ChatGPT on a new computer and making the portable package available.

First separate the two layers:

1. **ChatGPT account layer:** Apply `CUSTOM-INSTRUCTIONS-COMPACT.txt` only in ChatGPT's Custom Instructions setting. This is account-level behavior text that may sync with the signed-in account. It is not a Codex configuration file, and applying it does not install or configure Codex.
2. **Local Codex layer:** Treat Codex instructions, roles, skills, profiles, and configuration as local files. Inspect the existing local setup, back it up before changes, and merge only portable content. Preserve unrelated local settings, plugins, connectors, trusted projects, runtime paths, and permissions.

For the local layer:

- Use one writer per file or shared state. Preserve the selected parent and effort, including Astra. Sol High is a fresh-install fallback, not a forced override. Use Luna only for sizable independent leaves with hard checks and expected savings. Explicit Manager Mode uses one non-spawning Sol High implementer, deterministic phase state, and independent parent verification. Terra remains optional only in experimental controller work.
- On Windows, use Python 3.13 or newer and PowerShell 7 (`pwsh`). Do not run the verified portable installer in Windows PowerShell 5.1. Keep the app closed during configuration writes.
- Experimental context management is separately opt-in through `features.context_management.experimental_mode = true`; verify support on this laptop before enabling it. Do not copy the first computer's full configuration, runtime paths, credentials, or active task state.
- Validate the resulting configuration and run the platform's supported status or configuration check. Report the backup, changed files, commands, exit codes, evidence, and anything still unknown. Do not restart the app automatically or claim success from a copied file alone.
- Keep secrets, private paths, and computer-specific values out of portable files.

A local installer cannot carry over login state, active sessions, plugin installation state, connector state, or browser permissions. Re-authenticate, reconnect, or install those items locally and verify each one separately. Account sync is not proof of local Codex capability.
