"""Start the arbitrage scanner - double-click this file (or run:  py start.py).

Use this instead of start.bat when Windows "Smart App Control" blocks .bat files. It needs only
Python itself: on the first run it downloads the few pure-Python libraries the scanner uses into
the "lib" folder next to this file (no compiled add-ons, nothing installed system-wide), then
starts the scanner. Arguments are passed through, e.g.:  py start.py probe
"""

import os
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "lib")
MARKER = os.path.join(LIB, ".installed-v1")

# Pure-Python only: chardet instead of charset-normalizer (which ships compiled files).
# PyYAML's optional compiled part is skipped automatically if Windows refuses to load it.
PACKAGES = ["requests>=2.31", "urllib3>=1.26", "idna>=2.5", "certifi>=2017.4.17", "chardet>=3.0.2,<6", "PyYAML>=6.0"]


def install() -> None:
    print("First start: downloading the libraries the scanner needs into the 'lib' folder ...", flush=True)
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
           "--no-deps", "--upgrade", "--target", LIB, *PACKAGES]
    if subprocess.call(cmd) != 0:
        raise SystemExit("Could not download the libraries. Check your internet connection and try again.")
    with open(MARKER, "w", encoding="utf-8") as fh:
        fh.write("ok\n")


def main() -> int:
    if sys.version_info < (3, 11):
        print(f"Python 3.11 or newer is required (this is {sys.version.split()[0]}): https://www.python.org/downloads/")
        return 1
    os.chdir(HERE)  # config.yaml and the data folder live here
    if not os.path.exists(MARKER):
        install()
    sys.path.insert(0, LIB)
    sys.path.insert(0, HERE)
    from odds_scanner.cli import main as scanner_main

    return scanner_main(sys.argv[1:])


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if exc.code and not isinstance(exc.code, int):
            print(exc.code)
    except KeyboardInterrupt:
        code = 0
    except Exception:  # noqa: BLE001 - show the error instead of a window that just vanishes
        traceback.print_exc()
        code = 1
    if sys.stdin and sys.stdin.isatty():  # keep a double-clicked window open so the output can be read
        try:
            input("\nPress Enter to close this window ...")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
