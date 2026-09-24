"""The hook with a one-shot agy (no daemon): rewriting, checks on the result, fallbacks, state and settings."""
import json
import os
import subprocess
import threading
import time
import unittest
import uuid

from support import ROOT, SAMPLE, clean_env, encode, event, new_data_dir, read_jsonl, remove, run_hook, stream

from agy_readable import protect


class HookTest(unittest.TestCase):
    def setUp(self):
        self.data = new_data_dir()
        self.env = clean_env(self.data, AGY_READABLE_DAEMON="0")

    def tearDown(self):
        remove(self.data)

    def hook(self, text=SAMPLE, **env):
        return run_hook(dict(self.env, **env), event(text))

    def test_rewritten(self):
        self.assertTrue(self.hook().startswith(encode("[다듬음 model=gemini-3.8-flash-low]")))

    def test_protected_parts_never_reach_agy_and_come_back_exactly(self):
        out = self.hook()
        with open(os.path.join(self.data, "rec.txt"), encoding="utf-8") as f:
            sent = f.read()
        for kept in ("`config.load()`", "`CACHE_TTL`", "[설계 메모](docs/cache.md)"):
            self.assertIn(kept, SAMPLE)
            self.assertNotIn(kept, sent)
        self.assertEqual(out.split("\n", 1)[1], SAMPLE.strip())

    def test_dropped_placeholder_rejected(self):
        out = self.hook(FAKE_MODE="droptoken")
        self.assertTrue(out.startswith(SAMPLE.rstrip()))
        self.assertIn("코드·링크·경로 일부가 빠짐", out)

    def test_fenced_output_unwrapped(self):
        out = self.hook(FAKE_MODE="fence")
        self.assertTrue(out.startswith("[다듬음"))
        self.assertNotIn("```", out)

    def test_agy_error_shows_original_with_note(self):
        out = self.hook(FAKE_MODE="fail")
        self.assertTrue(out.startswith(SAMPLE.rstrip()))
        self.assertIn("종료 코드 1", out)

    def test_timeout_shows_original_within_budget(self):
        start = time.time()
        out = self.hook(FAKE_MODE="sleep", AGY_READABLE_TIMEOUT="6")
        self.assertLess(time.time() - start, 9)
        self.assertTrue(out.startswith(SAMPLE.rstrip()))
        self.assertIn("6초를 넘음", out)

    def test_changed_number_rejected(self):
        out = self.hook(FAKE_MODE="dropnum")
        self.assertIn("1,024", out)
        self.assertIn("바뀜", out)

    def test_list_numbering_may_become_bullets(self):
        numbered = SAMPLE.replace("- 대용량", "1. 대용량", 1)
        self.assertTrue(self.hook(numbered, FAKE_MODE="listbullets").startswith("[다듬음"))

    def test_notes_off(self):
        self.assertNotIn("_(", self.hook(FAKE_MODE="fail", AGY_READABLE_NOTES="0"))

    def test_missing_part_is_marked_not_rewritten(self):
        # part 1 never arrives: its lines were hidden while streaming, so the gap must show
        env = dict(self.env, AGY_READABLE_PART_WAIT="0.5")
        mid = str(uuid.uuid4())
        lines = SAMPLE.splitlines(keepends=True)
        first, lost, last = "".join(lines[:4]), "".join(lines[4:8]), "".join(lines[8:])
        self.assertEqual(run_hook(env, event(first, message_id=mid, index=0, final=False)), "")
        out = run_hook(env, event(last, message_id=mid, index=2, final=True))
        self.assertTrue(out.startswith(first.rstrip("\n")))
        self.assertIn("답변 일부를 화면에 표시하지 못했습니다", out)
        self.assertTrue(out.rstrip().endswith(last.rstrip()))
        self.assertNotIn(lost.strip(), out)
        self.assertFalse(os.path.exists(os.path.join(self.data, "rec.txt")), "a partial answer went to agy")
        self.assertEqual(read_jsonl(os.path.join(self.data, "hook.log"))[-1]["outcome"], "incomplete")

    def test_late_part_is_waited_for(self):
        mid = str(uuid.uuid4())
        lines = SAMPLE.splitlines(keepends=True)
        parts = ["".join(lines[:4]), "".join(lines[4:8]), "".join(lines[8:])]
        run_hook(self.env, event(parts[0], message_id=mid, index=0, final=False))
        result = {}
        final = threading.Thread(target=lambda: result.update(
            out=run_hook(self.env, event(parts[2], message_id=mid, index=2, final=True))))
        final.start()
        time.sleep(1.0)
        run_hook(self.env, event(parts[1], message_id=mid, index=1, final=False))
        final.join()
        self.assertEqual(result["out"].split("\n", 1)[1], SAMPLE.strip())

    def test_plugin_options(self):
        # /plugin configure values arrive as CLAUDE_PLUGIN_OPTION_*; AGY_READABLE_* wins over them
        out = self.hook(FAKE_MODE="fail", CLAUDE_PLUGIN_OPTION_NOTES="false")
        self.assertNotIn("_(", out)
        self.assertIn("model=other-model", self.hook(CLAUDE_PLUGIN_OPTION_MODEL="other-model"))
        self.assertIn("model=env-model",
                      self.hook(CLAUDE_PLUGIN_OPTION_MODEL="other-model", AGY_READABLE_MODEL="env-model"))

    def test_agy_missing(self):
        out = self.hook(AGY_READABLE_AGY="/nonexistent/agy")
        self.assertTrue(out.startswith(SAMPLE.rstrip()))
        self.assertIn("agy를 찾을 수 없음", out)

    def test_left_as_is(self):
        self.assertEqual(self.hook("짧은 답"), "짧은 답")
        english = "This answer has no Korean at all. " * 20
        self.assertEqual(self.hook(english), english)
        code = "코드:\n```python\n" + "print('x')\n" * 60 + "```\n"
        self.assertEqual(self.hook(code), code)

    def test_streamed_message_and_state_cleanup(self):
        aborted = event(SAMPLE, final=False)
        self.assertEqual(run_hook(self.env, aborted), "")
        stale = os.path.join(self.data, "state", "old")
        os.makedirs(stale)
        os.utime(stale, (0, 0))
        out = stream(self.env, SAMPLE)
        self.assertTrue(out.startswith("[다듬음"))
        self.assertEqual(out.count("[다듬음"), 1)
        self.assertEqual(os.listdir(os.path.join(self.data, "state")), [aborted["message_id"]])

    def test_streamed_text_reaches_agy_intact(self):
        # flushes run concurrently; the final one must still join every part in order
        rec = os.path.join(self.data, "rec.txt")
        for _ in range(10):
            if os.path.exists(rec):
                os.remove(rec)
            stream(self.env, SAMPLE)
            with open(rec, encoding="utf-8") as f:
                self.assertEqual(f.read(), protect.mask(SAMPLE)[0])

    def test_log_keeps_outcomes_not_text(self):
        self.hook()
        self.hook(FAKE_MODE="fail")
        self.hook("짧은 답")
        log = read_jsonl(os.path.join(self.data, "hook.log"))
        self.assertLessEqual({"refined", "fallback", "skipped"}, {e.get("outcome") for e in log})
        self.assertNotIn("캐시", json.dumps(log, ensure_ascii=False))

    def test_hooks_json_command(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json")) as f:
            command = json.load(f)["hooks"]["MessageDisplay"][0]["hooks"][0]["command"]
        ev = json.dumps(event(SAMPLE), ensure_ascii=False)
        r = subprocess.run(["sh", "-c", command], input=ev, capture_output=True, text=True, env=self.env)
        self.assertIn("[다듬음", json.loads(r.stdout)["hookSpecificOutput"]["displayContent"])
        # plugin files gone mid-update: exit quietly so Claude Code shows the answer unchanged
        r = subprocess.run(["sh", "-c", command], input=ev, capture_output=True, text=True,
                           env=dict(self.env, CLAUDE_PLUGIN_ROOT=self.data))
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_cli(self):
        self.hook()
        r = subprocess.run(["sh", os.path.join(ROOT, "bin", "agy-readable"), "status"], capture_output=True,
                           text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(os.path.realpath(self.data), r.stdout)
        self.assertIn("다듬음", r.stdout)


if __name__ == "__main__":
    unittest.main()
