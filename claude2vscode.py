#!/usr/bin/env python3
"""
claude2vscode — convert a Claude Code session into a real VS Code
(GitHub Copilot Chat) session, visible and openable in VS Code's Chat view.

Usage:
  claude2vscode.py list [--claude-projects DIR]
  claude2vscode.py convert <session-uuid-or-path> [--claude-projects DIR]
                           [--vscode-user-dir DIR] [--dry-run]

Notes:
  - See vscode_common.py for why this needs to write more than just a
    session file: VS Code's Chat view is driven entirely by an index kept
    in state.vscdb, not by scanning the session folder, so a bare file
    would be invisible in the UI. `convert` refuses to run if VS Code is
    currently open (that index is cached in memory by a running window and
    a write could be silently discarded), and always backs up state.vscdb
    first.
  - Only writes into an existing VS Code workspace (matched by directory);
    if this Claude Code project has never been opened in VS Code, the
    session is written as a no-folder ("empty window") chat instead of
    inventing a new workspace.
  - Scope is "core content only", the same as the read direction: text,
    thinking, and tool calls. Claude Code's tool names/inputs are carried
    through as a human-readable summary in the tool invocation's message,
    since VS Code's serialized format doesn't have a slot for raw
    structured tool arguments.
"""

import argparse

import claude2opencode as co
import vscode_common as vc


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List Claude Code sessions available to convert")
    p_list.add_argument("--claude-projects", default=co.DEFAULT_CLAUDE_PROJECTS)

    p_conv = sub.add_parser("convert", help="Convert a Claude Code session into a real VS Code chat session")
    p_conv.add_argument("session", help="Claude Code session UUID, or a path to its .jsonl file")
    p_conv.add_argument("--claude-projects", default=co.DEFAULT_CLAUDE_PROJECTS)
    p_conv.add_argument("--vscode-user-dir", default=vc.DEFAULT_VSCODE_USER_DIR)
    p_conv.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.cmd == "list":
        for info in co.list_claude_sessions(args.claude_projects):
            print(f"{info['session_id']}  {info['last_timestamp']}  {info['cwd']}  {info['title']}")
        return

    if args.cmd == "convert":
        path = co.find_claude_session_path(args.session, args.claude_projects)
        directory, turns = co.parse_claude_transcript(path)
        if directory is None:
            raise SystemExit(f"Could not determine the project directory for {path!r}")

        session_id, session_path, scope = vc.write_vscode_session(
            directory, turns, args.vscode_user_dir, dry_run=args.dry_run)
        print(f"Session {args.session!r}: {len(turns)} turns -> VS Code session "
              f"{session_id!r} at {session_path!r} ({scope})")
        return


if __name__ == "__main__":
    main()
