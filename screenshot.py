from io import BytesIO

import mss
from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPixmap

MAX_WIDTH = 1280
JPEG_QUALITY = 75
HASH_DISTANCE = 6


def qimage_to_jpeg(image: QImage, *, max_width=MAX_WIDTH, quality=JPEG_QUALITY) -> bytes:
    """Encode selected, already captured pixels without reading the desktop again."""
    if image.isNull():
        raise ValueError("选区图像为空")
    if max_width is not None and image.width() > max_width:
        image = image.scaledToWidth(max_width, Qt.TransformationMode.SmoothTransformation)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "JPEG", quality):
        raise RuntimeError("选区图像编码失败")
    return bytes(buffer.data())


def capture_selected_jpeg(rect=None) -> bytes:
    """Capture only explicitly selected Qt screen rectangles, preserving physical pixels."""
    from math import ceil
    from PySide6.QtGui import QPainter,QColor
    from PySide6.QtCore import QRectF
    from PySide6.QtWidgets import QApplication
    screens=QApplication.screens()
    if rect is None:rect=QApplication.primaryScreen().geometry()
    parts=[(s,rect.intersected(s.geometry())) for s in screens if s.geometry().intersects(rect)]
    if not parts or rect.isEmpty():raise ValueError('选区不在可用屏幕内')
    scale=max(s.devicePixelRatio() for s,_ in parts)
    width,height=ceil(rect.width()*scale),ceil(rect.height()*scale)
    if width*height>32_000_000:raise ValueError('选区过大，请缩小范围；不会降低小字分辨率。')
    canvas=QImage(width,height,QImage.Format_RGB32);canvas.fill(QColor('white'))
    painter=QPainter(canvas)
    try:
        for screen,part in parts:
            local=part.topLeft()-screen.geometry().topLeft()
            pixmap=screen.grabWindow(0,local.x(),local.y(),part.width(),part.height())
            if pixmap.isNull():raise ValueError('系统未返回选区画面')
            destination=QRectF((part.x()-rect.x())*scale,(part.y()-rect.y())*scale,part.width()*scale,part.height()*scale)
            painter.drawPixmap(destination,pixmap,QRectF(pixmap.rect()))
    finally:painter.end()
    return qimage_to_jpeg(canvas,max_width=None,quality=90)


def capture_primary_jpeg() -> bytes:
    """Capture the primary monitor and return a compressed JPEG."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)

    image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return _to_jpeg(image)


def capture_virtual_pixmap() -> QPixmap:
    """Capture the virtual desktop as a QPixmap for the snipping overlay."""
    with mss.mss() as sct:
        raw = sct.grab(sct.monitors[0])
    image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    qimage = QImage(
        image.tobytes("raw", "RGB"),
        image.width,
        image.height,
        image.width * 3,
        QImage.Format.Format_RGB888,
    )
    return QPixmap.fromImage(qimage.copy())


def capture_region_jpeg(left: int, top: int, width: int, height: int) -> bytes:
    """Capture a screen region in global Qt coordinates and return JPEG bytes."""
    width = max(1, int(width))
    height = max(1, int(height))
    left = int(left)
    top = int(top)

    with mss.mss() as sct:
        virtual = sct.monitors[0]
        raw = sct.grab(virtual)

    full = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        geo = app.primaryScreen().virtualGeometry()
        scale_x = full.width / max(1, geo.width())
        scale_y = full.height / max(1, geo.height())
        ox = int((left - geo.left()) * scale_x)
        oy = int((top - geo.top()) * scale_y)
        ow = max(1, int(width * scale_x))
        oh = max(1, int(height * scale_y))
    else:
        ox, oy, ow, oh = left - virtual["left"], top - virtual["top"], width, height

    ox = max(0, min(ox, full.width - 1))
    oy = max(0, min(oy, full.height - 1))
    ow = max(1, min(ow, full.width - ox))
    oh = max(1, min(oh, full.height - oy))
    image = full.crop((ox, oy, ox + ow, oy + oh))
    return _to_jpeg(image)


def _to_jpeg(image: Image.Image) -> bytes:
    if image.width > MAX_WIDTH:
        ratio = MAX_WIDTH / image.width
        image = image.resize(
            (MAX_WIDTH, int(image.height * ratio)),
            Image.Resampling.LANCZOS,
        )
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def screenshot_hash(image_jpeg: bytes) -> int:
    image = Image.open(BytesIO(image_jpeg)).convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    pixels = list(image.getdata())
    average = sum(pixels) / len(pixels)
    bits = 0
    for index, value in enumerate(pixels):
        if value >= average:
            bits |= 1 << index
    return bits


def hashes_similar(left: int, right: int, max_distance: int = HASH_DISTANCE) -> bool:
    return (left ^ right).bit_count() <= max_distance
