# Contributing

Bug reports, portability fixes, benchmark receipts, and narrowly scoped routing improvements are welcome.

## Evidence required for routing changes

A proposed routing change should include:

- the exact original task and fixed acceptance criteria;
- model and reasoning effort for every proven participant;
- before and after accepted quality;
- total tokens and wall time when available;
- retry, repair, and rework counts;
- integration defects and unrelated changes;
- human preference for visual or usability work;
- limitations, failed checks, and infrastructure differences.

Do not promote a workflow because one model self-reported success. Quality must be checked against the same executable or human acceptance bar. Cost and speed matter only after quality is non-inferior.

## Pull requests

Keep changes small and preserve the one-writer, bounded-retry, human-gate, and no-recursive-delegation rules. Run the focused validation commands from the README and include the exact results.
