# PyInstaller 打包配置。在项目根目录执行: pyinstaller desktop_app/build.spec
# 需先安装: pip install pyinstaller
# 生成 exe 在 dist/ 目录下；请将 desktop_app/config.json 复制到 dist 中与 exe 同目录

# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['desktop_app/main.py'],
    pathex=[],
    binaries=[],
    datas=[('desktop_app/config.json', '.')],
    hiddenimports=['customtkinter', 'yt_dlp'],
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
    name='视频收费下载',
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
)
