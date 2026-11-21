"""
Debug launcher: runs src/launch.py but captures any unhandled Python exception
(including the ones PyQt6 turns into "Unhandled Python exception" + abort) and
native crashes, writing them to the console and to openshot-crash.txt in the openshot-qt folder.

Use via tools\\windows\\run-openshot.cmd --trace   (or run this file with UCRT64 python).
"""
import faulthandler
import os
import runpy
import sys
import threading
import traceback

ROOT = os.path.abspath(os.environ.get("OPENSHOT_QT_DIR") or os.getcwd())
CRASH_FILE = os.path.join(ROOT, "openshot-crash.txt")
_crash_fh = open(CRASH_FILE, "w", encoding="utf-8", buffering=1)
faulthandler.enable(file=_crash_fh, all_threads=True)


def _report(kind, exc_type, exc, tb, thread=None):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    header = "==== %s%s ====\n" % (kind, " in thread %s" % thread if thread else "")
    for stream in (sys.__stderr__, _crash_fh):
        try:
            stream.write(header + text + "\n")
            stream.flush()
        except Exception:
            pass


def _excepthook(exc_type, exc, tb):
    _report("Unhandled exception", exc_type, exc, tb)


def _thread_hook(args):
    _report("Unhandled thread exception", args.exc_type, args.exc_value, args.exc_traceback,
            getattr(args.thread, "name", None))


sys.excepthook = _excepthook
threading.excepthook = _thread_hook

src = os.path.join(ROOT, "src")
sys.path.insert(0, src)
sys.argv = [os.path.join(src, "launch.py")] + sys.argv[1:]
os.chdir(ROOT)
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit:
    raise
except BaseException:
    _report("Fatal exception", *sys.exc_info())
    raise
