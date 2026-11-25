"""nanoCAD MCP server — управление nanoCAD (24–26, СПДС) через MCP.

Запуск (stdio):  python nanocad_mcp.py
Требуется: Windows, запущенный nanoCAD, Python-пакеты `mcp` и `pywin32`.

Как устроено:
* Чтение чертежа и массовые правки выполняет LISP внутри nanoCAD (быстро: тысячи
  объектов в секунду). Сервер пишет .lsp во временную папку, запускает его через
  SendCommand и забирает результат из файла.
* Через COM — только то, что требует немного вызовов: подключение, размеры, тексты,
  слои, сохранение (каждый объект через COM в nanoCAD стоит ~20 мс).
* Все обращения идут из одного выделенного потока (STA). Если операция дольше 45 с,
  инструмент отвечает «ещё выполняется» — повторный вызов с теми же аргументами
  дождётся результата, а не запустит работу второй раз.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import datetime as _dt
import fnmatch
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import pythoncom
import win32com.client
import win32event
import win32process
from win32com.client import VARIANT
from tool_catalog import ConfigurableMCP, COMMAND_GROUP, current_tool, enabled_tools, require_enabled

from cad_access import enabled_session

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s nanocad-mcp %(levelname)s %(message)s")
log = logging.getLogger("nanocad-mcp")

__version__ = "0.7.9"

mcp = ConfigurableMCP(
    "nanocad-mcp",
    instructions=(
        "Управление nanoCAD (24–26, СПДС). Перед правкой проанализируй чертёж "
        "и покажи пользователю план (dry_run=True, где поддерживается). "
        "Дождись подтверждения перед изменением, сохранением или выполнением кода. "
        "Если ответ «still_running» — повтори тот же вызов с теми же аргументами. "
        "Пользователь выбирает доступные команды в настройках nanoCAD MCP."
    ),
)

SOFT_TIMEOUT = 45.0      # секунд: дольше — отвечаем «ещё выполняется»
LISP_TIMEOUT = 600.0     # предел для одного LISP-прогона
WORK_DIR = os.path.join(tempfile.gettempdir(), "nanocad_mcp")
SEP = "\x1f"             # разделитель полей в обмене с LISP (chr 31)

# --------------------------------------------------------------------------- #
#  COM-поток и фоновые задачи                                                  #
# --------------------------------------------------------------------------- #

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, initializer=pythoncom.CoInitialize, thread_name_prefix="nanocad-com"
)

_BUSY_CODES = {-2147418111, -2147417846}  # RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER
_CAD_MUTEX = r"Local\nanoCAD-MCP-COM"
_DISABLED = ("Доступ к nanoCAD выключен. Откройте чертёж и нажмите «Вкл / Выкл» "
             "на вкладке MCP в nanoCAD или кнопку панели «nanoCAD MCP».")


class Busy(RuntimeError):
    pass


def _hresult(ex: BaseException) -> int | None:
    if isinstance(ex, pythoncom.com_error) and ex.args:
        return ex.args[0]
    return None


def _err_text(ex: BaseException) -> str:
    if isinstance(ex, pythoncom.com_error):
        hr, msg, exc, _arg = (list(ex.args) + [None] * 4)[:4]
        detail = f": {exc[2]}" if exc and len(exc) > 2 and exc[2] else ""
        if hr in _BUSY_CODES:
            return ("nanoCAD занят (выполняется команда или открыт диалог). "
                    "Нажмите Esc в nanoCAD и повторите.")
        return f"COM-ошибка {hr} ({msg}){detail}"
    return f"{type(ex).__name__}: {ex}"


_jobs: dict[tuple, dict] = {}
_job_session = None


async def _run(fn, *args, readonly: bool = False, job: str | None = None, **kwargs):
    """Выполнить fn в COM-потоке.

    job — ключ задачи (имя инструмента + аргументы). Если работа не уложилась в
    SOFT_TIMEOUT, она продолжается, а повторный вызов с тем же ключом дождётся её."""
    global _job_session

    access = enabled_session()
    requested_tool = current_tool.get()
    if access is None:
        return {"ok": False, "error": _DISABLED}

    if access != _job_session:
        _jobs.clear()
        _job_session = access
    key = (access, job) if job else None

    def call():
        mutex = win32event.CreateMutex(None, False, _CAD_MUTEX)
        try:
            if win32event.WaitForSingleObject(mutex, 0) == win32event.WAIT_TIMEOUT:
                return {"ok": False, "error": "Другой MCP-клиент работает с nanoCAD. Повторите позже."}
            try:
                attempts = 15 if readonly else 1
                for i in range(attempts):
                    if enabled_session() != access:
                        return {"ok": False, "error": _DISABLED}
                    if requested_tool:
                        require_enabled(requested_tool)
                    if S.access != access:
                        S.access = access
                        S.app = S.scan_cache = None
                    try:
                        return fn(*args, **kwargs)
                    except pythoncom.com_error as ex:
                        if _hresult(ex) in _BUSY_CODES and i < attempts - 1:
                            time.sleep(0.4)
                            continue
                        raise
            finally:
                win32event.ReleaseMutex(mutex)
        finally:
            mutex.Close()

    entry = _jobs.get(key) if key else None
    if entry and entry["fut"].done() and time.time() - entry.get("finished", 0) > 600:
        _jobs.pop(key, None)
        entry = None
    if entry is None:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_executor, call)
        entry = {"fut": fut, "started": time.time()}
        if key:
            _jobs[key] = entry
            fut.add_done_callback(lambda _f, e=entry: e.__setitem__("finished", time.time()))
    fut = entry["fut"]
    try:
        res = await asyncio.wait_for(asyncio.shield(fut), SOFT_TIMEOUT)
    except asyncio.TimeoutError:
        return {"ok": False, "still_running": True,
                "elapsed_s": round(time.time() - entry["started"]),
                "message": "Операция ещё выполняется в nanoCAD. Повтори этот же вызов "
                           "с теми же аргументами — он дождётся результата."}
    except Exception as ex:
        if key:
            _jobs.pop(key, None)
        log.error("tool error: %s", traceback.format_exc())
        return {"ok": False, "error": _err_text(ex)}
    if key:
        _jobs.pop(key, None)
    return res


def _jobkey(name, **kw):
    return name + json.dumps(kw, sort_keys=True, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
#  Подключение к nanoCAD                                                       #
# --------------------------------------------------------------------------- #

class _State:
    access = None
    app = None
    prog_id = None
    scan_cache = None


S = _State()


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _clsid_progid(clsid: str) -> str:
    import winreg
    for sub in ("ProgID", "VersionIndependentProgID"):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\{sub}") as k:
                return winreg.QueryValue(k, None) or ""
        except OSError:
            continue
    return ""


def _enabled_app(app) -> bool:
    if S.access is None:
        return False
    pid = _safe(lambda: win32process.GetWindowThreadProcessId(int(app.HWND))[1])
    return pid == S.access[0]


def _find_instances() -> list[tuple[str, object]]:
    """Экземпляр из ROT, в котором пользователь включил доступ."""
    found, seen = [], set()
    try:
        ctx = pythoncom.CreateBindCtx(0)
        rot = pythoncom.GetRunningObjectTable()
        # Product variants can publish the same CLSID. The SDK button registers
        # the actual in-process Application under a second, process-specific name.
        if S.access:
            try:
                name = pythoncom.CreateItemMoniker("!", f"NanoCAD-MCP.Window.{S.access[0]}")
                obj = rot.GetObject(name).QueryInterface(pythoncom.IID_IDispatch)
                app = win32com.client.Dispatch(obj)
                if _enabled_app(app):
                    return [("nanoCAD.Application", app)]
            except pythoncom.com_error:
                pass  # Older installations only publish the standard CLSID.
        enum = rot.EnumRunning()
        while True:
            mks = enum.Next(1)
            if not mks:
                break
            mk = mks[0]
            name = _safe(lambda: mk.GetDisplayName(ctx, None), "") or ""
            m = re.match(r"^!(\{[0-9A-Fa-f-]{36}\})$", name)
            if not m:
                continue
            prog = _clsid_progid(m.group(1).upper())
            if "nanocad" not in prog.lower():
                continue
            try:
                unk = rot.GetObject(mk)
                app = win32com.client.Dispatch(unk.QueryInterface(pythoncom.IID_IDispatch))
            except Exception:
                continue
            if not _enabled_app(app):
                continue
            key = _safe(lambda: int(app.HWND)) or (prog, _safe(lambda: app.Caption))
            if key in seen:
                continue
            seen.add(key)
            found.append((prog, app))
    except Exception:
        log.warning("ROT scan failed: %s", traceback.format_exc())
    if not found:
        for prog in ("nanoCAD.Application", "nanoCADx64.Application"):
            try:
                app = win32com.client.GetActiveObject(prog)
                if _enabled_app(app):
                    found.append((prog, app))
                    break
            except Exception:
                pass
    return found


def _describe(prog, app) -> dict:
    doc = _safe(lambda: app.ActiveDocument)
    return {
        "prog_id": prog,
        "process_id": win32process.GetWindowThreadProcessId(int(app.HWND))[1],
        "version": _safe(lambda: str(app.Version)),
        "caption": _safe(lambda: str(app.Caption)),
        "active_document": _safe(lambda: str(doc.Name)) if doc else None,
        "documents": _safe(lambda: int(app.Documents.Count)),
    }


def _app():
    if S.app is not None:
        try:
            _ = S.app.Documents.Count
            return S.app
        except Exception:
            S.app = None
    inst = _find_instances()
    if not inst:
        raise RuntimeError("Экземпляр nanoCAD с включённым доступом не найден. "
                           "Запустите nanoCAD без повышения прав и откройте чертёж.")
    S.prog_id, S.app = inst[0]
    return S.app


def _doc():
    app = _app()
    doc = _safe(lambda: app.ActiveDocument)
    if doc is None:
        raise RuntimeError("В nanoCAD нет открытого чертежа.")
    return doc


# --------------------------------------------------------------------------- #
#  LISP-движок                                                                 #
# --------------------------------------------------------------------------- #

LISP_COMMON = r'''
(setq mcp-sep (chr 31))
(defun mcp-g (k ed) (cdr (assoc k ed)))
(defun mcp-s (v)
  (cond ((= (type v) 'STR) v) ((= (type v) 'INT) (itoa v))
        ((= (type v) 'REAL) (rtos v 2 6)) (T "")))
(defun mcp-p (p)
  (if (and p (listp p) (numberp (car p)))
    (strcat (rtos (car p) 2 4) "," (rtos (cadr p) 2 4) ","
            (rtos (if (numberp (caddr p)) (caddr p) 0.0) 2 4))
    ""))
(defun mcp-split (s d / p r)
  (while (setq p (vl-string-search d s))
    (setq r (cons (substr s 1 p) r) s (substr s (+ p 2))))
  (reverse (cons s r)))
(defun mcp-done (fn status / f)
  (setq f (open (strcat fn ".done") "w")) (write-line status f) (close f))
(defun mcp-go (fn / r)
  (setq r (vl-catch-all-apply 'mcp-main (list fn)))
  (if mcp-undo-open (progn (vl-catch-all-apply 'command (list "_.UNDO" "_END"))
                           (setq mcp-undo-open nil)))
  (mcp-done fn (if (vl-catch-all-error-p r) (strcat "ERR " (vl-catch-all-error-message r)) "OK"))
  (princ))
(defun mcp-undo-begin () (command "_.UNDO" "_BEGIN") (setq mcp-undo-open T))
'''

LISP_DUMP = r'''
(defun mcp-effname (nm / x)
  (setq x (vl-catch-all-apply
    '(lambda (/ br h)
       (setq br (entget (cdr (assoc 330 (entget (tblobjname "BLOCK" nm)))) '("AcDbBlockRepBTag")))
       (setq h (cdr (assoc 1005 (cdadr (assoc -3 br)))))
       (cdr (assoc 2 (entget (handent h)))))
    nil))
  (if (or (vl-catch-all-error-p x) (not x)) nm x))
(defun mcp-main (fn / f ss i e ed typ verts txt a att c nm eff)
  (setq f (open fn "w"))
  (setq a (tblnext "LAYER" T))
  (while a
    (write-line (strcat "L" mcp-sep (mcp-g 2 a) mcp-sep (itoa (mcp-g 70 a)) mcp-sep
                        (itoa (mcp-g 62 a)) mcp-sep (mcp-s (mcp-g 6 a))) f)
    (setq a (tblnext "LAYER")))
  (setq a (tblnext "BLOCK" T))
  (while a
    (write-line (strcat "B" mcp-sep (mcp-g 2 a) mcp-sep (itoa (mcp-g 70 a))) f)
    (setq a (tblnext "BLOCK")))
  (setq i 0 a (tblnext "APPID" T))
  (while a (setq i (1+ i) a (tblnext "APPID")))
  (write-line (strcat "R" mcp-sep (itoa i)) f)
  (write-line (strcat "V" mcp-sep "CLAYER" mcp-sep (getvar "CLAYER")) f)
  (setq ss (ssget "_X"))
  (if ss (progn (setq i 0)
    (repeat (sslength ss)
      (setq e (ssname ss i) ed (entget e) typ (mcp-g 0 ed) verts "" txt "" eff "")
      (if (= typ "LWPOLYLINE")
        (foreach x ed
          (cond ((= (car x) 10)
                 (setq verts (strcat verts ";" (rtos (cadr x) 2 4) "," (rtos (caddr x) 2 4))))
                ((= (car x) 42) (setq verts (strcat verts "," (rtos (cdr x) 2 6)))))))
      (cond ((= typ "MTEXT")
             (foreach x ed (if (= (car x) 3) (setq txt (strcat txt (cdr x)))))
             (setq txt (strcat txt (mcp-s (mcp-g 1 ed)))))
            ((= typ "MULTILEADER") (setq txt (mcp-s (mcp-g 304 ed))))
            ((member typ '("TEXT" "ATTDEF" "DIMENSION")) (setq txt (mcp-s (mcp-g 1 ed)))))
      (setq nm (mcp-s (mcp-g 2 ed)))
      (if (and (= typ "INSERT") (wcmatch nm "`*U*")) (setq eff (mcp-effname nm)))
      (setq c (cond ((assoc 420 ed) "TC") ((assoc 62 ed) (itoa (mcp-g 62 ed))) (T "256")))
      (write-line (strcat "E" mcp-sep (mcp-g 5 ed) mcp-sep typ mcp-sep (mcp-s (mcp-g 8 ed)) mcp-sep
                          (mcp-s (mcp-g 410 ed)) mcp-sep c mcp-sep (mcp-s (mcp-g 6 ed)) mcp-sep
                          (mcp-p (mcp-g 10 ed)) mcp-sep (mcp-p (mcp-g 11 ed)) mcp-sep
                          (mcp-p (mcp-g 13 ed)) mcp-sep (mcp-p (mcp-g 14 ed)) mcp-sep
                          (mcp-s (mcp-g 38 ed)) mcp-sep nm mcp-sep (mcp-s (mcp-g 40 ed)) mcp-sep
                          (mcp-s (mcp-g 50 ed)) mcp-sep (mcp-s (mcp-g 51 ed)) mcp-sep
                          (mcp-s (mcp-g 41 ed)) mcp-sep (mcp-s (mcp-g 42 ed)) mcp-sep
                          (mcp-s (mcp-g 70 ed)) mcp-sep eff mcp-sep verts mcp-sep txt) f)
      (if (and (= typ "INSERT") (= (mcp-g 66 ed) 1))
        (progn (setq a (entnext e))
          (while (and a (= (mcp-g 0 (setq att (entget a))) "ATTRIB"))
            (write-line (strcat "A" mcp-sep (mcp-g 5 ed) mcp-sep (mcp-s (mcp-g 2 att)) mcp-sep
                                (mcp-s (mcp-g 1 att))) f)
            (setq a (entnext a)))))
      (setq i (1+ i)))))
  (close f))
'''

LISP_APPLY = r'''
(defun mcp-setcode (ed k v)
  (if (assoc k ed) (subst (cons k v) (assoc k ed) ed) (append ed (list (cons k v)))))
(defun mcp-z0 (ed k / p)
  (if (and (setq p (assoc k ed)) (listp (cdr p)) (numberp (caddr p)) (cdddr p))
    (subst (list k (cadr p) (caddr p) 0.0) p ed) ed))
(defun mcp-notc (ed) (vl-remove-if '(lambda (x) (member (car x) '(420 430))) ed))
(defun mcp-op (h op arg / e ed ok typ n)
    (if (wcmatch op "L*")
      (progn
        (setq e (tblobjname "LAYER" h))
        (if (and e (setq ed (entget e)))
          (progn (setq n (mcp-g 62 ed))
            (cond
              ((= op "Lcolor") (setq ok (entmod (subst (cons 62 (if (minusp n) (- (atoi arg)) (atoi arg)))
                                                       (assoc 62 ed) (mcp-notc ed)))))
              ((= op "Lon") (setq ok (entmod (subst (cons 62 (if (= arg "1") (abs n) (- (abs n))))
                                                    (assoc 62 ed) ed))))
              ((= op "Lfreeze") (setq ok (entmod (subst (cons 70 (if (= arg "1") (logior (mcp-g 70 ed) 1)
                                                                     (logand (mcp-g 70 ed) (~ 1))))
                                                        (assoc 70 ed) ed))))
              ((= op "Llock") (setq ok (entmod (subst (cons 70 (if (= arg "1") (logior (mcp-g 70 ed) 4)
                                                                   (logand (mcp-g 70 ed) (~ 4))))
                                                      (assoc 70 ed) ed))))
              ((= op "Lltype") (if (tblsearch "LTYPE" arg) (setq ok (entmod (mcp-setcode ed 6 arg)))))))))
      (progn
        (setq e (handent h))
        (if (and e (setq ed (entget e)))
          (progn (setq typ (mcp-g 0 ed))
            (cond
              ((= op "erase") (entdel e) (setq ok (not (entget e))))
              ((= op "color") (setq ok (entmod (mcp-setcode (mcp-notc ed) 62 (atoi arg)))))
              ((= op "layer")
               (if (not (tblsearch "LAYER" arg))
                 (entmake (list '(0 . "LAYER") '(100 . "AcDbSymbolTableRecord")
                                '(100 . "AcDbLayerTableRecord") (cons 2 arg) '(70 . 0) '(62 . 7)
                                '(6 . "Continuous"))))
               (setq ok (entmod (subst (cons 8 arg) (assoc 8 ed) ed))))
              ((= op "ltype") (setq ok (entmod (mcp-setcode ed 6 arg))))
              ((= op "flatten")
               (setq ok (entmod (if (= typ "LWPOLYLINE") (mcp-setcode ed 38 0.0)
                                  (mcp-z0 (mcp-z0 ed 10) 11))))))))))
  ok)
(defun mcp-main (fn / fin fout line parts h op ok regen)
  (setq fin (open (strcat fn ".ops") "r") fout (open fn "w"))
  (mcp-undo-begin)
  (while (setq line (read-line fin))
    (setq parts (mcp-split line mcp-sep) h (car parts) op (cadr parts))
    (if (wcmatch op "L*") (setq regen T))
    (setq ok (vl-catch-all-apply 'mcp-op (list h op (caddr parts))))
    (if (vl-catch-all-error-p ok) (setq ok nil))
    (write-line (strcat h mcp-sep (if ok "1" "0")) fout))
  (close fin) (close fout)
  (if regen (command "_.REGEN")))
'''

LISP_EVAL = r'''
(defun mcp-main (fn / f r)
  (setq r (progn <<CODE>>))
  (setq f (open fn "w")) (prin1 r f) (close f))
'''


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def _lisp(doc, body: str, ops: list[str] | None = None, timeout: float = LISP_TIMEOUT,
          undo: bool = False) -> str:
    """Выполнить LISP внутри nanoCAD. body определяет (mcp-main fn) и пишет результат в fn."""
    if int(_safe(lambda: doc.GetVariable("CMDACTIVE"), 0) or 0):
        raise Busy("В nanoCAD выполняется команда. Завершите её (Esc) и повторите.")
    os.makedirs(WORK_DIR, exist_ok=True)
    _cleanup_workdir()
    rid = uuid.uuid4().hex[:10]
    out = os.path.join(WORK_DIR, f"out_{rid}.txt")
    lsp = os.path.join(WORK_DIR, f"run_{rid}.lsp")
    done = out + ".done"
    with open(lsp, "w", encoding="cp1251", errors="replace") as f:
        f.write(LISP_COMMON)
        f.write(body)
    if ops is not None:
        with open(out + ".ops", "w", encoding="cp1251", errors="replace") as f:
            for line in ops:
                f.write(line.replace("\n", " ") + "\n")
    q = lambda p: p.replace("\\", "/")  # noqa: E731
    doc.SendCommand(f'(load "{q(lsp)}") (mcp-go "{q(out)}") ')
    t0 = time.time()
    while not os.path.exists(done):
        if time.time() - t0 > timeout:
            raise RuntimeError("LISP в nanoCAD не завершился вовремя")
        time.sleep(0.1)
    time.sleep(0.05)
    status = _decode(open(done, "rb").read()).strip()
    text = _decode(open(out, "rb").read()) if os.path.exists(out) else ""
    for p in (lsp, out, done, out + ".ops"):
        _safe(lambda p=p: os.remove(p))
    if status != "OK":
        raise RuntimeError(f"Ошибка LISP: {status}")
    return text


def _cleanup_workdir():
    now = time.time()
    for name in _safe(lambda: os.listdir(WORK_DIR), []) or []:
        p = os.path.join(WORK_DIR, name)
        if now - _safe(lambda p=p: os.path.getmtime(p), now) > 3600:
            _safe(lambda p=p: os.remove(p))


# --------------------------------------------------------------------------- #
#  Разбор дампа и геометрия (чистые функции)                                   #
# --------------------------------------------------------------------------- #

_ZERO = 1e-6
MODEL = ("Model", "")


def _f(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _p(s):
    if not s:
        return None
    try:
        x, y, z = (float(v) for v in s.split(","))
        return (x, y, z)
    except ValueError:
        return None


def parse_verts(s):
    """';x,y[,bulge];x,y[,bulge]…' → [(x, y, bulge)]"""
    out = []
    for part in (s or "").split(";"):
        if not part:
            continue
        v = part.split(",")
        try:
            out.append((float(v[0]), float(v[1]), float(v[2]) if len(v) > 2 and v[2] else 0.0))
        except (ValueError, IndexError):
            continue
    return out


def poly_length_area(verts, closed):
    """Длина и площадь LWPOLYLINE с учётом дуг (bulge)."""
    n = len(verts)
    if n < 2:
        return 0.0, 0.0
    segs = n if closed else n - 1
    length = area2 = 0.0
    for i in range(segs):
        x1, y1, b = verts[i]
        x2, y2, _ = verts[(i + 1) % n]
        c = math.hypot(x2 - x1, y2 - y1)
        area2 += x1 * y2 - x2 * y1
        if abs(b) > 1e-12 and c > 0:
            th = 4 * math.atan(b)
            r = c / (2 * math.sin(abs(th) / 2))
            length += abs(r * th)
            area2 += (r * r * (th - math.sin(th)))  # сегмент дуги (знак по направлению)
        else:
            length += c
    return length, (abs(area2) / 2 if closed else 0.0)


def parse_dump(text: str) -> dict:
    recs, layers, blocks, attrs = [], [], [], defaultdict(list)
    regapps, clayer = None, None
    for line in text.splitlines():
        if not line:
            continue
        kind = line[0]
        if kind == "E":
            f = line.split(SEP, 21)
            if len(f) < 22:
                continue
            r = {"h": f[1], "t": f[2], "layer": f[3], "space": f[4], "color": f[5],
                 "ltype": f[6], "p10": _p(f[7]), "p11": _p(f[8]), "p13": _p(f[9]),
                 "p14": _p(f[10]), "elev": _f(f[11], 0.0), "name": f[12], "c40": _f(f[13]),
                 "c50": _f(f[14]), "c51": _f(f[15]), "c41": _f(f[16]), "c42": _f(f[17]),
                 "c70": int(_f(f[18], 0) or 0), "eff": f[19] or f[12],
                 "verts": parse_verts(f[20]), "text": f[21]}
            recs.append(r)
        elif kind == "A":
            f = line.split(SEP, 3)
            if len(f) == 4:
                attrs[f[1]].append((f[2], f[3]))
        elif kind == "L":
            f = line.split(SEP)
            if len(f) >= 5:
                flags, color = int(f[2]), int(f[3])
                layers.append({"name": f[1], "color": abs(color), "on": color >= 0,
                               "frozen": bool(flags & 1), "locked": bool(flags & 4),
                               "linetype": f[4]})
        elif kind == "B":
            f = line.split(SEP)
            if len(f) >= 3:
                blocks.append({"name": f[1], "flags": int(f[2])})
        elif kind == "R":
            regapps = int(line.split(SEP)[1])
        elif kind == "V":
            f = line.split(SEP)
            if len(f) >= 3 and f[1] == "CLAYER":
                clayer = f[2]
    for r in recs:
        r["attrs"] = attrs.get(r["h"], [])
        _derive(r)
    return {"recs": recs, "layers": layers, "blocks": blocks, "regapps": regapps,
            "clayer": clayer}


_FLATTEN_TYPES = {"LINE", "LWPOLYLINE", "TEXT", "MTEXT", "INSERT", "CIRCLE", "ARC",
                  "ELLIPSE", "POINT", "ATTDEF"}


def _derive(r):
    t, p10, p11 = r["t"], r["p10"], r["p11"]
    kp = None
    if t == "LWPOLYLINE":
        kp = (r["verts"][0][0], r["verts"][0][1], r["elev"]) if r["verts"] else None
    elif t == "DIMENSION":
        kp = p11
    elif t not in ("HATCH", "VIEWPORT"):
        kp = p10
    r["kp"] = kp
    z = False
    if t == "LINE":
        z = bool((p10 and abs(p10[2]) > _ZERO) or (p11 and abs(p11[2]) > _ZERO))
    elif t == "LWPOLYLINE":
        z = abs(r["elev"] or 0.0) > _ZERO
    elif t in _FLATTEN_TYPES and p10:
        z = abs(p10[2]) > _ZERO
    r["z"] = z
    zero = False
    if t == "LINE" and p10 and p11:
        zero = math.dist(p10, p11) < _ZERO
    elif t == "LWPOLYLINE":
        v = r["verts"]
        zero = len(v) < 2 or all(abs(a[0] - v[0][0]) < _ZERO and abs(a[1] - v[0][1]) < _ZERO
                                 for a in v)
    elif t in ("TEXT", "MTEXT"):
        zero = not clean_mtext(r["text"]).strip()
    r["zero"] = zero
    r["sig"] = _signature(r)


def _signature(r):
    R = lambda v: round(v, 2) if isinstance(v, float) else v  # noqa: E731
    P = lambda p: tuple(R(c) for c in p) if p else None  # noqa: E731
    t, L = r["t"], r["layer"]
    if t == "LINE":
        return ("L", L, tuple(sorted((P(r["p10"]), P(r["p11"])), key=str)))
    if t == "LWPOLYLINE":
        return ("P", L, tuple((R(x), R(y), R(b)) for x, y, b in r["verts"]), r["c70"] & 1)
    if t == "CIRCLE":
        return ("C", L, P(r["p10"]), R(r["c40"]))
    if t == "ARC":
        return ("A", L, P(r["p10"]), R(r["c40"]), R(r["c50"]), R(r["c51"]))
    if t == "INSERT":
        return ("I", L, r["name"], P(r["p10"]), R(r["c50"]), R(r["c41"]), R(r["c42"]))
    if t in ("TEXT", "MTEXT"):
        return ("T", L, r["text"], P(r["p10"]))
    return None


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def outlier_box(points, lo=0.02, hi=0.98, factor=2.0):
    """Рамка основного чертежа (процентили 2–98 %, расширенные на factor×размер)."""
    if len(points) < 20:
        return None
    xs = sorted(p[0] for p in points)
    ys = sorted(p[1] for p in points)
    x0, x1 = _percentile(xs, lo), _percentile(xs, hi)
    y0, y1 = _percentile(ys, lo), _percentile(ys, hi)
    m = factor * max(x1 - x0, y1 - y0, 1.0)
    return (x0 - m, y0 - m, x1 + m, y1 + m), (x0, y0, x1, y1)


def in_box(p, box):
    return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]


def find_issues(recs):
    pts = [r["kp"] for r in recs if r["kp"] is not None]
    box_info = outlier_box(pts)
    outliers, main, box = [], None, None
    if box_info:
        box, main = box_info
        outliers = [r for r in recs if r["kp"] is not None and not in_box(r["kp"], box)]
    seen, dups = set(), []
    for r in recs:
        s = r["sig"]
        if s is None:
            continue
        if s in seen:
            dups.append(r)
        else:
            seen.add(s)
    return {
        "outliers": outliers, "main_area": main, "box": box, "duplicates": dups,
        "zero": [r for r in recs if r["zero"]],
        "z_nonzero": [r for r in recs if r["z"]],
        "color_not_bylayer": [r for r in recs if r["color"] != "256"],
        "linetype_not_bylayer": [r for r in recs if r["ltype"] and
                                 r["ltype"].lower() not in ("bylayer", "послою")],
        "proxies": [r for r in recs if "PROXY" in r["t"].upper()],
        "raster": [r for r in recs if r["t"] in ("IMAGE", "OLE2FRAME", "PDFUNDERLAY",
                                                   "DWFUNDERLAY", "DGNUNDERLAY")],
    }


def _refine_outliers(doc, iss, limit=500):
    """Блок с далёкой точкой вставки, но геометрией на плане — не мусор (например,
    размеры Archicad). Проверяем габариты таких вставок через COM."""
    box = iss.get("box")
    if not box:
        return iss
    keep = []
    for i, r in enumerate(iss["outliers"]):
        if r["t"] == "INSERT" and i < limit:
            try:
                mn, mx = doc.HandleToObject(r["h"]).GetBoundingBox()
                if not (mx[0] < box[0] or mn[0] > box[2] or mx[1] < box[1] or mn[1] > box[3]):
                    continue
            except Exception:
                pass
        keep.append(r)
    iss["outliers"] = keep
    return iss


def chain_layout(points, angle_deg=0.0, offset=1000.0, tol=1e-6):
    """Раскладка размерной цепочки: сегменты (p1, p2, location, length) и параметры линии."""
    a = math.radians(angle_deg)
    ux, uy = math.cos(a), math.sin(a)
    nx, ny = -uy, ux
    proj = sorted(((float(p[0]) * ux + float(p[1]) * uy, float(p[0]) * nx + float(p[1]) * ny,
                    (float(p[0]), float(p[1]))) for p in points), key=lambda t: t[0])
    uniq = []
    for t in proj:
        if not uniq or abs(t[0] - uniq[-1][0]) > tol:
            uniq.append(t)
    if len(uniq) < 2:
        return [], None
    perps = [t[1] for t in uniq]
    base = (max(perps) + offset) if offset >= 0 else (min(perps) + offset)
    segs = []
    for (sa, _pa, a_xy), (sb, _pb, b_xy) in zip(uniq, uniq[1:]):
        s = (sa + sb) / 2.0
        segs.append((a_xy, b_xy, (s * ux + base * nx, s * uy + base * ny), sb - sa))
    line = {"base": base, "u": (ux, uy), "n": (nx, ny),
            "start": uniq[0][2], "end": uniq[-1][2], "total": uniq[-1][0] - uniq[0][0]}
    return segs, line


def dim_text(value: float, precision: int = 0) -> str:
    if precision <= 0:
        return str(int(round(value)))
    return f"{value:.{precision}f}".replace(".", ",")


def est_text_width(text: str, height: float) -> float:
    """Оценка ширины размерного числа (шрифт ГОСТ ≈ 0,7 высоты на символ + зазоры)."""
    return len(text) * height * 0.72 + height * 0.6


def narrow_flags(lengths, widths):
    return [w > L for L, w in zip(lengths, widths)]


_MTEXT_CODES = re.compile(r"\\[ACcFfHhLlOoQqTtWwp][^;\\{}]*;|\\[PpNn~]|[{}]|\\[LlOoKk]")


def clean_mtext(s: str) -> str:
    s = (s or "").replace("\\P", "\n")
    s = _MTEXT_CODES.sub("", s)
    return s.replace("\\\\", "\\").strip()


def _glob_ok(name: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    return any(fnmatch.fnmatchcase(name.lower(), p.strip().lower())
               for p in pattern.split(";") if p.strip())


# --------------------------------------------------------------------------- #
#  Сканирование (LISP) и правка (LISP)                                         #
# --------------------------------------------------------------------------- #

def _doc_key(doc):
    return str(_safe(lambda: doc.FullName) or _safe(lambda: doc.Name))


def _scan(doc, use_cache=True):
    key = _doc_key(doc)
    c = S.scan_cache
    if use_cache and c and c["key"] == key and time.time() - c["time"] < 120:
        return c
    t0 = time.time()
    data = parse_dump(_lisp(doc, LISP_DUMP))
    model = [r for r in data["recs"] if r["space"] in MODEL]
    S.scan_cache = {"key": key, "time": time.time(), "all": data["recs"], "recs": model,
                    "layers": data["layers"], "blocks": data["blocks"],
                    "regapps": data["regapps"], "clayer": data["clayer"],
                    "seconds": round(time.time() - t0, 1)}
    return S.scan_cache


def _apply(doc, ops: list[tuple[str, str, str]]) -> dict:
    """Массовая правка в nanoCAD одной отметкой отмены. ops: (handle|слой, операция, аргумент)."""
    if not ops:
        return {"applied": 0, "failed": 0}
    text = _lisp(doc, LISP_APPLY, ops=[SEP.join((h, op, str(arg))) for h, op, arg in ops])
    ok = fail = 0
    failed = []
    for line in text.splitlines():
        parts = line.split(SEP)
        if len(parts) == 2:
            if parts[1] == "1":
                ok += 1
            else:
                fail += 1
                if len(failed) < 20:
                    failed.append(parts[0])
    S.scan_cache = None
    return {"applied": ok, "failed": fail, "failed_sample": failed}


# --------------------------------------------------------------------------- #
#  COM-утилиты                                                                 #
# --------------------------------------------------------------------------- #

def vpt(p) -> VARIANT:
    z = float(p[2]) if len(p) > 2 else 0.0
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (float(p[0]), float(p[1]), z))


def _r(v, nd=3):
    try:
        return round(float(v), nd)
    except Exception:
        return v


def _pt(p, nd=1):
    return [_r(c, nd) for c in tuple(p)[:3]] if p else None


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


@contextlib.contextmanager
def _undo_group(doc):
    started = _safe(lambda: (doc.StartUndoMark(), True)[1], False)
    try:
        yield
    finally:
        if started:
            _safe(lambda: doc.EndUndoMark())


def _sysvars(doc, names):
    return {n: _jsonable(_safe(lambda n=n: doc.GetVariable(n))) for n in names}


def _hl(recs, limit=40):
    return [r["h"] for r in recs[:limit]]


# --------------------------------------------------------------------------- #
#  Инструменты: подключение и обзор                                            #
# --------------------------------------------------------------------------- #

@mcp.tool()
async def status() -> dict:
    """Подключение к разрешённому экземпляру nanoCAD и сведения об активном чертеже."""
    def go():
        inst = _find_instances()
        if not inst:
            return {"ok": False, "server_version": __version__,
                    "process_id": S.access[0],
                    "error": "Доступ включён, но COM-объект выбранного окна недоступен. "
                             "Проверьте сообщение кнопки MCP в этом окне."}
        app = _app()
        doc = _safe(lambda: app.ActiveDocument)
        info = {"ok": True, "server_version": __version__,
                "instances": [_describe(p, a) for p, a in inst],
                "connected": _describe(S.prog_id, app)}
        if doc is not None:
            info["document"] = {
                "name": _safe(lambda: str(doc.Name)), "path": _safe(lambda: str(doc.FullName)),
                "saved": _safe(lambda: bool(doc.Saved)),
                "sysvars": _sysvars(doc, ["INSUNITS", "LUNITS", "DIMSCALE", "LTSCALE",
                                          "EXTMIN", "EXTMAX", "CLAYER", "DIMSTYLE", "CMDACTIVE"]),
            }
        return info
    return await _run(go, readonly=True)


@mcp.tool()
async def use_instance(match: str) -> dict:
    """Проверить, совпадает ли включённый экземпляр с match (версия, часть заголовка)."""
    def go():
        app = _app()
        instance = _describe(S.prog_id, app)
        if match.lower() not in " ".join(str(v) for v in instance.values()).lower():
            return {"ok": False, "error": "Нажмите кнопку MCP в нужном экземпляре nanoCAD.",
                    "connected": instance}
        return {"ok": True, "connected": instance}
    return await _run(go)


@mcp.tool()
async def documents(activate: str | None = None, open_path: str | None = None) -> dict:
    """Открытые чертежи. activate — сделать активным (часть имени); open_path — открыть файл."""
    def go():
        app = _app()
        if open_path:
            app.Documents.Open(open_path)
            S.scan_cache = None
        if activate:
            for d in app.Documents:
                if activate.lower() in str(d.Name).lower():
                    d.Activate()
                    S.scan_cache = None
                    break
            else:
                return {"ok": False, "error": f"Чертёж '{activate}' не найден среди открытых"}
        return {"ok": True, "active": _safe(lambda: str(app.ActiveDocument.Name)),
                "documents": [{"name": str(d.Name), "path": _safe(lambda d=d: str(d.FullName)),
                               "saved": _safe(lambda d=d: bool(d.Saved))} for d in app.Documents]}
    return await _run(go)


@mcp.tool()
async def drawing_summary() -> dict:
    """Сводка по модели: количество объектов по типам и слоям, слои, блоки, единицы."""
    def go():
        doc = _doc()
        sc = _scan(doc, use_cache=False)
        recs = sc["recs"]
        return _jsonable({
            "ok": True, "document": str(doc.Name), "model_entities": len(recs),
            "paper_entities": len(sc["all"]) - len(recs), "scan_seconds": sc["seconds"],
            "by_type": dict(Counter(r["t"] for r in recs).most_common()),
            "by_layer": dict(Counter(r["layer"] for r in recs).most_common(60)),
            "layers_total": len(sc["layers"]), "blocks_total": len(sc["blocks"]),
            "regapps": sc["regapps"],
            "sysvars": _sysvars(doc, ["INSUNITS", "LUNITS", "DIMSCALE", "LTSCALE",
                                      "EXTMIN", "EXTMAX"]),
        })
    return await _run(go, readonly=True, job=_jobkey("summary"))


# --------------------------------------------------------------------------- #
#  Подложка АР: анализ и чистка                                                #
# --------------------------------------------------------------------------- #

@mcp.tool()
async def audit_underlay() -> dict:
    """Анализ подложки АР: что в ней «странного». Ничего не меняет.

    Ищет: объекты далеко от плана, точные дубликаты, нулевые линии и пустые тексты,
    ненулевую Z, цвет/тип линий не ПоСлою, proxy, растры/PDF/OLE, пустые/выключенные/
    замороженные слои, внешние ссылки, мусорные блоки и regapp, единицы.
    Возвращает количества и образцы дескрипторов (handle) — их можно показать через zoom."""
    def go():
        doc = _doc()
        sc = _scan(doc, use_cache=False)
        recs = sc["recs"]
        iss = _refine_outliers(doc, find_issues(recs))
        used = Counter(r["layer"] for r in sc["all"])
        lay = sc["layers"]
        blocks = [b for b in sc["blocks"]]
        inserted = {r["name"] for r in sc["all"] if r["t"] == "INSERT"}

        def issue(code, title, items, hint):
            return {"code": code, "title": title, "count": len(items),
                    "sample_handles": _hl(items), "hint": hint}

        issues = [
            issue("outliers", "Объекты далеко от основного плана", iss["outliers"],
                  "Обычно мусор или забытые фрагменты; из-за них «Показать всё» уводит в точку."),
            issue("duplicates", "Точные дубликаты (объект лежит на объекте)", iss["duplicates"],
                  "Удаляются безопасно, первый экземпляр остаётся."),
            issue("zero", "Линии/полилинии нулевой длины и пустые тексты", iss["zero"],
                  "Мусор, удалить."),
            issue("z_nonzero", "Объекты с ненулевой Z", iss["z_nonzero"],
                  "Мешают привязкам и размерам; привести к Z=0 (flatten)."),
            issue("color_not_bylayer", "Цвет задан не ПоСлою", iss["color_not_bylayer"],
                  "Подложка будет пёстрой; привести к ПоСлою."),
            issue("linetype_not_bylayer", "Тип линий не ПоСлою", iss["linetype_not_bylayer"],
                  "Привести к ПоСлою."),
            issue("proxies", "Proxy-объекты (чужие приложения)", iss["proxies"],
                  "Часто от плагинов АР; обычно удалить."),
            issue("raster", "Растры / PDF / OLE", iss["raster"], "Утяжеляют файл; решить, нужны ли."),
        ]
        named_blocks = [b for b in blocks if not b["name"].startswith("*")]
        return _jsonable({
            "ok": True, "document": str(doc.Name), "model_entities": len(recs),
            "scan_seconds": sc["seconds"],
            "main_area": [_r(v, 0) for v in iss["main_area"]] if iss["main_area"] else None,
            "issues": [i for i in issues if i["count"]],
            "by_type": dict(Counter(r["t"] for r in recs).most_common()),
            "layers": {
                "total": len(lay),
                "empty": [x["name"] for x in lay if not used.get(x["name"])
                          and x["name"] not in ("0", "Defpoints") and "|" not in x["name"]],
                "xref_layers": sum(1 for x in lay if "|" in x["name"]),
                "off": [x["name"] for x in lay if not x["on"]],
                "frozen": [x["name"] for x in lay if x["frozen"]],
                "locked": [x["name"] for x in lay if x["locked"]],
            },
            "blocks": {
                "definitions": len(blocks),
                "anonymous": len(blocks) - len(named_blocks),
                "xrefs": [b["name"] for b in blocks if b["flags"] & 4],
                "named_not_inserted_top_level": sum(1 for b in named_blocks
                                                    if b["name"] not in inserted),
                "note": "Невставленные блоки (если не вложены в другие) убирает purge",
            },
            "regapps": sc["regapps"],
            "sysvars": _sysvars(doc, ["INSUNITS", "LUNITS", "DIMSCALE", "LTSCALE",
                                      "EXTMIN", "EXTMAX"]),
        })
    return await _run(go, readonly=True, job=_jobkey("audit"))


_CLEAN_ACTIONS = ("outliers", "duplicates", "zero", "flatten", "color_bylayer",
                  "linetype_bylayer", "proxies", "raster", "purge", "audit")


@mcp.tool()
async def cleanup_underlay(actions: list[str], dry_run: bool = True,
                           exclude_handles: list[str] | None = None) -> dict:
    """Чистка подложки. По умолчанию dry_run=True — только показывает, что будет сделано.

    actions (любые из):
      outliers         — удалить объекты далеко от основного плана
      duplicates       — удалить точные дубликаты
      zero             — удалить нулевые линии/полилинии и пустые тексты
      flatten          — привести Z к 0
      color_bylayer    — цвет объектов → ПоСлою
      linetype_bylayer — тип линий объектов → ПоСлою
      proxies          — удалить proxy-объекты
      raster           — удалить растры, PDF-подложки, OLE
      purge            — очистить неиспользуемые слои/блоки/стили
      audit            — проверка и исправление ошибок файла
    exclude_handles — объекты, которые не трогать.
    Правка объектов — одна отметка отмены (Ctrl+Z откатывает целиком; purge/audit — отдельно).
    Объекты на заблокированных слоях пропускаются."""
    def go():
        bad = [a for a in actions if a not in _CLEAN_ACTIONS]
        if bad:
            return {"ok": False, "error": f"Неизвестные действия: {bad}. Доступны: {list(_CLEAN_ACTIONS)}"}
        doc = _doc()
        result = {"ok": True, "dry_run": dry_run, "document": str(doc.Name), "done": {}}
        skip = set(exclude_handles or [])
        ent_actions = [a for a in actions if a not in ("purge", "audit")]
        if ent_actions:
            sc = _scan(doc, use_cache=False)
            iss = _refine_outliers(doc, find_issues(sc["recs"]))
            locked = {x["name"] for x in sc["layers"] if x["locked"]}
            plan = {
                "outliers": ("erase", iss["outliers"], ""),
                "duplicates": ("erase", iss["duplicates"], ""),
                "zero": ("erase", iss["zero"], ""),
                "proxies": ("erase", iss["proxies"], ""),
                "raster": ("erase", iss["raster"], ""),
                "flatten": ("flatten", [r for r in iss["z_nonzero"] if r["t"] in _FLATTEN_TYPES], ""),
                "color_bylayer": ("color", iss["color_not_bylayer"], "256"),
                "linetype_bylayer": ("ltype", iss["linetype_not_bylayer"], "BYLAYER"),
            }
            ops, erased = [], set()
            for a in ent_actions:
                op, recs, arg = plan[a]
                recs = [r for r in recs if r["h"] not in skip and r["h"] not in erased]
                todo = [r for r in recs if r["layer"] not in locked]
                result["done"][a] = {"count": len(todo), "sample_handles": _hl(todo, 20),
                                     "skipped_on_locked_layers": len(recs) - len(todo)}
                if op == "erase":
                    erased.update(r["h"] for r in todo)
                ops += [(r["h"], op, arg) for r in todo]
            if not dry_run and ops:
                result["applied"] = _apply(doc, ops)
        if "purge" in actions:
            before = {"layers": _safe(lambda: int(doc.Layers.Count)),
                      "blocks": _safe(lambda: int(doc.Blocks.Count))}
            entry = {"before": before}
            if not dry_run:
                try:
                    for _ in range(3):
                        doc.PurgeAll()
                    entry["via"] = "PurgeAll"
                except Exception:
                    for _ in range(3):
                        _lisp(doc, '(defun mcp-main (fn) (command "_-PURGE" "_A" "*" "_N"))')
                    entry["via"] = "command"
                entry["after"] = {"layers": _safe(lambda: int(doc.Layers.Count)),
                                  "blocks": _safe(lambda: int(doc.Blocks.Count))}
                S.scan_cache = None
            result["done"]["purge"] = entry
        if "audit" in actions:
            if not dry_run:
                try:
                    doc.AuditInfo(True)
                    result["done"]["audit"] = {"applied": True}
                except Exception:
                    _lisp(doc, '(defun mcp-main (fn) (command "_AUDIT" "_Y"))')
                    result["done"]["audit"] = {"applied": True, "via": "command"}
                S.scan_cache = None
            else:
                result["done"]["audit"] = {"will_run": True}
        return _jsonable(result)
    return await _run(go, job=_jobkey("cleanup", a=actions, d=dry_run, x=exclude_handles))


# --------------------------------------------------------------------------- #
#  Слои                                                                        #
# --------------------------------------------------------------------------- #

@mcp.tool()
async def layers(action: str = "list", pattern: str | None = None,
                 color: int | None = None, on: bool | None = None,
                 freeze: bool | None = None, lock: bool | None = None,
                 linetype: str | None = None, target: str | None = None,
                 new_name: str | None = None, dry_run: bool = False) -> dict:
    """Слои. pattern — маска имени (* ?), несколько через «;», регистр не важен.

    action:
      list   — список с числом объектов
      set    — свойства по маске: color (1–255), on, freeze, lock, linetype
      merge  — перенести объекты слоёв по маске на target (создаётся) и удалить пустые слои
      rename — переименовать слой pattern → new_name
      delete — удалить слои по маске (только пустые)
      create — создать слой pattern (color/linetype)
    """
    def go():
        doc = _doc()
        if action == "create":
            la = doc.Layers.Add(pattern)
            if color is not None:
                la.Color = color
            if linetype:
                la.Linetype = linetype
            S.scan_cache = None
            return {"ok": True, "created": pattern}
        if action == "rename":
            doc.Layers.Item(pattern).Name = new_name
            S.scan_cache = None
            return {"ok": True, "renamed": [pattern, new_name]}

        sc = _scan(doc, use_cache=False)
        counts = Counter(r["layer"] for r in sc["all"])
        matched = [x for x in sc["layers"] if _glob_ok(x["name"], pattern)]
        if action == "list":
            return _jsonable({"ok": True, "count": len(matched), "current": sc["clayer"],
                              "layers": [dict(x, entities=counts.get(x["name"], 0)) for x in matched]})
        if not pattern:
            return {"ok": False, "error": "Нужна маска pattern"}
        names = [x["name"] for x in matched]

        if action == "set":
            ops = []
            for n in names:
                if color is not None:
                    ops.append((n, "Lcolor", str(color)))
                if on is not None:
                    ops.append((n, "Lon", "1" if on else "0"))
                if lock is not None:
                    ops.append((n, "Llock", "1" if lock else "0"))
                if freeze is not None and n != sc["clayer"]:
                    ops.append((n, "Lfreeze", "1" if freeze else "0"))
                if linetype:
                    ops.append((n, "Lltype", linetype))
            if dry_run:
                return {"ok": True, "dry_run": True, "layers": names, "operations": len(ops)}
            res = _apply(doc, ops)
            return {"ok": True, "layers": names[:300], **res}

        if action == "merge":
            if not target:
                return {"ok": False, "error": "Нужен target — слой, куда переносить"}
            src = [n for n in names if n.lower() != target.lower()]
            locked_src = [x["name"] for x in matched if x["locked"] and x["name"] in src]
            recs = [r for r in sc["all"] if r["layer"] in set(src)]
            if dry_run:
                return {"ok": True, "dry_run": True, "from_layers": src, "entities": len(recs),
                        "locked_layers_will_be_unlocked": locked_src}
            ops = [(n, "Llock", "0") for n in locked_src]
            ops += [(r["h"], "layer", target) for r in recs]
            res = _apply(doc, ops)
            deleted, kept = [], []
            for n in src:
                try:
                    doc.Layers.Item(n).Delete()
                    deleted.append(n)
                except Exception:
                    kept.append(n)
            S.scan_cache = None
            return {"ok": True, "target": target, **res, "deleted_layers": deleted,
                    "not_deleted": kept,
                    "note": "Не удалены слои, которые используются внутри блоков или текущий слой"}

        if action == "delete":
            if dry_run:
                return {"ok": True, "dry_run": True, "layers": names}
            deleted, kept = [], []
            for n in names:
                try:
                    doc.Layers.Item(n).Delete()
                    deleted.append(n)
                except Exception:
                    kept.append(n)
            S.scan_cache = None
            return {"ok": True, "deleted": deleted, "not_deleted_in_use": kept}
        return {"ok": False, "error": f"Неизвестное действие {action}"}
    return await _run(go, readonly=(action == "list"),
                      job=_jobkey("layers", a=action, p=pattern, c=color, o=on, f=freeze,
                                  l=lock, lt=linetype, t=target, n=new_name, d=dry_run))


# --------------------------------------------------------------------------- #
#  Запросы и извлечение данных                                                 #
# --------------------------------------------------------------------------- #

def _brief(r):
    t = r["t"]
    d = {"handle": r["h"], "type": t, "layer": r["layer"]}
    if r["color"] != "256":
        d["color"] = r["color"]
    if t == "LINE":
        d.update(start=_pt(r["p10"]), end=_pt(r["p11"]),
                 length=_r(math.dist(r["p10"][:2], r["p11"][:2]), 1) if r["p10"] and r["p11"] else None)
    elif t == "LWPOLYLINE":
        closed = bool(r["c70"] & 1)
        L, A = poly_length_area(r["verts"], closed)
        d.update(vertices=len(r["verts"]), closed=closed, length=_r(L, 1),
                 first=_pt(r["kp"]))
        if closed:
            d["area"] = _r(A, 1)
    elif t in ("CIRCLE", "ARC"):
        d.update(center=_pt(r["p10"]), radius=_r(r["c40"], 1))
    elif t in ("TEXT", "MTEXT", "MULTILEADER"):
        d.update(text=clean_mtext(r["text"]), at=_pt(r["p10"]), height=_r(r["c40"], 1))
    elif t == "INSERT":
        d.update(block=r["eff"], at=_pt(r["p10"]),
                 rotation=_r(math.degrees(r["c50"] or 0.0), 1))
        if r["attrs"]:
            d["attributes"] = dict(r["attrs"])
    elif t == "DIMENSION":
        d.update(value=_r(r["c42"], 1), override=r["text"] or None, text_at=_pt(r["p11"]),
                 p1=_pt(r["p13"]), p2=_pt(r["p14"]))
    elif r["kp"]:
        d["at"] = _pt(r["kp"])
    return d


_SELECTION_LISP = r'''
(defun mcp-main (fn / f selected i input handle obj saved)
  (setq f (open fn "w"))
  (if (getcname "_NCMCPGETSELECTION")
    (progn
      ;; NETLOAD may clear PICKFIRST. Restore it only if the selection is still empty.
      (if (and (findfile (strcat fn ".ops")) (null (ssget "_I")))
        (progn
          (setq input (open (strcat fn ".ops") "r") saved (ssadd))
          (while (setq handle (read-line input))
            (if (setq obj (handent handle)) (ssadd obj saved)))
          (close input)
          (if (> (sslength saved) 0) (sssetfirst nil saved))))
      (write-line <<EXPRESSION>> f))
    (progn
      (write-line "LOAD_MODULE" f)
      (setq selected (ssget "_I") i 0)
      (if selected
        (repeat (sslength selected)
          (write-line (cdr (assoc 5 (entget (ssname selected i)))) f)
          (setq i (1+ i))))))
  (close f))
'''


def _selection_request(expression: str, expected_document: str | None = None) -> dict:
    doc = _doc()
    if str(_app().Version).split(".")[0] != "26":
        raise RuntimeError("Для команд чтения выделения нужен nanoCAD 26.")
    document = os.path.normcase(_doc_key(doc))
    if expected_document and document != os.path.normcase(expected_document):
        raise Busy("Сменился активный чертёж. Повторите предварительный просмотр.")
    body = _SELECTION_LISP.replace("<<EXPRESSION>>", expression)
    text = _lisp(doc, body)
    if text.startswith("LOAD_MODULE"):
        handles = text.splitlines()[1:]
        dll = Path(__file__).resolve().with_name("NanoCadMcp.Tools26.dll")
        if not dll.is_file():
            raise RuntimeError(f"В комплекте nanoCAD MCP отсутствует {dll.name}.")
        if enabled_session() != S.access or os.path.normcase(_doc_key(_doc())) != document:
            raise Busy("Изменилось активное окно nanoCAD. Повторите запрос.")
        doc.SendCommand(f'_-NETLOAD\n"{dll}"\n')
        if enabled_session() != S.access or os.path.normcase(_doc_key(_doc())) != document:
            raise Busy("После загрузки DLL изменилось активное окно. Повторите запрос.")
        text = _lisp(doc, body, ops=handles or None)
        if text.startswith("LOAD_MODULE"):
            raise RuntimeError(f"nanoCAD не зарегистрировал модуль {dll.name}. Проверьте сообщения NETLOAD.")
    data = json.loads(text)
    if data.get("ok") and os.path.normcase(data["document"]) != document:
        raise Busy("При чтении сменился активный чертёж. Повторите запрос.")
    return data


_EXTRA_LISP = r'''
(defun mcp-main (fn / f)
  (setq f (open fn "w"))
  (if (getcname "_NCMCPExtrasInfo")
    (write-line (NCMCP_EXTRA (strcat fn ".ops")) f)
    (write-line "LOAD_MODULE" f))
  (close f))
'''


async def _call_extra(name, arguments):
    from extra_commands import invoke

    def go():
        app = _app()
        if str(app.Version).split(".")[0] != "26":
            raise ValueError("Дополнительные команды используют SDK nanoCAD 26")
        doc = _doc()
        document = _doc_key(doc)

        def check_target():
            require_enabled(name)
            if enabled_session() != S.access or _doc_key(_doc()) != document:
                raise Busy("Изменилось разрешение или активный чертёж")

        def request(method, path, body):
            check_target()
            payload = json.dumps({"Pid": S.access[0], "Document": document, "Method": method,
                                  "Path": path, "Body": body or {}}, ensure_ascii=True, allow_nan=False)
            response = _lisp(doc, _EXTRA_LISP, ops=[payload])
            if response.strip() == "LOAD_MODULE":
                check_target()
                dll = Path(__file__).with_name("NanoCadMcp.Extras26.dll").resolve(strict=True)
                doc.SendCommand(f'_-NETLOAD\n"{dll}"\n')
                check_target()
                response = _lisp(doc, _EXTRA_LISP, ops=[payload])
            if response.strip() == "LOAD_MODULE":
                raise RuntimeError("Не удалось загрузить NanoCadMcp.Extras26.dll")
            data = json.loads(response)
            if isinstance(data, dict) and (data.get("error") or data.get("success") is False):
                raise ValueError(data.get("error") or "Команда nanoCAD завершилась ошибкой")
            return data

        try:
            return invoke(name, arguments, request, (S.access, document), check_target)
        finally:
            S.scan_cache = None

    return await _run(go, job=_jobkey(name, args=arguments))


mcp.call_extra = _call_extra


async def _specialty_button(command, title, document, dry_run):
    def go():
        doc = _doc()
        target = _doc_key(doc)
        if not dry_run and (not document or os.path.normcase(document) != os.path.normcase(target)):
            raise ValueError("Укажите document из предварительного просмотра в активном чертеже.")
        if str(_app().Version).split(".")[0] != "26":
            raise ValueError("Для этой команды нужен nanoCAD 26.")
        registered = f'(or (getcname "_{command}") (getcname "{command}"))'
        probe = f'(if {registered} 1 0)'
        if command == "GcSfRedPntGradeMarker":
            # GeoniCS 26 handles the command in GcSrfEngine even when getcname returns nil.
            module = '(member "GCSRFENGINE" (mapcar \'(lambda (m) (strcase (vl-filename-base m))) (arx)))'
            probe = f'(cond ({registered} 1) ({module} 2) (T 0))'
        source = _lisp(doc, LISP_EVAL.replace("<<CODE>>", probe)).strip()
        if source not in ("1", "2"):
            return {"ok": False, "document": target, "command": command,
                    "error": "Команда отсутствует в разрешённом окне. Проверьте, где загружен профильный модуль."}
        if dry_run:
            return {"ok": True, "dry_run": True, "document": target, "command": command, "action": title,
                    "detected_by": "loaded_GcSrfEngine" if source == "2" else "getcname"}
        if enabled_session() != S.access:
            raise Busy("Сменилось разрешённое окно nanoCAD.")
        if command == "GcSfRedPntGradeMarker" and int(doc.GetVariable("CVPORT")) == 1:
            return {"ok": False, "document": target, "error": "Перейдите в модель или активируйте видовой экран с опорными точками."}
        S.scan_cache = None
        # The LISP transport is ANSI. Pass the exact Unicode document name to the native check.
        encoded_target = base64.b64encode(target.encode("utf-8")).decode("ascii")
        return _selection_request(f'(NCMCP_QUEUE_SPECIALTY "{command}" "{encoded_target}")',
                                  expected_document=target)

    return await _run(go, readonly=dry_run, job=_jobkey(command, document=document, preview=dry_run))


@mcp.tool()
async def electro_project_manager(document: str | None = None, dry_run: bool = True) -> dict:
    """Открыть менеджер проекта nanoCAD Электро. После просмотра передайте document и dry_run=False."""
    return await _specialty_button("CW_PROJECT", "Открыть менеджер проекта Электро", document, dry_run)


@mcp.tool()
async def electro_object_properties(document: str | None = None, dry_run: bool = True) -> dict:
    """Открыть штатную страницу свойств в Электро. Выбор объекта выполняется в CAD.
    После просмотра передайте document и dry_run=False."""
    return await _specialty_button("CW_SHOW_PROPERTIES", "Открыть свойства объекта Электро", document, dry_run)


@mcp.tool()
async def geonics_slope_marker(document: str | None = None, dry_run: bool = True) -> dict:
    """Поставить уклоноуказатель GeoniCS. После просмотра передайте document и dry_run=False.
    Для работы нужен открытый проект GeoniCS. Укажите две опорные точки в CAD.
    queued означает только передачу команды в окно."""
    return await _specialty_button("GcSfRedPntGradeMarker", "Указать две опорные точки GeoniCS",
                                   document, dry_run)


@mcp.tool()
async def geonics_inspect_relief(handles: list[str] | None = None, limit: int = 20) -> dict:
    """Осмотреть рельеф и признаки оврага в модели. Считает опорные точки, структурные линии,
    указатели уклонов, грани поверхности и ищет явные обозначения оврага, бровки, тальвега.
    Передайте handles из выделения, чтобы ограничить проверку. Возвращает примеры и слои."""
    if not 1 <= limit <= 100:
        return {"ok": False, "error": "limit должен быть от 1 до 100."}
    def go():
        doc = _doc()
        records = _scan(doc, use_cache=False)["recs"]
        wanted = None if handles is None else {h.upper() for h in handles}
        types = Counter()
        layers = Counter()
        examples = []
        labels = []
        label_count = 0
        found = set()
        for r in records:
            if wanted is not None and r["h"].upper() not in wanted:
                continue
            if wanted is not None:
                found.add(r["h"].upper())
            sources = [r["layer"], r["eff"], clean_mtext(r["text"]), *(v for _, v in r["attrs"])]
            matches = [s for s in sources if any(w in s.casefold() for w in ("овраг", "тальвег", "бровк"))]
            if matches:
                label_count += 1
                if len(labels) < limit:
                    labels.append({"handle": r["h"], "layer": r["layer"], "label": matches[0][:240]})
            terrain_layer = any(w in r["layer"].casefold() for w in ("откос", "рельеф", "горизонтал", "гидрограф", "отметк"))
            if not (terrain_layer or matches or r["t"] in {"GCDBREDPOINT", "GCDBBREAKLINE", "GCDBGRADEMARKER",
                                                          "GCDBBOUNDARY", "GCDBCONTOURPREVIEW", "3DFACE"}):
                continue
            types[r["t"]] += 1
            layers[r["layer"]] += 1
            if len(examples) < limit:
                examples.append(_brief(r))
        return {"ok": True, "document": _doc_key(doc), "by_type": dict(types), "by_layer": dict(layers),
                "examples": examples, "label_matches": label_count, "labels": labels,
                "unknown_handles": sorted((wanted or set()) - found),
                "note": "Подписи и названия слоёв указывают кандидатов. Геометрию дна и бровок подтверждают по месту."}
    return await _run(go, readonly=True, job=_jobkey("geonics_inspect_relief", handles=handles, limit=limit))


@mcp.tool()
async def get_selection(offset: int = 0, limit: int = 50) -> dict:
    """Прочитай выделенное в nanoCAD 26. Тип, слой, handle, концы и 3D-длина
    отрезков, имена и атрибуты блоков. offset и limit (1–500) задают страницу."""
    if offset < 0 or limit < 1 or limit > 500 or offset > 2147483647:
        return {"ok": False, "error": "offset должен быть в пределах 0–2147483647, limit — 1–500."}
    return await _run(_selection_request, f"(NCMCP_GET_SELECTION {offset} {limit})",
                      readonly=True, job=_jobkey("get_selection", offset=offset, limit=limit))


@mcp.tool()
async def measure_selection() -> dict:
    """Измерь всё текущее выделение в nanoCAD 26. Сумма длин отрезков, дуг,
    окружностей, полилиний, эллипсов и сплайнов. Площадь замкнутых плоских кривых
    и штриховок в квадратных единицах чертежа. Указывает пропуски и ошибки."""
    return await _run(_selection_request, "(NCMCP_MEASURE_SELECTION)",
                      readonly=True, job="measure_selection")


@mcp.tool()
async def query(layer: str | None = None, type: str | None = None,
                bbox: list[float] | None = None, handles: list[str] | None = None,
                text_contains: str | None = None, limit: int = 300, offset: int = 0) -> dict:
    """Найти объекты модели. layer — маска слоя; type — тип DXF или его часть (LINE,
    LWPOLYLINE, TEXT, MTEXT, INSERT, DIMENSION, HATCH, CIRCLE…); bbox — [x1, y1, x2, y2]
    по характерной точке; handles — конкретные дескрипторы; text_contains — по тексту.
    Возвращает координаты, длины, площади, тексты, атрибуты."""
    def go():
        doc = _doc()
        sc = _scan(doc)
        hs = set(handles or [])
        box = None
        if bbox:
            x1, y1, x2, y2 = bbox
            box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        flt = (text_contains or "").lower()
        out, matched = [], 0
        for r in (sc["all"] if hs else sc["recs"]):
            if hs and r["h"] not in hs:
                continue
            if layer and not _glob_ok(r["layer"], layer):
                continue
            if type and type.upper() not in r["t"].upper():
                continue
            if box and (r["kp"] is None or not in_box(r["kp"], box)):
                continue
            if flt and flt not in clean_mtext(r["text"]).lower() \
                    and not any(flt in v.lower() for _, v in r["attrs"]):
                continue
            matched += 1
            if matched <= offset or len(out) >= limit:
                continue
            out.append(_brief(r))
        return _jsonable({"ok": True, "matched": matched, "returned": len(out),
                          "offset": offset, "items": out})
    return await _run(go, readonly=True, job=_jobkey("query", l=layer, t=type, b=bbox,
                                                     h=handles, x=text_contains, n=limit, o=offset))


@mcp.tool()
async def get_texts(layer: str | None = None, contains: str | None = None,
                    include_attributes: bool = True, include_tables: bool = True,
                    include_dimension_overrides: bool = False, limit: int = 5000) -> dict:
    """Все тексты модели: TEXT, MTEXT (без кодов форматирования), атрибуты блоков,
    мультивыноски, ячейки таблиц. contains — фильтр по подстроке."""
    def go():
        doc = _doc()
        sc = _scan(doc)
        flt = (contains or "").lower()
        items = []

        def add(kind, text, r, **extra):
            if not text or (flt and flt not in text.lower()) or len(items) >= limit:
                return
            items.append({"kind": kind, "text": text, "layer": r["layer"], "handle": r["h"],
                          "at": _pt(r["kp"]), **extra})

        tables = []
        for r in sc["recs"]:
            if layer and not _glob_ok(r["layer"], layer):
                continue
            t = r["t"]
            if t == "TEXT":
                add("text", r["text"], r)
            elif t == "MTEXT":
                add("mtext", clean_mtext(r["text"]), r)
            elif t == "MULTILEADER":
                add("mleader", clean_mtext(r["text"]), r)
            elif t == "INSERT" and include_attributes:
                for tag, val in r["attrs"]:
                    add("attribute", val, r, block=r["eff"], tag=tag)
            elif t == "DIMENSION" and include_dimension_overrides and r["text"]:
                add("dim_override", r["text"], r, value=_r(r["c42"], 1))
            elif t == "ACAD_TABLE" and include_tables:
                tables.append(r)
        for r in tables[:50]:
            try:
                e = doc.HandleToObject(r["h"])
                for i in range(int(e.Rows)):
                    for j in range(int(e.Columns)):
                        add("table_cell", clean_mtext(str(_safe(lambda: e.GetText(i, j), "") or "")),
                            r, row=i, col=j)
            except Exception:
                continue
        return _jsonable({"ok": True, "count": len(items), "truncated": len(items) >= limit,
                          "items": items})
    return await _run(go, readonly=True, job=_jobkey("texts", l=layer, c=contains,
                                                     a=include_attributes, t=include_tables,
                                                     d=include_dimension_overrides, n=limit))


@mcp.tool()
async def get_blocks(name: str | None = None, layer: str | None = None,
                     with_attributes: bool = True, limit: int = 2000) -> dict:
    """Вставки блоков в модели: количество по именам (динамические — по исходному имени)
    и значения атрибутов каждой вставки. name/layer — маски."""
    def go():
        doc = _doc()
        sc = _scan(doc)
        counts, inst = Counter(), []
        for r in sc["recs"]:
            if r["t"] != "INSERT":
                continue
            if layer and not _glob_ok(r["layer"], layer):
                continue
            if name and not _glob_ok(r["eff"], name):
                continue
            counts[r["eff"]] += 1
            if with_attributes and len(inst) < limit:
                inst.append({"handle": r["h"], "block": r["eff"], "layer": r["layer"],
                             "at": _pt(r["p10"]), "attributes": dict(r["attrs"])})
        return _jsonable({"ok": True, "by_name": dict(counts.most_common()),
                          "instances": inst if with_attributes else None})
    return await _run(go, readonly=True, job=_jobkey("blocks", n=name, l=layer,
                                                     a=with_attributes, m=limit))


@mcp.tool()
async def measure(layer: str | None = None, type: str | None = None,
                  handles: list[str] | None = None, hatch_areas: bool = False) -> dict:
    """Сумма длин и площадей по слоям: отрезки, полилинии (с дугами), дуги, окружности;
    площади замкнутых полилиний и окружностей; hatch_areas=True — ещё площади штриховок
    (медленнее). Для длин трубопроводов/воздуховодов и площадей помещений."""
    def go():
        doc = _doc()
        sc = _scan(doc)
        hs = set(handles or [])
        per = defaultdict(lambda: {"count": 0, "length": 0.0, "area": 0.0})
        hatch_n = 0
        for r in (sc["all"] if hs else sc["recs"]):
            if hs and r["h"] not in hs:
                continue
            if layer and not _glob_ok(r["layer"], layer):
                continue
            if type and type.upper() not in r["t"].upper():
                continue
            t, L, A = r["t"], 0.0, 0.0
            if t == "LINE" and r["p10"] and r["p11"]:
                L = math.dist(r["p10"], r["p11"])
            elif t == "LWPOLYLINE":
                L, A = poly_length_area(r["verts"], bool(r["c70"] & 1))
            elif t == "CIRCLE" and r["c40"]:
                L, A = 2 * math.pi * r["c40"], math.pi * r["c40"] ** 2
            elif t == "ARC" and r["c40"] is not None:
                sweep = ((r["c51"] or 0) - (r["c50"] or 0)) % (2 * math.pi)
                L = r["c40"] * sweep
            elif t == "HATCH" and hatch_areas and hatch_n < 500:
                hatch_n += 1
                A = float(_safe(lambda: doc.HandleToObject(r["h"]).Area, 0.0) or 0.0)
            else:
                continue
            k = per[r["layer"]]
            k["count"] += 1
            k["length"] += L
            k["area"] += A
        tot = {"count": sum(v["count"] for v in per.values()),
               "length": _r(sum(v["length"] for v in per.values()), 1),
               "area": _r(sum(v["area"] for v in per.values()), 1)}
        return _jsonable({"ok": True, "units_note": "в единицах чертежа (обычно мм и мм²)",
                          "total": tot,
                          "by_layer": {k: {"count": v["count"], "length": _r(v["length"], 1),
                                           "area": _r(v["area"], 1)}
                                       for k, v in sorted(per.items())}})
    return await _run(go, readonly=True, job=_jobkey("measure", l=layer, t=type, h=handles,
                                                     a=hatch_areas))


# --------------------------------------------------------------------------- #
#  Размеры и тексты (COM)                                                      #
# --------------------------------------------------------------------------- #

def _apply_common(doc, obj, layer, style):
    if layer:
        try:
            doc.Layers.Item(layer)
        except Exception:
            doc.Layers.Add(layer)
        obj.Layer = layer
    if style:
        obj.StyleName = style


@mcp.tool()
async def dim_chain(points: list[list[float]], direction: str = "h", angle: float | None = None,
                    offset: float = 1000.0, overall: bool = True, overall_gap: float | None = None,
                    layer: str | None = None, style: str | None = None,
                    precision: int = 0, stagger_narrow: bool = True) -> dict:
    """Размерная цепочка через все точки — от крайней до крайней — плюс (overall) габарит
    вторым рядом.

    points    — точки привязки [[x, y], ...] в любом порядке (стены, оси, оборудование);
    direction — "h" (горизонтальная), "v" (вертикальная) или angle в градусах;
    offset    — отступ размерной линии от крайней точки, единицы чертежа (минус — другая сторона);
    overall_gap — расстояние между цепочкой и габаритом (по умолчанию ≈ 3 высоты текста);
    stagger_narrow — если число шире размера, поднимать его через одно, чтобы не налезали.
    Возвращает дескрипторы и список тесных размеров — проверь результат визуально (zoom)."""
    def go():
        doc = _doc()
        ms = doc.ModelSpace
        a = angle if angle is not None else (90.0 if direction.lower().startswith("v") else 0.0)
        segs, line = chain_layout(points, a, offset)
        if not segs:
            return {"ok": False, "error": "Нужно минимум 2 разные точки вдоль направления"}
        rad = math.radians(a)
        rows, created = [], []
        with _undo_group(doc):
            for p1, p2, loc, L in segs:
                d = ms.AddDimRotated(vpt(p1), vpt(p2), vpt(loc), rad)
                _apply_common(doc, d, layer, style)
                th = float(_safe(lambda: d.TextHeight, 2.5) or 2.5) * \
                    float(_safe(lambda: d.ScaleFactor, 1.0) or 1.0)
                txt = dim_text(float(_safe(lambda: d.Measurement, L) or L), precision)
                rows.append({"d": d, "len": L, "text": txt, "th": th, "loc": loc,
                             "w": est_text_width(txt, th)})
                created.append(str(d.Handle))
            flags = narrow_flags([r["len"] for r in rows], [r["w"] for r in rows])
            nx, ny = line["n"]
            sign = 1.0 if offset >= 0 else -1.0
            warnings, k = [], 0
            for r, narrow in zip(rows, flags):
                if not narrow:
                    k = 0
                    continue
                k += 1
                moved = False
                if stagger_narrow and k % 2 == 0:
                    lift = 1.6 * r["th"] * sign
                    try:
                        _safe(lambda r=r: setattr(r["d"], "TextMovement", 2))
                        r["d"].TextPosition = vpt((r["loc"][0] + nx * lift, r["loc"][1] + ny * lift))
                        moved = True
                    except Exception:
                        pass
                warnings.append({"handle": str(r["d"].Handle), "value": r["text"],
                                 "length": _r(r["len"], 1), "issue": "число шире размера",
                                 "text_lifted": moved})
            overall_handle = None
            if overall and len(segs) > 1:
                gap = overall_gap if overall_gap is not None else 3.0 * rows[0]["th"]
                base2 = line["base"] + sign * gap
                ux, uy = line["u"]
                s_mid = ((line["start"][0] * ux + line["start"][1] * uy) +
                         (line["end"][0] * ux + line["end"][1] * uy)) / 2
                loc2 = (s_mid * ux + base2 * nx, s_mid * uy + base2 * ny)
                d2 = ms.AddDimRotated(vpt(line["start"]), vpt(line["end"]), vpt(loc2), rad)
                _apply_common(doc, d2, layer, style)
                overall_handle = str(d2.Handle)
        S.scan_cache = None
        return _jsonable({
            "ok": True, "chain_handles": created, "overall_handle": overall_handle,
            "segments": [{"value": r["text"], "length": _r(r["len"], 1)} for r in rows],
            "total": _r(line["total"], 1), "warnings": warnings,
            "check": "Проверь визуально: числа не налезают, цепочка доведена до крайних элементов.",
        })
    return await _run(go)


@mcp.tool()
async def dim_linear(p1: list[float], p2: list[float], location: list[float] | None = None,
                     angle: float | None = None, aligned: bool = False, offset: float = 1000.0,
                     layer: str | None = None, style: str | None = None) -> dict:
    """Одиночный размер. aligned=True — параллельный отрезку; иначе повёрнутый (angle,
    по умолчанию горизонтальный/вертикальный по преобладающему направлению)."""
    def go():
        doc = _doc()
        ms = doc.ModelSpace
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        ang = math.atan2(dy, dx) if aligned else (
            math.radians(angle) if angle is not None else (0.0 if abs(dx) >= abs(dy) else math.pi / 2))
        loc = location
        if loc is None:
            nx, ny = -math.sin(ang), math.cos(ang)
            loc = [(p1[0] + p2[0]) / 2 + nx * offset, (p1[1] + p2[1]) / 2 + ny * offset]
        with _undo_group(doc):
            d = ms.AddDimAligned(vpt(p1), vpt(p2), vpt(loc)) if aligned else \
                ms.AddDimRotated(vpt(p1), vpt(p2), vpt(loc), ang)
            _apply_common(doc, d, layer, style)
        S.scan_cache = None
        return {"ok": True, "handle": str(d.Handle), "value": _r(_safe(lambda: d.Measurement), 1)}
    return await _run(go)


@mcp.tool()
async def add_text(x: float, y: float, text: str, height: float = 250.0,
                   layer: str | None = None, rotation: float = 0.0, mtext_width: float = 0.0,
                   style: str | None = None) -> dict:
    """Текст в модели. mtext_width > 0 — многострочный (MTEXT, перенос строки — \\P)."""
    def go():
        doc = _doc()
        ms = doc.ModelSpace
        with _undo_group(doc):
            if mtext_width > 0:
                t = ms.AddMText(vpt((x, y)), float(mtext_width), text)
                t.Height = height
            else:
                t = ms.AddText(text, vpt((x, y)), float(height))
            if rotation:
                t.Rotation = math.radians(rotation)
            _apply_common(doc, t, layer, style)
        S.scan_cache = None
        return {"ok": True, "handle": str(t.Handle)}
    return await _run(go)


@mcp.tool()
async def styles() -> dict:
    """Размерные и текстовые стили, текущие стили и масштаб размеров."""
    def go():
        doc = _doc()
        txt = _lisp(doc, r'''
(defun mcp-main (fn / f a)
  (setq f (open fn "w"))
  (setq a (tblnext "DIMSTYLE" T))
  (while a (write-line (strcat "D" mcp-sep (cdr (assoc 2 a))) f) (setq a (tblnext "DIMSTYLE")))
  (setq a (tblnext "STYLE" T))
  (while a (write-line (strcat "S" mcp-sep (cdr (assoc 2 a)) mcp-sep (mcp-s (cdr (assoc 3 a)))) f)
           (setq a (tblnext "STYLE")))
  (close f))
''')
        dims, texts = [], []
        for line in txt.splitlines():
            f = line.split(SEP)
            if f[0] == "D":
                dims.append(f[1])
            elif f[0] == "S" and f[1]:
                texts.append({"name": f[1], "font": f[2] if len(f) > 2 else ""})
        return _jsonable({"ok": True, "dimstyles": dims, "textstyles": texts,
                          "current": _sysvars(doc, ["DIMSTYLE", "TEXTSTYLE", "DIMSCALE",
                                                    "DIMTXT", "CLAYER"])})
    return await _run(go, readonly=True)


# --------------------------------------------------------------------------- #
#  Правка объектов, вид, файлы                                                 #
# --------------------------------------------------------------------------- #

@mcp.tool()
async def edit_entities(handles: list[str], action: str = "set", layer: str | None = None,
                        color: int | None = None, linetype: str | None = None,
                        dx: float = 0.0, dy: float = 0.0, dry_run: bool = False) -> dict:
    """Правка объектов по дескрипторам. action: set (layer/color/linetype), erase, move (dx, dy).
    color: 256 — ПоСлою, 0 — ПоБлоку, 1–255 — индекс. Одна отметка отмены."""
    def go():
        doc = _doc()
        if dry_run:
            sc = _scan(doc, use_cache=False)
            known = {r["h"] for r in sc["all"]}
            return {"ok": True, "dry_run": True, "found": sum(h in known for h in handles),
                    "missing": [h for h in handles if h not in known][:50]}
        if action == "move":
            ok, fails = 0, []
            with _undo_group(doc):
                for h in handles:
                    try:
                        doc.HandleToObject(h).Move(vpt((0, 0)), vpt((dx, dy)))
                        ok += 1
                    except Exception as ex:
                        fails.append(f"{h}: {_err_text(ex)}")
            S.scan_cache = None
            return {"ok": True, "applied": ok, "failed": fails[:20]}
        ops = []
        for h in handles:
            if action == "erase":
                ops.append((h, "erase", ""))
                continue
            if layer:
                ops.append((h, "layer", layer))
            if color is not None:
                ops.append((h, "color", str(color)))
            if linetype:
                ops.append((h, "ltype", linetype))
        return {"ok": True, **_apply(doc, ops)}
    return await _run(go, job=_jobkey("edit", h=handles, a=action, l=layer, c=color,
                                      t=linetype, x=dx, y=dy, d=dry_run))


@mcp.tool()
async def zoom(handles: list[str] | None = None, window: list[float] | None = None,
               margin: float = 0.2) -> dict:
    """Показать в nanoCAD: объекты по дескрипторам, окно [x1, y1, x2, y2] или всё."""
    def go():
        app = _app()
        doc = _doc()
        if handles:
            sc = _scan(doc)
            hs = set(handles)
            pts = []
            for r in sc["all"]:
                if r["h"] in hs:
                    pts += [p for p in (r["kp"], r["p11"] if r["t"] == "LINE" else None) if p]
                    pts += [(x, y, 0) for x, y, _ in r["verts"]]
            if not pts:
                return {"ok": False, "error": "Объекты не найдены"}
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            m = max(max(xs) - min(xs), max(ys) - min(ys), 2000.0) * margin
            app.ZoomWindow(vpt((min(xs) - m, min(ys) - m)), vpt((max(xs) + m, max(ys) + m)))
        elif window:
            app.ZoomWindow(vpt(window[:2]), vpt(window[2:4]))
        else:
            app.ZoomExtents()
        return {"ok": True}
    return await _run(go)


@mcp.tool()
async def backup() -> dict:
    """Резервная копия файла чертежа рядом с ним (копируется последнее СОХРАНЁННОЕ состояние)."""
    def go():
        doc = _doc()
        path = str(doc.FullName)
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "Чертёж ещё не сохранён на диск"}
        base, ext = os.path.splitext(path)
        dst = f"{base}_backup_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        shutil.copy2(path, dst)
        return {"ok": True, "backup": dst,
                "unsaved_changes": not _safe(lambda: bool(doc.Saved), True)}
    return await _run(go)


@mcp.tool()
async def save(path: str | None = None) -> dict:
    """Сохранить чертёж. path — «Сохранить как» (дальше чертёж работает уже с новым файлом)."""
    def go():
        doc = _doc()
        doc.SaveAs(path) if path else doc.Save()
        return {"ok": True, "path": str(_safe(lambda: doc.FullName))}
    return await _run(go)


@mcp.tool()
async def run_lisp(code: str) -> dict:
    """Выполнить AutoLISP-выражения внутри nanoCAD и вернуть значение последнего (как prin1).
    Быстро работает с любым числом объектов (entget/entmod/ssget/tblnext…). Для правок
    оберни код в (mcp-undo-begin) … — тогда изменения откатятся одним Ctrl+Z."""
    def go():
        doc = _doc()
        out = _lisp(doc, LISP_EVAL.replace("<<CODE>>", code))
        S.scan_cache = None
        return {"ok": True, "result": out.strip()[:50000]}
    return await _run(go, job=_jobkey("lisp", c=code))


@mcp.tool()
async def exec_python(code: str) -> dict:
    """Выполнить Python в потоке сервера. Доступно: app, doc, ms, vpt((x,y[,z])),
    scan() — объекты модели из LISP-дампа (dict: h, t, layer, color, p10, p11, kp, text, attrs…),
    lisp(body) — выполнить LISP с (defun mcp-main (fn) …), apply_ops([(handle, op, arg)]),
    clean_mtext(s). Ответ положи в result; print() тоже возвращается."""
    def go():
        app = _app()
        doc = _doc()
        ns = {"app": app, "doc": doc, "ms": doc.ModelSpace, "vpt": vpt,
              "scan": lambda: _scan(doc, use_cache=False)["recs"],
              "lisp": lambda body: _lisp(doc, body),
              "apply_ops": lambda ops: _apply(doc, ops),
              "clean_mtext": clean_mtext, "math": math, "result": None}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                exec(code, ns)  # noqa: S102 — инструмент автора чертежа
                err = None
            except Exception:
                err = traceback.format_exc(limit=4)
        S.scan_cache = None
        return _jsonable({"ok": err is None, "result": ns.get("result"),
                          "stdout": buf.getvalue()[-20000:], "error": err})
    return await _run(go, job=_jobkey("py", c=code))


def _cfg_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be"
    return "latin-1"  # Preserves the exact bytes of ANSI and UTF-8 files.


def install_cad_ui():
    """Install the partial ribbon and toolbar in initialized nanoCAD profiles."""
    root = Path(os.environ["APPDATA"]) / "Nanosoft"
    profile_name = re.compile(r"\bnanoCAD\b.*\b(?:24|25|26)(?:[.\s]|$)", re.IGNORECASE)
    profiles = sorted(p for p in root.glob("*") if profile_name.search(p.name))
    source = Path(__file__).resolve().parent
    files = ("NanoCAD-MCP.cfg", "NanoCAD-MCP.cuix", "NanoCAD-MCP.ico",
             "NanoCAD-MCP-Extras.ico", "NanoCAD-MCP-3D.ico")
    include = '#include "NanoCAD-MCP.cfg"'
    installed = 0

    for profile in profiles:
        config_dir = profile / "Config"
        configs = {config_dir / "nanoCAD.cfg"}
        ini = config_dir / "Cfg.ini"
        if ini.is_file():
            ini_raw = ini.read_bytes()
            ini_text = ini_raw.decode(_cfg_encoding(ini_raw))
            for name in re.findall(r"(?im)^\s*CfgFile\s*=s([^\r\n]+)", ini_text):
                path = Path(name.strip().strip('"'))
                if path.name == str(path) and path.suffix.lower() == ".cfg":
                    configs.add(config_dir / path)
        configs = sorted(p for p in configs if p.is_file() and not p.is_symlink())
        if not configs:
            continue

        for name in files:
            shutil.copyfile(source / name, config_dir / name)
        for config in configs:
            original = config.read_bytes()
            encoding = _cfg_encoding(original)
            config_text = original.decode(encoding)
            if any(line.strip().casefold() == include.casefold() for line in config_text.splitlines()):
                continue
            stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shutil.copy2(config, config.with_name(f"{config.name}.mcp-backup-{stamp}"))
            pending = config.with_name(f"{config.name}.{uuid.uuid4().hex}.tmp")
            try:
                pending.write_bytes(original + f"\r\n{include}\r\n".encode(encoding))
                pending.replace(config)
            finally:
                pending.unlink(missing_ok=True)
        print("Лента и панель nanoCAD MCP: " + str(config_dir))
        installed += 1

    if not installed:
        print("Не найден профиль nanoCAD 24–26. Запустите нужную версию один раз, "
              "полностью закройте её и повторите установку.")
        return 1
    return 0


def install_config():
    """Добавить nanocad-mcp в claude_desktop_config.json (с резервной копией)."""
    import glob
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    paths = [os.path.join(appdata, "Claude", "claude_desktop_config.json")]
    paths += glob.glob(os.path.join(local, "Packages", "*Claude*", "LocalCache", "Roaming",
                                    "Claude", "claude_desktop_config.json"))
    existing = [p for p in paths if os.path.exists(p)]
    targets = existing or [paths[0]]
    entry = {"command": sys.executable, "args": [os.path.abspath(__file__)]}
    for p in targets:
        cfg = {}
        if os.path.exists(p):
            raw = open(p, "rb").read()
            shutil.copy2(p, p + ".backup_" + _dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
            try:
                cfg = json.loads(raw.decode("utf-8-sig") or "{}")
            except ValueError:
                print("ОШИБКА: не удалось прочитать " + p + " (испорчен JSON). Ничего не изменено.")
                return 1
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
        cfg.setdefault("mcpServers", {})["nanocad-mcp"] = entry
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("Готово: " + p)
    return 0


def main():
    if "--tool-catalog" in sys.argv:
        enabled = enabled_tools()
        print(json.dumps([{"name": n, "group": g, "enabled": n in enabled}
                          for n, g in COMMAND_GROUP.items()], ensure_ascii=True))
        return
    if "--install-ui" in sys.argv:
        sys.exit(install_cad_ui())
    if "--install" in sys.argv:
        sys.exit(install_cad_ui() or install_config())
    log.info("nanocad-mcp %s starting", __version__)
    mcp.run()


if __name__ == "__main__":
    main()
