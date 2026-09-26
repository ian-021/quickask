# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for llm-popup.app. Run through build.sh.

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["llm_popup.py"],
    hiddenimports=collect_submodules("ServiceManagement"),
    # Qt modules the app never imports; keeps the bundle smaller.
    excludes=[
        "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtOpenGL",
        "PySide6.QtPdf", "PySide6.QtSvg", "PySide6.QtWebEngineCore", "PySide6.QtMultimedia",
        "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "tkinter", "unittest", "pydoc",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="llm-popup",
    console=False,          # no terminal window
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None, # ad-hoc signature; enough for the machine it is built on
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="llm-popup")

app = BUNDLE(
    coll,
    name="llm-popup.app",
    icon="build/icon/llm-popup.icns",
    bundle_identifier="com.ian.llm-popup",
    info_plist={
        "CFBundleName": "llm-popup",
        "CFBundleDisplayName": "llm-popup",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",   # SMAppService (launch at login) needs Ventura
        "LSUIElement": True,                # menu bar app: no Dock icon, no app menu
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "",
    },
)
