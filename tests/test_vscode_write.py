#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import tempfile
import unittest

from harness2go import vscode_common as vc
from harness2go import opencode2claude as oc


def sample_turns():
    return [
        {"role": "user", "text": "how do I connect?", "created": 1000},
        {"role": "assistant", "message_id": "m1", "model": "gpt-5-mini",
         "usage": {"input_tokens": 3, "output_tokens": 4}, "stop_reason": "tool_use",
         "created": 2000, "blocks": [
             {"type": "thinking", "thinking": "let me check"},
             {"type": "tool_use", "id": "call1", "name": "run_in_terminal",
              "input": {"command": "psql -h localhost"}, "_result_text": "connected",
              "_is_error": False},
             {"type": "text", "text": "You're connected."},
         ]},
    ]


class BuildVscodeSession(unittest.TestCase):
    def test_pairs_user_and_assistant_into_one_request(self):
        session, index_entry = vc.build_vscode_session("/proj", sample_turns())
        self.assertEqual(len(session["requests"]), 1)
        req = session["requests"][0]
        self.assertEqual(req["message"]["text"], "how do I connect?")
        kinds = [b["kind"] for b in req["response"]]
        self.assertEqual(kinds, ["thinking", "toolInvocationSerialized", "markdownContent"])
        self.assertEqual(index_entry["title"], "how do I connect?")
        self.assertEqual(index_entry["workingDirectory"], "file:///proj")
        self.assertFalse(index_entry["isEmpty"])

    def test_tool_result_becomes_result_details(self):
        session, _ = vc.build_vscode_session("/proj", sample_turns())
        tool_block = session["requests"][0]["response"][1]
        self.assertEqual(tool_block["toolId"], "run_in_terminal")
        self.assertEqual(tool_block["resultDetails"]["output"][0]["value"], "connected")
        self.assertFalse(tool_block["resultDetails"]["isError"])

    def test_no_output_omits_result_details(self):
        turns = [
            {"role": "user", "text": "hi", "created": 1},
            {"role": "assistant", "message_id": "m1", "model": "x", "usage": {},
             "stop_reason": "tool_use", "created": 2, "blocks": [
                 {"type": "tool_use", "id": "c1", "name": "t", "input": {},
                  "_result_text": "(no output)", "_is_error": False}]},
        ]
        session, _ = vc.build_vscode_session("/proj", turns)
        self.assertNotIn("resultDetails", session["requests"][0]["response"][0])

    def test_empty_turns_produces_empty_session(self):
        session, index_entry = vc.build_vscode_session("/proj", [])
        self.assertEqual(session["requests"], [])
        self.assertTrue(index_entry["isEmpty"])

    def test_trailing_user_turn_without_reply_still_becomes_a_request(self):
        turns = [{"role": "user", "text": "unanswered", "created": 1}]
        session, _ = vc.build_vscode_session("/proj", turns)
        self.assertEqual(len(session["requests"]), 1)
        self.assertEqual(session["requests"][0]["message"]["text"], "unanswered")
        self.assertEqual(session["requests"][0]["response"], [])


class IsVscodeRunning(unittest.TestCase):
    def test_returns_true_when_pgrep_finds_match(self):
        import unittest.mock as mock
        result = mock.Mock(returncode=0)
        with mock.patch("subprocess.run", return_value=result):
            self.assertTrue(vc.is_vscode_running())

    def test_returns_false_when_pgrep_finds_nothing(self):
        import unittest.mock as mock
        result = mock.Mock(returncode=1)
        with mock.patch("subprocess.run", return_value=result):
            self.assertFalse(vc.is_vscode_running())

    def test_fails_safe_on_error(self):
        import unittest.mock as mock
        with mock.patch("subprocess.run", side_effect=OSError):
            self.assertTrue(vc.is_vscode_running())


class FindWorkspaceHashForDirectory(unittest.TestCase):
    def test_matches_existing_workspace(self):
        with tempfile.TemporaryDirectory() as user_dir:
            ws_dir = os.path.join(user_dir, "workspaceStorage", "hash1")
            os.makedirs(ws_dir)
            with open(os.path.join(ws_dir, "workspace.json"), "w") as f:
                json.dump({"folder": "file:///Users/me/proj"}, f)

            self.assertEqual(vc.find_workspace_hash_for_directory("/Users/me/proj", user_dir), "hash1")
            self.assertIsNone(vc.find_workspace_hash_for_directory("/Users/me/other", user_dir))
            self.assertIsNone(vc.find_workspace_hash_for_directory(None, user_dir))


class WriteVscodeSession(unittest.TestCase):
    SCHEMA = "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"

    def _make_vscdb(self, path, existing_index=None):
        conn = sqlite3.connect(path)
        conn.execute(self.SCHEMA)
        if existing_index is not None:
            conn.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                         (vc.CHAT_INDEX_STORAGE_KEY, json.dumps(existing_index)))
        conn.commit()
        conn.close()

    def test_refuses_when_vscode_is_running(self):
        with tempfile.TemporaryDirectory() as user_dir:
            with self.assertRaises(SystemExit):
                vc.write_vscode_session("/proj", sample_turns(), user_dir,
                                         running_check=lambda: True)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as user_dir:
            session_id, path, scope = vc.write_vscode_session(
                "/proj", sample_turns(), user_dir, dry_run=True, running_check=lambda: True)
            self.assertFalse(os.path.exists(path))
            self.assertEqual(scope, "no-folder (empty window)")

    def test_writes_session_file_and_merges_index_preserving_existing_entries(self):
        with tempfile.TemporaryDirectory() as user_dir:
            global_dir = os.path.join(user_dir, "globalStorage")
            os.makedirs(global_dir)
            vscdb_path = os.path.join(global_dir, "state.vscdb")
            self._make_vscdb(vscdb_path, existing_index={
                "version": 1, "entries": {"pre-existing": {"sessionId": "pre-existing", "title": "old"}}})

            backup_calls = []
            session_id, session_path, scope = vc.write_vscode_session(
                "/proj", sample_turns(), user_dir, running_check=lambda: False,
                backup=lambda s, d: backup_calls.append((s, d)))

            self.assertEqual(len(backup_calls), 1)
            self.assertTrue(os.path.exists(session_path))
            with open(session_path) as f:
                written = json.load(f)
            self.assertEqual(written["sessionId"], session_id)

            conn = sqlite3.connect(vscdb_path)
            row = conn.execute("SELECT value FROM ItemTable WHERE key=?",
                               (vc.CHAT_INDEX_STORAGE_KEY,)).fetchone()
            index_data = json.loads(row[0])
            conn.close()
            self.assertIn("pre-existing", index_data["entries"])  # not clobbered
            self.assertIn(session_id, index_data["entries"])

    def test_uses_matched_workspace_scope_not_global(self):
        with tempfile.TemporaryDirectory() as user_dir:
            ws_dir = os.path.join(user_dir, "workspaceStorage", "hash1")
            os.makedirs(ws_dir)
            with open(os.path.join(ws_dir, "workspace.json"), "w") as f:
                json.dump({"folder": "file:///proj"}, f)
            self._make_vscdb(os.path.join(ws_dir, "state.vscdb"))

            session_id, session_path, scope = vc.write_vscode_session(
                "/proj", sample_turns(), user_dir, running_check=lambda: False)
            self.assertIn("workspaceStorage/hash1/chatSessions", session_path)
            self.assertEqual(scope, "workspace (/proj)")


class BuildTurnsFromOpencode(unittest.TestCase):
    def test_matches_canonical_shape_consumed_by_vscode_common(self):
        messages = [
            {"id": "m1", "time_created": 100,
             "data": json.dumps({"role": "user", "time": {"created": 100}})},
            {"id": "m2", "time_created": 200,
             "data": json.dumps({"role": "assistant", "modelID": "claude-opus-4-8",
                                  "finish": "tool-calls", "time": {"created": 200},
                                  "tokens": {"input": 1, "output": 2}})},
        ]
        parts_by_message = {
            "m1": [{"type": "text", "text": "hi"}],
            "m2": [
                {"type": "reasoning", "text": "thinking..."},
                {"type": "tool", "tool": "bash", "callID": "c1",
                 "state": {"status": "completed", "input": {"command": "ls"}, "output": "a.txt"}},
            ],
        }
        turns = oc.build_turns_from_opencode(messages, parts_by_message)
        session, _ = vc.build_vscode_session("/proj", turns)
        self.assertEqual(len(session["requests"]), 1)
        kinds = [b["kind"] for b in session["requests"][0]["response"]]
        self.assertEqual(kinds, ["thinking", "toolInvocationSerialized"])
        self.assertEqual(session["requests"][0]["response"][1]["toolId"], "Bash")


if __name__ == "__main__":
    unittest.main()
