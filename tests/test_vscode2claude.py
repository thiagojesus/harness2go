#!/usr/bin/env python3
import json
import os
import sys
import unittest

from harness2go import vscode2claude as m


class BuildClaudeTranscript(unittest.TestCase):
    def _turns(self):
        return [
            {"role": "user", "text": "hi", "created": 1000},
            {"role": "assistant", "message_id": "request_abc", "model": "gpt-5-mini",
             "usage": {"input_tokens": 5, "output_tokens": 7}, "stop_reason": "tool_use",
             "created": 2000, "blocks": [
                 {"type": "thinking", "thinking": "pondering"},
                 {"type": "tool_use", "id": "call1", "name": "copilot_readFile",
                  "input": {"description": "reading x"}, "_result_text": "contents",
                  "_is_error": False},
                 {"type": "text", "text": "done"},
             ]},
        ]

    def test_chain_is_valid_and_message_ids_are_msg_prefixed(self):
        # Regression test: Claude Code sends the last assistant message's
        # `id` back to the API as `previous_message_id` on resume, and
        # rejects anything not shaped like a real msg_-prefixed id. VS
        # Code's own requestId ("request_abc") must never leak through.
        _, lines = m.build_claude_transcript("/proj", self._turns(), "2.1.220")

        prev = None
        for line in lines:
            self.assertEqual(line["parentUuid"], prev)
            prev = line["uuid"]
            json.dumps(line)

        assistant_lines = [l for l in lines if l["type"] == "assistant"]
        self.assertTrue(assistant_lines)
        for l in assistant_lines:
            self.assertTrue(l["message"]["id"].startswith("msg_"))
            self.assertNotEqual(l["message"]["id"], "request_abc")

    def test_thinking_becomes_plain_text_block(self):
        _, lines = m.build_claude_transcript("/proj", self._turns(), "2.1.220")
        assistant_lines = [l for l in lines if l["type"] == "assistant"]
        first_content = assistant_lines[0]["message"]["content"][0]
        self.assertEqual(first_content["type"], "text")
        self.assertEqual(first_content["text"], "pondering")

    def test_tool_use_paired_with_tool_result(self):
        _, lines = m.build_claude_transcript("/proj", self._turns(), "2.1.220")
        tool_use_ids = {c["id"] for l in lines if l["type"] == "assistant"
                        for c in l["message"]["content"] if c["type"] == "tool_use"}
        tool_result_ids = {c["tool_use_id"] for l in lines if l["type"] == "user"
                           and isinstance(l["message"]["content"], list)
                           for c in l["message"]["content"] if c["type"] == "tool_result"}
        self.assertEqual(tool_use_ids, tool_result_ids)
        self.assertEqual(tool_use_ids, {"call1"})

    def test_tool_name_passed_through_verbatim(self):
        _, lines = m.build_claude_transcript("/proj", self._turns(), "2.1.220")
        tool_use = [c for l in lines if l["type"] == "assistant"
                    for c in l["message"]["content"] if c["type"] == "tool_use"][0]
        self.assertEqual(tool_use["name"], "copilot_readFile")

    def test_first_user_line_has_no_parent(self):
        _, lines = m.build_claude_transcript("/proj", self._turns(), "2.1.220")
        self.assertIsNone(lines[0]["parentUuid"])
        self.assertEqual(lines[0]["message"]["content"], "hi")


if __name__ == "__main__":
    unittest.main()
