#!/usr/bin/env python3
"""
opencode2claude — convert an OpenCode session (stored in opencode.db) into a
Claude Code session transcript (.jsonl) that can be listed/resumed by Claude Code.

Usage:
  opencode2claude.py list [--db PATH]
  opencode2claude.py convert <session-id> [--db PATH] [--claude-projects DIR] [--dry-run]
  opencode2claude.py convert --all [--db PATH] [--claude-projects DIR] [--dry-run]
  opencode2claude.py import-global

`import-global` imports OpenCode's *global* MCP servers and agent
definitions into Claude Code without needing any session/opencode.db at
all — it only reads OpenCode's own config files
(~/.config/opencode/opencode.jsonc, ~/.config/opencode/agent/*.md,
~/.config/opencode/oh-my-openagent.json).

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
  - Optionally, after converting a session, an interactive wizard offers to
    import the MCP servers configured for that OpenCode project (via the
    real `claude mcp add` CLI, at local/project/user scope) and to generate
    Claude Code subagent stubs for the agents referenced in the session (at
    project or user scope). Agent stubs are honest placeholders: OpenCode's
    actual system-prompt text for built-in/plugin agents isn't recoverable
    from the session or stored locally in editable form, so the stub only
    carries what's genuinely observable (name, usage count, task
    descriptions, best-effort model mapping) and says so in its body.
  - `import-global` does the same MCP/agent import, but session-independent:
    MCP servers come from OpenCode's global config only, and agents come
    from ~/.config/opencode/agent/*.md (real, full-fidelity definitions the
    user actually wrote — copied verbatim) plus any additional names known
    only via oh-my-openagent.json (stub fidelity, same caveat as above).
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone

DEFAULT_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
DEFAULT_CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CLAUDE_VERSION_FALLBACK = "2.1.220"
OPENCODE_GLOBAL_CONFIG_PATHS = [
    os.path.expanduser("~/.config/opencode/opencode.jsonc"),
    os.path.expanduser("~/.config/opencode/opencode.json"),
]
OH_MY_OPENAGENT_CONFIG_PATH = os.path.expanduser("~/.config/opencode/oh-my-openagent.json")
MCP_SCOPES = ("local", "project", "user")
AGENT_SCOPES = ("local", "global")


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


def strip_jsonc_comments(text):
    """Remove // and /* */ comments from JSONC, respecting string literals
    (so URLs like "https://..." don't get truncated by a naive // strip)."""
    out = []
    i, n = 0, len(text)
    in_string = False
    escape = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def load_jsonc(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = f.read()
    cleaned = strip_jsonc_comments(raw)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)  # trailing commas
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {}


def find_project_config_paths(directory):
    """Walk from `directory` up to $HOME looking for opencode.json(c)."""
    home = os.path.expanduser("~")
    paths = []
    cur = os.path.abspath(directory)
    while True:
        for name in ("opencode.jsonc", "opencode.json"):
            candidate = os.path.join(cur, name)
            if os.path.exists(candidate):
                paths.append(candidate)
        parent = os.path.dirname(cur)
        if cur == home or parent == cur:
            break
        cur = parent
    return list(reversed(paths))  # root-most first, so nearer dir wins last


def find_global_mcp_servers():
    """MCP servers configured in OpenCode's global config only (no project)."""
    servers = {}
    for path in OPENCODE_GLOBAL_CONFIG_PATHS:
        cfg = load_jsonc(path)
        for name, spec in (cfg.get("mcp") or {}).items():
            servers[name] = spec
    return servers


def find_opencode_mcp_servers(directory):
    """MCP servers configured for this OpenCode project: global config,
    overridden by any project-level opencode.json(c) found above `directory`."""
    servers = dict(find_global_mcp_servers())
    for path in find_project_config_paths(directory):
        cfg = load_jsonc(path)
        for name, spec in (cfg.get("mcp") or {}).items():
            servers[name] = spec
    return servers


def load_oh_my_openagent_models():
    """Best-effort agent-name -> model-id map from oh-my-openagent.json, if present."""
    if not os.path.exists(OH_MY_OPENAGENT_CONFIG_PATH):
        return {}
    try:
        with open(OH_MY_OPENAGENT_CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for name, spec in (cfg.get("agents") or {}).items():
        model = spec.get("model")
        if model:
            out[name] = model.split("/")[-1]
    return out


def agent_lookup_key(agent_name):
    """Reduce a display name like 'Sisyphus - ultraworker' to a config key like 'sisyphus'."""
    first_word = agent_name.strip().lower().split()[0] if agent_name.strip() else ""
    return first_word.split("-")[0]


def find_session_agents(messages, parts_by_message):
    """Distinct agents actually referenced in this session: assistant turns
    that ran under a given agent name, plus subagent_type values dispatched
    via `task` tool calls (with their description text, for context)."""
    agents = {}

    def touch(name):
        return agents.setdefault(name, {"count": 0, "descriptions": set()})

    for m in messages:
        data = json.loads(m["data"])
        if data.get("role") == "assistant" and data.get("agent"):
            touch(data["agent"])["count"] += 1
        for part in parts_by_message.get(m["id"], []):
            if part.get("type") == "tool" and part.get("tool") == "task":
                inp = part.get("state", {}).get("input", {}) or {}
                sub = inp.get("subagent_type")
                if sub:
                    entry = touch(sub)
                    entry["count"] += 1
                    if inp.get("description"):
                        entry["descriptions"].add(inp["description"])
    return agents


GLOBAL_AGENT_DIR = os.path.expanduser("~/.config/opencode/agent")


def parse_scalar(v):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def parse_frontmatter(text):
    """Minimal YAML-ish frontmatter parser: flat `key: value` pairs plus one
    level of nested `key:\\n  sub: value` maps — enough for typical OpenCode
    agent markdown files, without pulling in a YAML dependency."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")

    data = {}
    current_key = None
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        if line[:1] in (" ", "\t") and current_key is not None:
            k, sep, v = line.strip().partition(":")
            if sep and isinstance(data.get(current_key), dict):
                data[current_key][k.strip()] = parse_scalar(v.strip())
            continue
        k, sep, v = line.partition(":")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if v == "":
            data[k] = {}
            current_key = k
        else:
            data[k] = parse_scalar(v)
            current_key = None
    return data, body


def find_global_agent_files():
    """Real, full-fidelity agent definitions the user authored themselves as
    ~/.config/opencode/agent/*.md — unlike plugin-bundled agents, these have
    genuine recoverable prompt content."""
    defs = {}
    if not os.path.isdir(GLOBAL_AGENT_DIR):
        return defs
    for fname in sorted(os.listdir(GLOBAL_AGENT_DIR)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(GLOBAL_AGENT_DIR, fname)
        with open(path) as f:
            text = f.read()
        frontmatter, body = parse_frontmatter(text)
        slug = re.sub(r"[^a-z0-9]+", "-", os.path.splitext(fname)[0].lower()).strip("-")
        defs[slug] = {
            "kind": "file",
            "display_name": frontmatter.get("name", os.path.splitext(fname)[0]),
            "path": path,
            "frontmatter": frontmatter,
            "body": body,
        }
    return defs


def find_global_agent_definitions():
    """All agents importable without a session: real markdown files under
    ~/.config/opencode/agent/ (full fidelity) plus any additional names only
    known via oh-my-openagent.json's model-routing config (stub fidelity,
    same caveat as session-based stubs — real files always take precedence)."""
    defs = find_global_agent_files()
    if os.path.exists(OH_MY_OPENAGENT_CONFIG_PATH):
        try:
            with open(OH_MY_OPENAGENT_CONFIG_PATH) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            cfg = {}
        for name, spec in (cfg.get("agents") or {}).items():
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            if slug in defs:
                continue
            model = spec.get("model")
            defs[slug] = {
                "kind": "stub",
                "display_name": name,
                "model": model.split("/")[-1] if model else None,
            }
    return defs


def build_claude_agent_frontmatter(slug, oc_frontmatter):
    lines = [f"name: {slug}"]
    description = oc_frontmatter.get("description") or f"Imported from OpenCode agent '{slug}'."
    lines.append(f"description: {description}")
    model = oc_frontmatter.get("model")
    if model:
        lines.append(f"model: {str(model).split('/')[-1]}")
    tools = oc_frontmatter.get("tools")
    if isinstance(tools, dict):
        enabled = [k for k, v in tools.items() if v]
        if enabled:
            lines.append(f"tools: {', '.join(enabled)}")
    return lines


def write_full_agent_file(target_dir, slug, oc_frontmatter, body):
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"{slug}.md")
    frontmatter_lines = build_claude_agent_frontmatter(slug, oc_frontmatter)
    with open(path, "w") as f:
        f.write("---\n" + "\n".join(frontmatter_lines) + "\n---\n\n" + body)
    return path


def mask_secret(value):
    if not isinstance(value, str) or len(value) <= 8:
        return "***"
    return value[:4] + "…" + value[-2:]


def build_mcp_add_args(name, spec, scope, mask=False):
    """Build the argv for `claude mcp add` from an OpenCode mcp config entry.
    With mask=True, secret-bearing values (headers/env) are redacted, for
    safe display in prompts/logs — never use the masked form to actually run."""
    args = ["claude", "mcp", "add", "--scope", scope]
    mcp_type = (spec.get("type") or "local").lower()

    if mcp_type in ("remote", "http", "sse"):
        transport = "sse" if mcp_type == "sse" else "http"
        args += ["--transport", transport]
        for k, v in (spec.get("headers") or {}).items():
            shown = mask_secret(v) if mask else v
            args += ["--header", f"{k}: {shown}"]
        args += [name, spec.get("url", "")]
    else:
        command = spec.get("command") or []
        if isinstance(command, str):
            command = [command]
        for k, v in (spec.get("environment") or spec.get("env") or {}).items():
            shown = mask_secret(v) if mask else v
            args += ["-e", f"{k}={shown}"]
        args += [name, "--"] + list(command)
    return args


def agents_target_dir(scope, directory):
    if scope == "global":
        return os.path.expanduser("~/.claude/agents")
    return os.path.join(directory, ".claude", "agents")


def write_agent_stub(target_dir, agent_name, description, model=None):
    os.makedirs(target_dir, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-") or "agent"
    path = os.path.join(target_dir, f"{slug}.md")
    frontmatter = [f"name: {slug}", f"description: {description}"]
    if model:
        frontmatter.append(f"model: {model}")
    body = (
        f'Imported from an OpenCode session (agent name: "{agent_name}"). '
        "OpenCode's system-prompt text for this agent lives inside a plugin "
        "bundle and isn't recoverable from the session data, so this is a "
        "stub carrying only what's genuinely observable (usage, task "
        "descriptions, best-effort model). Replace this body with real "
        "instructions before relying on it.\n"
    )
    with open(path, "w") as f:
        f.write("---\n" + "\n".join(frontmatter) + "\n---\n\n" + body)
    return path


def prompt_yes_no(question, default=False):
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def prompt_choice(question, choices, default=None):
    choice_str = "/".join(choices)
    default_hint = f" [{default}]" if default else ""
    while True:
        try:
            ans = input(f"{question} ({choice_str}){default_hint}: ").strip().lower()
        except EOFError:
            return default
        if not ans and default:
            return default
        if ans in choices:
            return ans
        print(f"  Please answer one of: {choice_str}")


def prompt_text(question, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{question}{suffix}: ").strip()
    except EOFError:
        return default
    return ans or default


def import_mcp_wizard(servers, run=subprocess.run, default_scope="local"):
    if not servers:
        print("No MCP servers found in OpenCode config.")
        return
    if not prompt_yes_no(f"Import {len(servers)} MCP server(s) found in OpenCode config into Claude Code?"):
        return
    for name, spec in servers.items():
        target = spec.get("url") or spec.get("command") or "?"
        print(f"\n- {name} ({spec.get('type', 'local')}): {target}")
        if not prompt_yes_no(f"  Import '{name}'?", default=True):
            continue
        scope = prompt_choice("  Scope for this MCP server", MCP_SCOPES, default=default_scope)
        preview = build_mcp_add_args(name, spec, scope, mask=True)
        print(f"  Running: {' '.join(preview)}")
        real_args = build_mcp_add_args(name, spec, scope, mask=False)
        try:
            run(real_args, check=True)
        except Exception as e:
            print(f"  Failed to add '{name}': {e}")


def import_agents_wizard(agents_info, directory, model_map):
    if not agents_info:
        print("No agent usage detected in this session.")
        return
    if not prompt_yes_no(f"Import {len(agents_info)} agent(s) referenced in this session as Claude Code subagent stubs?"):
        return
    scope = prompt_choice("Scope for imported agents", AGENT_SCOPES, default="local")
    target_dir = agents_target_dir(scope, directory)
    for name, info in agents_info.items():
        if not prompt_yes_no(f"  Import agent '{name}' (used {info['count']} time(s))?", default=True):
            continue
        description = " / ".join(sorted(info["descriptions"]))[:500] or f"Imported OpenCode agent '{name}'."
        model = model_map.get(agent_lookup_key(name))
        path = write_agent_stub(target_dir, name, description, model)
        print(f"  Wrote {path}")


def import_global_agents_wizard(agent_defs):
    if not agent_defs:
        print("No global OpenCode agent definitions found "
              f"({GLOBAL_AGENT_DIR}/*.md or {OH_MY_OPENAGENT_CONFIG_PATH}).")
        return
    if not prompt_yes_no(f"Import {len(agent_defs)} agent(s) found in your global OpenCode config?"):
        return
    scope = prompt_choice("Scope for imported agents", AGENT_SCOPES, default="global")
    if scope == "global":
        target_dir = os.path.expanduser("~/.claude/agents")
    else:
        target = prompt_text("  Target project directory", default=os.getcwd())
        target_dir = os.path.join(target, ".claude", "agents")

    for slug, info in agent_defs.items():
        label = info["display_name"]
        kind_label = "full definition" if info["kind"] == "file" else "stub (no local source found)"
        if not prompt_yes_no(f"  Import '{label}' ({kind_label})?", default=True):
            continue
        if info["kind"] == "file":
            path = write_full_agent_file(target_dir, slug, info["frontmatter"], info["body"])
        else:
            path = write_agent_stub(
                target_dir, label,
                f"Imported OpenCode agent '{label}' (from global config, no session context).",
                model=info.get("model"),
            )
        print(f"  Wrote {path}")


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

    result = {
        "output_path": output_path,
        "lines": lines,
        "directory": directory,
        "messages": messages,
        "parts_by_message": parts_by_message,
    }

    if dry_run:
        return result

    os.makedirs(project_dir, exist_ok=True)
    with open(output_path, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    return result


def run_import_wizard(result):
    """Interactively offer to import MCP servers + agents relevant to a
    just-converted session. `result` is convert_session()'s return value."""
    directory = result["directory"]

    print("\n--- MCP servers ---")
    servers = find_opencode_mcp_servers(directory)
    import_mcp_wizard(servers)

    print("\n--- Agents ---")
    agents_info = find_session_agents(result["messages"], result["parts_by_message"])
    model_map = load_oh_my_openagent_models()
    import_agents_wizard(agents_info, directory, model_map)


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
    p_conv.add_argument("--wizard", choices=("auto", "always", "never"), default="auto",
                         help="Offer to import MCP servers/agents after converting "
                              "(auto: only for a single interactive-terminal conversion)")

    sub.add_parser("import-global",
                    help="Import OpenCode's global MCP servers and agent definitions into "
                         "Claude Code, without needing any session/opencode.db")

    args = parser.parse_args()

    if args.cmd == "import-global":
        print("--- MCP servers (global OpenCode config) ---")
        import_mcp_wizard(find_global_mcp_servers(), default_scope="user")
        print("\n--- Agents (global OpenCode config) ---")
        import_global_agents_wizard(find_global_agent_definitions())
        return

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
            result = convert_session(converter, args.session_id, args.claude_projects, args.dry_run)
            run_wizard = {
                "always": True,
                "never": False,
                "auto": not args.dry_run and sys.stdin.isatty(),
            }[args.wizard]
            if run_wizard:
                run_import_wizard(result)
        else:
            parser.error("convert requires a session_id or --all")


if __name__ == "__main__":
    main()
