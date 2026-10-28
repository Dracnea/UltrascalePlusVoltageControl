"""Where the host scripts and the prebuilt bridges live.

Two layouts, one answer:

  installed (wheel)   <pkg>/_host/*.py, <pkg>/_data/prebuilt/, <pkg>/_data/boards/u55c/
                      (copied in at build time by setup.py; the repo is not changed)
  checkout / .deb     <root>/host/*.py, <root>/rtl/prebuilt/, <root>/boards/u55c/
                      with this package at <root>/packaging/python/upvc/
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _layouts():
    yield (os.path.join(_HERE, "_host"),
           os.path.join(_HERE, "_data", "prebuilt"),
           os.path.join(_HERE, "_data", "boards", "u55c"))
    root = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
    yield (os.path.join(root, "host"),
           os.path.join(root, "rtl", "prebuilt"),
           os.path.join(root, "boards", "u55c"))


def locate():
    """(host_dir, prebuilt_dir, board_dir) of the first layout that has the host scripts."""
    for host, pre, brd in _layouts():
        if os.path.isfile(os.path.join(host, "boardguard.py")):
            return host, pre, brd
    raise RuntimeError("upvc: cannot find the host scripts (broken install?)")
