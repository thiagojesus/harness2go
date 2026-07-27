#!/usr/bin/env python3
"""
vscode2claude — convert a VS Code (GitHub Copilot Chat) session into a
Claude Code session transcript (.jsonl) that Claude Code can list and
`--resume`.

Usage:
  vscode2claude.py list [--vscode-user-dir DIR]
  vscode2claude.py convert <session-uuid-or-path> [--directory DIR]
                           [--claude-projects DIR] [--dry-run]

Notes:
  - See vscode_common.py for how VS Code's chat session format is decoded
    and normalized. Scope: text/markdown, thinking, and tool invocations
    only — VS Code's other ~35 response content kinds (progress messages,
    confirmations, todoList widgets, etc.) are UI chrome and are dropped.
  - VS Code's "thinking" blocks become plain assistant `text` blocks, not
    Claude `thinking` blocks — same reasoning as opencode2claude.py: a real
    Anthropic `thinking` block carries a signature tied to the exact API
    response that produced it, and these turns often aren't even from an
    Anthropic model (VS Code routes through GPT/Gemini/etc. too), so
    fabricating one would risk breaking resume for no benefit.
  - Tool names are passed through verbatim from VS Code's own vocabulary
    (e.g. `copilot_readFile`, `run_in_terminal`) — they don't correspond to
    Claude Code's built-in tools, so no renaming is attempted; Claude Code
    will render them as an unrecognized tool call, which is honest.
  - No-folder ("empty window") VS Code chats have no associated project
    directory; pass --directory explicitly, or it falls back to the
    current working directory with a warning.
"""

import argparse
import json
import os
import sys
import uuid

from .opencode2claude import detect_claude_version, detect_git_branch, iso_ms
from .harness_common import slugify_cwd
from . import vscode_common as vc


def build_claude_transcript(directory, turns, claude_version):
    new_session_id = str(uuid.uuid4())
    lines = []
    prev_uuid = None

    def emit(obj):
        nonlocal prev_uuid
        new_uuid = str(uuid.uuid4())
        obj["parentUuid"] = prev_uuid
        obj["isSidechain"] = False
        obj["uuid"] = new_uuid
        obj["userType"] = "external"
        obj["entrypoint"] = "cli"
        obj["cwd"] = directory
        obj["sessionId"] = new_session_id
        obj["version"] = claude_version
        obj["gitBranch"] = detect_git_branch(directory)
        lines.append(obj)
        prev_uuid = new_uuid

    for turn in turns:
        created_ms = turn.get("created") or 0

        if turn["role"] == "user":
            emit({
                "promptId": str(uuid.uuid4()),
                "type": "user",
                "message": {"role": "user", "content": turn.get("text", "")},
                "timestamp": iso_ms(created_ms) if created_ms else iso_ms(0),
            })
            continue

        # Always synthesize the id (never reuse VS Code's own requestId,
        # e.g. "request_xxxx"): Claude Code sends the last assistant
        # message's `id` back to the API as a `previous_message_id`
        # diagnostic on resume, and the API rejects anything not shaped
        # like a real `msg_`-prefixed response id.
        message_id = f"msg_{uuid.uuid4().hex[:24]}"
        request_id = f"req_{uuid.uuid4().hex[:24]}"
        model_field = turn.get("model", "unknown")
        stop_reason = turn.get("stop_reason", "end_turn")
        usage_in = turn.get("usage") or {}
        usage = {
            "input_tokens": usage_in.get("input_tokens", 0) or 0,
            "output_tokens": usage_in.get("output_tokens", 0) or 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": None,
        }

        for block in turn.get("blocks", []):
            btype = block.get("type")
            if btype in ("text", "thinking"):
                text = block.get("text") if btype == "text" else block.get("thinking", "")
                if not text:
                    continue
                content_block = {"type": "text", "text": text}
            elif btype == "tool_use":
                content_block = {
                    "type": "tool_use",
                    "id": block.get("id", str(uuid.uuid4())),
                    "name": block.get("name", "unknown_tool"),
                    "input": block.get("input", {}),
                }
            else:
                continue

            emit({
                "type": "assistant",
                "message": {
                    "model": model_field,
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [content_block],
                    "stop_reason": stop_reason,
                    "stop_sequence": None,
                    "stop_details": None,
                    "usage": usage,
                    "diagnostics": None,
                },
                "requestId": request_id,
                "timestamp": iso_ms(created_ms) if created_ms else iso_ms(0),
            })

            if btype == "tool_use":
                tool_result = {
                    "tool_use_id": content_block["id"],
                    "type": "tool_result",
                    "content": block.get("_result_text", "(no output)"),
                }
                if block.get("_is_error"):
                    tool_result["is_error"] = True
                emit({
                    "promptId": str(uuid.uuid4()),
                    "type": "user",
                    "message": {"role": "user", "content": [tool_result]},
                    "timestamp": iso_ms(created_ms) if created_ms else iso_ms(0),
                })

    return new_session_id, lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List VS Code chat sessions available to convert")
    p_list.add_argument("--vscode-user-dir", default=vc.DEFAULT_VSCODE_USER_DIR)

    p_conv = sub.add_parser("convert", help="Convert a VS Code chat session into a Claude Code session")
    p_conv.add_argument("session", help="VS Code session UUID, or a path to its session file")
    p_conv.add_argument("--vscode-user-dir", default=vc.DEFAULT_VSCODE_USER_DIR)
    p_conv.add_argument("--directory", default=None,
                         help="Project directory to attribute the session to "
                              "(required for no-folder VS Code chats; default: cwd)")
    p_conv.add_argument("--claude-projects", default=os.path.expanduser("~/.claude/projects"))
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
        _, lines = build_claude_transcript(directory, turns, detect_claude_version())

        slug = slugify_cwd(directory)
        project_dir = os.path.join(args.claude_projects, slug)
        new_session_id = lines[0]["sessionId"] if lines else str(uuid.uuid4())
        output_path = os.path.join(project_dir, f"{new_session_id}.jsonl")

        print(f"Session {args.session!r}: {len(lines)} lines -> {output_path}")
        if args.dry_run:
            return

        os.makedirs(project_dir, exist_ok=True)
        with open(output_path, "w") as f:
            for obj in lines:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        return


if __name__ == "__main__":
    main()
