"""Tải mô hình nhận dạng chữ (CRNN-CN, OpenCV Zoo, Apache-2.0) dùng cho kiểm tra 'Douyin fallback sạch'.

Chạy 1 lần: python tai_mo_hinh.py   (CAI_DAT.bat tự gọi). Kiểm tra SHA-256 sau khi tải.
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

MODELS = {
    "text_recognition_CRNN_CN_2021nov.onnx": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/text_recognition_crnn/"
        "text_recognition_CRNN_CN_2021nov.onnx",
        "c760bf82d684b87dfabb288e6c0f92d41a8cd6c1780661ca2c3cd10c2065a9ba"),
    "text_detection_cn_ppocrv3_2023may.onnx": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/text_detection_ppocr/"
        "text_detection_cn_ppocrv3_2023may.onnx",
        "03f550c6b406fda8bf54bd8327815f6c7e2edd98cea02348c93d879254366587"),
}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    d = Path(__file__).resolve().parent / "models"
    d.mkdir(exist_ok=True)
    ok = True
    for name, (url, digest) in MODELS.items():
        p = d / name
        if p.exists() and sha256(p) == digest:
            print(f"[OK] {name}")
            continue
        print(f"Đang tải {name} ...")
        tmp = p.with_suffix(".part")
        try:
            urllib.request.urlretrieve(url, tmp)
        except Exception as e:
            print(f"[LỖI] Không tải được {name}: {e}")
            ok = False
            continue
        if sha256(tmp) != digest:
            print(f"[LỖI] Sai SHA-256: {name}")
            tmp.unlink(missing_ok=True)
            ok = False
            continue
        tmp.replace(p)
        print(f"[OK] {name}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
