"""Helpers shared by the tests: run the hook the way Claude Code does (sh hooks/run, JSON on stdin)
against the fake agy in tests/fakebin, with a throwaway plugin data directory."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKEBIN = os.path.join(ROOT, "tests", "fakebin")
sys.path.insert(0, ROOT)

SAMPLE = """설정 파일을 다시 읽도록 바꿨습니다. 이제 `config.load()`가 호출될 때마다 캐시를 확인하고, 파일이 바뀌었으면 새로 읽습니다.

변경한 부분은 다음과 같습니다.

- 대용량 파일은 1,024 KB 단위로 나눠 읽도록 해서, 메모리 사용량이 이전보다 줄었습니다.
- 캐시 만료 시간은 기본 300초이고, 환경 변수 `CACHE_TTL`로 바꿀 수 있습니다.
- 읽기에 실패하면 이전 설정을 그대로 쓰고, 경고를 한 번만 남깁니다.

테스트는 `python -m unittest`로 돌렸고 42개 모두 통과했습니다.
다만 네트워크 드라이브에 있는 파일은 수정 시각이 늦게 반영될 수 있어서, 이 경우에는 캐시가 최대 2초 늦게 갱신될 수 있습니다.
필요하면 수정 시각 대신 파일 내용의 해시를 비교하는 방식으로 바꿀 수 있는데, 그러면 매번 파일을 끝까지 읽어야 하므로 큰 파일에서는 느려집니다.
자세한 내용은 [설계 메모](docs/cache.md)에 정리해 두었습니다.
어느 쪽이 나은지 알려주시면 그에 맞춰 고치겠습니다.
"""


def decode(text):
    """The fake agy writes the digits in its marker as letters (a=0 ... j=9); for letters-only fields."""
    return text.translate(str.maketrans("abcdefghij", "0123456789"))


def encode(text):
    """A marker text as the fake agy writes it."""
    return text.translate(str.maketrans("0123456789", "abcdefghij"))


def clean_env(data_dir, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGY_READABLE_", "CLAUDE_PLUGIN_OPTION_", "FAKE_"))}
    env.update(PATH=FAKEBIN + os.pathsep + env.get("PATH", ""), CLAUDE_PLUGIN_DATA=data_dir,
               CLAUDE_PLUGIN_ROOT=ROOT, FAKE_RECORD=os.path.join(data_dir, "rec.txt"))
    env.update(extra)
    return env


def new_data_dir():
    return tempfile.mkdtemp(prefix="agyr-")


def event(text, **kw):
    return dict({"hook_event_name": "MessageDisplay", "turn_id": "t", "message_id": str(uuid.uuid4()),
                 "index": 0, "final": True, "delta": text}, **kw)


def run_hook(env, ev):
    r = subprocess.run(["sh", os.path.join(ROOT, "hooks", "run")], input=json.dumps(ev, ensure_ascii=False),
                       capture_output=True, text=True, env=env, timeout=150)
    if not r.stdout.strip():
        raise AssertionError(f"hook printed nothing (exit {r.returncode}): {r.stderr[-500:]}")
    return json.loads(r.stdout)["hookSpecificOutput"]["displayContent"]


def stream(env, text, gap=0.1, final_gap=0.003):
    """Mimic Claude's streaming: a flush of completed lines every `gap` s, the final one right after the last."""
    lines = text.splitlines(keepends=True)
    chunks = ["".join(lines[i:i + 3]) for i in range(0, len(lines), 3)]
    mid, outs, threads = str(uuid.uuid4()), {}, []

    def fire(i, delta, final):
        outs[i] = run_hook(env, event(delta, message_id=mid, index=i, final=final))

    for i, c in enumerate(chunks[:-1]):
        if i:
            time.sleep(gap)
        threads.append(threading.Thread(target=fire, args=(i, c, False)))
        threads[-1].start()
    time.sleep(final_gap)
    threads.append(threading.Thread(target=fire, args=(len(chunks) - 1, chunks[-1], True)))
    threads[-1].start()
    for t in threads:
        t.join()
    return "".join(outs[i] for i in sorted(outs))


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def wait_until(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def remove(path):
    shutil.rmtree(path, ignore_errors=True)
