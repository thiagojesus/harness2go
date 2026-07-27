#!/usr/bin/env python3
"""
vscode2opencode — convert a VS Code (GitHub Copilot Chat) session into a
real OpenCode session (rows inserted into opencode.db).

Usage:
  vscode2opencode.py list [--vscode-user-dir DIR]
  vscode2opencode.py convert <session-uuid-or-path> [--directory DIR]
                             [--opencode-db PATH] [--dry-run]

Notes:
  - See vscode_common.py for how VS Code's chat session format is decoded
    and normalized. Scope: text/markdown, thinking, and tool invocations
    only — VS Code's ~35 other response content kinds (progress messages,
    confirmations, todoList widgets, etc.) are UI chrome and are dropped.
  - VS Code's own tool names (e.g. `run_in_terminal`, `copilot_readFile`)
    don't match Claude Code's naming convention, so claude2opencode's
    reverse tool-name mapping mostly falls through to its passthrough case
    (lowercased name, input as a free-text description rather than
    structured args) — an accepted limitation of the "core content only"
    scope, not a bug.
  - No-folder ("empty window") VS Code chats have no associated project
    directory; pass --directory explicitly, or it falls back to the
    current working directory with a warning.
  - Like claude2opencode.py, writing to a live application database is
    riskier than writing a plain file, so `convert` always backs up
    opencode.db first and inserts everything in one transaction.
"""

import argparse
import os
import sys

import claude2opencode as co
import vscode_common as vc


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List VS Code chat sessions available to convert")
    p_list.add_argument("--vscode-user-dir", default=vc.DEFAULT_VSCODE_USER_DIR)

    p_conv = sub.add_parser("convert", help="Convert a VS Code chat session into a real OpenCode session")
    p_conv.add_argument("session", help="VS Code session UUID, or a path to its session file")
    p_conv.add_argument("--vscode-user-dir", default=vc.DEFAULT_VSCODE_USER_DIR)
    p_conv.add_argument("--directory", default=None,
                         help="Project directory to attribute the session to "
                              "(required for no-folder VS Code chats; default: cwd)")
    p_conv.add_argument("--opencode-db", default=co.DEFAULT_OPENCODE_DB)
    p_conv.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.cmd == "list":
        for info in vc.list_vscode_sessions(args.vscode_user_dir):
            print(f"{info['session_id']}  {info['last_timestamp']}  "
                  f"{info['directory'] or '(no folder)'}  {info['title']}")
        return

    if args.cmd == "convert":
        path = vc.find_vscode_session_path(args.session, args.vscode_user_dir)
        directory = args.directory or vc.find_vscode_session_directory(path, args.vscode_user_dir)
        if not directory:
            directory = os.getcwd()
            print(f"No project folder associated with this VS Code session — using {directory!r} "
                  "(pass --directory to override)")

        turns = vc.parse_vscode_session(path)
        session_row, messages, parts = co.build_opencode_session(directory, turns, co.detect_opencode_version())
        session_id = co.write_opencode_session(args.opencode_db, session_row, messages, parts, args.dry_run)
        print(f"Session {args.session!r}: {len(messages)} messages, {len(parts)} parts "
              f"-> OpenCode session {session_id!r} "
              f"({'dry-run, not written' if args.dry_run else args.opencode_db})")
        return


if __name__ == "__main__":
    main()
