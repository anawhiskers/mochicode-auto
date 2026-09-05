# MochiCode Auto architecture

```text
ordinary Codex prompt
        |
        +-- running selected Astra -> direct Astra authority, preserve selected effort
        |       `-- same bounded Luna/Terra support; no automatic manager layer
        |
        +-- trivial or one-step -> answer directly
        |
        +-- substantive or tightly coupled -> direct selected authority
        |
        +-- small or sequential work -> direct selected authority
        |
        +-- sizable independent implementation leaf -> proven Luna Medium child
        |
        +-- frozen disjoint leaves + predicted saving -> authority-led two-to-three-worker fan-out
        |
        +-- explicit Manager Mode implementation -> bounded Manager Mode beta
        |       +-- authority manager: goal, phase order, one replan, final judgment
        |       +-- one direct non-spawning Sol High child: sequential phase implementation
        |       `-- manager ledger: child + parent receipts, rotate, park, stop, resume
        |
        `-- explicit experimental controller only
                |
                +-- Sol Max: binding product/architecture plan with wave-one vertical slice
                +-- Terra: mechanically derived packet contract and executable checks
        +-- controller: immutable failing baseline plus protected hashes
                +-- Luna Max by default: one durably reserved bounded attempt
                +-- controller: read-only network-disabled OS-sandboxed verification
                +-- Terra: fresh read-only GREEN review of one immutable packet commit
                +-- controller: accept, rotate, park, block, or replan once
                `-- Sol: final review bound to the reverified integration identity
```

MochiCode Auto owns route selection automatically from the user's goal. Manager Mode beta activates only for an explicit Manager Mode implementation request; automatic candidates are classified in shadow until matched promotion. The heavier deterministic controller is unpromoted and never entered automatically.

Native policy depth is one, current selected authority to child. Children must not delegate. Ordinary native restrictions are behavioral instructions unless host tool denial is verified; configuration alone does not prove enforcement. Eight active children is a host ceiling, while automatic waves start with two and cap at three until larger waves earn benchmark promotion. The installed runtime is capability-probed before unsupported global agent defaults are written.

Manager Mode uses one direct non-spawning Sol High child across sequential phases. If child creation fails before writing, ownership stays with the current selected authority parent. Its lightweight state contains no raw goal or transcript, only hashes and bounded phase, writer, revision, receipt, usage, and status metadata.

## Release boundary

The supported release is Windows-only. The release CLI, model backend, and verifier refuse non-Windows hosts before selecting or starting the POSIX/Linux supervisor. `posix_supervisor.py` is experimental containment code and is unreachable from the release entry points.

## Experimental controller trust boundaries

- The planner cannot edit the project.
- The executor cannot choose or edit acceptance criteria, protected checks, verifier commands, or the reviewer rubric.
- The reviewer cannot edit the project and never receives executor reasoning or trajectory.
- The controller is the only intended writer of trusted run state and the hash-chained evidence ledger. On resume, persisted state is reconciled against the hash-bound Sol plan, receipts, attempt reservations, acceptance evidence, and pinned Git identities.
- Controller child Codex runs are marked as MochiCode children and request subagent spawning disabled. Verify the host honors those controls before claiming mechanical enforcement; these controller settings do not establish enforcement for ordinary native children.
- Model child environments filter API keys, provider tokens, credentials, passwords, cookies, and authentication variables. The release requires saved ChatGPT subscription authentication; missing subscription access blocks instead of falling back to an API key or API billing.
- Configuration may choose models and budgets, but workflow data cannot remove required gates or define arbitrary loops.

## State model

Packet states are distinct: pending, running, accepted, already satisfied, refused, failed, parked, and blocked. Run states include running, stopped, complete, blocked, refused, and budget exhausted.

State and evidence live under a plugin-owned user data directory. Run creation pins the source branch and HEAD plus the integration HEAD. Resume and finalization refuse source drift, integration drift, dirty integration worktrees, state-plan substitution, and unreviewed mutations. The source working tree is not changed by controller integration. Reviewed packet commits may be merged into the controller-owned integration branch, which is retained for human review; the controller never merges that branch into the source branch.

## Scheduling

The queue is dependency-aware and breadth-first. A failed packet moves behind independent peers in the same wave. Every Luna invocation has a durable reservation before execution. Two consumed implementation attempts park the packet, including crash-interrupted attempts. A repeated diff-plus-verifier fingerprint parks it immediately. Parked or blocked work cannot prevent independent packets from advancing. One Sol replan is available only after the ready queue is exhausted.

## Verification

Terra supplies executable argument arrays rather than shell pipelines. Shell launchers and inline interpreter code are refused. Repository verifier scripts and check inputs must be protected and cannot overlap Luna write paths. Protected measurement inputs are SHA-256 hashed around the baseline, implementation, verification, review, and final-integration boundaries. Workspace fingerprints and path checks cover protected tests and harnesses plus Python startup-hook paths. A baseline exit of zero means already satisfied and is not implementation progress. Expected assertion failure is valid red. Usage errors, empty collections, permission failures, crashes, and timeouts are refusals.

Every baseline and final verifier runs through a Codex OS sandbox with read-only workspace access, network disabled, and sensitive environment variables filtered. Protected inputs and workspace fingerprints are checked around every baseline, verifier, Luna, review, and final-integration boundary. Terra reviews the committed packet identity. Sol reviews the exact integration HEAD and fingerprint that passed final re-verification. A model verdict cannot override a failed hard gate.

Model and verifier children use exact tracked `Popen` process identities. On Windows, each child is assigned to a kill-on-close Windows Job Object, and stop or timeout cleanup terminates and verifies that exact contained process set. The controller does not use process-name, port, or broad process sweeps.

A `STOP` request is honored at controller boundaries. Resume is allowed only after persisted evidence, the hash-bound plan, attempt reservations, and pinned source and integration identities reconcile. A final Sol `MERGE` is a recommendation, not an automatic source-branch merge, so the human remains the merge gate.
