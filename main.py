import os
import ctypes
import logging
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from GUI import MainGUI
from controller import ControlAPI


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
RUNTIME_DIRS = ("temp_state", "logs")


def _ensure_runtime_dirs():
    for directory in RUNTIME_DIRS:
        Path(directory).mkdir(parents=True, exist_ok=True)


def _prevent_sleep():
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
        logger.info("[Power] Sleep prevention enabled")
    except Exception as e:
        logger.warning("[Power] Could not enable sleep prevention: %s", e)


def _allow_sleep():
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        logger.info("[Power] Sleep prevention disabled")
    except Exception as e:
        logger.warning("[Power] Could not disable sleep prevention: %s", e)

def main():
    _ensure_runtime_dirs()
    _prevent_sleep()
    app = QApplication([])
    controller = ControlAPI()
    gui = MainGUI(controller)
    controller.set_gui(gui)
    gui.show()
    try:
        app.exec()
    finally:
        _allow_sleep()


if __name__ == "__main__":
    main()
