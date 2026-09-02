# Controller commands

All commands use:

```text
python <plugin-root>/scripts/mochicode.py <command>
```

- `doctor`: read-only environment and installation checks.
- `demo`: zero-cost stub state-machine demonstration.
- `run`: start or continue a project task.
- `status`: concise live state from disk. Add `--verbose` for packets, hashes, and usage.
- `stop`: request a boundary-safe stop for one run.
- `resume`: clear the stop request and continue from verified state.

The controller prints a usable recovery command on failure. It never installs dependencies or changes Codex configuration during a run.
