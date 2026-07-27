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
from urllib.parse import unquote

DEFAULT_VSCODE_USER_DIR = os.path.expanduser("~/Library/Application Support/Code/User")


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
