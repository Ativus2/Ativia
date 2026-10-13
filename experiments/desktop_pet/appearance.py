"""Shared character assets for the pet, daily chat and tray."""
from pathlib import Path
from PySide6.QtGui import QIcon, QPixmap

ASSETS = Path(__file__).resolve().parent / 'assets'
CHARACTER_PATH = ASSETS / 'ativia-pixel.png'
FALLBACK_PATH = ASSETS / 'ativia.svg'


def character_icon():
    pixmap = QPixmap(str(CHARACTER_PATH))
    return QIcon(pixmap) if not pixmap.isNull() else QIcon(str(FALLBACK_PATH))
