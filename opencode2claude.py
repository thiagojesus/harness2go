#!/usr/bin/env python3
"""
opencode2claude — convert an OpenCode session (stored in opencode.db) into a
Claude Code session transcript (.jsonl) that can be listed/resumed by Claude Code.

Usage:
  opencode2claude.py list [--db PATH]
  opencode2claude.py convert <session-id> [--db PATH] [--claude-projects DIR] [--dry-run]
  opencode2claude.py convert --all [--db PATH] [--claude-projects DIR] [--dry-run]

Notes:
  - OpenCode's real conversation data lives in a SQLite DB
    (~/.local/share/opencode/opencode.db), in the `session`, `message` and
    `part` tables. Small per-session JSON files some OpenCode wrappers leave
    lying around (e.g. "ses_xxx.json") are just idle/background-task status
    stubs, not the conversation itself.
  - One OpenCode assistant `message` row == one Claude Code assistant API
    turn (`message.id`/requestId). Its `part` rows (reasoning/text/tool,
    bounded by step-start/step-finish) become one Claude Code JSONL line per
    content block, chained by parentUuid, exactly like a real Claude Code
    transcript.
  - OpenCode "reasoning" parts are converted to plain assistant `text` blocks
    rather than Claude "thinking" blocks. A real Anthropic `thinking` block
    carries a cryptographic signature tying it to that exact API response;
    fabricating one would make the resumed session fail (or get silently
    stripped) the moment Claude Code sends it back to the API. Folding the
    reasoning into visible text keeps the information as context without
    that risk.
  - Tool calls/results are paired: each OpenCode `tool` part becomes a
    `tool_use` block immediately followed by a `user` role `tool_result`
    line, mirroring how Claude Code itself records tool round-trips.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone

DEFAULT_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
DEFAULT_CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CLAUDE_VERSION_FALLBACK = "2.1.220"


def iso_ms(ms):
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def slugify_cwd(cwd):
    return "".join("-" if c in ("/", ".") else c for c in cwd)


def detect_git_branch(directory):
    try:
        out = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        branch = out.stdout.strip()
        return branch if out.returncode == 0 and branch else "HEAD"
    except Exception:
        return "HEAD"


def detect_claude_version():
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.split()[0].strip()
    except Exception:
        pass
    return CLAUDE_VERSION_FALLBACK


def map_tool(part_data):
    """Map an OpenCode tool part to a Claude Code (name, input) pair."""
    tool = part_data.get("tool", "")
    state = part_data.get("state", {})
    inp = state.get("input", {}) or {}

    if tool == "bash":
        return "Bash", {"command": inp.get("command", "")}
    if tool == "read":
        return "Read", {"file_path": inp.get("filePath", "")}
    if tool == "write":
        return "Write", {"file_path": inp.get("filePath", ""), "content": inp.get("content", "")}
    if tool == "edit":
        out = {
            "file_path": inp.get("filePath", ""),
            "old_string": inp.get("oldString", ""),
            "new_string": inp.get("newString", ""),
        }
        if "replaceAll" in inp:
            out["replace_all"] = inp["replaceAll"]
        return "Edit", out
    if tool == "task":
        out = {
            "description": inp.get("description", ""),
            "prompt": inp.get("prompt", ""),
            "subagent_type": inp.get("subagent_type", "general-purpose"),
        }
        if inp.get("run_in_background"):
            out["run_in_background"] = True
        return "Task", out
    if tool == "background_output":
        return "TaskOutput", {"task_id": inp.get("task_id", "")}
    if tool == "question":
        questions = inp.get("questions", [])
        for q in questions:
            q.setdefault("multiSelect", False)
        return "AskUserQuestion", {"questions": questions}
    if tool == "todowrite":
        todos = []
        for td in inp.get("todos", []):
            todos.append({
                "content": td.get("content", ""),
                "status": td.get("status", "pending"),
                "activeForm": td.get("content", ""),
            })
        return "TodoWrite", {"todos": todos}

    # Fallback: pass through unrecognized tools rather than dropping them.
    name = "".join(w.capitalize() for w in tool.split("_")) or "UnknownTool"
    return name, inp


def tool_output_text(part_data):
    state = part_data.get("state", {})
    output = state.get("output")
    if not output:
        output = (state.get("metadata") or {}).get("output")
    return output if output else "(no output)"


def map_stop_reason(finish):
    return {
        "tool-calls": "tool_use",
        "stop": "end_turn",
        "length": "max_tokens",
    }.get(finish, "end_turn")


def build_usage(tokens, cost):
    tokens = tokens or {}
    cache = tokens.get("cache", {}) or {}
    return {
        "input_tokens": tokens.get("input", 0) or 0,
        "output_tokens": tokens.get("output", 0) or 0,
        "cache_creation_input_tokens": cache.get("write", 0) or 0,
        "cache_read_input_tokens": cache.get("read", 0) or 0,
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": None,
    }


class Converter:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def list_sessions(self):
        rows = self.conn.execute(
            "SELECT id, title, directory, time_created, time_updated FROM session "
            "ORDER BY time_updated DESC"
        ).fetchall()
        return rows

    def load_session(self, session_id):
        session = self.conn.execute(
            "SELECT * FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise SystemExit(f"No OpenCode session found with id {session_id!r}")

        messages = self.conn.execute(
            "SELECT id, data, time_created FROM message WHERE session_id = ? "
            "ORDER BY time_created, id",
            (session_id,),
        ).fetchall()

        parts = self.conn.execute(
            "SELECT id, message_id, data, time_created FROM part WHERE session_id = ? "
            "ORDER BY time_created, id",
            (session_id,),
        ).fetchall()

        parts_by_message = {}
        for p in parts:
            parts_by_message.setdefault(p["message_id"], []).append(json.loads(p["data"]))

        return session, messages, parts_by_message


def convert_session(converter, session_id, claude_projects_dir, dry_run=False):
    session, messages, parts_by_message = converter.load_session(session_id)
    directory = session["directory"]
    slug = slugify_cwd(directory)
    project_dir = os.path.join(claude_projects_dir, slug)
    new_session_id = str(uuid.uuid4())
    output_path = os.path.join(project_dir, f"{new_session_id}.jsonl")

    git_branch = detect_git_branch(directory)
    claude_version = detect_claude_version()

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
        obj["gitBranch"] = git_branch
        lines.append(obj)
        prev_uuid = new_uuid

    for m in messages:
        data = json.loads(m["data"])
        role = data.get("role")
        parts = [p for p in parts_by_message.get(m["id"], [])
                 if p.get("type") not in ("step-start", "step-finish")]
        created_ms = data.get("time", {}).get("created", m["time_created"])

        if role == "user":
            text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
            emit({
                "promptId": str(uuid.uuid4()),
                "type": "user",
                "message": {"role": "user", "content": text},
                "timestamp": iso_ms(created_ms),
            })
            continue

        if role != "assistant" or not parts:
            continue

        message_id = m["id"]
        request_id = f"req_{uuid.uuid4().hex[:24]}"
        model_field = data.get("modelID", "unknown")
        stop_reason = map_stop_reason(data.get("finish"))
        usage = build_usage(data.get("tokens"), data.get("cost"))

        for part in parts:
            ptype = part.get("type")
            if ptype in ("text", "reasoning"):
                block = {"type": "text", "text": part.get("text", "")}
            elif ptype == "tool":
                tool_name, tool_input = map_tool(part)
                block = {
                    "type": "tool_use",
                    "id": part.get("callID", str(uuid.uuid4())),
                    "name": tool_name,
                    "input": tool_input,
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
                    "content": [block],
                    "stop_reason": stop_reason,
                    "stop_sequence": None,
                    "stop_details": None,
                    "usage": usage,
                    "diagnostics": None,
                },
                "requestId": request_id,
                "timestamp": iso_ms(created_ms),
            })

            if ptype == "tool":
                is_error = part.get("state", {}).get("status") == "error"
                tool_result = {
                    "tool_use_id": block["id"],
                    "type": "tool_result",
                    "content": tool_output_text(part),
                }
                if is_error:
                    tool_result["is_error"] = True
                end_ms = part.get("state", {}).get("time", {}).get("end", created_ms)
                emit({
                    "promptId": str(uuid.uuid4()),
                    "type": "user",
                    "message": {"role": "user", "content": [tool_result]},
                    "timestamp": iso_ms(end_ms),
                })

    print(f"Session {session_id!r} ({session['title']!r}): {len(lines)} lines "
          f"-> {output_path}")

    if dry_run:
        return output_path, lines

    os.makedirs(project_dir, exist_ok=True)
    with open(output_path, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    return output_path, lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to opencode.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List OpenCode sessions available to convert")

    p_conv = sub.add_parser("convert", help="Convert one or all OpenCode sessions")
    p_conv.add_argument("session_id", nargs="?", help="OpenCode session id (ses_...)")
    p_conv.add_argument("--all", action="store_true", help="Convert every session in the DB")
    p_conv.add_argument("--claude-projects", default=DEFAULT_CLAUDE_PROJECTS,
                         help="Claude Code projects directory (default: ~/.claude/projects)")
    p_conv.add_argument("--dry-run", action="store_true", help="Don't write files, just report")

    args = parser.parse_args()
    converter = Converter(args.db)

    if args.cmd == "list":
        for row in converter.list_sessions():
            updated = iso_ms(row["time_updated"])
            print(f"{row['id']}  {updated}  {row['directory']}  {row['title']}")
        return

    if args.cmd == "convert":
        if args.all:
            for row in converter.list_sessions():
                convert_session(converter, row["id"], args.claude_projects, args.dry_run)
        elif args.session_id:
            convert_session(converter, args.session_id, args.claude_projects, args.dry_run)
        else:
            parser.error("convert requires a session_id or --all")


if __name__ == "__main__":
    main()
