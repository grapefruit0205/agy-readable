"""`agy-readable status | log [N] | stop`: see what the hook and the daemon are doing."""
import json
import os
import shutil
import sys
import time

from agy_readable import __version__, config, daemon

USAGE = """usage: agy-readable <command>

  status     settings, agy, daemon and the last answers' outcomes
  log [N]    the last N hook log lines (default 20)
  stop       stop the daemon and its pre-started agy (it restarts with the next answer)
"""


def read_log(name, n):
    path = os.path.join(config.data_dir(), name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def ping():
    try:
        return daemon.call({"op": "ping"}, 3)
    except (OSError, ValueError):
        return None


def describe(entry):
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(entry.get("t", 0)))
    if entry.get("outcome") == "refined":
        return f"{when}  다듬음   {entry.get('chars')}→{entry.get('out')}자  {entry.get('seconds')}s  ({entry.get('via')})"
    if entry.get("outcome") == "skipped":
        return f"{when}  건너뜀   {entry.get('chars')}자  ({entry.get('reason')})"
    if entry.get("outcome"):
        return f"{when}  원문     {entry.get('chars', '?')}자  {entry.get('seconds', '')}s  {entry.get('reason')}"
    return f"{when}  {json.dumps(entry, ensure_ascii=False)}"


def status():
    agy = shutil.which(config.AGY)
    print(f"agy-readable {__version__}")
    print(f"  data dir   {config.data_dir()}")
    print(f"  agy        {agy or '찾을 수 없음 (' + config.AGY + ')'}")
    print(f"  model      {config.MODEL}   timeout {config.TIMEOUT:g}s   notes {'on' if config.NOTES else 'off'}")
    p = ping()
    if p:
        spares = ", ".join(f"{s['pid']} {'ready' if s['ready'] else 'starting'} {s['age']:.0f}s" for s in p["spares"])
        print(f"  daemon     pid {p['pid']}  model {p.get('model')}  spares [{spares}]  busy {p['busy']}  fails {p['fails']}")
    else:
        print("  daemon     not running (starts with the next answer)")
    entries = [e for e in read_log("hook.log", 200) if e.get("outcome")][-10:]
    if entries:
        print("\nrecent answers")
        for e in entries:
            print("  " + describe(e))


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "status"
    if cmd == "status":
        status()
    elif cmd == "log":
        n = int(args[1]) if len(args) > 1 else 20
        for e in read_log("hook.log", n):
            print(describe(e))
    elif cmd == "stop":
        if ping():
            daemon.call({"op": "stop"}, 5)
            print("daemon stopped")
        else:
            print("daemon was not running")
    else:
        sys.stdout.write(USAGE)
        return 0 if cmd in ("-h", "--help", "help") else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
