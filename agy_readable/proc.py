"""Starting and stopping agy and the daemon the same way on POSIX and Windows."""
import os
import shutil
import signal
import subprocess

from agy_readable import config

WINDOWS = os.name == "nt"


def agy():
    """agy as a path: on Windows, Popen finds only `.exe` files on PATH by itself."""
    return shutil.which(config.AGY) or config.AGY


def detached():
    """Popen arguments that put a child in its own process group, so killing the one never takes the other
    down: its own session on POSIX; on Windows its own Ctrl+C group, with a hidden console (a child of the
    windowless daemon would otherwise pop up a console window of its own)."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def alive(pid):
    """Whether a process with this pid is running (one we may not signal counts)."""
    if WINDOWS:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p  # a handle; the default int would truncate it
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        code = ctypes.c_ulong()
        try:
            return not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # on Windows signal 0 would be CTRL_C_EVENT
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def kill_tree(p):
    """Kill a process started with detached() and whatever it started (agy may leave helpers behind)."""
    if WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, **detached())
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            p.kill()  # taskkill missing or refused; at least agy itself goes
        except OSError:
            pass
        return
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
