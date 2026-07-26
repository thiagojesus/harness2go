#!/usr/bin/env python3
"""
harness2go — swiss-army knife for migrating coding-agent harness sessions
(and their MCP/agent config) between OpenCode and Claude Code.

Usage:
  harness2go.py opencode2claude <list|convert|import-global> ...
  harness2go.py claude2opencode <list|convert|import-global> ...

This is a thin dispatcher; run `harness2go.py <direction> --help` for the
full set of options for that direction. Each direction also works as a
standalone script (opencode2claude.py / claude2opencode.py) for anyone who
only ever migrates one way.
"""

import sys

import claude2opencode
import opencode2claude

DIRECTIONS = {
    "opencode2claude": opencode2claude.main,
    "claude2opencode": claude2opencode.main,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in DIRECTIONS:
        print(__doc__)
        print(f"Available directions: {', '.join(DIRECTIONS)}")
        sys.exit(1 if len(sys.argv) >= 2 else 0)

    direction = sys.argv[1]
    sys.argv = [f"{sys.argv[0]} {direction}"] + sys.argv[2:]
    DIRECTIONS[direction]()


if __name__ == "__main__":
    main()
