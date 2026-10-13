"""A direct practice entry point using Ativia's existing configuration, without other channels."""
from pathlib import Path
import sys


def main():
    from dotenv import load_dotenv
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from assistant_pause import PauseGate
    from controls import ControlBar
    from .controller import PracticeController

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / '.env')
    app = QApplication(sys.argv)
    app.setApplicationName('Ativia Language Practice')
    app.setQuitOnLastWindowClosed(False)

    class IdleTranslation:
        worker = None

        def stop(self):
            pass

    class PracticeHost:
        def __init__(self):
            self.control = ControlBar(text_only=True)
            self._pause_gate = PauseGate()
            self.translation = IdleTranslation()
            self.practice = PracticeController(self)
            self.practice.open()
            self.practice.window.closed.connect(self.quit)

        def _stop_current_reply(self):
            self.practice.stop()

        def quit(self):
            self._pause_gate.transition('PAUSING')
            self.practice.stop()
            self._finish_quit()

        def _finish_quit(self):
            if not self.practice.quiescent():
                QTimer.singleShot(30, self._finish_quit)
            else:
                self.control.deleteLater()
                app.quit()

    host = PracticeHost()
    app.aboutToQuit.connect(host.practice.stop)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
