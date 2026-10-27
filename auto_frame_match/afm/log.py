"""Hệ thống log: ghi ra file trong thư mục logs/ và chuyển tiếp lên GUI."""

from __future__ import annotations

import logging
import time
from pathlib import Path

_LOGGER_NAME = "afm"


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def setup_logging(log_dir: str | Path | None = None, level: int = logging.INFO,
                  extra_handler: logging.Handler | None = None) -> Path | None:
    logger = get_logger()
    logger.setLevel(logging.DEBUG)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)
    logger.addHandler(console)
    path = None
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        path = Path(log_dir) / f"afm_{time.strftime('%Y%m%d_%H%M%S')}.log"
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
    if extra_handler:
        extra_handler.setFormatter(fmt)
        extra_handler.setLevel(logging.INFO)
        logger.addHandler(extra_handler)
    logger.propagate = False
    return path


class Progress:
    """Báo tiến độ dạng (bước, phần trăm, thông điệp) cho CLI/GUI."""

    def __init__(self, callback=None):
        self.callback = callback
        self._last = 0.0

    def __call__(self, stage: str, frac: float, msg: str = "") -> None:
        now = time.time()
        if self.callback and (frac >= 1.0 or now - self._last > 0.2):
            self._last = now
            self.callback(stage, max(0.0, min(1.0, frac)), msg)
