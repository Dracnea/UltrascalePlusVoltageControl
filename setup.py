"""Build hook only; the metadata is in pyproject.toml.

The host scripts and the prebuilt bridges stay where changeVoltage.sh expects
them (host/, rtl/prebuilt/, boards/u55c/). At build time they are copied into
the package as upvc/_host and upvc/_data, so nothing in the repo moves.
"""
import glob
import os
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = os.path.dirname(os.path.abspath(__file__))

INCLUDE = (
    ("host/*.py", "_host"),
    ("rtl/prebuilt/*.bit.xz", os.path.join("_data", "prebuilt")),
    ("rtl/prebuilt/SHA256SUMS", os.path.join("_data", "prebuilt")),
    ("boards/u55c/*.bit.xz", os.path.join("_data", "boards", "u55c")),
)


class BuildPy(build_py):
    def run(self):
        build_py.run(self)
        if self.dry_run:
            return
        for pattern, dest in INCLUDE:
            files = glob.glob(os.path.join(ROOT, pattern))
            if not files:
                raise SystemExit("upvc build: nothing matches %s" % pattern)
            d = os.path.join(self.build_lib, "upvc", dest)
            os.makedirs(d, exist_ok=True)
            for f in files:
                shutil.copy2(f, d)


setup(cmdclass={"build_py": BuildPy})
