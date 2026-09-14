# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

from comtypes.client import GetModule


sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))

project_root = Path(SPECPATH).parent
datas = []
binaries = []
hiddenimports = []

# Generate and collect the Windows UI Automation wrapper used by the external
# Flow parameter guard. This avoids trying to create comtypes.gen files beside
# the installed executable at runtime.
GetModule("UIAutomationCore.dll")

for package_name in (
    "stable_whisper",
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "av",
    "elevenlabs",
    "pycaw",
    "comtypes",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

hiddenimports += collect_submodules("googleapiclient")
hiddenimports += collect_submodules("google_auth_oauthlib")
hiddenimports += collect_submodules("comtypes.gen")
hiddenimports += collect_submodules("app_plugins")

a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "jupyter",
        "jupyter_client",
        "jupyter_core",
        "jupyterlab",
        "keras",
        "notebook",
        "pandas",
        "tensorboard",
        "tensorflow",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AssistantTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AssistantTool",
)
