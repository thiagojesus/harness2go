#!/usr/bin/env python3
"""
claude2vscode — convert a Claude Code session into a real VS Code
(GitHub Copilot Chat) session, visible and openable in VS Code's Chat view.

Usage:
  claude2vscode.py list [--claude-projects DIR]
  claude2vscode.py convert <session-uuid-or-path> [--claude-projects DIR]
                           [--vscode-user-dir DIR] [--dry-run]
  claude2vscode.py import-global [--vscode-user-dir DIR]

`import-global` imports Claude Code's user-scope MCP servers
(~/.claude.json top-level "mcpServers") into VS Code's own global MCP
config (<vscode-user-dir>/mcp.json). The two schemas are field-identical
(type/command/args/env, or type/url/headers), so this is close to a
straight copy rather than a real conversion. No session needed.

Agents aren't part of this command on purpose: VS Code natively discovers
~/.claude/agents/*.md directly (it's hardcoded into VS Code's own
agent-discovery list), so there's nothing to import — it already works.

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
import json

from . import claude2opencode as co
from . import vscode_common as vc
from .harness_common import mask_secret, prompt_yes_no


def import_mcp_wizard_to_vscode(servers, vscode_user_dir):
    if not servers:
        print("No MCP servers found in Claude Code's user (global) config.")
        return
    if not prompt_yes_no(f"Import {len(servers)} MCP server(s) from Claude Code into VS Code's global config?"):
        return
    for name, spec in servers.items():
        target = spec.get("url") or spec.get("command") or "?"
        print(f"\n- {name} ({spec.get('type', 'stdio')}): {target}")
        if not prompt_yes_no(f"  Import '{name}'?", default=True):
            continue
        preview = dict(spec)
        if "headers" in preview:
            preview["headers"] = {k: mask_secret(v) for k, v in preview["headers"].items()}
        if "env" in preview:
            preview["env"] = {k: mask_secret(v) for k, v in preview["env"].items()}
        print(f"  Writing to VS Code global mcp.json: {json.dumps({name: preview})}")
        path = vc.write_vscode_global_mcp_config(name, spec, vscode_user_dir)
        print(f"  Wrote {path}")


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

    p_global = sub.add_parser("import-global",
                               help="Import Claude Code's global MCP servers into VS Code")
    p_global.add_argument("--vscode-user-dir", default=vc.DEFAULT_VSCODE_USER_DIR)

    args = parser.parse_args()

    if args.cmd == "import-global":
        print("--- MCP servers (Claude Code user/global config) ---")
        import_mcp_wizard_to_vscode(co.find_claude_mcp_servers_user(), args.vscode_user_dir)
        print("\n--- Agents ---")
        print("Nothing to do: VS Code already reads ~/.claude/agents/*.md directly.")
        return

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
