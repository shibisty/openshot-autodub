"""
OpenShot AutoDub plugin.

Loaded by openshot-qt's classes/plugins.py (installed with install.py), which calls
register(main_window) after the main window has created its docks and menus.
"""

__version__ = "0.1.0"


def register(window):
    from .plugin import AutoDubPlugin
    return AutoDubPlugin(window)
