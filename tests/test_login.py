"""Signing agy in from inside Claude Code, with the fake agy: the offer under an answer, the browser,
the code pasted into the prompt, wrong and late codes, /agy-readable:login and `agy-readable login`."""
import json
import os
import subprocess
import sys
import time
import unittest

from support import ROOT, SAMPLE, clean_env, event, new_data_dir, remove, run_hook, wait_until

from agy_readable import daemon

GOOD = "4/0AgoodFakeCode" + "x" * 30
BAD = "4/0AbadFakeCode" + "y" * 30


class LoginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = new_data_dir()
        cls.auth = os.path.join(cls.data, "auth.txt")
        cls.opened = os.path.join(cls.data, "opened.txt")
        cls.env = clean_env(cls.data, FAKE_AUTH_FILE=cls.auth, FAKE_STARTUP="0.5", FAKE_GEN="0.1",
                            FAKE_LOGIN_WAIT="20",
                            AGY_READABLE_BROWSER=f"sh -c 'echo \"$0\" >> {cls.opened}'")
        os.environ["CLAUDE_PLUGIN_DATA"] = cls.data
        cls.set_auth("no")

    @classmethod
    def tearDownClass(cls):
        cls.stop()
        remove(cls.data)

    @classmethod
    def set_auth(cls, value):
        with open(cls.auth, "w") as f:
            f.write(value)

    @classmethod
    def ping(cls):
        try:
            return daemon.call({"op": "ping"}, 3)
        except (OSError, ValueError):
            return None

    @classmethod
    def stop(cls):
        if cls.ping():
            daemon.call({"op": "stop"}, 5)
        wait_until(lambda: cls.ping() is None, 10)

    def read(self, name):
        with open(os.path.join(self.data, name), encoding="utf-8") as f:
            return f.read()

    def opened_urls(self):
        if not os.path.exists(self.opened):
            return []
        with open(self.opened) as f:
            return f.read().split()

    def prompt(self, text, **env):
        r = subprocess.run(["sh", os.path.join(ROOT, "hooks", "run"), "prompt"],
                           input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": text}),
                           capture_output=True, text=True, env=dict(self.env, **env), timeout=90)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def answer(self, **env):
        start = time.time()
        out = run_hook(dict(self.env, **env), event(SAMPLE))
        return out, time.time() - start

    def test_01_signed_out_answer_offers_sign_in(self):
        out, took = self.answer()
        self.assertTrue(out.startswith(SAMPLE.rstrip()))
        self.assertIn("Antigravity 로그인이 필요합니다", out)
        self.assertIn("브라우저에 Google 로그인 페이지를 열었습니다", out)
        self.assertIn("](https://accounts.google.com/", out)
        self.assertLess(took, 12)
        self.assertEqual(len(self.opened_urls()), 1)
        p = self.ping()
        self.assertTrue(p["auth_required"])
        self.assertEqual(p["login"], "waiting")

    def test_02_next_answer_reuses_the_attempt(self):
        out, _ = self.answer(AGY_READABLE_NOTES="0")  # shown even with notes off
        self.assertIn("앞에서 연 Google 로그인 페이지에서", out)
        self.assertIn("[로그인 페이지](https://accounts.google.com/", out)
        self.assertEqual(len(self.opened_urls()), 1)

    def test_03_other_prompts_pass_through(self):
        self.assertIsNone(self.prompt("안녕, 이 코드 좀 봐줘"))
        self.assertIsNone(self.prompt("4/ 는 무슨 뜻이야?"))

    def test_04_wrong_code_starts_over(self):
        r = self.prompt(BAD)
        self.assertEqual(r["decision"], "block")
        self.assertIn("코드가 맞지 않거나", r["reason"])
        self.assertIn("https://accounts.google.com/", r["reason"])
        self.assertEqual(len(self.opened_urls()), 2)
        self.assertNotIn(BAD, self.read("hook.log"))

    def test_05_good_code_signs_in(self):
        r = self.prompt(GOOD)
        self.assertEqual(r["decision"], "block")
        self.assertIn("로그인이 끝났습니다", r["reason"])
        with open(self.auth) as f:
            self.assertEqual(f.read(), "ok")
        self.assertTrue(wait_until(lambda: not self.ping()["auth_required"]
                                   and any(s["ready"] for s in self.ping()["spares"]), 10), self.ping())
        out, _ = self.answer()
        self.assertTrue(out.startswith("[다듬음"), out[-200:])
        for name in ("hook.log", "daemon.log"):
            self.assertNotIn(GOOD, self.read(name))

    def test_06_code_after_sign_in(self):
        self.assertIn("이미 로그인되어", self.prompt(GOOD)["reason"])

    def test_07_late_code_starts_over(self):
        self.stop()
        self.set_auth("no")
        subprocess.Popen([sys.executable, "-m", "agy_readable.daemon"], cwd=ROOT,
                         env=dict(self.env, PYTHONPATH=ROOT, FAKE_LOGIN_WAIT="2"), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        out, _ = self.answer()
        self.assertIn("로그인이 필요합니다", out)
        time.sleep(3)  # the fake agy gives up after 2 s
        r = self.prompt(GOOD)
        self.assertIn("60초가 지나", r["reason"])
        self.assertIn("https://accounts.google.com/", r["reason"])

    def test_08_login_command_in_prompt(self):
        r = self.prompt("/agy-readable:login")
        self.assertEqual(r["decision"], "block")
        self.assertIn("https://accounts.google.com/", r["reason"])

    def test_09_cli_login_without_terminal(self):
        r = subprocess.run(["sh", os.path.join(ROOT, "bin", "agy-readable"), "login"], capture_output=True,
                           text=True, env=self.env, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("https://accounts.google.com/", r.stdout)
        self.assertIn("입력창", r.stdout)
        r = subprocess.run(["sh", os.path.join(ROOT, "bin", "agy-readable"), "status"], capture_output=True,
                           text=True, env=self.env)
        self.assertIn("로그인 필요", r.stdout)

    def test_10_one_shot_mode_fails_fast_and_offers_sign_in(self):
        out, took = self.answer(AGY_READABLE_DAEMON="0")
        self.assertIn("Antigravity 로그인이 필요합니다", out)
        self.assertLess(took, 12)


if __name__ == "__main__":
    unittest.main()
