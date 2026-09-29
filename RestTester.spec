from pathlib import Path
import subprocess
import sys


project_root = Path(SPECPATH)

# The icon is drawn in code, so the .ico is rendered at build time rather than
# committed as a binary. A failure here must not be silent: the executable
# would otherwise ship with PyInstaller's default icon.
icon_path = project_root / "build" / "app.ico"
subprocess.run(
    [sys.executable, "-m", "tools.make_icon", str(icon_path)],
    cwd=str(project_root),
    check=True,
)

analysis = Analysis(
    ["run.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        # The API catalog is deliberately NOT bundled: this build expects the
        # team to load their own via Settings or generate one in the Catalog
        # Builder. api_tester.main falls back to an empty catalog and prompts
        # for one, so the shell still starts. Re-add the line below to ship a
        # catalog-inclusive build:
        #   (str(project_root / "data" / "api_catalog.json"), "data"),
        (str(project_root / "api_tester" / "assets"), "api_tester/assets"),
    ],
    hiddenimports=[
        # The .NET scanner imports the catalog generator lazily, so PyInstaller
        # cannot see it statically.
        "tools",
        "tools.generate_catalog",
        "api_tester.catalog_builder_ui",
        "api_tester.schema_editor",
        "api_tester.scanners.registry",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pymongo"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="RestTester",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path),
)

bundle = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="RestTester",
)
