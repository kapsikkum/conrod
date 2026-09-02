# PyInstaller build spec for Conrod.
#
# What ships inside the exe: the app, its Python dependencies, the web UI, and
# the RapidOCR ONNX models (which live inside the installed package, so
# PyInstaller does not find them on its own).
#
# What does NOT ship, and why:
#   * the YOLO vehicle weights (~19 MB) and the plate detector (7.5 MB) —
#     both download themselves on first run, and bundling them would mean
#     re-releasing the app to update a model.
#   * the vision model (~6 GB) — it runs under Ollama, which the setup screen
#     checks for and links to. No executable can reasonably carry it.
#   * ExifTool — the setup screen checks for it and links to the download.
#
# Build:  pyinstaller conrod.spec --noconfirm

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None
here = Path(SPECPATH)

datas = [
    (str(here / "conrod" / "web"), "conrod/web"),
    (str(here / "samples"), "samples"),
]

# RapidOCR keeps its detection/recognition ONNX models and config.yaml inside
# the package directory; without these the frozen build starts and then fails
# the first time it tries to read any text.
datas += collect_data_files("rapidocr_onnxruntime", include_py_files=False)

hiddenimports = [
    "conrod.analyze", "conrod.culling", "conrod.detect", "conrod.exif",
    "conrod.keywords", "conrod.mapping", "conrod.ocr", "conrod.pipeline",
    "conrod.plates", "conrod.server", "conrod.setup_check", "conrod.store",
    "conrod.vlm", "conrod.writer",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
]
hiddenimports += collect_submodules("rapidocr_onnxruntime")

# Excluded to keep the build down: these arrive via torch/ultralytics but the
# app never plots anything or trains.
excludes = [
    "matplotlib", "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "notebook", "pandas", "scipy", "sympy", "torchvision.datasets",
]

a = Analysis(
    ["main.py"],
    pathex=[str(here)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Conrod",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # a desktop app, not a terminal tool
    disable_windowed_traceback=False,
    icon=str(here / "assets" / "conrod.ico")
        if (here / "assets" / "conrod.ico").exists() else None,
)

# One-folder rather than one-file: torch and onnxruntime carry hundreds of
# megabytes of DLLs, and a one-file build would unpack all of them to a temp
# directory on every launch, adding tens of seconds to startup.
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Conrod",
)
