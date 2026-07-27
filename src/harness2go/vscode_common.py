"""Shared VS Code (GitHub Copilot Chat) session reading.

VS Code persists chat sessions as an append-only "operation log": the first
line is a full JSON snapshot (`kind: 0`), later lines are `Set`/`Push`/
`Delete` patches against explicit object paths (or a plain `.json` file for
sessions that never got past their initial state). This is confirmed
against VS Code's own source (`chatSessionStore.ts` / `objectMutationLog.ts`
in microsoft/vscode) and validated by replaying real session files.

`parse_vscode_session` then normalizes a session's requests/responses into
the same canonical "turns" shape claude2opencode.py's transcript parser
produces — a list of {"role": "user", "text", "created"} and
{"role": "assistant", "message_id", "model", "usage", "stop_reason",
"created", "blocks": [...]} dicts, where each block is {"type": "text" |
"thinking" | "tool_use", ...} — so both vscode2opencode.py and
vscode2claude.py can build on the same normalized representation.

Scope: only text/markdown, thinking, and tool invocations
(toolInvocationSerialized) are mapped — the substantive conversation
content that OpenCode/Claude Code already model. VS Code's chat responses
carry ~35 distinct content-block kinds (progress messages,
mcpServersStarting notices, confirmations, todoList widgets, terminal/
notebook/file-edit blocks, pull requests, ...); everything past the three
above is UI chrome dropped on the floor, the same way opencode2claude.py
drops OpenCode's step-start/step-finish structural markers.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from urllib.parse import unquote

from .harness_common import load_jsonc, mask_secret, parse_frontmatter, slugify_name

DEFAULT_VSCODE_USER_DIR = os.path.expanduser("~/Library/Application Support/Code/User")
CHAT_INDEX_STORAGE_KEY = "chat.ChatSessionStore.index"

# VS Code's own MCP/agent config locations and formats — distinct from the
# session-storage constants above. Confirmed by reading VS Code's actual
# source (mcpConfiguration.ts, workspaceDotMcpDiscovery.ts,
# promptFileLocations.ts in microsoft/vscode):
#
#   - Project-scope MCP servers: `.mcp.json` at the workspace root, using
#     the *exact same* `{"mcpServers": {...}}` shape Claude Code uses for
#     its own project scope — VS Code's own comment literally says "Uses
#     the Claude-style format". Already shared with zero conversion needed;
#     opencode2claude.py/claude2opencode.py's existing project-scope MCP
#     handling already reads/writes this file.
#   - Global-scope MCP servers: `<user_dir>/mcp.json`, VS Code's own
#     `{"servers": {...}}` shape (JSONC — comments/trailing commas allowed).
#     Per-server fields (stdio: type/command/args/env/cwd; remote:
#     type/url/headers) match Claude/OpenCode's shape closely enough to
#     reuse the same entry-building helpers.
#   - Custom agents: `.agent.md` files (frontmatter: name/description/
#     model/tools, among others). VS Code natively discovers
#     `.claude/agents/*.md` (project) and `~/.claude/agents/*.md` (global)
#     directly — again zero conversion needed for that pairing. Its own
#     native folders are `.github/agents/*.md` (project) and
#     `~/.copilot/agents/*.md` (global, note the literal `~` — always
#     resolved with os.path.expanduser).
VSCODE_GLOBAL_AGENT_DIR = os.path.expanduser("~/.copilot/agents")


def _apply_set(state, path, value):
    if not path:
        return value
    cur = state
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value
    return state


def _apply_push(state, path, values, start_index):
    cur = state
    for p in path[:-1]:
        cur = cur[p]
    key = path[-1]
    arr = cur.get(key) if isinstance(cur, dict) else cur[key]
    if arr is None:
        arr = []
    if start_index is not None:
        del arr[start_index:]
    if values:
        arr.extend(values)
    cur[key] = arr
    return state


def replay_operation_log(path):
    """Reads a VS Code chat session file — a plain full-snapshot `.json`,
    or a `.jsonl` operation log — and returns the materialized session dict."""
    if path.endswith(".json"):
        with open(path) as f:
            return json.load(f)

    state = None
    with open(path) as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            entry = json.loads(raw_line)
            kind = entry["kind"]
            if kind == 0:
                state = entry["v"]
            elif kind == 1:
                state = _apply_set(state, entry["k"], entry["v"])
            elif kind == 2:
                _apply_push(state, entry["k"], entry.get("v"), entry.get("i"))
            elif kind == 3:
                _apply_set(state, entry["k"], None)
    if state is None:
        raise ValueError(f"{path}: empty or missing initial entry")
    return state


def _workspace_folder_for_hash(user_dir, ws_hash):
    path = os.path.join(user_dir, "workspaceStorage", ws_hash, "workspace.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    folder_uri = d.get("folder")
    if folder_uri and folder_uri.startswith("file://"):
        return unquote(folder_uri[len("file://"):])
    return None


def _peek(path, directory):
    try:
        state = replay_operation_log(path)
    except (OSError, ValueError, json.JSONDecodeError, KeyError):
        return None
    requests = state.get("requests") or []
    title = state.get("customTitle")
    if not title:
        for r in requests:
            text = (r.get("message") or {}).get("text")
            if text:
                title = text.strip().splitlines()[0][:80]
                break
    last_ts = state.get("lastMessageDate")
    if not last_ts and requests:
        last_ts = requests[-1].get("timestamp")
    return {
        "session_id": state.get("sessionId") or os.path.splitext(os.path.basename(path))[0],
        "path": path,
        "directory": directory,
        "title": title or "(empty)",
        "last_timestamp": last_ts,
        "num_requests": len(requests),
    }


def list_vscode_sessions(user_dir=DEFAULT_VSCODE_USER_DIR):
    sessions = []

    empty_dir = os.path.join(user_dir, "globalStorage", "emptyWindowChatSessions")
    if os.path.isdir(empty_dir):
        for fname in sorted(os.listdir(empty_dir)):
            if fname.endswith((".json", ".jsonl")):
                info = _peek(os.path.join(empty_dir, fname), directory=None)
                if info:
                    sessions.append(info)

    ws_root = os.path.join(user_dir, "workspaceStorage")
    if os.path.isdir(ws_root):
        for ws_hash in sorted(os.listdir(ws_root)):
            chat_dir = os.path.join(ws_root, ws_hash, "chatSessions")
            if not os.path.isdir(chat_dir):
                continue
            directory = _workspace_folder_for_hash(user_dir, ws_hash)
            for fname in sorted(os.listdir(chat_dir)):
                if fname.endswith((".json", ".jsonl")):
                    info = _peek(os.path.join(chat_dir, fname), directory=directory)
                    if info:
                        sessions.append(info)

    return sessions


def find_vscode_session_path(ref, user_dir=DEFAULT_VSCODE_USER_DIR):
    if os.path.exists(ref):
        return ref
    for info in list_vscode_sessions(user_dir):
        if info["session_id"] == ref:
            return info["path"]
    raise SystemExit(f"No VS Code chat session found for {ref!r}")


def _markdown_text(block):
    value = block.get("value")
    if isinstance(value, dict):
        return value.get("value", "")
    if isinstance(value, str):
        return value
    return ""


def _tool_invocation_block(block):
    tool_id = block.get("toolId", "unknown_tool")
    call_id = block.get("toolCallId") or f"call_{tool_id}"
    message = block.get("invocationMessage")
    description = _markdown_text(message) if isinstance(message, dict) else (message or "")

    output_text = ""
    is_error = False
    result_details = block.get("resultDetails")
    if isinstance(result_details, dict):
        is_error = bool(result_details.get("isError"))
        output = result_details.get("output")
        if isinstance(output, list):
            pieces = []
            for o in output:
                if isinstance(o, dict):
                    pieces.append(o.get("value", "") if isinstance(o.get("value"), str) else "")
                elif isinstance(o, str):
                    pieces.append(o)
            output_text = "\n".join(p for p in pieces if p)
        elif isinstance(output, str):
            output_text = output

    return {
        "type": "tool_use",
        "id": call_id,
        "name": tool_id,
        "input": {"description": description},
        "_result_text": output_text or "(no output)",
        "_is_error": is_error,
    }


def parse_vscode_session(path):
    """Returns `turns` in the canonical shape described in this module's
    docstring. The session's project directory (if any) isn't part of this
    return value — look it up via list_vscode_sessions()/find_vscode_session_path()
    matching on `path`, since only the workspaceStorage layout (not the
    session file itself) knows which folder a session belongs to."""
    state = replay_operation_log(path)
    turns = []

    for req in state.get("requests") or []:
        message_text = (req.get("message") or {}).get("text", "")
        created = req.get("timestamp")
        turns.append({"role": "user", "text": message_text, "created": created})

        blocks = []
        for block in req.get("response") or []:
            kind = block.get("kind")
            if kind == "thinking":
                text = block.get("value")
                if isinstance(text, str) and text:
                    blocks.append({"type": "thinking", "thinking": text})
            elif kind == "toolInvocationSerialized":
                blocks.append(_tool_invocation_block(block))
            elif kind is None or kind == "markdownContent":
                text = _markdown_text(block)
                if text:
                    blocks.append({"type": "text", "text": text})
            # else: UI chrome (progress/mcp/confirmation/todoList/...) — dropped.

        result = req.get("result") or {}
        metadata = result.get("metadata") or {}
        model = metadata.get("resolvedModel") or req.get("modelId") or "unknown"
        stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"

        turns.append({
            "role": "assistant",
            "message_id": req.get("requestId") or req.get("responseId"),
            "model": model,
            "usage": {
                "input_tokens": metadata.get("promptTokens", 0) or 0,
                "output_tokens": metadata.get("outputTokens", req.get("completionTokens", 0)) or 0,
            },
            "stop_reason": stop_reason,
            "created": created,
            "blocks": blocks,
        })

    return turns


def find_vscode_session_directory(path, user_dir=DEFAULT_VSCODE_USER_DIR):
    """Best-effort project directory for a session file: None for a
    no-folder ("empty window") session, or the workspace's folder path for
    a per-project one."""
    abs_path = os.path.abspath(path)
    for info in list_vscode_sessions(user_dir):
        if os.path.abspath(info["path"]) == abs_path:
            return info["directory"]
    return None


# ---------------------------------------------------------------------------
# Writing a new VS Code session (OpenCode/Claude Code -> VS Code)
#
# VS Code's Chat view discovers sessions purely through an index stored in
# state.vscdb (its general-purpose settings SQLite store) under the key
# `chat.ChatSessionStore.index` — confirmed by reading chatSessionStore.ts's
# actual load path, which has no directory-scan fallback. So a session file
# alone, correctly placed, is invisible to the UI; the index entry has to be
# written too for it to actually show up and be openable.
#
# That index is cached in memory by a running VS Code window and only read
# fresh on cold start, so writing it while VS Code is open risks the write
# being silently discarded the next time that window saves any chat state.
# write_vscode_session() therefore refuses outright unless VS Code isn't
# running (or the caller is doing a --dry-run), and always backs up
# state.vscdb first — same safety pattern as claude2opencode.py's writes to
# opencode.db.
# ---------------------------------------------------------------------------

def is_vscode_running():
    try:
        result = subprocess.run(
            ["pgrep", "-f", "Visual Studio Code.app/Contents/MacOS/Code"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return True  # can't tell — fail safe, assume running
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return True  # unexpected pgrep exit code — fail safe


def find_workspace_hash_for_directory(directory, user_dir=DEFAULT_VSCODE_USER_DIR):
    """The existing workspaceStorage hash for `directory`, or None. Never
    invents a new hash for a project VS Code hasn't opened before — same
    "only write into what already exists" discipline as the other
    directions' MCP/agent import."""
    if not directory:
        return None
    ws_root = os.path.join(user_dir, "workspaceStorage")
    if not os.path.isdir(ws_root):
        return None
    target = os.path.abspath(directory)
    for ws_hash in os.listdir(ws_root):
        folder = _workspace_folder_for_hash(user_dir, ws_hash)
        if folder and os.path.abspath(folder) == target:
            return ws_hash
    return None


def _state_vscdb_path(user_dir, ws_hash):
    if ws_hash:
        return os.path.join(user_dir, "workspaceStorage", ws_hash, "state.vscdb")
    return os.path.join(user_dir, "globalStorage", "state.vscdb")


def _tool_input_summary(inp):
    if not inp:
        return ""
    if "description" in inp:
        return str(inp["description"])
    if "command" in inp:
        return f"Running: {inp['command']}"
    for key in ("file_path", "filePath"):
        if key in inp:
            return f"File: {inp[key]}"
    return json.dumps(inp)[:200]


def _build_request(user_turn, assistant_turn):
    request_id = f"request_{uuid.uuid4()}"
    response_id = f"response_{uuid.uuid4()}"
    created = user_turn.get("created") or assistant_turn.get("created") or int(time.time() * 1000)
    text = user_turn.get("text", "")

    message = {
        "text": text,
        "parts": [{
            "range": {"start": 0, "endExclusive": len(text)},
            "editorRange": {"startLineNumber": 1, "startColumn": 1,
                             "endLineNumber": 1, "endColumn": len(text) + 1},
            "text": text,
            "kind": "text",
        }],
    }

    response_blocks = []
    for block in assistant_turn.get("blocks", []):
        btype = block.get("type")
        if btype == "text":
            response_blocks.append({"kind": "markdownContent", "value": block.get("text", ""),
                                     "supportThemeIcons": False, "supportHtml": False})
        elif btype == "thinking":
            response_blocks.append({"kind": "thinking", "value": block.get("thinking", ""), "id": ""})
        elif btype == "tool_use":
            entry = {
                "kind": "toolInvocationSerialized",
                "toolId": block.get("name", "unknown_tool"),
                "toolCallId": block.get("id", str(uuid.uuid4())),
                "invocationMessage": {"value": _tool_input_summary(block.get("input", {}))},
                "isComplete": True,
                "isConfirmed": {"type": 1},
            }
            result_text = block.get("_result_text")
            if result_text and result_text != "(no output)":
                entry["resultDetails"] = {"output": [{"value": result_text}],
                                           "isError": bool(block.get("_is_error"))}
            response_blocks.append(entry)

    usage = assistant_turn.get("usage") or {}
    model = assistant_turn.get("model", "unknown")

    return {
        "requestId": request_id,
        "timestamp": created,
        "modelId": model,
        "responseId": response_id,
        "result": {
            "timings": {"firstProgress": 0, "totalElapsed": 0},
            "metadata": {
                "promptTokens": usage.get("input_tokens", 0),
                "outputTokens": usage.get("output_tokens", 0),
                "resolvedModel": model,
            },
            "details": None,
        },
        "followups": [],
        "modelState": {"value": 1, "completedAt": created},
        "contentReferences": [],
        "timeSpentWaiting": 0,
        "completionTokens": usage.get("output_tokens", 0),
        "elapsedMs": 0,
        "modeInfo": {"kind": "agent", "isBuiltin": True, "modeId": "agent",
                     "modeName": "agent", "permissionLevel": "default"},
        "response": response_blocks,
        "message": message,
        "variableData": {"variables": []},
    }


def build_vscode_session(directory, turns, session_id=None):
    """Builds (session_dict, index_entry_dict) from the canonical `turns`
    shape. Pairs each user turn with the assistant turn that follows it into
    one VS Code "request"; an unpaired trailing user or leading assistant
    turn (shouldn't normally happen) still gets a request, with an empty
    counterpart, rather than being silently dropped."""
    session_id = session_id or str(uuid.uuid4())
    requests = []
    pending_user = None
    empty_assistant = {"blocks": [], "usage": {}, "model": "unknown", "created": None}

    for turn in turns:
        if turn["role"] == "user":
            if pending_user is not None:
                requests.append(_build_request(pending_user, empty_assistant))
            pending_user = turn
        else:
            if pending_user is None:
                pending_user = {"text": "", "created": turn.get("created")}
            requests.append(_build_request(pending_user, turn))
            pending_user = None
    if pending_user is not None:
        requests.append(_build_request(pending_user, empty_assistant))

    creation_date = requests[0]["timestamp"] if requests else int(time.time() * 1000)
    last_message_date = requests[-1]["timestamp"] if requests else creation_date
    title = None
    if requests:
        first_text = requests[0]["message"]["text"].strip()
        if first_text:
            title = first_text.splitlines()[0][:120]

    session = {
        "version": 3,
        "requesterUsername": os.environ.get("USER", "user"),
        "responderUsername": "GitHub Copilot",
        "responderAvatarIconUri": {"id": "copilot"},
        "initialLocation": "panel",
        "requests": requests,
        "sessionId": session_id,
        "creationDate": creation_date,
        "isImported": True,
        "lastMessageDate": last_message_date,
        "customTitle": title,
    }

    index_entry = {
        "sessionId": session_id,
        "title": title or "Imported chat",
        "lastMessageDate": last_message_date,
        "timing": {
            "created": creation_date,
            "lastRequestStarted": requests[-1]["timestamp"] if requests else None,
            "lastRequestEnded": last_message_date,
        },
        "initialLocation": "panel",
        "hasPendingEdits": False,
        "isEmpty": len(requests) == 0,
        "isExternal": False,
        "lastResponseState": 1,  # ResponseModelState.Complete
        "workingDirectory": f"file://{directory}" if directory else None,
    }
    return session, index_entry


def _merge_index_entry(vscdb_path, index_entry, backup=shutil.copy2):
    if os.path.exists(vscdb_path):
        backup_path = f"{vscdb_path}.bak-{int(time.time())}"
        backup(vscdb_path, backup_path)

    conn = sqlite3.connect(vscdb_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        row = conn.execute("SELECT value FROM ItemTable WHERE key = ?", (CHAT_INDEX_STORAGE_KEY,)).fetchone()
        try:
            index_data = json.loads(row[0]) if row else {"version": 1, "entries": {}}
        except (json.JSONDecodeError, TypeError):
            index_data = {"version": 1, "entries": {}}
        index_data.setdefault("entries", {})[index_entry["sessionId"]] = index_entry

        conn.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                     (CHAT_INDEX_STORAGE_KEY, json.dumps(index_data)))
        conn.commit()
    finally:
        conn.close()


def write_vscode_session(directory, turns, user_dir=DEFAULT_VSCODE_USER_DIR, dry_run=False,
                          backup=shutil.copy2, running_check=is_vscode_running):
    session, index_entry = build_vscode_session(directory, turns)
    session_id = session["sessionId"]

    ws_hash = find_workspace_hash_for_directory(directory, user_dir)
    if ws_hash:
        session_dir = os.path.join(user_dir, "workspaceStorage", ws_hash, "chatSessions")
        scope = f"workspace ({directory})"
    else:
        session_dir = os.path.join(user_dir, "globalStorage", "emptyWindowChatSessions")
        scope = "no-folder (empty window)"
        if directory:
            print(f"No existing VS Code workspace found for {directory!r} — writing as a "
                  "no-folder chat instead (never inventing a new workspace).")

    session_path = os.path.join(session_dir, f"{session_id}.json")
    vscdb_path = _state_vscdb_path(user_dir, ws_hash)

    if dry_run:
        print(f"[dry-run] would write {session_path}")
        print(f"[dry-run] would register its index entry in {vscdb_path}")
        return session_id, session_path, scope

    if running_check():
        raise SystemExit(
            "VS Code appears to be running. Writing this session's index entry into "
            "state.vscdb while VS Code has it cached in memory risks the write being silently "
            "discarded the next time that window saves any chat state. Close VS Code and try again."
        )

    os.makedirs(session_dir, exist_ok=True)
    with open(session_path, "w") as f:
        json.dump(session, f)

    _merge_index_entry(vscdb_path, index_entry, backup=backup)

    return session_id, session_path, scope


# ---------------------------------------------------------------------------
# Global MCP config: <user_dir>/mcp.json, VS Code's own {"servers": {...}}
# shape. Per-server fields (stdio: type/command/args/env/cwd; remote:
# type/url/headers) are field-identical to Claude Code's own mcpServers
# entries, so a Claude-shaped spec can be written here (or read from here)
# with no translation at all — only OpenCode's shape (command+args merged
# into one list, "environment" instead of "env", "local"/"remote" instead
# of "stdio"/"http") needs real conversion.
# ---------------------------------------------------------------------------

def find_vscode_global_mcp_servers(user_dir=DEFAULT_VSCODE_USER_DIR):
    path = os.path.join(user_dir, "mcp.json")
    cfg = load_jsonc(path)
    return dict(cfg.get("servers") or {})


def write_vscode_global_mcp_config(name, entry, user_dir=DEFAULT_VSCODE_USER_DIR):
    path = os.path.join(user_dir, "mcp.json")
    if os.path.exists(path):
        backup_path = f"{path}.bak-{int(time.time())}"
        shutil.copy2(path, backup_path)
        print(f"  Backed up {path} -> {backup_path}")
    cfg = load_jsonc(path)
    cfg.setdefault("servers", {})[name] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    return path


def opencode_mcp_entry_to_vscode(spec, mask=False):
    mcp_type = (spec.get("type") or "local").lower()
    if mcp_type == "remote":
        entry = {"type": "http", "url": spec.get("url", "")}
        headers = spec.get("headers") or {}
        if headers:
            entry["headers"] = {k: (mask_secret(v) if mask else v) for k, v in headers.items()}
        return entry
    command = spec.get("command") or []
    entry = {"type": "stdio"}
    if command:
        entry["command"] = command[0]
        if len(command) > 1:
            entry["args"] = list(command[1:])
    env = spec.get("environment") or {}
    if env:
        entry["env"] = {k: (mask_secret(v) if mask else v) for k, v in env.items()}
    return entry


def vscode_mcp_entry_to_opencode(spec, mask=False):
    mcp_type = spec.get("type", "stdio")
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


def build_claude_mcp_add_args_from_spec(name, spec, scope, mask=False):
    """Build `claude mcp add` argv from a VS Code/Claude-shaped MCP entry
    (type/command/args/env, or type/url/headers — the two schemas are
    field-identical). Mirrors opencode2claude.build_mcp_add_args, which
    expects OpenCode's combined-command-list shape instead."""
    args = ["claude", "mcp", "add", "--scope", scope]
    mcp_type = (spec.get("type") or "stdio").lower()
    if mcp_type in ("http", "sse"):
        args += ["--transport", "sse" if mcp_type == "sse" else "http"]
        for k, v in (spec.get("headers") or {}).items():
            args += ["--header", f"{k}: {mask_secret(v) if mask else v}"]
        args += [name, spec.get("url", "")]
    else:
        for k, v in (spec.get("env") or {}).items():
            args += ["-e", f"{k}={mask_secret(v) if mask else v}"]
        command = [spec.get("command", "")] + list(spec.get("args", []) or [])
        args += [name, "--"] + command
    return args


# ---------------------------------------------------------------------------
# Custom agents: VS Code's own `.agent.md` files (frontmatter: name/
# description/model/tools, among other VS-Code-specific fields we don't
# need). Project scope: .github/agents/*.md. Global scope: ~/.copilot/agents/*.md.
# (.claude/agents/*.md is natively read by VS Code directly — no conversion
# needed for that pairing at all, so there's no vscode-target writer for it.)
# ---------------------------------------------------------------------------

def build_vscode_agent_frontmatter(source_frontmatter, slug):
    lines = [f"name: {source_frontmatter.get('name') or slug}"]
    description = source_frontmatter.get("description") or "Imported agent."
    lines.append(f"description: {description}")
    model = source_frontmatter.get("model")
    if isinstance(model, list) and model:
        model = model[0]
    if model:
        lines.append(f"model: {model}")
    tools = source_frontmatter.get("tools")
    tool_names = []
    if isinstance(tools, dict):
        tool_names = [k for k, v in tools.items() if v]
    elif isinstance(tools, str):
        tool_names = [t.strip() for t in tools.split(",") if t.strip()]
    elif isinstance(tools, list):
        tool_names = [str(t) for t in tools]
    if tool_names:
        lines.append(f"tools: [{', '.join(tool_names)}]")
    return lines


def write_vscode_agent_file(target_dir, slug, source_frontmatter, body):
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"{slug}.agent.md")
    fm_lines = build_vscode_agent_frontmatter(source_frontmatter, slug)
    with open(path, "w") as f:
        f.write("---\n" + "\n".join(fm_lines) + "\n---\n\n" + body)
    return path


def write_claude_agent_file(target_dir, slug, source_frontmatter, body):
    """Writes a Claude Code-style agent file (flat frontmatter:
    description/model/tools-as-comma-list) from another harness's agent
    frontmatter — used when the source is VS Code's own .agent.md, since
    .claude/agents files themselves need no conversion at all."""
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"{slug}.md")
    lines = [f"name: {slug}"]
    description = source_frontmatter.get("description") or "Imported agent."
    lines.append(f"description: {description}")
    model = source_frontmatter.get("model")
    if isinstance(model, list) and model:
        model = model[0]
    if model:
        lines.append(f"model: {model}")
    tools = source_frontmatter.get("tools")
    tool_names = []
    if isinstance(tools, list):
        tool_names = [str(t) for t in tools]
    elif isinstance(tools, str):
        tool_names = [t.strip() for t in tools.split(",") if t.strip()]
    if tool_names:
        lines.append(f"tools: {', '.join(tool_names)}")
    with open(path, "w") as f:
        f.write("---\n" + "\n".join(lines) + "\n---\n\n" + body)
    return path
