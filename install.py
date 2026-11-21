#!/usr/bin/env python3
"""
Install / uninstall the OpenShot AutoDub plugin into an openshot-qt source tree.

    python install.py                       # openshot-qt next to this folder (../openshot-qt)
    python install.py --openshot-dir D:/src/openshot-qt
    python install.py --uninstall

What it does (idempotent, stdlib only, any Python 3.8+):
  1. copies openshot_patch/plugins.py  ->  <openshot-qt>/src/classes/plugins.py
  2. adds a 5-line plugin loader hook to <openshot-qt>/src/windows/main_window.py
  3. adds a PyQt6 >= 6.12 compatibility line to <openshot-qt>/src/qt_api.py
     (QEvent.LayoutRequest & co. are only reachable as QEvent.Type.* there)
  4. registers this folder in ~/.openshot_qt/plugins.json
"""

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
USER_PATH = os.path.join(os.path.expanduser("~"), ".openshot_qt")
PLUGINS_FILE = os.path.join(USER_PATH, "plugins.json")

HOOK_ANCHOR = """        if self.dockAudioRecording.isVisible():
            self._ensure_audio_recording_dock_content()
"""
HOOK = """
        # External plugins (classes/plugins.py, installed by openshot-autodub)
        try:
            from classes.plugins import load_plugins
            load_plugins(self)
        except Exception:
            log.error("Plugin loader failed", exc_info=True)
"""
QT_ANCHOR = """            (getattr(QtCore, "Qt", None), "WidgetAttribute"),
"""
QT_LINE = """            (getattr(QtCore, "QEvent", None), "Type"),  # PyQt6 6.12: QEvent.LayoutRequest etc.
"""


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    return text, ("\r\n" if "\r\n" in text else "\n")


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _nl(block, nl):
    return block.replace("\n", nl)


def patch_text(path, anchor, addition, marker, after=True):
    text, nl = _read(path)
    if marker in text:
        return "already"
    a = _nl(anchor, nl)
    if a not in text:
        raise SystemExit("Anchor not found in %s - this openshot-qt version is not supported "
                         "by the installer, add the hook manually (see README)." % path)
    new = a + _nl(addition, nl) if after else _nl(addition, nl) + a
    _write(path, text.replace(a, new, 1))
    return "patched"


def unpatch_text(path, addition):
    text, nl = _read(path)
    block = _nl(addition, nl)
    if block in text:
        _write(path, text.replace(block, "", 1))
        return "removed"
    return "absent"


def load_plugins_json():
    try:
        with open(PLUGINS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def save_plugins_json(data):
    os.makedirs(USER_PATH, exist_ok=True)
    with open(PLUGINS_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--openshot-dir", default=os.path.join(os.path.dirname(HERE), "openshot-qt"))
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--keep-qt-fix", action="store_true", help="with --uninstall: leave the qt_api fix in place")
    args = ap.parse_args()

    src = os.path.join(os.path.abspath(args.openshot_dir), "src")
    main_window = os.path.join(src, "windows", "main_window.py")
    qt_api = os.path.join(src, "qt_api.py")
    loader_dst = os.path.join(src, "classes", "plugins.py")
    if not os.path.isfile(main_window):
        raise SystemExit("openshot-qt not found at %s (use --openshot-dir)" % args.openshot_dir)

    data = load_plugins_json()
    paths = [p for p in data.get("paths", []) if not same(p, HERE)]

    if args.uninstall:
        print("main_window.py:", unpatch_text(main_window, HOOK))
        if not args.keep_qt_fix:
            print("qt_api.py:", unpatch_text(qt_api, QT_LINE))
        if os.path.isfile(loader_dst) and not paths:
            os.remove(loader_dst)
            print("removed", loader_dst)
        data["paths"] = paths
        save_plugins_json(data)
        print("unregistered", HERE, "from", PLUGINS_FILE)
        return 0

    shutil.copyfile(os.path.join(HERE, "openshot_patch", "plugins.py"), loader_dst)
    print("copied loader ->", loader_dst)
    print("main_window.py:", patch_text(main_window, HOOK_ANCHOR, HOOK, "from classes.plugins import load_plugins"))
    print("qt_api.py:", patch_text(qt_api, QT_ANCHOR, QT_LINE, '"QEvent", None), "Type")', after=False))
    data["paths"] = paths + [HERE]
    save_plugins_json(data)
    print("registered", HERE, "in", PLUGINS_FILE)
    print("\nDone. Start OpenShot and open  View -> AutoDub  (Alt+Shift+4).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
