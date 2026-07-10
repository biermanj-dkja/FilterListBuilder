# Build Instructions — Filter List Builder

How to compile the app into a standalone folder that runs without Python,
Playwright, or any setup on the target machine.

These instructions are for **maintainers building a release**. End users of
the built app need none of this — they just unzip and run. Developers running
from source also need none of this — `python filter_list_builder.py` works
exactly as described in `README.md` and never touches the build tooling.

---

## What the build produces

`dist/FilterListBuilder/` — a self-contained folder containing:

- `FilterListBuilder.exe` (or `FilterListBuilder` on macOS/Linux)
- an `_internal/` folder with Python, all dependencies, and a bundled
  Chromium browser (~150 MB)

Distribute the **whole folder** (zip it). The exe must stay next to
`_internal/` — it will not run if moved out on its own.

A folder build (`--onedir`) is used deliberately instead of a single-file
exe: single-file builds unpack to a temp directory on every launch (slow
with a bundled browser) and are far more likely to trigger antivirus
false positives.

---

## One-time setup (per build machine)

1. Clone the repo and set up the environment as described in `README.md`
   (venv + `pip install -r requirements.txt`). You do **not** need to run
   `playwright install chromium` — the build script handles browsers itself.
2. Install PyInstaller into the same environment:

   ```
   pip install pyinstaller
   ```

> **Build on the OS you are targeting.** PyInstaller does not
> cross-compile: a Windows build must be made on Windows, a macOS build on
> macOS, etc.

---

## Building

From the repo root, with the venv activated:

```
python build.py
```

That's the whole process. The script:

1. Downloads Chromium into a repo-local `pw-browsers/` folder (skipped if
   the correct version is already there; re-downloads automatically after a
   `playwright` version bump in `requirements.txt`).
2. Cleans any previous `build/` and `dist/FilterListBuilder/` output.
3. Runs PyInstaller with `FilterListBuilder.spec`.

Output lands in `dist/FilterListBuilder/`.

`pw-browsers/`, `build/`, and `dist/` are all gitignored — they are build
artifacts, never committed.

---

## Testing a build

Always test on a machine (or VM) **without Python installed** — that is the
only environment where missing bundled files actually show up. Minimum
checks:

- App launches and the GUI renders in both light and dark appearance modes.
- **Manual Mode** opens a visible browser window (this exercises the bundled
  Chromium — the most likely thing to be broken in a bad build).
- A Batch or Manual session completes through the review dialog and writes a
  CSV to the output folder.
- Blocklist download works (or fails gracefully with a warning if offline).

---

## How the pieces fit together

| File | Role |
|---|---|
| `build.py` | One-command build: installs Chromium locally, runs PyInstaller. Sets `PLAYWRIGHT_BROWSERS_PATH` only for its own child processes — nothing on the machine or in the shell is modified. |
| `FilterListBuilder.spec` | PyInstaller configuration: collects CustomTkinter assets, the Playwright driver, tldextract's suffix-list snapshot, and copies `pw-browsers/` into the bundle as `browsers/`. |
| `filter_list_builder.py` (top of file) | A `frozen`-guarded override that points Playwright at the bundled `browsers/` folder **only when running as a compiled app**. Running from source, the guard is false and Playwright behaves normally. |

Because of that guard, the build tooling can live in the repo permanently
with zero effect on development: nothing needs editing, commenting out, or
setting before either a normal run or a build.

---

## Troubleshooting

- **"Executable doesn't exist" when starting a session** — the bundled
  browser is missing or version-mismatched. Delete `pw-browsers/` and
  rebuild so the script fetches a fresh copy matching the installed
  `playwright` package.
- **Antivirus flags the exe** — expected occasionally for unsigned
  PyInstaller output. The spec already avoids the biggest triggers
  (onedir, no UPX). The durable fix is code-signing; for internal
  distribution, whitelisting the exe is usually sufficient.
- **Blank/ugly widgets in the built app** — CustomTkinter theme files
  didn't get collected. Verify PyInstaller and customtkinter are installed
  in the *same* venv you ran `build.py` from.
- **Build succeeds but the folder is suspiciously small (< 200 MB)** —
  Chromium probably wasn't bundled; check that `pw-browsers/` exists and
  contains a `chromium-*` folder before PyInstaller runs.
