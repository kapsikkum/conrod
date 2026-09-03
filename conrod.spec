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

from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs,
                                     collect_submodules)

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
    "conrod.grouping", "conrod.nativeui", "conrod.selftest",
    "conrod.update", "conrod.vlm", "conrod.writer",
    "conrod.nativeui",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
]
hiddenimports += collect_submodules("rapidocr_onnxruntime")
# Imported lazily inside a function, and its absence is silent: the app runs
# and simply never reads a plate. Named explicitly so it cannot go missing.
hiddenimports += ["open_image_models", "fast_plate_ocr"]
hiddenimports += collect_submodules("open_image_models")
hiddenimports += collect_submodules("fast_plate_ocr")
datas += collect_data_files("fast_plate_ocr", include_py_files=False)

# torchvision ships its operators in a compiled extension, and ultralytics
# calls into it for NMS on the inference path. PyInstaller's own hook found it
# under torchvision 0.28 and silently did not under 0.29: the build logged
# "Hidden import torchvision._C not found!" as a warning, produced an exe, and
# that exe raised "operator torchvision::nms does not exist" the first time it
# ran a detection.
#
# collect_dynamic_libs is not enough on its own -- on Windows it returns the
# .dll files and skips the .pyd, which is the one that matters -- so the
# extension modules are gathered by hand and the build stops here if the
# important one is absent, rather than shipping an exe that cannot detect.
import torchvision as _torchvision

_tv_dir = Path(_torchvision.__file__).parent
binaries = collect_dynamic_libs("torchvision")
binaries += [(str(pyd), "torchvision") for pyd in _tv_dir.glob("*.pyd")]
if not any(Path(src).stem == "_C" for src, _ in binaries):
    raise SystemExit(
        f"conrod.spec: torchvision's compiled extension (_C.pyd) is not in "
        f"{_tv_dir}. The build would start and then fail on the first "
        f"detection. Check the installed torchvision against requirements.txt."
    )
hiddenimports += ["torchvision", "torchvision._C", "torchvision.ops"]

# Excluded to keep the build down: these arrive via torch/ultralytics but the
# app never plots anything or trains.
#
# Do not add to this list by eye. Every entry here was checked by blocking it
# from the import graph and then running a real detection, because three
# plausible-looking exclusions turned out to be load-bearing and each one
# broke only the frozen build:
#
#   sympy               torch imports it while loading
#   pandas              ultralytics imports it on the inference path
#   torchvision.datasets  torchvision's own __init__ imports it
#
# `Conrod.exe --selftest` runs that real detection and is a required step in
# the release workflow, so a bad exclusion cannot reach a published build.
excludes = [
    "matplotlib", "scipy", "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "notebook",
]

a = Analysis(
    ["main.py"],
    pathex=[str(here)],
    binaries=binaries,
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
