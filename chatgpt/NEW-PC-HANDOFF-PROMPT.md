# New-computer handoff for ChatGPT and Codex

Use this prompt after signing in to ChatGPT on a new computer and making the portable package available.

First separate the two layers:

1. **ChatGPT account layer:** Apply `CUSTOM-INSTRUCTIONS-COMPACT.txt` only in ChatGPT's Custom Instructions setting. This is account-level behavior text that may sync with the signed-in account. It is not a Codex configuration file, and applying it does not install or configure Codex.
2. **Local Codex layer:** Treat Codex instructions, roles, skills, profiles, and configuration as local files. Inspect the existing local setup, back it up before changes, and merge only portable content. Preserve unrelated local settings, plugins, connectors, trusted projects, runtime paths, and permissions.

For the local layer:

- Use one writer per file or shared state. Preserve the direct-work default and use Sol for bounded planning and final judgment, Terra for acceptance and evidence review, and Luna for bounded implementation only when those roles are actually available.
- Validate the resulting configuration and run the platform's supported status or configuration check. Report the backup, changed files, commands, exit codes, and anything still unknown. Do not restart the app automatically or claim success from a copied file alone.
- Keep secrets, private paths, and computer-specific values out of portable files.

A local installer cannot carry over login state, active sessions, plugin installation state, connector state, or browser permissions. Re-authenticate, reconnect, or install those items locally and verify each one separately. Account sync is not proof of local Codex capability.
