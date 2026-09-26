"""Draw the app icon ('?' on a dark rounded square) and write it as .icns.
Usage: python make_icon.py out/llm-popup.icns
Used by build.sh; needs only PySide6 and macOS's iconutil."""

import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPen

SIZES = [16, 32, 64, 128, 256, 512, 1024]


def draw(size):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    # macOS icons leave a margin inside the canvas; ~10% each side.
    inset = size * 0.1
    rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0, QColor("#3a3a3f"))
    grad.setColorAt(1, QColor("#17171a"))
    p.setPen(QPen(QColor(255, 255, 255, 40), max(1.0, size * 0.008)))
    p.setBrush(grad)
    p.drawRoundedRect(rect, rect.width() * 0.22, rect.width() * 0.22)
    font = QFont("Helvetica Neue")
    font.setPixelSize(int(rect.height() * 0.42))
    font.setWeight(QFont.DemiBold)
    p.setFont(font)
    p.setPen(QColor("#f2f2f2"))
    p.drawText(rect, Qt.AlignCenter, "?")
    p.end()
    return img


def main(out):
    QGuiApplication(sys.argv)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "llm-popup.iconset"
        iconset.mkdir()
        for s in SIZES:
            img = draw(s)
            if s <= 512:
                img.save(str(iconset / f"icon_{s}x{s}.png"))
            if s >= 32:
                img.save(str(iconset / f"icon_{s // 2}x{s // 2}@2x.png"))
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "build/icon/llm-popup.icns")
