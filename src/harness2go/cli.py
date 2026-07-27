#!/usr/bin/env python3
"""
h2go (harness2go) — swiss-army knife for migrating coding-agent harness
sessions (and their MCP/agent config) between OpenCode, Claude Code, and
VS Code (GitHub Copilot Chat).

Usage:
  h2go opencode2claude <list|convert|import-global> ...
  h2go claude2opencode <list|convert|import-global> ...
  h2go vscode2opencode <list|convert> ...
  h2go vscode2claude   <list|convert> ...
  h2go opencode2vscode <list|convert> ...
  h2go claude2vscode   <list|convert> ...

This is a thin dispatcher; run `h2go <direction> --help` for the full set
of options for that direction. Each direction is also installed as its
own standalone command (opencode2claude / claude2opencode /
vscode2opencode / vscode2claude / opencode2vscode / claude2vscode) for
anyone who only ever migrates one way.
"""

import os
import sys

from . import claude2opencode
from . import claude2vscode
from . import opencode2claude
from . import opencode2vscode
from . import vscode2claude
from . import vscode2opencode

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
    prog = os.path.basename(sys.argv[0])
    sys.argv = [f"{prog} {direction}"] + sys.argv[2:]
    DIRECTIONS[direction]()


if __name__ == "__main__":
    main()
