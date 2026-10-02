"""Writes the application icon to a multi-resolution Windows ``.ico``.

The icon is drawn in code, so there is no source image to keep in sync. The
build needs a real file, and PyInstaller wants a ``.ico``, so this renders one
on demand rather than committing a binary.

An ICO is a small directory of embedded images; PNG payloads are valid from
Vista onwards, which lets Qt do all the drawing.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Sizes Windows actually asks for: title bar, taskbar, and the large shell views.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _png_bytes(size: int) -> bytes:
    from PyQt6.QtCore import QBuffer, QByteArray

    from api_tester.icons import app_pixmap

    # The QByteArray must outlive the QBuffer that wraps it, so it is held in a
    # local rather than passed as a temporary.
    storage = QByteArray()
    buffer = QBuffer(storage)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    app_pixmap(size).save(buffer, "PNG")
    buffer.close()
    return bytes(storage)


def build_ico(destination: Path) -> Path:
    """Renders the icon and writes it to *destination*."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    owned = QApplication.instance() is None
    app = QApplication([]) if owned else QApplication.instance()

    images = [(size, _png_bytes(size)) for size in ICO_SIZES]
    # Qt objects must not be torn down while pixmaps are still being written,
    # so the application is left for the interpreter to collect.
    del app

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    directory = b""
    payload = b""
    for size, data in images:
        # 256 is stored as 0 in the single-byte width/height fields.
        stored = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII", stored, stored, 0, 0, 1, 32, len(data), offset
        )
        payload += data
        offset += len(data)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(header + directory + payload)
    return destination


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "build" / "app.ico"
    written = build_ico(target)
    print(f"Wrote {written} ({written.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
