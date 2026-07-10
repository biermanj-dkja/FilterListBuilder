# FilterListBuilder.spec
# Build with:  pyinstaller FilterListBuilder.spec
#
# Prerequisite: install Chromium into ./pw-browsers first —
#   Windows:      set PLAYWRIGHT_BROWSERS_PATH=%CD%\pw-browsers && playwright install chromium
#   macOS/Linux:  PLAYWRIGHT_BROWSERS_PATH=$PWD/pw-browsers playwright install chromium
#
# Output: dist/FilterListBuilder/ — distribute the whole folder (zip it).

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [
    # Bundled Chromium — copied into <app>/_internal/browsers, where the
    # runtime PLAYWRIGHT_BROWSERS_PATH override in filter_list_builder.py
    # expects to find it.
    ("pw-browsers", "browsers"),
]
binaries = []
hiddenimports = []

# customtkinter ships JSON themes/assets; playwright ships its Node-based
# driver. Both are invisible to PyInstaller's import analysis.
for pkg in ("customtkinter", "playwright"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# tldextract's bundled public-suffix-list snapshot (fallback when the
# machine running the exe has no network access on first use).
datas += collect_data_files("tldextract")

a = Analysis(
    ["filter_list_builder.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,   # onedir: binaries live in the folder, not the exe
    name="FilterListBuilder",
    debug=False,
    strip=False,
    upx=False,               # UPX-packed exes are a common AV false-positive trigger
    console=False,           # GUI app — no console window
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FilterListBuilder",
)
