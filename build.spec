# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main1.py'],  # Make sure main.py is in the same directory as this spec file
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'serial',
        'serial.tools.list_ports',
        'webview',
        'webview.platforms',
        'webview.platforms.winforms',
        'webview.platforms.gtk',
        'webview.platforms.cocoa',
        'webview.platforms.qt',
        'plotly',
        'tkinter',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Scientech Thrustmeter v2.2',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',  # Make sure icon.ico is in the same directory
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Scientech Thrustmeter v2.2',
)