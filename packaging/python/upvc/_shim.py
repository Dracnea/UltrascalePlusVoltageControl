"""Run one of the host scripts as __main__, with an optional libftdi1 location.

    python -m upvc._shim <script.py> [args ...]

If UPVC_LIBFTDI names a libftdi1 shared library (a full path, e.g. a Windows
libftdi1.dll), ctypes.util.find_library("ftdi1") is made to return it, so the
host scripts load that one. The scripts themselves are not modified.
"""
import ctypes.util
import os
import runpy
import sys


def main():
    lib = os.environ.get("UPVC_LIBFTDI", "").strip()
    if lib:
        real = ctypes.util.find_library

        def find_library(name, _real=real, _lib=lib):
            return _lib if name == "ftdi1" else _real(name)
        ctypes.util.find_library = find_library
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
