# -*- coding: utf-8 -*-
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("V94_TELEMETRY", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile as _tf  # noqa: E402
os.environ.setdefault("V94_TELEMETRY_DIR", _tf.mkdtemp(prefix="v94_telem_"))


def _install_pyside6_stub():
    """Chỉ dùng khi máy test không có PySide6 (CI/Linux). Trên Windows thật luôn dùng PySide6."""
    if "PySide6" in sys.modules:
        return
    try:
        from PySide6 import QtWidgets, QtCore, QtGui  # noqa: F401
        return
    except Exception:
        for k in [k for k in sys.modules if k.startswith("PySide6")]:
            sys.modules.pop(k, None)

    class _Any:
        def __init__(self, *a, **k): pass
        def __getattr__(self, n): return _Any()
        def __call__(self, *a, **k): return _Any()

    class Signal:
        def __init__(self, *a, **k): self._subs = []
        def __set_name__(self, owner, name): self._name = name
        def __get__(self, obj, objtype=None):
            if obj is None:
                return self
            key = "_sig_" + self._name
            if key not in obj.__dict__:
                obj.__dict__[key] = _Bound()
            return obj.__dict__[key]

    class _Bound:
        def __init__(self): self._subs = []
        def connect(self, f): self._subs.append(f)
        def emit(self, *a):
            for f in list(self._subs):
                f(*a)

    class QObject:
        def __init__(self, *a, **k): pass

    class QEventLoop:
        def __init__(self, *a, **k): pass
        def quit(self): pass
        def exec(self): return 0

    class QTimer(_Any):
        @staticmethod
        def singleShot(ms, fn):
            try: fn()
            except Exception: pass

    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.Qt = _Any(); qtcore.QTimer = QTimer; qtcore.Signal = Signal
    qtcore.QObject = QObject; qtcore.QEventLoop = QEventLoop
    qtw = types.ModuleType("PySide6.QtWidgets")
    for n in ("QApplication", "QWidget", "QVBoxLayout", "QHBoxLayout", "QPushButton", "QLabel",
              "QFileDialog", "QTextEdit", "QLineEdit", "QFrame", "QMessageBox", "QSpinBox",
              "QCheckBox", "QRadioButton", "QComboBox", "QGridLayout", "QSizePolicy"):
        setattr(qtw, n, type(n, (_Any,), {}))
    qtg = types.ModuleType("PySide6.QtGui")
    qtg.QFont = _Any; qtg.QTextCursor = _Any
    pkg = types.ModuleType("PySide6")
    pkg.QtCore, pkg.QtWidgets, pkg.QtGui = qtcore, qtw, qtg
    sys.modules.update({"PySide6": pkg, "PySide6.QtCore": qtcore,
                        "PySide6.QtWidgets": qtw, "PySide6.QtGui": qtg})
    pkg.__v94_stub__ = True


_install_pyside6_stub()
_MODS = {}


def load_module(name: str):
    if name in _MODS:
        return _MODS[name]
    path = ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _MODS[name] = mod
    return mod


@pytest.fixture(scope="session")
def v94():
    return load_module("V9_4_SUB2S")


@pytest.fixture(scope="session")
def v93():
    return load_module("V9_3_AUDIO")


@pytest.fixture()
def gvs():
    from mock_gvs import MockGVS
    srv = MockGVS()
    srv.start()
    yield srv
    srv.stop()


class LogSink:
    def __init__(self):
        self.lines = []

    def __call__(self, msg):
        self.lines.append(str(msg))

    def text(self):
        return "\n".join(self.lines)


@pytest.fixture()
def logsink():
    return LogSink()


def get_qapp():
    """1 QApplication duy nhất cho cả phiên test (QWidget cần QApplication, không phải QCoreApplication)."""
    import PySide6
    if getattr(PySide6, "__v94_stub__", False):
        return None
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="session", autouse=True)
def qapp():
    return get_qapp()
