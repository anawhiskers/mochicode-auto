# Safety boundaries

- Inherit Codex host approvals and project instructions. Never weaken them.
- Never spend money, publish, deploy, send externally, or perform destructive work without the authorization that action normally requires.
- Use saved ChatGPT Codex authentication only. Filter API keys, tokens, credentials, passwords, cookies, and auth variables from model and verifier children. Missing subscription access is a blocker, not permission to use paid API fallback.
- Disable child subagent spawning mechanically. The controller is the only recursive boundary.
- Keep Sol and reviewers read-only. Use workspace-write only for Terra’s approved check-authoring step and Luna’s implementation worktree.
- Allow Terra to add focused check files only. It cannot modify or delete an existing test. Protect every repository-resident verifier input and reject any expanded overlap with Luna write paths.
- Run baselines and final verifiers read-only in the Codex OS sandbox with network disabled. Shell and inline interpreter verifier forms are forbidden. Usage errors, empty collections, permission failures, crashes, and timeouts cannot count as a failing baseline.
- Hash protected tests, packet artifacts, and integration identity before and after every relevant boundary. Any drift refuses the packet or final review.
- Track every child process by exact PID and process group. Stop only that process group.
- Store run state, logs, and evidence outside the target repository by default.
- Never merge into the source branch automatically. Retain worktrees and the integration branch for human inspection.
- Do not disable old automatic workflows until this plugin passes a real fresh-task canary. Keep timestamped backups and a guarded one-command rollback. Reject install and restore targets that cross reparse points.
