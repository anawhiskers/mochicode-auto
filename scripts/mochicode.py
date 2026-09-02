#!/usr/bin/env python3
"""Portable entrypoint for the MochiCode Codex controller."""

from __future__ import annotations

from collections.abc import Sequence

from mochicode_core.cli import build_parser, run_cli

def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
