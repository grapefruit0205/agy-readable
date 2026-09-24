"""Keeps a pre-started agy (stream-json mode) waiting, so the hook skips agy's startup
(sign-in, quota, eligibility, conversation setup: ~5 s normally, 60 s+ when the backend is slow).

Each worker answers exactly one request and is then shut down, so nothing from one answer can
leak into the next one's context. A fresh spare is started as soon as one is taken.

agy's backend stalls (30-60 s waits during startup, sending, or mid-generation) hit single processes,
not all of them, so requests are hedged: a spare still not ready after STUCK_AFTER gets another one
started beside it; a request that finds no ready spare goes to a starting one and a fresh one at once;
and a request with no answer after HEDGE_AFTER (or whose workers all failed) is sent to one more.
The first answer wins and every worker involved is shut down.

Protocol: one JSON line each way over a unix socket.
  {"op": "ask", "prompt": str, "timeout": s, "model": str} -> {"ok": true, "text": str, "warm": bool, ...}
                                                             | {"ok": false, "error": str, "timed_out": bool}
  {"op": "ping"} -> {"ok": true, "pid": int, "model": str, "spares": [...]}
  {"op": "stop"} -> {"ok": true}
"""
import hashlib
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time

from agy_readable import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPARE_MAX_AGE = 1200  # replace a spare that has waited this long; agy's sign-in token lives about an hour
BACKOFF = [5, 15, 60, 300]  # seconds before respawning after consecutive spare failures (e.g. backend 503)
MAX_STARTING = 3  # cap on spares starting at once while hedging against stuck ones
HEDGE_MIN_LEFT = 8  # don't add a worker that has less time than this to answer
MAX_RACERS = 3  # workers one request may use in total


def sock_path():
    # unix socket paths are limited to ~108 bytes, so the data directory is hashed into the name
    name = f"agy-readable-{hashlib.sha1(config.data_dir().encode()).hexdigest()[:8]}.sock"
    base = os.environ.get("XDG_RUNTIME_DIR", "")
    if not base or len(os.path.join(base, name)) > 100:
        base = f"/tmp/agy-readable-{os.getuid()}"
    os.makedirs(base, mode=0o700, exist_ok=True)
    return os.path.join(base, name)


def log(**fields):
    config.log("daemon.log", **fields)


# ---------------------------------------------------------------- client side (hook and CLI)

def call(msg, timeout):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(sock_path())
        s.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            buf += chunk
    return json.loads(buf)


def ensure_running(wait=0.0):
    """Start the daemon if its socket does not answer; optionally wait until it does. Returns True if reachable."""
    deadline = time.time() + wait
    spawned = False
    while True:
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(sock_path())
            return True
        except OSError:
            pass
        if not spawned:
            # own session + no inherited stdio: the hook must not wait on (or kill) the daemon
            env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
            subprocess.Popen([sys.executable, "-m", "agy_readable.daemon"], cwd=ROOT, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            spawned = True
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


# ---------------------------------------------------------------- daemon side

class Worker:
    def __init__(self, model):
        cwd = os.path.join(config.data_dir(), "agy_cwd")  # empty, so agy has no files to pick up as context
        os.makedirs(cwd, exist_ok=True)
        self.born = time.time()
        self.ready_at = None
        self.usage = {}
        self.events = queue.Queue()
        self.p = subprocess.Popen(
            [config.AGY, "--model", model, "--disable-slash-commands", "--input-format", "stream-json",
             "--output-format", "stream-json", "-p", ""],
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            start_new_session=True)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "init" and self.ready_at is None:
                self.ready_at = time.time()
                log(worker=self.p.pid, ready_after=round(self.ready_at - self.born, 1))
            self.events.put(ev)
        self.events.put(None)

    def alive(self):
        return self.p.poll() is None

    def ask(self, prompt, timeout):
        # a result that arrived before any request means agy gave up during startup (e.g. 503)
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            if ev is None or ev.get("event") == "result":
                raise RuntimeError((ev or {}).get("result", {}).get("error") or "agy 종료됨")
        self.p.stdin.write(json.dumps({"event": "user", "message": {"role": "user", "content": prompt}},
                                      ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        deadline = time.time() + timeout
        while True:
            try:
                ev = self.events.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                raise TimeoutError
            if ev is None:
                raise RuntimeError(f"agy 종료됨 (코드 {self.p.wait()})")
            if ev.get("event") == "result":
                r = ev.get("result", {})
                self.usage = r.get("usage") or {}
                if r.get("status") != "SUCCESS":
                    raise RuntimeError(r.get("error") or r.get("status") or "알 수 없는 오류")
                return r.get("response", "")

    def retire(self, grace=3.0):
        """Close stdin so agy exits on its own; kill the whole group if it lingers."""
        def run():
            try:
                self.p.stdin.close()
            except OSError:
                pass
            try:
                self.p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.p.wait()
        threading.Thread(target=run, daemon=True).start()


class Daemon:
    def __init__(self):
        self.lock = threading.Lock()
        self.model = config.MODEL
        self.spares = []
        self.busy = set()
        self.fails = 0
        self.next_spawn = 0.0
        self.last_used = time.time()
        self.stopping = threading.Event()

    def take(self):
        """The workers to send a request to: a ready spare, else the oldest starting one plus a fresh one."""
        with self.lock:
            self.last_used = time.time()
            live = [w for w in self.spares if w.alive()]
            ready = sorted((w for w in live if w.ready_at), key=lambda w: w.born)
            starting = sorted((w for w in live if not w.ready_at), key=lambda w: w.born)
            if ready:
                picked = ready[:1]
            elif starting:
                picked = [starting[0], Worker(self.model)]
            else:
                picked = [Worker(self.model), Worker(self.model)]
            for w in picked:
                if w in self.spares:
                    self.spares.remove(w)
                self.busy.add(w)
            return picked

    def take_one(self):
        """One more worker for a slow or failed request: a ready spare if there is one, else a fresh one."""
        with self.lock:
            ready = sorted((w for w in self.spares if w.alive() and w.ready_at), key=lambda w: w.born)
            w = ready[0] if ready else Worker(self.model)
            if w in self.spares:
                self.spares.remove(w)
            self.busy.add(w)
            return w

    def use_model(self, model):
        """A changed model option reaches the daemon with the next request; spares on the old model go."""
        with self.lock:
            if not model or model == self.model:
                return
            log(model_changed=model, was=self.model)
            self.model = model
            for w in self.spares:
                w.retire()
            self.spares = []
            self.next_spawn = 0.0

    @staticmethod
    def _run_one(w, prompt, timeout, results):
        try:
            results.put((w, {"ok": True, "text": w.ask(prompt, timeout)}))
        except TimeoutError:
            results.put((w, {"ok": False, "error": "agy 응답 시간 초과", "timed_out": True}))
        except Exception as e:
            results.put((w, {"ok": False, "error": f"agy 오류: {e}"[:300]}))

    def handle_ask(self, msg):
        start = time.time()
        deadline = start + float(msg.get("timeout", config.TIMEOUT))
        prompt = msg["prompt"]
        self.use_model(msg.get("model"))
        hedge_at = start + config.HEDGE_AFTER + len(prompt) / 400
        racers, failed, hedges, results = [], [], [], queue.Queue()
        winner, res = None, None

        def launch(w):
            racers.append(w)
            threading.Thread(target=self._run_one, args=(w, prompt, deadline - time.time(), results),
                             daemon=True).start()

        def can_add():
            return len(racers) < MAX_RACERS and deadline - time.time() > HEDGE_MIN_LEFT

        first = self.take()
        warm = len(first) == 1 and first[0].ready_at is not None
        for w in first:
            launch(w)
        while True:
            slow_hedge = not hedges and can_add()
            try:
                w, r = results.get(timeout=max(0.0, (hedge_at if slow_hedge else deadline + 1) - time.time()))
            except queue.Empty:
                if not slow_hedge:
                    break
                hedges.append(f"slow@{time.time() - start:.0f}s")
                launch(self.take_one())
                continue
            if r["ok"]:
                winner, res = w, r
                break
            failed.append(r)
            if len(failed) == len(racers):
                if not can_add():
                    break
                hedges.append(f"failed@{time.time() - start:.0f}s")
                launch(self.take_one())
        if res is None:  # a real error says more than "timed out"
            res = sorted(failed, key=lambda r: r.get("timed_out", False))[0] if failed else \
                {"ok": False, "error": "agy 응답 시간 초과", "timed_out": True}
        for w in racers:
            w.retire()
        with self.lock:
            self.busy.difference_update(racers)
            if res["ok"]:
                self.fails = 0
            elif not res.get("timed_out"):
                self.fails += 1
        w = winner or racers[0]
        res = dict(res, warm=warm, racers=len(racers), hedges=hedges,
                   worker_age=round(start - w.born, 1), seconds=round(time.time() - start, 1))
        # input_tokens stays flat from request to request because every worker starts a fresh conversation
        log(op="ask", worker=w.p.pid, model=self.model, **{k: v for k, v in res.items() if k != "text"},
            chars_out=len(res.get("text", "")), input_tokens=w.usage.get("input_tokens"),
            thinking_tokens=w.usage.get("thinking_tokens"), output_tokens=w.usage.get("output_tokens"))
        return res

    def maintain(self):
        while not self.stopping.wait(1.0):
            now = time.time()
            with self.lock:
                if self.stopping.is_set():
                    return
                for w in list(self.spares):
                    if not w.alive():
                        self.spares.remove(w)
                        self.fails += 1
                        delay = BACKOFF[min(self.fails, len(BACKOFF)) - 1]
                        self.next_spawn = now + delay
                        log(worker=w.p.pid, died_idle=True, code=w.p.returncode, retry_in=delay)
                    elif w.ready_at and now - w.born > SPARE_MAX_AGE:
                        self.spares.remove(w)
                        w.retire()
                        log(worker=w.p.pid, recycled=True)
                    elif w.ready_at:
                        self.fails = 0  # backend reachable again
                ready = sorted((w for w in self.spares if w.ready_at), key=lambda w: w.born)
                starting = [w for w in self.spares if not w.ready_at]
                if len(ready) >= config.SPARES:  # hedges that lost the race, and any surplus, are let go
                    for w in starting + ready[:len(ready) - config.SPARES]:
                        self.spares.remove(w)
                        w.retire()
                        log(worker=w.p.pid, surplus=True)
                elif now >= self.next_spawn and (
                        len(starting) < config.SPARES - len(ready)
                        or len(starting) < MAX_STARTING and all(now - w.born > config.STUCK_AFTER for w in starting)):
                    try:
                        w = Worker(self.model)
                    except OSError as e:  # agy not installed or not runnable
                        self.fails += 1
                        self.next_spawn = now + BACKOFF[min(self.fails, len(BACKOFF)) - 1]
                        log(spawn_error=str(e))
                    else:
                        self.spares.append(w)
                        log(worker=w.p.pid, spawned=True, hedge=bool(starting))
                idle = not self.busy and now - self.last_used > config.IDLE_EXIT
            if idle:
                log(idle_exit=True)
                self.stop()

    def stop(self):
        self.stopping.set()
        try:
            os.unlink(sock_path())
        except OSError:
            pass
        self.lock_file.close()  # after the unlink, so a successor never has its fresh socket deleted by us
        with self.lock:
            for w in self.spares + list(self.busy):
                w.retire(grace=1.0)
        time.sleep(1.5)
        os._exit(0)

    def serve_conn(self, conn):
        with conn:
            try:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                msg = json.loads(buf)
                if msg.get("op") == "ask":
                    res = self.handle_ask(msg)
                elif msg.get("op") == "ping":
                    with self.lock:
                        res = {"ok": True, "pid": os.getpid(), "model": self.model, "fails": self.fails,
                               "busy": len(self.busy),
                               "spares": [{"pid": w.p.pid, "ready": bool(w.ready_at), "age": round(time.time() - w.born, 1)}
                                          for w in self.spares]}
                elif msg.get("op") == "stop":
                    conn.sendall(b'{"ok": true}\n')
                    threading.Thread(target=self.stop).start()
                    return
                else:
                    res = {"ok": False, "error": "unknown op"}
                conn.sendall((json.dumps(res, ensure_ascii=False) + "\n").encode())
            except (OSError, ValueError):
                pass  # client gave up (hook timeout); the workers were already retired in handle_ask

    def run(self):
        import fcntl  # POSIX only; imported here so the hook can load this module anywhere

        path = sock_path()
        self.lock_file = open(path + ".lock", "w")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another daemon already serves this data directory
        try:
            os.unlink(path)  # left over from a daemon that was killed
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(16)
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=self.stop).start())
        log(started=os.getpid(), model=self.model, spares=config.SPARES)
        threading.Thread(target=self.maintain, daemon=True).start()
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self.serve_conn, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    Daemon().run()
