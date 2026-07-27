#!/usr/bin/env python3
"""
opencode2vscode — convert an OpenCode session into a real VS Code
(GitHub Copilot Chat) session, visible and openable in VS Code's Chat view.

Usage:
  opencode2vscode.py list [--db PATH]
  opencode2vscode.py convert <session-id> [--db PATH]
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
    if this OpenCode project has never been opened in VS Code, the session
    is written as a no-folder ("empty window") chat instead of inventing a
    new workspace.
  - Scope is "core content only", the same as the read direction: text,
    reasoning (-> VS Code "thinking"), and tool calls. OpenCode's own tool
    names/inputs are carried through as a human-readable summary in the
    tool invocation's message, since VS Code's serialized format doesn't
    have a slot for raw structured tool arguments.
"""

import argparse

from . import opencode2claude as oc
from . import vscode_common as vc


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=oc.DEFAULT_DB, help="Path to opencode.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List OpenCode sessions available to convert")

    p_conv = sub.add_parser("convert", help="Convert an OpenCode session into a real VS Code chat session")
    p_conv.add_argument("session_id", help="OpenCode session id (ses_...)")
    p_conv.add_argument("--vscode-user-dir", default=vc.DEFAULT_VSCODE_USER_DIR)
    p_conv.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    converter = oc.Converter(args.db)

    if args.cmd == "list":
        for row in converter.list_sessions():
            updated = oc.iso_ms(row["time_updated"])
            print(f"{row['id']}  {updated}  {row['directory']}  {row['title']}")
        return

    if args.cmd == "convert":
        session, messages, parts_by_message = converter.load_session(args.session_id)
        directory = session["directory"]
        turns = oc.build_turns_from_opencode(messages, parts_by_message)

        session_id, session_path, scope = vc.write_vscode_session(
            directory, turns, args.vscode_user_dir, dry_run=args.dry_run)
        print(f"Session {args.session_id!r}: {len(turns)} turns -> VS Code session "
              f"{session_id!r} at {session_path!r} ({scope})")
        return


if __name__ == "__main__":
    main()
