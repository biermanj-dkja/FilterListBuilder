"""Build script for Filter List Builder.

Produces a distributable folder at dist/FilterListBuilder/ containing the
app and a bundled Chromium, so end users need neither Python nor a
`playwright install` step.

Usage (from an activated venv with requirements installed):

    python build.py

What it does:
  1. Sets PLAYWRIGHT_BROWSERS_PATH to ./pw-browsers for this process only —
     your normal Playwright cache (~/.cache/ms-playwright) is untouched.
  2. Runs `playwright install chromium` into that folder. This is a no-op
     if the correct Chromium version is already present, and automatically
     fetches a matching browser after a playwright version bump.
  3. Runs PyInstaller with FilterListBuilder.spec.

This script has no effect on normal development runs; `python
filter_list_builder.py` never touches pw-browsers/.
"""

import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
BROWSERS_DIR = os.path.join(REPO_ROOT, "pw-browsers")
SPEC_FILE = os.path.join(REPO_ROOT, "FilterListBuilder.spec")


def run(cmd, env=None):
    print(f"\n>>> {' '.join(cmd)}")
    result = subprocess.run(cmd, env=env, cwd=REPO_ROOT)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    # Fail early with clear messages rather than deep in a traceback.
    try:
        import playwright  # noqa: F401
    except ImportError:
        sys.exit("playwright is not installed — run: pip install -r requirements.txt")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("PyInstaller is not installed — run: pip install pyinstaller")
    if not os.path.exists(SPEC_FILE):
        sys.exit("FilterListBuilder.spec not found next to build.py")

    # Point Playwright's installer at the repo-local browser folder for the
    # child processes below. Setting it here (not in the shell) means no
    # per-machine or per-build setup for whoever runs this.
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS_DIR

    print(f"Installing/verifying Chromium in {BROWSERS_DIR} ...")
    run([sys.executable, "-m", "playwright", "install", "chromium"], env=env)

    # Clean previous build output so stale files never ship.
    for stale in ("build", os.path.join("dist", "FilterListBuilder")):
        path = os.path.join(REPO_ROOT, stale)
        if os.path.exists(path):
            print(f"Removing stale {stale}/")
            shutil.rmtree(path)

    print("Running PyInstaller ...")
    run([sys.executable, "-m", "PyInstaller", SPEC_FILE, "--noconfirm"])

    out_dir = os.path.join(REPO_ROOT, "dist", "FilterListBuilder")
    print(f"\nDone. Distributable folder: {out_dir}")
    print("Zip that folder to hand it to someone; they run FilterListBuilder"
          + (".exe" if sys.platform == "win32" else "") + " inside it.")


if __name__ == "__main__":
    main()
