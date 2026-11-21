"""Creates the AutoDub dock and its View-menu entry inside OpenShot's main window."""

import os

from qt_api import Qt, QObject, QDockWidget, QAction, QKeySequence, QIcon, QTabWidget

from classes import info
from classes.logger import log


class AutoDubPlugin(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.content = None

        self.dock = QDockWidget("AutoDub", window)
        self.dock.setObjectName("dockAutoDub")          # stable name -> OpenShot restores its position
        self.dock.setProperty("_skip_auto_tab_order", True)
        self.dock.setFocusPolicy(Qt.NoFocus)
        self.dock.hide()
        window.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.visibilityChanged.connect(self._on_visibility)

        icon = QIcon(os.path.join(info.PATH, "themes/cosmic/images/tool-microphone.svg"))
        self.action = QAction(icon, "AutoDub View", window)
        self.action.setObjectName("actionAutoDub_View")
        self.action.setShortcut(QKeySequence("Alt+Shift+4"))
        self.action.triggered.connect(self.show_view)
        menu = getattr(window, "menuView", None)
        if menu is not None:
            anchor = getattr(getattr(window, "menuWindow", None), "menuAction", lambda: None)()
            if anchor is not None and anchor in menu.actions():
                menu.insertAction(anchor, self.action)
            else:
                menu.addAction(self.action)
        log.info("AutoDub: dock and menu registered")

    def ensure_content(self):
        if self.content is None:
            from .panel import DubbingDockContent, DUBBING_DOCK_MIN_WIDTH
            self.content = DubbingDockContent(self.window)
            self.dock.setMinimumWidth(DUBBING_DOCK_MIN_WIDTH)
            self.dock.setWidget(self.content)
        return self.content

    def _on_visibility(self, visible):
        if visible:
            try:
                self.ensure_content().on_shown()
            except Exception:
                log.error("AutoDub: cannot create panel", exc_info=True)

    def show_view(self):
        """Simple View layout with AutoDub docked on the right (falls back to just showing the dock)."""
        w = self.window
        self.ensure_content()
        try:
            if hasattr(w, "_set_active_custom_view_id"):
                w._set_active_custom_view_id("")
            if hasattr(w, "_apply_simple_view_layout"):
                w._apply_simple_view_layout()
            w.addDocks([self.dock], Qt.RightDockWidgetArea)
            w.setTabPosition(Qt.RightDockWidgetArea, QTabWidget.North)
            w.floatDocks(False)
        except Exception:
            log.warning("AutoDub: view layout failed, showing dock only", exc_info=True)
            if w.dockWidgetArea(self.dock) == Qt.NoDockWidgetArea:
                w.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.show()
        self.dock.raise_()
        if hasattr(w, "style_dock_widgets"):
            w.style_dock_widgets()
