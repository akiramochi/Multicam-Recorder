# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for NDI Multicam Recorder.

Produces a one-folder distribution in  dist/NDI Multicam Recorder/
that requires no Python, no NDI SDK, and no other runtime installs.
"""

import glob
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

# ---------------------------------------------------------------------------
# Python runtime DLLs — PyInstaller sometimes misses these, causing
# "Failed to load Python DLL" on machines without Python installed.
# ---------------------------------------------------------------------------
_py_ver = f"{sys.version_info.major}{sys.version_info.minor}"
_runtime_binaries = []

for _name in (f"python{_py_ver}.dll", "python3.dll"):
    _path = os.path.join(sys.base_prefix, _name)
    if os.path.exists(_path):
        _runtime_binaries.append((_path, "."))

# MSVC C++ runtime — needed on clean Windows installs that lack the
# Visual C++ Redistributable (vcruntime140.dll etc.).
for _pattern in ("vcruntime140.dll", "vcruntime140_1.dll"):
    for _search in (sys.base_prefix,
                    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")):
        _found = glob.glob(os.path.join(_search, _pattern))
        if _found:
            _runtime_binaries.append((_found[0], "."))
            break

# Collect every file belonging to NDIlib (includes Processing.NDI.Lib.x64.dll)
# and to av (PyAV — FFmpeg is statically linked into its .pyd extension modules)
ndi_datas,    ndi_binaries,    ndi_hidden    = collect_all('NDIlib')
av_datas,     av_binaries,     av_hidden     = collect_all('av')
numpy_datas,  numpy_binaries,  numpy_hidden  = collect_all('numpy')
ct_datas,     ct_binaries,     ct_hidden     = collect_all('comtypes')
# python-osc is pure Python; collect_all returns empty lists silently if the
# package is missing at build time, which produces an exe that shows "not installed".
# Run setup.bat (or: pip install python-osc) before building.
try:
    osc_datas, osc_binaries, osc_hidden = collect_all('pythonosc')
    osc_hidden += collect_submodules('pythonosc')
except Exception:
    osc_datas = osc_binaries = osc_hidden = []

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=ndi_binaries + av_binaries + numpy_binaries + ct_binaries + osc_binaries + _runtime_binaries,
    datas=ndi_datas + av_datas + numpy_datas + ct_datas + osc_datas,
    hiddenimports=(
        ndi_hidden + av_hidden + numpy_hidden + ct_hidden + osc_hidden
        + ['NDIlib', 'NDIlib.NDIlib']
        + ['comtypes', 'comtypes.client', 'comtypes.server',
           'comtypes.server.inprocserver', 'comtypes.typeinfo']
        + ['pythonosc', 'pythonosc.dispatcher', 'pythonosc.osc_server',
           'pythonosc.udp_client', 'pythonosc.osc_message_builder',
           'pythonosc.osc_bundle_builder', 'pythonosc.osc_message',
           'pythonosc.osc_bundle', 'pythonosc.osc_packet',
           'pythonosc.osc_types', 'pythonosc.parsing',
           'pythonosc.parsing.osc_message', 'pythonosc.parsing.osc_bundle',
           'pythonosc.parsing.osc_types']
        + ['src', 'src.ndi_manager', 'src.stream_worker',
           'src.decklink_manager', 'src.decklink_worker',
           'src.osc_manager', 'src.recording_settings',
           'src.gui', 'src.gui.main_window', 'src.gui.stream_tile',
           'src.gui.settings_dialog', 'src.gui.add_source_dialog',
           'src.gui.styles']
    ),
    hookspath=[],
    hooksconfig={
        'gi': {'module-versions': {}},
    },
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'PIL', 'IPython'],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # use COLLECT (one-folder layout)
    name='NDI Multicam Recorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # leave UPX off; it can break Qt and NDI DLLs
    console=False,               # no black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,                   # set to an .ico path here to add an app icon
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='NDI Multicam Recorder',
)
