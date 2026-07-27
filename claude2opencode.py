#!/usr/bin/env python3
"""
claude2opencode — the mirror image of opencode2claude.py: convert a Claude
Code session (.jsonl transcript) into a real OpenCode session (rows inserted
into opencode.db), and import Claude Code's MCP servers / subagent
definitions into OpenCode's own config.

Usage:
  claude2opencode.py list [--claude-projects DIR]
  claude2opencode.py convert <session-uuid-or-path> [--opencode-db PATH] [--dry-run]
  claude2opencode.py import-global [--opencode-db PATH]

Notes:
  - Claude Code's transcripts are plain JSONL files under
    ~/.claude/projects/<slugified-cwd>/<session-uuid>.jsonl, one line per
    content block, chained by parentUuid. OpenCode instead stores structured
    rows in a SQLite DB (~/.local/share/opencode/opencode.db): one `session`
    row, one `message` row per API turn, one `part` row per content
    block/tool call (bounded by synthesized step-start/step-finish markers).
  - Writing to a live application database is a materially riskier
    operation than writing plain session files, so `convert` always takes a
    timestamped backup of opencode.db before inserting anything, and does
    the whole insert in a single transaction.
  - Claude `thinking` blocks become OpenCode `reasoning` parts (the
    signature is dropped — it isn't meaningful outside Anthropic's API and
    OpenCode has no equivalent concept). `tool_use`/`tool_result` pairs
    become a single OpenCode `tool` part with `state.output` set from the
    paired result. Known Claude tool names are mapped back to their
    OpenCode equivalents (the exact inverse of opencode2claude.py's
    mapping); unrecognized tools pass through lowercased by name.
  - `import-global` reads Claude Code's user-scope MCP servers
    (~/.claude.json top-level "mcpServers") and global agent files
    (~/.claude/agents/*.md, real full-fidelity copies — these are files the
    user actually wrote) into OpenCode's global config, no session needed.
  - `convert` additionally offers to import the MCP servers configured for
    that session's specific project (Claude's local/project scopes) and any
    project-level Claude agents, into OpenCode's project-level config
    (`<project>/opencode.json(c)` and `<project>/.opencode/agent/*.md` —
    mirroring OpenCode's own global/project convention the same way Claude
    Code mirrors ~/.claude/agents with <project>/.claude/agents).
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime

from harness_common import (
    has_jsonc_comments,
    load_jsonc,
    mask_secret,
    parse_frontmatter,
    prompt_choice,
    prompt_text,
    prompt_yes_no,
    slugify_name,
)

DEFAULT_OPENCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
DEFAULT_CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CLAUDE_JSON_PATH = os.path.expanduser("~/.claude.json")
GLOBAL_CLAUDE_AGENT_DIR = os.path.expanduser("~/.claude/agents")
GLOBAL_OPENCODE_CONFIG_PATH = os.path.expanduser("~/.config/opencode/opencode.jsonc")
GLOBAL_OPENCODE_AGENT_DIR = os.path.expanduser("~/.config/opencode/agent")
OPENCODE_VERSION_FALLBACK = "1.18.5"
MCP_TARGET_SCOPES = ("global", "project")
AGENT_SCOPES = ("global", "local")

SESSION_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def new_opencode_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def to_epoch_ms(timestamp, fallback=None):
    """Accepts either an ISO-8601 string (Claude Code's transcript format)
    or an epoch-millisecond int/float (VS Code's chat session format)."""
    if not timestamp:
        return fallback if fallback is not None else int(time.time() * 1000)
    if isinstance(timestamp, (int, float)):
        return int(timestamp)
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return fallback if fallback is not None else int(time.time() * 1000)


def detect_opencode_version():
    try:
        out = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.split()[0].strip()
    except Exception:
        pass
    return OPENCODE_VERSION_FALLBACK


# ---------------------------------------------------------------------------
# Discovering Claude Code sessions
# ---------------------------------------------------------------------------

def peek_claude_session(path):
    cwd = None
    first_user_text = None
    last_timestamp = None
    try:
        with open(path) as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    d = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if d.get("cwd"):
                    cwd = d["cwd"]
                if d.get("timestamp"):
                    last_timestamp = d["timestamp"]
                if first_user_text is None and d.get("type") == "user" and not d.get("isMeta"):
                    content = d.get("message", {}).get("content")
                    if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
                        first_user_text = content.strip().splitlines()[0][:80]
    except OSError:
        return None
    if cwd is None:
        return None
    return {
        "session_id": os.path.splitext(os.path.basename(path))[0],
        "path": path,
        "cwd": cwd,
        "title": first_user_text or "(no prompt found)",
        "last_timestamp": last_timestamp,
    }


def list_claude_sessions(projects_dir=DEFAULT_CLAUDE_PROJECTS):
    sessions = []
    if not os.path.isdir(projects_dir):
        return sessions
    for project_name in sorted(os.listdir(projects_dir)):
        project_path = os.path.join(projects_dir, project_name)
        if not os.path.isdir(project_path):
            continue
        for fname in sorted(os.listdir(project_path)):
            if not fname.endswith(".jsonl"):
                continue
            info = peek_claude_session(os.path.join(project_path, fname))
            if info:
                sessions.append(info)
    return sessions


def find_claude_session_path(ref, projects_dir=DEFAULT_CLAUDE_PROJECTS):
    if os.path.exists(ref) and ref.endswith(".jsonl"):
        return ref
    if os.path.isdir(projects_dir):
        for project_name in sorted(os.listdir(projects_dir)):
            candidate = os.path.join(projects_dir, project_name, f"{ref}.jsonl")
            if os.path.exists(candidate):
                return candidate
    raise SystemExit(f"No Claude Code session found for {ref!r}")


# ---------------------------------------------------------------------------
# Parsing a transcript into (role, ...) turns
# ---------------------------------------------------------------------------

def extract_tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for block in content:
            if isinstance(block, str):
                pieces.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                pieces.append(block.get("text", ""))
        return "\n".join(pieces)
    return ""


def parse_claude_transcript(path):
    """Returns (directory, turns). `turns` is a list of:
    {"role": "user", "text": ..., "created": iso}
    {"role": "assistant", "message_id":, "model":, "usage":, "stop_reason":,
     "created":, "blocks": [content-block, ...]}
    with each tool_use block annotated in-place with "_result_text"/"_is_error"."""
    directory = None
    turns = []
    tool_results = {}  # tool_use_id -> (text, is_error)

    with open(path) as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                d = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if d.get("cwd"):
                directory = d["cwd"]

            line_type = d.get("type")
            if line_type == "user":
                content = d.get("message", {}).get("content")
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                ):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tool_results[block.get("tool_use_id")] = (
                                extract_tool_result_text(block.get("content")),
                                bool(block.get("is_error")),
                            )
                    continue
                text = content if isinstance(content, str) else extract_tool_result_text(content)
                turns.append({"role": "user", "text": text, "created": d.get("timestamp")})

            elif line_type == "assistant":
                message = d.get("message", {})
                mid = message.get("id")
                if turns and turns[-1].get("role") == "assistant" and turns[-1].get("message_id") == mid:
                    group = turns[-1]
                else:
                    group = {
                        "role": "assistant",
                        "message_id": mid,
                        "model": message.get("model"),
                        "usage": message.get("usage") or {},
                        "stop_reason": message.get("stop_reason"),
                        "created": d.get("timestamp"),
                        "blocks": [],
                    }
                    turns.append(group)
                group["blocks"].extend(message.get("content", []))

    for turn in turns:
        if turn["role"] != "assistant":
            continue
        for block in turn["blocks"]:
            if block.get("type") == "tool_use":
                text, is_error = tool_results.get(block.get("id"), ("(no output)", False))
                block["_result_text"] = text
                block["_is_error"] = is_error

    return directory, turns


# ---------------------------------------------------------------------------
# Reverse tool-name mapping (inverse of opencode2claude.map_tool)
# ---------------------------------------------------------------------------

def reverse_map_tool(name, inp):
    if name == "Bash":
        return "bash", {"command": inp.get("command", "")}
    if name == "Read":
        return "read", {"filePath": inp.get("file_path", "")}
    if name == "Write":
        return "write", {"filePath": inp.get("file_path", ""), "content": inp.get("content", "")}
    if name == "Edit":
        out = {
            "filePath": inp.get("file_path", ""),
            "oldString": inp.get("old_string", ""),
            "newString": inp.get("new_string", ""),
        }
        if "replace_all" in inp:
            out["replaceAll"] = inp["replace_all"]
        return "edit", out
    if name == "Task":
        out = {
            "description": inp.get("description", ""),
            "prompt": inp.get("prompt", ""),
            "subagent_type": inp.get("subagent_type", "general-purpose"),
        }
        if inp.get("run_in_background"):
            out["run_in_background"] = True
        return "task", out
    if name == "TaskOutput":
        return "background_output", {"task_id": inp.get("task_id") or inp.get("agent", "")}
    if name == "AskUserQuestion":
        return "question", {"questions": inp.get("questions", [])}
    if name == "TodoWrite":
        todos = [
            {"content": t.get("content", ""), "status": t.get("status", "pending"), "priority": "medium"}
            for t in inp.get("todos", [])
        ]
        return "todowrite", {"todos": todos}
    return name.lower(), inp


def reverse_stop_reason(stop_reason):
    return {"tool_use": "tool-calls", "end_turn": "stop", "max_tokens": "length"}.get(stop_reason, "stop")


def reverse_map_model(model):
    """Claude Code transcripts only ever carry bare Anthropic model ids, but
    this is also reused by vscode2opencode.py, whose sessions carry
    provider/model strings (e.g. "copilot/auto") or bare non-Anthropic ids
    (e.g. "gpt-5-mini") — so this makes a best-effort provider guess rather
    than assuming Anthropic unconditionally."""
    if not model or model == "<synthetic>":
        return "unknown", "anthropic"
    if "/" in model:
        provider, _, model_id = model.partition("/")
        return model_id, provider
    lowered = model.lower()
    if any(tag in lowered for tag in ("gpt", "o1", "o3", "o4")):
        return model, "openai"
    if "gemini" in lowered:
        return model, "google"
    return model, "anthropic"


# ---------------------------------------------------------------------------
# Building OpenCode rows
# ---------------------------------------------------------------------------

def build_opencode_session(directory, turns, opencode_version):
    session_id = new_opencode_id("ses")
    messages = []  # (id, data)
    parts = []     # (id, message_id, data)
    prev_message_id = None
    tokens_input = tokens_output = tokens_cache_read = tokens_cache_write = 0
    last_model_info = None
    title = None
    first_created = None
    last_created = None

    for turn in turns:
        created_ms = to_epoch_ms(turn.get("created"))
        first_created = created_ms if first_created is None else min(first_created, created_ms)
        last_created = created_ms if last_created is None else max(last_created, created_ms)

        if turn["role"] == "user":
            if title is None and turn.get("text"):
                title = turn["text"].strip().splitlines()[0][:120]
            mid = new_opencode_id("msg")
            messages.append((mid, {"role": "user", "time": {"created": created_ms}}))
            parts.append((new_opencode_id("prt"), mid, {"type": "text", "text": turn.get("text", "")}))
            prev_message_id = mid
            continue

        model_id, provider_id = reverse_map_model(turn.get("model"))
        last_model_info = {"id": model_id, "providerID": provider_id, "variant": "default"}
        usage = turn.get("usage") or {}
        tokens = {
            "total": (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0),
            "input": usage.get("input_tokens", 0) or 0,
            "output": usage.get("output_tokens", 0) or 0,
            "reasoning": 0,
            "cache": {
                "write": usage.get("cache_creation_input_tokens", 0) or 0,
                "read": usage.get("cache_read_input_tokens", 0) or 0,
            },
        }
        tokens_input += tokens["input"]
        tokens_output += tokens["output"]
        tokens_cache_read += tokens["cache"]["read"]
        tokens_cache_write += tokens["cache"]["write"]

        mid = new_opencode_id("msg")
        messages.append((mid, {
            "parentID": prev_message_id,
            "role": "assistant",
            "mode": "build",
            "agent": "build",
            "variant": "default",
            "path": {"cwd": directory, "root": "/"},
            "cost": 0,
            "tokens": tokens,
            "modelID": model_id,
            "providerID": provider_id,
            "time": {"created": created_ms, "completed": created_ms},
            "finish": reverse_stop_reason(turn.get("stop_reason")),
        }))
        prev_message_id = mid

        parts.append((new_opencode_id("prt"), mid, {"type": "step-start"}))
        for block in turn["blocks"]:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                parts.append((new_opencode_id("prt"), mid, {"type": "text", "text": block["text"]}))
            elif btype == "thinking" and block.get("thinking"):
                parts.append((new_opencode_id("prt"), mid, {"type": "reasoning", "text": block["thinking"]}))
            elif btype == "tool_use":
                tool_name, tool_input = reverse_map_tool(block.get("name", ""), block.get("input", {}) or {})
                status = "error" if block.get("_is_error") else "completed"
                parts.append((new_opencode_id("prt"), mid, {
                    "type": "tool",
                    "tool": tool_name,
                    "callID": block.get("id", new_opencode_id("call")),
                    "state": {
                        "status": status,
                        "input": tool_input,
                        "output": block.get("_result_text", "(no output)"),
                        "time": {"start": created_ms, "end": created_ms},
                    },
                }))
        parts.append((new_opencode_id("prt"), mid, {"type": "step-finish"}))

    session_row = {
        "id": session_id,
        "project_id": None,  # filled in by caller once it knows/creates the project row
        "workspace_id": None,
        "parent_id": None,
        "slug": session_id[-12:],
        "directory": directory,
        "path": directory.lstrip("/"),
        "title": title or "(imported from Claude Code)",
        "version": opencode_version,
        "share_url": None,
        "summary_additions": 0,
        "summary_deletions": 0,
        "summary_files": 0,
        "summary_diffs": None,
        "metadata": None,
        "cost": 0,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "tokens_reasoning": 0,
        "tokens_cache_read": tokens_cache_read,
        "tokens_cache_write": tokens_cache_write,
        "revert": None,
        "permission": None,
        "agent": "build",
        "model": json.dumps(last_model_info) if last_model_info else None,
        "time_created": first_created or int(time.time() * 1000),
        "time_updated": last_created or int(time.time() * 1000),
        "time_compacting": None,
        "time_archived": None,
    }
    return session_row, messages, parts


# ---------------------------------------------------------------------------
# Writing into opencode.db
# ---------------------------------------------------------------------------

def ensure_project_row(conn):
    row = conn.execute("SELECT id FROM project LIMIT 1").fetchone()
    if row:
        return row[0]
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO project (id, worktree, vcs, name, icon_url, icon_url_override, icon_color, "
        "time_created, time_updated, time_initialized, sandboxes, commands) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("global", "/", None, None, None, None, None, now, now, None, "[]", "[]"),
    )
    return "global"


def write_opencode_session(db_path, session_row, messages, parts, dry_run=False, backup=shutil.copy2):
    if dry_run:
        print(f"[dry-run] would insert session {session_row['id']!r} "
              f"({len(messages)} messages, {len(parts)} parts) into {db_path}")
        return session_row["id"]

    if os.path.exists(db_path):
        backup_path = f"{db_path}.bak-{int(time.time())}"
        backup(db_path, backup_path)
        print(f"Backed up {db_path} -> {backup_path}")

    conn = sqlite3.connect(db_path)
    try:
        project_id = ensure_project_row(conn)
        session_row = dict(session_row, project_id=project_id)
        cols = list(session_row.keys())
        conn.execute(
            f"INSERT INTO session ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [session_row[c] for c in cols],
        )
        now = int(time.time() * 1000)
        for mid, data in messages:
            conn.execute(
                "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?)",
                (mid, session_row["id"], data.get("time", {}).get("created", now), now, json.dumps(data)),
            )
        for pid, mid, data in parts:
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
                "VALUES (?,?,?,?,?,?)",
                (pid, mid, session_row["id"], now, now, json.dumps(data)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return session_row["id"]


# ---------------------------------------------------------------------------
# Reverse MCP import: Claude Code -> OpenCode
# ---------------------------------------------------------------------------

def read_claude_json():
    if not os.path.exists(CLAUDE_JSON_PATH):
        return {}
    try:
        with open(CLAUDE_JSON_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def find_claude_mcp_servers_user():
    cfg = read_claude_json()
    return dict(cfg.get("mcpServers") or {})


def find_claude_mcp_servers_for_project(directory):
    """Local (~/.claude.json projects[dir]) + project (.mcp.json) scoped
    servers for a specific project — used by `convert`'s session wizard."""
    cfg = read_claude_json()
    servers = {}
    proj = (cfg.get("projects") or {}).get(os.path.abspath(directory)) or {}
    servers.update(proj.get("mcpServers") or {})
    mcp_json_path = os.path.join(directory, ".mcp.json")
    if os.path.exists(mcp_json_path):
        try:
            with open(mcp_json_path) as f:
                project_cfg = json.load(f)
            servers.update(project_cfg.get("mcpServers") or {})
        except (OSError, json.JSONDecodeError):
            pass
    return servers


def build_opencode_mcp_entry(spec, mask=False):
    mcp_type = (spec.get("type") or "stdio").lower()
    if mcp_type in ("http", "sse"):
        entry = {"type": "remote", "url": spec.get("url", ""), "enabled": True}
        headers = spec.get("headers") or {}
        if headers:
            entry["headers"] = {k: (mask_secret(v) if mask else v) for k, v in headers.items()}
        return entry
    command = [spec.get("command", "")] + list(spec.get("args", []) or [])
    entry = {"type": "local", "command": command, "enabled": True}
    env = spec.get("env") or {}
    if env:
        entry["environment"] = {k: (mask_secret(v) if mask else v) for k, v in env.items()}
    return entry


def write_opencode_mcp_config(path, name, entry):
    if os.path.exists(path):
        if has_jsonc_comments(path):
            print(f"  Note: {path} has comments — they will be dropped when rewritten "
                  "(a backup is saved first).")
        backup_path = f"{path}.bak-{int(time.time())}"
        shutil.copy2(path, backup_path)
        print(f"  Backed up {path} -> {backup_path}")
    cfg = load_jsonc(path)
    cfg.setdefault("mcp", {})[name] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    return path


def import_mcp_wizard_reverse(servers, resolve_target_path, default_scope="global"):
    if not servers:
        print("No Claude Code MCP servers found.")
        return
    if not prompt_yes_no(f"Import {len(servers)} MCP server(s) from Claude Code into OpenCode?"):
        return
    for name, spec in servers.items():
        target = spec.get("url") or spec.get("command") or "?"
        print(f"\n- {name} ({spec.get('type', 'stdio')}): {target}")
        if not prompt_yes_no(f"  Import '{name}'?", default=True):
            continue
        scope = prompt_choice("  Scope for this MCP server", MCP_TARGET_SCOPES, default=default_scope)
        config_path = resolve_target_path(scope)
        entry = build_opencode_mcp_entry(spec, mask=False)
        preview_entry = build_opencode_mcp_entry(spec, mask=True)
        print(f"  Writing to {config_path}: {json.dumps({name: preview_entry})}")
        write_opencode_mcp_config(config_path, name, entry)


# ---------------------------------------------------------------------------
# Reverse agent import: Claude Code -> OpenCode
# ---------------------------------------------------------------------------

def find_claude_agent_files(agent_dir):
    defs = {}
    if not os.path.isdir(agent_dir):
        return defs
    for fname in sorted(os.listdir(agent_dir)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(agent_dir, fname)
        with open(path) as f:
            text = f.read()
        frontmatter, body = parse_frontmatter(text)
        slug = slugify_name(frontmatter.get("name") or os.path.splitext(fname)[0])
        defs[slug] = {
            "display_name": frontmatter.get("name", os.path.splitext(fname)[0]),
            "path": path,
            "frontmatter": frontmatter,
            "body": body,
        }
    return defs


def build_opencode_agent_frontmatter(cc_frontmatter):
    lines = []
    description = cc_frontmatter.get("description") or "Imported from a Claude Code agent."
    lines.append(f"description: {description}")
    model = cc_frontmatter.get("model")
    if model:
        provider = "anthropic" if "claude" in str(model).lower() else "opencode"
        lines.append(f"model: {provider}/{model}")
    tools = cc_frontmatter.get("tools")
    tool_names = []
    if isinstance(tools, str):
        tool_names = [t.strip() for t in tools.split(",") if t.strip()]
    elif isinstance(tools, list):
        tool_names = [str(t) for t in tools]
    if tool_names:
        lines.append("tools:")
        for t in tool_names:
            lines.append(f"  {t}: true")
    return lines


def write_opencode_agent_file(target_dir, slug, cc_frontmatter, body):
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"{slug}.md")
    fm_lines = build_opencode_agent_frontmatter(cc_frontmatter)
    with open(path, "w") as f:
        f.write("---\n" + "\n".join(fm_lines) + "\n---\n\n" + body)
    return path


def import_agents_wizard_reverse(agent_defs, resolve_target_dir):
    if not agent_defs:
        print("No Claude Code agent definitions found.")
        return
    if not prompt_yes_no(f"Import {len(agent_defs)} agent(s) from Claude Code into OpenCode?"):
        return
    scope = prompt_choice("Scope for imported agents", AGENT_SCOPES, default="global")
    target_dir = resolve_target_dir(scope)
    for slug, info in agent_defs.items():
        if not prompt_yes_no(f"  Import '{info['display_name']}'?", default=True):
            continue
        path = write_opencode_agent_file(target_dir, slug, info["frontmatter"], info["body"])
        print(f"  Wrote {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_import_wizard(directory):
    print("\n--- MCP servers ---")

    def resolve_mcp_path(scope):
        if scope == "global":
            return GLOBAL_OPENCODE_CONFIG_PATH
        return os.path.join(directory, "opencode.jsonc")

    import_mcp_wizard_reverse(find_claude_mcp_servers_for_project(directory), resolve_mcp_path)

    print("\n--- Agents ---")

    def resolve_agent_dir(scope):
        if scope == "global":
            return GLOBAL_OPENCODE_AGENT_DIR
        return os.path.join(directory, ".opencode", "agent")

    project_agents = find_claude_agent_files(os.path.join(directory, ".claude", "agents"))
    import_agents_wizard_reverse(project_agents, resolve_agent_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List Claude Code sessions available to convert")
    p_list.add_argument("--claude-projects", default=DEFAULT_CLAUDE_PROJECTS)

    p_conv = sub.add_parser("convert", help="Convert a Claude Code session into a real OpenCode session")
    p_conv.add_argument("session", help="Claude Code session UUID, or a path to its .jsonl file")
    p_conv.add_argument("--claude-projects", default=DEFAULT_CLAUDE_PROJECTS)
    p_conv.add_argument("--opencode-db", default=DEFAULT_OPENCODE_DB)
    p_conv.add_argument("--dry-run", action="store_true")
    p_conv.add_argument("--wizard", choices=("auto", "always", "never"), default="auto")

    sub.add_parser("import-global",
                    help="Import Claude Code's user-scope MCP servers and global agents into OpenCode")

    args = parser.parse_args()

    if args.cmd == "list":
        for info in list_claude_sessions(args.claude_projects):
            print(f"{info['session_id']}  {info['last_timestamp']}  {info['cwd']}  {info['title']}")
        return

    if args.cmd == "convert":
        path = find_claude_session_path(args.session, args.claude_projects)
        directory, turns = parse_claude_transcript(path)
        if directory is None:
            raise SystemExit(f"Could not determine the project directory for {path!r}")
        session_row, messages, parts = build_opencode_session(directory, turns, detect_opencode_version())
        session_id = write_opencode_session(args.opencode_db, session_row, messages, parts, args.dry_run)
        print(f"Session {args.session!r}: {len(messages)} messages, {len(parts)} parts "
              f"-> OpenCode session {session_id!r} ({'dry-run, not written' if args.dry_run else args.opencode_db})")

        run_wizard = {
            "always": True,
            "never": False,
            "auto": not args.dry_run and sys.stdin.isatty(),
        }[args.wizard]
        if run_wizard:
            run_import_wizard(directory)
        return

    if args.cmd == "import-global":
        print("--- MCP servers (Claude Code user scope) ---")
        import_mcp_wizard_reverse(find_claude_mcp_servers_user(), lambda scope: (
            GLOBAL_OPENCODE_CONFIG_PATH if scope == "global"
            else os.path.join(prompt_text("  Target project directory", default=os.getcwd()), "opencode.jsonc")
        ))
        print("\n--- Agents (Claude Code global config) ---")
        import_agents_wizard_reverse(find_claude_agent_files(GLOBAL_CLAUDE_AGENT_DIR), lambda scope: (
            GLOBAL_OPENCODE_AGENT_DIR if scope == "global"
            else os.path.join(prompt_text("  Target project directory", default=os.getcwd()), ".opencode", "agent")
        ))
        return


if __name__ == "__main__":
    main()
