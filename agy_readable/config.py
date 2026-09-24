"""Settings and file locations shared by the hook, the daemon and the CLI.

Each setting comes from AGY_READABLE_<KEY> in the environment, else from the plugin option of the
same name (Claude Code passes options set with /plugin configure as CLAUDE_PLUGIN_OPTION_<KEY>),
else the default here.
"""
import glob
import json
import os
import time


def opt(key, default):
    for name in (f"AGY_READABLE_{key}", f"CLAUDE_PLUGIN_OPTION_{key}"):
        value = os.environ.get(name, "").strip()
        if value:
            if isinstance(default, bool):
                return value.lower() not in ("0", "false", "no", "off")
            try:
                return type(default)(value)
            except ValueError:
                pass
    return default


AGY = opt("AGY", "agy")
MODEL = opt("MODEL", "gemini-3.8-flash-low")  # `agy models` lists the choices; High thinks ~10 s longer per answer
TIMEOUT = opt("TIMEOUT", 40.0)  # seconds before the original answer is shown instead
NOTES = opt("NOTES", True)  # one line under an answer shown unrewritten, saying why
MIN_CHARS = opt("MIN_CHARS", 300)
MAX_CHARS = opt("MAX_CHARS", 6000)
USE_DAEMON = opt("DAEMON", True)  # False: a one-shot `agy -p` per answer (slower, nothing left running)
SPARES = opt("SPARES", 1)
IDLE_EXIT = opt("IDLE_EXIT", 1800.0)  # the daemon stops, freeing its spare agy, after this long unused
HEDGE_AFTER = opt("HEDGE_AFTER", 8.0)  # + 1 s per 400 prompt chars; a warm Flash Low answers in 3-7 s
STUCK_AFTER = opt("STUCK_AFTER", 20.0)  # a healthy agy is ready in 4-10 s; past this it is probably stuck
PART_WAIT = opt("PART_WAIT", 10.0)  # how long the final flush waits for a part an earlier flush has not written
BROWSER = opt("BROWSER", "")  # command that opens the sign-in page; "" = open / xdg-open, "none" = only show the link


def data_dir():
    """CLAUDE_PLUGIN_DATA inside hooks (and the daemon they start); from a shell, the plugin data
    directory Claude Code made for this plugin; else ~/.agy-readable."""
    configured = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if configured:
        return os.path.realpath(configured)
    found = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data", "agy-readable*"))
    if found:
        log_mtime = lambda p: os.path.getmtime(os.path.join(p, "hook.log")) if os.path.exists(
            os.path.join(p, "hook.log")) else 0
        return os.path.realpath(max(found, key=log_mtime))
    return os.path.realpath(os.path.join(os.path.expanduser("~"), ".agy-readable"))


def log(name, **fields):
    """Append one JSON line to <data dir>/<name>; past 1 MB the file is rotated to <name>.1."""
    path = os.path.join(data_dir(), name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".1")
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **fields}, ensure_ascii=False) + "\n")
