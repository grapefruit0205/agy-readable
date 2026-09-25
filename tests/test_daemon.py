"""The daemon with the fake agy: warm path, single-use workers, concurrency, timeouts, backend failures,
hedging and lifecycle. The steps share one data directory and run in order."""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from support import ROOT, SAMPLE, clean_env, decode, encode, event, new_data_dir, read_jsonl, remove, run_hook, stream, wait_until

from agy_readable import __version__, daemon


def marker(out):
    m = re.match(r"\[다듬음 pid=([a-j]+) turn=([a-j]+) model=(\S+)\]", out)
    return (int(decode(m.group(1))), int(decode(m.group(2))), m.group(3)) if m else None


class DaemonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = new_data_dir()
        cls.mode_file = os.path.join(cls.data, "mode.txt")
        cls.pids = os.path.join(cls.data, "pids")
        cls.review_file = os.path.join(cls.data, "review.txt")
        os.makedirs(cls.pids)
        # the second pass is off here (one request per answer) except where a test turns it on
        cls.env = clean_env(cls.data, FAKE_MODE_FILE=cls.mode_file, FAKE_PIDS=cls.pids, FAKE_STARTUP="1.5",
                            FAKE_GEN="0.2", AGY_READABLE_REVIEW="0", FAKE_REVIEW_FILE=cls.review_file)
        os.environ["CLAUDE_PLUGIN_DATA"] = cls.data  # so daemon.call / sock_path here reach the test daemon
        cls.set_mode("ok")

    @classmethod
    def tearDownClass(cls):
        cls.stop()
        remove(cls.data)

    # ------------------------------------------------------------ helpers

    @classmethod
    def set_mode(cls, m):
        with open(cls.mode_file, "w") as f:
            f.write(m)

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
        wait_until(lambda: cls.ping() is None and not cls.workers(), 10)

    @classmethod
    def start(cls, **env):
        subprocess.Popen([sys.executable, "-m", "agy_readable.daemon"], cwd=ROOT,
                         env=dict(cls.env, PYTHONPATH=ROOT, **env), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    @classmethod
    def workers(cls):
        """Fake agy workers of this test run still alive."""
        alive = []
        for name in os.listdir(cls.pids):
            try:
                os.kill(int(name), 0)
            except ProcessLookupError:
                os.remove(os.path.join(cls.pids, name))
                continue
            except PermissionError:
                pass
            alive.append(name)
        return alive

    def spare_ready(self):
        p = self.ping()
        return bool(p and any(s["ready"] for s in p["spares"]))

    def ask(self, text=SAMPLE, **env):
        start = time.time()
        out = run_hook(dict(self.env, **env), event(text))
        return out, time.time() - start

    def last_hook(self):
        return read_jsonl(os.path.join(self.data, "hook.log"))[-1]

    def daemon_log(self):
        return read_jsonl(os.path.join(self.data, "daemon.log"))

    def last_ask(self):
        return [e for e in self.daemon_log() if e.get("op") == "ask"][-1]

    # ------------------------------------------------------------ steps

    def test_01_first_message_starts_daemon(self):
        self.stop()
        self.assertIsNone(self.ping())
        out = stream(self.env, SAMPLE)
        self.assertIsNotNone(marker(out), out[:60])
        self.assertIn(self.last_hook().get("via"), ("warm", "cold"))

    def test_02_ready_spare_answers_fast(self):
        self.assertTrue(wait_until(self.spare_ready))
        out, took = self.ask()
        self.assertEqual(self.last_hook().get("via"), "warm")
        self.assertLess(took, 1.4)

    def test_03_each_answer_in_a_fresh_conversation(self):
        seen = []
        for _ in range(3):
            wait_until(self.spare_ready)
            seen.append(marker(self.ask()[0]))
        self.assertTrue(all(m and m[1] == 1 for m in seen), seen)
        self.assertEqual(len({m[0] for m in seen}), 3, seen)

    def test_04_concurrent_answers_use_separate_workers(self):
        wait_until(self.spare_ready)
        res = [None, None]
        threads = [threading.Thread(target=lambda i=i: res.__setitem__(i, self.ask()[0])) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ms = [marker(r) for r in res]
        self.assertTrue(all(ms), res)
        self.assertNotEqual(ms[0][0], ms[1][0])
        self.assertTrue(wait_until(lambda: len(self.workers()) == 1, 8), self.workers())

    def test_05_hung_worker_cut_off_and_killed(self):
        wait_until(self.spare_ready)
        self.set_mode("hang")
        out, took = self.ask(AGY_READABLE_TIMEOUT="3")
        self.set_mode("ok")
        self.assertIn("3초를 넘음", out)
        self.assertTrue(out.startswith(SAMPLE.rstrip()))
        self.assertLess(took, 5.5)
        self.assertTrue(wait_until(lambda: len(self.workers()) <= 1, 8), self.workers())

    def test_06_backend_refusing_then_recovering(self):
        self.set_mode("startfail")
        wait_until(self.spare_ready)
        self.ask()  # takes the ready spare; its replacement starts under startfail and dies
        self.assertTrue(wait_until(lambda: any(e.get("died_idle") and e.get("retry_in") for e in self.daemon_log()), 10))
        out, _ = self.ask()
        self.assertIn("503", out)
        self.assertIn("원문 표시", out)
        self.set_mode("ok")
        self.assertIsNotNone(marker(self.ask()[0]))

    def test_07_killed_daemon_replaced(self):
        self.stop()
        stream(self.env, SAMPLE)
        wait_until(self.spare_ready)
        pid = self.ping()["pid"]
        os.kill(pid, signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not self.workers(), 8), self.workers())  # orphans exit on stdin EOF
        out, _ = self.ask()
        self.assertIsNotNone(marker(out))
        self.assertNotEqual(self.ping()["pid"], pid)

    def test_08_stuck_spare_raced_against_fresh_worker(self):
        self.stop()
        self.set_mode("stuckonce")
        self.start()
        time.sleep(1)
        out, took = self.ask()
        self.assertIsNotNone(marker(out))
        self.assertLess(took, 5)
        self.assertEqual(self.last_ask().get("racers"), 2)
        self.assertTrue(wait_until(lambda: len(self.workers()) <= 1, 10), self.workers())

    def test_09_stuck_idle_spare_gets_hedge(self):
        self.stop()
        self.set_mode("stuckonce")
        self.start(AGY_READABLE_STUCK_AFTER="3")
        self.assertTrue(wait_until(self.spare_ready, 12))
        self.assertTrue(wait_until(lambda: any(e.get("hedge") for e in self.daemon_log())
                                   and any(e.get("surplus") for e in self.daemon_log()), 5))
        self.assertTrue(wait_until(lambda: len(self.workers()) == 1, 10), self.workers())

    def test_10_stalled_or_failed_answer_resent(self):
        self.stop()
        self.start(AGY_READABLE_HEDGE_AFTER="1")
        wait_until(self.spare_ready)
        self.set_mode("slowonce")
        out, took = self.ask()
        self.assertIsNotNone(marker(out))
        self.assertLess(took, 10)
        self.assertTrue(self.last_ask()["hedges"][0].startswith("slow"), self.last_ask())
        wait_until(self.spare_ready)
        self.set_mode("failonce")
        out, _ = self.ask()
        self.assertIsNotNone(marker(out))
        self.assertTrue(self.last_ask()["hedges"][0].startswith("failed"), self.last_ask())
        self.assertTrue(wait_until(lambda: len(self.workers()) == 1, 10), self.workers())

    def test_11_model_change_reaches_daemon(self):
        wait_until(self.spare_ready)
        out, _ = self.ask(CLAUDE_PLUGIN_OPTION_MODEL="gemini-3.8-flash-medium")
        self.assertEqual(marker(out)[2], encode("gemini-3.8-flash-medium"))
        self.assertEqual(self.ping()["model"], "gemini-3.8-flash-medium")
        self.assertTrue(any(e.get("model_changed") for e in self.daemon_log()))

    def test_12_cli_stop(self):
        wait_until(self.spare_ready)
        r = subprocess.run(["sh", os.path.join(ROOT, "bin", "agy-readable"), "stop"], capture_output=True, text=True,
                           env=self.env)
        self.assertIn("stopped", r.stdout)
        self.assertTrue(wait_until(lambda: self.ping() is None and not self.workers(), 8), self.workers())

    def test_13_idle_exit(self):
        self.stop()
        self.start(AGY_READABLE_IDLE_EXIT="3")
        self.assertTrue(wait_until(self.spare_ready))
        self.assertTrue(wait_until(lambda: self.ping() is None and not self.workers(), 10), self.workers())


    def test_14_daemon_of_another_version_replaced(self):
        # after a plugin update the socket is the same; a 0.1.0 daemon answers pings without a version
        self.stop()
        path, got = daemon.sock_path(), []
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
        srv.listen(4)

        def serve():
            while True:
                conn, _ = srv.accept()
                with conn:
                    op = json.loads(conn.recv(65536))["op"]
                    got.append(op)
                    conn.sendall(b'{"ok": true, "pid": 1}\n')
                    if op == "stop":
                        os.unlink(path)
                        srv.close()
                        return

        threading.Thread(target=serve, daemon=True).start()
        with mock.patch.dict(os.environ, self.env):  # the new daemon must run the fake agy
            self.assertTrue(daemon.ensure_running(wait=10))
        self.assertEqual(got, ["ping", "stop"])
        self.assertEqual(self.ping()["version"], __version__)

    def test_15_rejected_rewrite_asked_again(self):
        # the retry is a second request to the daemon, answered by another single-use worker
        wait_until(self.spare_ready)
        self.set_mode("dropnum,ok")
        out, _ = self.ask()
        self.assertIsNotNone(marker(out))
        self.assertIn("1,024", out)
        self.assertEqual((self.last_hook()["outcome"], self.last_hook()["retries"]), ("refined", 1))
        asks = [e for e in self.daemon_log() if e.get("op") == "ask"][-2:]
        self.assertTrue(all(e["ok"] for e in asks))
        self.assertNotEqual(asks[0]["worker"], asks[1]["worker"])

    def test_16_second_pass_is_another_request(self):
        # the rewrite and its review each go to their own single-use worker
        wait_until(self.spare_ready)
        self.set_mode("ok")
        with open(self.review_file, "w", encoding="utf-8") as f:
            f.write("replace:캐시를 확인하고:캐시를 먼저 확인하고")
        self.addCleanup(os.remove, self.review_file)
        out, _ = self.ask(AGY_READABLE_REVIEW="1")
        self.assertIsNotNone(marker(out))
        self.assertIn("캐시를 먼저 확인하고", out)
        entry = self.last_hook()
        self.assertEqual((entry["outcome"], entry["review"]), ("refined", "고침 1"))
        self.assertRegex(entry["via"], r"^warm,review:(warm|cold)")
        asks = [e for e in self.daemon_log() if e.get("op") == "ask"][-2:]
        self.assertTrue(all(e["ok"] for e in asks))
        self.assertNotEqual(asks[0]["worker"], asks[1]["worker"])


if __name__ == "__main__":
    unittest.main()
