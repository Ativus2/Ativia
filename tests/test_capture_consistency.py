"""Regression coverage for the actual frozen-frame crop and failure recovery."""
import behavior_probes as probes
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QPixmap


class CaptureConsistencyTests(probes.AppCase):
    test_static_region = probes.BehaviorProbes.test_static_region
    test_frozen_error_region = probes.BehaviorProbes.test_frozen_error_region
    test_snip_start_failure_recovers = probes.BehaviorProbes.test_snip_start_failure_recovers

    def test_cancelled_selection_stays_idle(self):
        self.app._selecting = True
        self.app._on_region_cancelled()
        self.assertFalse(self.app._busy())
        self.ask.assert_not_called()

    def test_region_clips_at_frozen_desktop_edge(self):
        selector = self.app.region_overlay
        selector.setGeometry(QRect(-50, -20, 100, 80))
        selector._bg = QPixmap(200, 160)
        selector._bg.fill(QColor("green"))
        import io
        from PIL import Image
        selected = Image.open(io.BytesIO(selector.selected_jpeg(QRect(-70, -30, 40, 30))))
        self.assertEqual(selected.size, (40, 40))
        with self.assertRaises(ValueError):
            selector.selected_jpeg(QRect(500, 500, 10, 10))
