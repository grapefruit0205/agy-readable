"""The second pass by Claude (the fake claude in tests/fakebin): how Claude is run, what it is sent, and what
happens when it fails."""
import json
import os
import unittest

from support import SAMPLE, clean_env, event, new_data_dir, read_jsonl, remove, run_hook


class ReviewerTest(unittest.TestCase):
    def setUp(self):
        self.data = new_data_dir()
        self.args = os.path.join(self.data, "claude-args.jsonl")
        self.prompts = os.path.join(self.data, "prompts.txt")
        self.env = clean_env(self.data, AGY_READABLE_DAEMON="0", FAKE_CLAUDE_ARGS=self.args, FAKE_PROMPTS=self.prompts)

    def tearDown(self):
        remove(self.data)

    def hook(self, text=SAMPLE, **env):
        return run_hook(dict(self.env, **env), event(text))

    def last_log(self):
        return read_jsonl(os.path.join(self.data, "hook.log"))[-1]

    def review_prompts(self):
        with open(self.prompts, encoding="utf-8") as f:
            return [p for p in f.read().split("\n=====\n") if "[다시 쓴 글]" in p]

    def test_claude_checks_every_rewrite(self):
        out = self.hook()
        self.assertTrue(out.startswith("[다듬음"))
        self.assertEqual(len(self.review_prompts()), 1)  # even with nothing suspect, Claude reads it all
        self.assertRegex(self.last_log()["via"], r"review:opus-oneshot$")

    def test_claude_runs_bare_and_cannot_recurse(self):
        self.hook()
        run = read_jsonl(self.args)[0]
        argv = run["argv"]
        for flag, value in (("--model", "opus"), ("--tools", ""), ("--setting-sources", ""), ("--effort", "low")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands", "--system-prompt"):
            self.assertIn(flag, argv)
        self.assertEqual((run["inner"], run["no_claude_md"]), ("1", "1"))
        self.assertNotIn("config.load()", self.review_prompts()[0])  # code never reaches Claude either

    def test_claude_fix_is_shown(self):
        out = self.hook(FAKE_REVIEW="replace:다음과 같습니다:아래와 같습니다")
        self.assertIn("아래와 같습니다", out)
        self.assertEqual(self.last_log()["review"], "고침 1")

    def test_claude_signed_out_shows_the_first_rewrite(self):
        out = self.hook(FAKE_CLAUDE_AUTH="out")
        self.assertTrue(out.startswith("[다듬음"))
        self.assertIn("claude 로그인 필요", self.last_log()["review"])

    def test_translation_is_checked_as_a_translation(self):
        self.hook("I fixed two files and saved the deck as a new file. " * 8)
        prompt = self.review_prompts()[0]
        self.assertIn("한국어로 옮긴 글", prompt)

    def test_agy_as_reviewer(self):
        self.hook(AGY_READABLE_REVIEWER="agy")
        self.assertFalse(os.path.exists(self.args))
        self.assertRegex(self.last_log()["via"], r"review:(warm|cold|oneshot)")

    def test_inside_the_reviewing_claude_the_hook_passes_answers_through(self):
        for ev in (event(SAMPLE), event("The deck was checked and saved as a new file.")):
            self.assertEqual(run_hook(dict(self.env, AGY_READABLE_INNER="1"), ev), ev["delta"])
        self.assertFalse(os.path.exists(os.path.join(self.data, "hook.log")))


class ReviewerDaemonTest(unittest.TestCase):
    """The daemon keeps one Claude waiting, so the second pass skips Claude's startup."""

    def setUp(self):
        from agy_readable import daemon  # noqa: F401  (imported here so CLAUDE_PLUGIN_DATA is set first)
        self.data = new_data_dir()
        self.old = os.environ.get("CLAUDE_PLUGIN_DATA")
        os.environ["CLAUDE_PLUGIN_DATA"] = self.data
        self.env = clean_env(self.data, FAKE_STARTUP="0.3")

    def tearDown(self):
        from agy_readable import daemon
        try:
            daemon.call({"op": "stop"}, 5)
        except (OSError, ValueError):
            pass
        if self.old is None:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        else:
            os.environ["CLAUDE_PLUGIN_DATA"] = self.old
        remove(self.data)

    def test_second_pass_uses_a_waiting_claude(self):
        import time
        from support import wait_until
        from agy_readable import daemon
        run_hook(self.env, event(SAMPLE))  # starts the daemon, which then keeps a Claude waiting
        self.assertTrue(wait_until(lambda: any(r["ready"] for r in (daemon.call({"op": "ping"}, 3).get("reviewers") or [])), 20))
        run_hook(self.env, event(SAMPLE))
        entry = read_jsonl(os.path.join(self.data, "hook.log"))[-1]
        self.assertEqual(entry["outcome"], "refined")
        self.assertRegex(entry["via"], r"review:opus-warm$")
        reviews = [e for e in read_jsonl(os.path.join(self.data, "daemon.log")) if e.get("op") == "review"]
        self.assertTrue(reviews and reviews[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
