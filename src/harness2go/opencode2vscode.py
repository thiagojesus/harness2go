#!/usr/bin/env python3
"""
opencode2vscode — convert an OpenCode session into a real VS Code
(GitHub Copilot Chat) session, visible and openable in VS Code's Chat view.

Usage:
  opencode2vscode.py list [--db PATH]
  opencode2vscode.py convert <session-id> [--db PATH]
                             [--vscode-user-dir DIR] [--dry-run]
  opencode2vscode.py import-global [--vscode-user-dir DIR]

`import-global` imports OpenCode's global MCP servers
(~/.config/opencode/opencode.jsonc) into VS Code's own global MCP config
(<vscode-user-dir>/mcp.json — a different schema/location from OpenCode's,
so this is a real conversion, not a passthrough), and OpenCode's global
agent files (~/.config/opencode/agent/*.md) into VS Code's own global
agent folder (~/.copilot/agents/*.md, VS Code's `.agent.md` frontmatter).
No session/opencode.db needed for either.

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
import json

from . import opencode2claude as oc
from . import vscode_common as vc
from .harness_common import prompt_yes_no


def import_mcp_wizard_to_vscode(servers, vscode_user_dir):
    if not servers:
        print("No MCP servers found in OpenCode's global config.")
        return
    if not prompt_yes_no(f"Import {len(servers)} MCP server(s) from OpenCode into VS Code's global config?"):
        return
    for name, spec in servers.items():
        target = spec.get("url") or spec.get("command") or "?"
        print(f"\n- {name} ({spec.get('type', 'local')}): {target}")
        if not prompt_yes_no(f"  Import '{name}'?", default=True):
            continue
        preview = vc.opencode_mcp_entry_to_vscode(spec, mask=True)
        print(f"  Writing to VS Code global mcp.json: {json.dumps({name: preview})}")
        entry = vc.opencode_mcp_entry_to_vscode(spec, mask=False)
        path = vc.write_vscode_global_mcp_config(name, entry, vscode_user_dir)
        print(f"  Wrote {path}")


def import_agents_wizard_to_vscode(agent_defs):
    if not agent_defs:
        print("No OpenCode global agent definitions found.")
        return
    if not prompt_yes_no(f"Import {len(agent_defs)} agent(s) from OpenCode into VS Code?"):
        return
    for slug, info in agent_defs.items():
        label = info["display_name"]
        kind_label = "full definition" if info["kind"] == "file" else "stub (no local source found)"
        if not prompt_yes_no(f"  Import '{label}' ({kind_label})?", default=True):
            continue
        if info["kind"] == "file":
            frontmatter, body = info["frontmatter"], info["body"]
        else:
            frontmatter = {
                "description": f"Imported OpenCode agent '{label}' — no local prompt source found "
                               "(it's a built-in/plugin agent), so this is a stub.",
                "model": info.get("model"),
            }
            body = (f'Imported from OpenCode\'s global config (agent name: "{label}"). '
                    "OpenCode's system-prompt text for this agent lives inside a plugin bundle and "
                    "isn't recoverable, so this is a stub — replace this body with real instructions "
                    "before relying on it.\n")
        path = vc.write_vscode_agent_file(vc.VSCODE_GLOBAL_AGENT_DIR, slug, frontmatter, body)
        print(f"  Wrote {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=oc.DEFAULT_DB, help="Path to opencode.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List OpenCode sessions available to convert")

    p_conv = sub.add_parser("convert", help="Convert an OpenCode session into a real VS Code chat session")
    p_conv.add_argument("session_id", help="OpenCode session id (ses_...)")
    p_conv.add_argument("--vscode-user-dir", default=vc.DEFAULT_VSCODE_USER_DIR)
    p_conv.add_argument("--dry-run", action="store_true")

    p_global = sub.add_parser("import-global",
                               help="Import OpenCode's global MCP servers and agents into VS Code")
    p_global.add_argument("--vscode-user-dir", default=vc.DEFAULT_VSCODE_USER_DIR)

    args = parser.parse_args()

    if args.cmd == "import-global":
        print("--- MCP servers (OpenCode global config) ---")
        import_mcp_wizard_to_vscode(oc.find_global_mcp_servers(), args.vscode_user_dir)
        print("\n--- Agents (OpenCode global config) ---")
        import_agents_wizard_to_vscode(oc.find_global_agent_definitions())
        return

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
