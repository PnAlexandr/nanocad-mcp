"""Access granted from a nanoCAD window for the lifetime of its process."""

from contextlib import contextmanager
import contextlib
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import sys
import uuid

import pywintypes
import win32api
import win32con
import win32event
import win32process
import win32ts


_WINDOWS_SESSION = win32ts.ProcessIdToSessionId(os.getpid())
_STATE = Path(__file__).with_name(f"access-{_WINDOWS_SESSION}.json")
_STATE_MUTEX = r"Local\nanoCAD-MCP-Access"
_HERE = Path(__file__).parent
ON, OFF, FAILED, AMBIGUOUS = 10, 11, 12, 13


def _process_start(pid: int) -> str | None:
    # PROCESS_QUERY_LIMITED_INFORMATION is sufficient for GetProcessTimes.
    process = win32api.OpenProcess(0x1000 | win32con.SYNCHRONIZE, False, pid)
    try:
        if win32event.WaitForSingleObject(process, 0) == win32event.WAIT_TIMEOUT:
            return win32process.GetProcessTimes(process)["CreationTime"].isoformat()
    finally:
        process.Close()
    return None


@contextmanager
def _state_lock():
    mutex = win32event.CreateMutex(None, False, _STATE_MUTEX)
    try:
        if win32event.WaitForSingleObject(mutex, 1000) == win32event.WAIT_TIMEOUT:
            raise TimeoutError("Could not lock the nanoCAD MCP access state")
        try:
            yield
        finally:
            win32event.ReleaseMutex(mutex)
    finally:
        mutex.Close()


def _read_state() -> tuple[int, str, str] | None:
    try:
        pid, started, grant = json.loads(_STATE.read_text(encoding="ascii"))
    except (OSError, ValueError, TypeError):
        return None
    if type(pid) is int and pid > 0 and isinstance(started, str) and isinstance(grant, str):
        return pid, started, grant
    return None


def enabled_session() -> tuple[int, str, str] | None:
    try:
        with _state_lock():
            state = _read_state()
        if state and _process_start(state[0]) == state[1]:
            return state
    except (OSError, pywintypes.error):
        pass
    return None


def toggle_window(hwnd: int) -> bool:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return toggle_process(pid)


def toggle_process(pid: int) -> bool:
    if win32ts.ProcessIdToSessionId(pid) != _WINDOWS_SESSION:
        raise ValueError("The nanoCAD window must be in the same Windows session")
    started = _process_start(pid)
    if started is None:
        raise ValueError("The nanoCAD process has exited")

    with _state_lock():
        current = _read_state()
        if current and current[:2] == (pid, started):
            _STATE.unlink()
            return False
        pending = _STATE.with_suffix(".tmp")
        pending.write_text(json.dumps((pid, started, uuid.uuid4().hex)), encoding="ascii")
        pending.replace(_STATE)
        return True


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _process_table() -> dict[int, tuple[int, str]]:
    """Map every process id to its parent id and lowercase image name."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    for walk in (kernel32.Process32FirstW, kernel32.Process32NextW):
        walk.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
        walk.restype = wintypes.BOOL

    th32cs_snapprocess = 2
    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if snapshot in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        table = {}
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.casefold())
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return table
    finally:
        kernel32.CloseHandle(snapshot)


def _in_this_session(pid: int) -> bool:
    try:
        return win32ts.ProcessIdToSessionId(pid) == _WINDOWS_SESSION
    except pywintypes.error:
        return False


def _is_nanocad(image: str) -> bool:
    # The process is nmc.exe in some nanoCAD installations and ncad.exe in others.
    return image == "nmc.exe" or "ncad" in image or "nanocad" in image


def _calling_nanocad(table: dict[int, tuple[int, str]], pid: int) -> tuple[int | None, str]:
    """Find the nanoCAD behind the button press and describe how it was found.

    Normally it is an ancestor of this process. A single instance in the same
    session is unambiguous. Focus can move during a click, so never use it to
    choose between two eligible instances."""
    chain, seen = [], set()
    while pid in table and pid not in seen:
        seen.add(pid)
        parent, image = table[pid]
        chain.append(image)
        if _is_nanocad(image):
            return pid, f"parent chain {chain}"
        pid = parent
    running = [p for p, (_, image) in table.items()
               if _is_nanocad(image) and _in_this_session(p)]
    if len(running) == 1:
        return running[0], f"only one running, chain {chain}"
    return None, f"running {running}, chain {chain}"


def _has_window_module(pid: int) -> bool:
    process = win32api.OpenProcess(0x410, False, pid)
    try:
        exe = win32process.GetModuleFileNameEx(process, 0)
    finally:
        process.Close()
    return win32api.GetFileVersionInfo(exe, "\\")["FileVersionMS"] >> 16 == 26


def _answer(nonce: str, code: int, detail: str, native: bool) -> None:
    with contextlib.suppress(OSError), (_HERE / "toggle.log").open("a", encoding="utf-8") as trace:
        trace.write(f"helper: {code} {detail}\n")
    result = _HERE / f"toggle-result-{nonce}.txt"
    pending = result.with_suffix(".tmp")
    pending.write_text(f"{code}{'-26' if native else ''}\n", encoding="ascii")
    pending.replace(result)


def press_button(nonce: str | None = None) -> int:
    """Handle a press of the nanoCAD button and report back to cad_toggle.lsp."""
    legacy = nonce is None
    if legacy:
        nonce = (_HERE / "toggle-request.txt").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9.,_-]{1,80}", nonce):
        raise ValueError("Bad toggle request")
    native = False
    try:
        pid, how = _calling_nanocad(_process_table(), os.getpid())
        if pid is not None and not legacy:
            native = _has_window_module(pid)
        code = AMBIGUOUS if pid is None else ON if toggle_process(pid) else OFF
        detail = f"pid {pid}, {how}"
    except (ValueError, OSError, pywintypes.error) as ex:
        code, detail = FAILED, repr(ex)
    _answer(nonce, code, detail, native and code in (ON, OFF))
    return code


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["--button"]:
            sys.exit(press_button(sys.argv[2]))
        if len(sys.argv) > 1:
            sys.exit(ON if toggle_window(int(sys.argv[1])) else OFF)
        sys.exit(press_button())
    except (ValueError, OSError, pywintypes.error):
        sys.exit(FAILED)
