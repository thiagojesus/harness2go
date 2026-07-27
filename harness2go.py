#!/usr/bin/env python3
"""
harness2go — swiss-army knife for migrating coding-agent harness sessions
(and their MCP/agent config) between OpenCode, Claude Code, and VS Code
(GitHub Copilot Chat).

Usage:
  harness2go.py opencode2claude <list|convert|import-global> ...
  harness2go.py claude2opencode <list|convert|import-global> ...
  harness2go.py vscode2opencode <list|convert> ...
  harness2go.py vscode2claude   <list|convert> ...
  harness2go.py opencode2vscode <list|convert> ...
  harness2go.py claude2vscode   <list|convert> ...

This is a thin dispatcher; run `harness2go.py <direction> --help` for the
full set of options for that direction. Each direction also works as a
standalone script (opencode2claude.py / claude2opencode.py /
vscode2opencode.py / vscode2claude.py / opencode2vscode.py /
claude2vscode.py) for anyone who only ever migrates one way.
"""

import sys

import claude2opencode
import claude2vscode
import opencode2claude
import opencode2vscode
import vscode2claude
import vscode2opencode

DIRECTIONS = {
    "opencode2claude": opencode2claude.main,
    "claude2opencode": claude2opencode.main,
    "vscode2opencode": vscode2opencode.main,
    "vscode2claude": vscode2claude.main,
    "opencode2vscode": opencode2vscode.main,
    "claude2vscode": claude2vscode.main,
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
