#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import tempfile
import unittest

from harness2go import claude2opencode as co
from harness2go import vscode2opencode as m

SCHEMA = """
    CREATE TABLE project (
        id text PRIMARY KEY, worktree text, vcs text, name text, icon_url text,
        icon_url_override text, icon_color text, time_created integer, time_updated integer,
        time_initialized integer, sandboxes text, commands text);
    CREATE TABLE session (
        id text PRIMARY KEY, project_id text, workspace_id text, parent_id text, slug text,
        directory text, path text, title text, version text, share_url text,
        summary_additions integer, summary_deletions integer, summary_files integer,
        summary_diffs text, metadata text, cost real, tokens_input integer, tokens_output integer,
        tokens_reasoning integer, tokens_cache_read integer, tokens_cache_write integer,
        revert text, permission text, agent text, model text,
        time_created integer, time_updated integer, time_compacting integer, time_archived integer);
    CREATE TABLE message (
        id text PRIMARY KEY, session_id text, time_created integer, time_updated integer, data text);
    CREATE TABLE part (
        id text PRIMARY KEY, message_id text, session_id text, time_created integer,
        time_updated integer, data text);
"""


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def make_vscode_session(path, directory_hint=True):
    session = {
        "sessionId": "vs1",
        "requests": [{
            "requestId": "request_1",
            "timestamp": 1000,
            "modelId": "copilot/auto",
            "message": {"text": "how do I connect to postgres?"},
            "result": {"metadata": {"promptTokens": 3, "outputTokens": 4, "resolvedModel": "gpt-5-mini"}},
            "response": [
                {"kind": "toolInvocationSerialized", "toolId": "run_in_terminal", "toolCallId": "c1",
                 "invocationMessage": {"value": "Running psql"}, "resultDetails": {"output": [{"value": "connected"}]}},
                {"value": "Use the DATABASE_URL env var."},
            ],
        }],
    }
    with open(path, "w") as f:
        json.dump(session, f)


class Vscode2OpencodeEndToEnd(unittest.TestCase):
    def test_convert_writes_real_session_reusing_claude2opencode(self):
        with tempfile.TemporaryDirectory() as d:
            session_path = os.path.join(d, "vs1.json")
            make_vscode_session(session_path)
            db_path = os.path.join(d, "opencode.db")
            make_db(db_path)

            from harness2go import vscode_common as vc
            turns = vc.parse_vscode_session(session_path)
            session_row, messages, parts = co.build_opencode_session("/proj", turns, "1.0.0")

            backup_calls = []
            session_id = co.write_opencode_session(
                db_path, session_row, messages, parts,
                backup=lambda s, dst: backup_calls.append(1),
            )
            self.assertEqual(len(backup_calls), 1)

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT directory, model FROM session WHERE id=?", (session_id,)).fetchone()
            self.assertEqual(row[0], "/proj")
            model = json.loads(row[1])
            # resolvedModel ("gpt-5-mini") wins over the raw modelId
            # ("copilot/auto") per vscode_common.parse_vscode_session, and
            # reverse_map_model recognizes "gpt" and attributes it to openai.
            self.assertEqual(model, {"id": "gpt-5-mini", "providerID": "openai", "variant": "default"})

            tool_parts = [json.loads(r[0]) for r in
                          conn.execute("SELECT data FROM part WHERE data LIKE '%tool%'").fetchall()]
            tool_parts = [p for p in tool_parts if p.get("type") == "tool"]
            self.assertEqual(len(tool_parts), 1)
            self.assertEqual(tool_parts[0]["tool"], "run_in_terminal")
            self.assertEqual(tool_parts[0]["state"]["output"], "connected")
            conn.close()


if __name__ == "__main__":
    unittest.main()
