import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import ai_provider


def turn(text="검토용 모의 문안", *, completed=True, tool=False):
    events = [{"type": "thread.started", "thread_id": "test-thread"}, {"type": "turn.started"}]
    if tool:
        events.append({"type": "item.started", "item": {"id": "t", "type": "command_execution"}})
    events.append({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    if completed:
        events.append({"type": "turn.completed"})
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events).encode()


class DraftProviderTests(unittest.TestCase):
    def test_completed_turn_and_local_isolation(self):
        def simulated_cli(args, **options):
            self.assertEqual(args[0], "codex-for-test")
            self.assertIn("--ephemeral", args)
            self.assertIn("--ignore-user-config", args)
            self.assertIn("apps", args)
            self.assertIn("shell_tool", args)
            self.assertIn('permissions.w07_draft.filesystem={":root"="deny",":workspace_roots"="read"}', args)
            self.assertFalse(options["shell"])
            self.assertEqual(list(Path(options["cwd"]).iterdir()), [])
            self.assertIn("한글 소명", options["input"].decode())
            self.assertNotIn("한글 소명", str(args))
            return subprocess.CompletedProcess(args, 0, turn(), b"")

        with patch.object(ai_provider, "_codex_binary", return_value="codex-for-test"), \
                patch.object(ai_provider.subprocess, "run", side_effect=simulated_cli), \
                patch.dict(ai_provider.os.environ, {"W07_AI_MODEL": "fixture-model"}):
            for task in ("summary", "question", "proposal"):
                with self.subTest(task=task):
                    result = ai_provider.generate_text(task, {"answer": "한글 소명"})
                    self.assertEqual(result["text"], "검토용 모의 문안")
                    self.assertEqual(result["request_id"], "test-thread")
                    self.assertEqual(result["model"], "fixture-model")

    def test_invalid_partial_or_tool_events_never_yield_a_draft(self):
        for data in [b"{", turn(completed=False), turn(text="  "), turn(tool=True),
                     b'{"type":"turn.failed"}', b'{"type":"thread.started","thread_id":"partial"}']:
            with self.subTest(data=data), self.assertRaises(ai_provider.ProviderError):
                ai_provider._read_turn(data)

    def test_provider_failure_is_sanitized_and_never_returns_synthetic_text(self):
        with patch.object(ai_provider, "_codex_binary", return_value="codex-for-test"), \
                patch.object(ai_provider.subprocess, "run") as call:
            call.return_value = subprocess.CompletedProcess(["codex-for-test"], 1, b"", b"PRIVATE-DIAGNOSTIC")
            with self.assertRaises(ai_provider.ProviderError) as failure:
                ai_provider.generate_text("question", {})
            self.assertNotIn("PRIVATE-DIAGNOSTIC", str(failure.exception))
            call.side_effect = subprocess.TimeoutExpired(["codex-for-test"], 90)
            with self.assertRaisesRegex(ai_provider.ProviderError, "90초"):
                ai_provider.generate_text("question", {})
