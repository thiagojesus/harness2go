#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vscode_common as vc


def write_jsonl(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class ReplayOperationLog(unittest.TestCase):
    def test_initial_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            write_jsonl(path, [{"kind": 0, "v": {"sessionId": "x", "requests": []}}])
            state = vc.replay_operation_log(path)
            self.assertEqual(state["sessionId"], "x")

    def test_set_updates_nested_path(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            write_jsonl(path, [
                {"kind": 0, "v": {"requests": [{"message": {"text": "hi"}}]}},
                {"kind": 1, "k": ["requests", 0, "message", "text"], "v": "hello world"},
            ])
            state = vc.replay_operation_log(path)
            self.assertEqual(state["requests"][0]["message"]["text"], "hello world")

    def test_push_appends_and_truncates(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            write_jsonl(path, [
                {"kind": 0, "v": {"requests": [{"a": 1}]}},
                {"kind": 2, "k": ["requests"], "v": [{"a": 2}, {"a": 3}]},
            ])
            state = vc.replay_operation_log(path)
            self.assertEqual(len(state["requests"]), 3)
            self.assertEqual(state["requests"][2]["a"], 3)

    def test_push_with_truncate_index(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            write_jsonl(path, [
                {"kind": 0, "v": {"items": [1, 2, 3]}},
                {"kind": 2, "k": ["items"], "v": [9], "i": 1},
            ])
            state = vc.replay_operation_log(path)
            self.assertEqual(state["items"], [1, 9])

    def test_delete_sets_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            write_jsonl(path, [
                {"kind": 0, "v": {"foo": "bar"}},
                {"kind": 3, "k": ["foo"]},
            ])
            state = vc.replay_operation_log(path)
            self.assertIsNone(state["foo"])

    def test_plain_json_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            with open(path, "w") as f:
                json.dump({"sessionId": "y", "requests": []}, f)
            state = vc.replay_operation_log(path)
            self.assertEqual(state["sessionId"], "y")

    def test_empty_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            open(path, "w").close()
            with self.assertRaises(ValueError):
                vc.replay_operation_log(path)


class ListAndFindSessions(unittest.TestCase):
    def _make_user_dir(self, d):
        empty_dir = os.path.join(d, "globalStorage", "emptyWindowChatSessions")
        os.makedirs(empty_dir)
        with open(os.path.join(empty_dir, "empty1.json"), "w") as f:
            json.dump({"sessionId": "empty1", "requests": [
                {"message": {"text": "no folder question"}, "timestamp": 1000}]}, f)

        ws_hash = "abc123"
        ws_dir = os.path.join(d, "workspaceStorage", ws_hash)
        os.makedirs(os.path.join(ws_dir, "chatSessions"))
        with open(os.path.join(ws_dir, "workspace.json"), "w") as f:
            json.dump({"folder": "file:///Users/me/myproj"}, f)
        with open(os.path.join(ws_dir, "chatSessions", "proj1.json"), "w") as f:
            json.dump({"sessionId": "proj1", "requests": [
                {"message": {"text": "project question"}, "timestamp": 2000}]}, f)

        return d

    def test_list_finds_both_scopes(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_user_dir(d)
            sessions = vc.list_vscode_sessions(d)
            by_id = {s["session_id"]: s for s in sessions}
            self.assertIsNone(by_id["empty1"]["directory"])
            self.assertEqual(by_id["proj1"]["directory"], "/Users/me/myproj")
            self.assertEqual(by_id["proj1"]["title"], "project question")

    def test_find_session_path_and_directory(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_user_dir(d)
            path = vc.find_vscode_session_path("proj1", d)
            self.assertTrue(path.endswith("proj1.json"))
            self.assertEqual(vc.find_vscode_session_directory(path, d), "/Users/me/myproj")
            self.assertIsNone(vc.find_vscode_session_directory(
                vc.find_vscode_session_path("empty1", d), d))

    def test_find_missing_session_raises(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_user_dir(d)
            with self.assertRaises(SystemExit):
                vc.find_vscode_session_path("nope", d)


class ParseVscodeSession(unittest.TestCase):
    def test_core_content_kept_chrome_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            session = {
                "sessionId": "s1",
                "requests": [{
                    "requestId": "request_abc",
                    "timestamp": 1234,
                    "modelId": "copilot/auto",
                    "message": {"text": "what tools do you have?"},
                    "result": {"metadata": {"promptTokens": 10, "outputTokens": 20,
                                             "resolvedModel": "gpt-5-mini"}},
                    "response": [
                        {"kind": "mcpServersStarting", "didStartServerIds": []},
                        {"kind": "thinking", "value": "let me think", "id": "x"},
                        {"kind": "toolInvocationSerialized", "toolId": "copilot_readFile",
                         "toolCallId": "call1",
                         "invocationMessage": {"value": "Reading file.py"},
                         "resultDetails": {"output": [{"value": "file contents"}]}},
                        {"value": "Here's my answer."},
                    ],
                }],
            }
            with open(path, "w") as f:
                json.dump(session, f)

            turns = vc.parse_vscode_session(path)
            self.assertEqual(turns[0], {"role": "user", "text": "what tools do you have?", "created": 1234})

            assistant = turns[1]
            self.assertEqual(assistant["message_id"], "request_abc")
            self.assertEqual(assistant["model"], "gpt-5-mini")
            self.assertEqual(assistant["usage"], {"input_tokens": 10, "output_tokens": 20})
            self.assertEqual(assistant["stop_reason"], "tool_use")

            types = [b["type"] for b in assistant["blocks"]]
            self.assertEqual(types, ["thinking", "tool_use", "text"])  # mcpServersStarting dropped

            tool_block = assistant["blocks"][1]
            self.assertEqual(tool_block["name"], "copilot_readFile")
            self.assertEqual(tool_block["id"], "call1")
            self.assertEqual(tool_block["_result_text"], "file contents")
            self.assertFalse(tool_block["_is_error"])

    def test_no_tool_calls_means_end_turn(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            session = {"sessionId": "s2", "requests": [{
                "requestId": "r1", "timestamp": 1,
                "message": {"text": "hi"},
                "response": [{"value": "hello"}],
            }]}
            with open(path, "w") as f:
                json.dump(session, f)
            turns = vc.parse_vscode_session(path)
            self.assertEqual(turns[1]["stop_reason"], "end_turn")


if __name__ == "__main__":
    unittest.main()
