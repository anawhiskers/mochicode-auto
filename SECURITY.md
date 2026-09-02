# Security policy

Please do not disclose suspected credential exposure, command injection, path traversal, unsafe process termination, or sandbox escape in a public issue.

Use GitHub's private vulnerability reporting for this repository. Include affected versions, reproduction steps, expected boundaries, and the smallest safe proof. Do not include live credentials or unrelated private data.

The installer and controller must preserve exact path containment, backup and rollback, credential filtering, tracked process identity, protected-input hashing, and explicit human gates for destructive or external effects.

Official release ZIPs are built by the tagged GitHub Actions workflow and carry a GitHub artifact attestation. Consumers should verify it with `gh attestation verify <zip> -R anawhiskers/mochicode-auto` before running the package verifier or installer.
