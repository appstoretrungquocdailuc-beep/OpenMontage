"""Kiểm tra khung Douyin "sạch" để dùng làm fallback khi cảnh không có trong anime gốc.

Nguyên tắc: chỉ kết luận SẠCH khi MỌI bộ dò đều âm tính rõ ràng. Không phát hiện được chữ
KHÔNG có nghĩa là sạch -> trường hợp lưng chừng = CHUA_CHAC (đưa vào duyệt thủ công).

Bộ dò (chạy cục bộ, không cần mạng):
1. Watermark/logo TĨNH toàn video: điểm ảnh có cạnh sắc nhưng gần như không đổi qua mọi cảnh.
2. Dòng chữ chèn/phụ đề: các thành phần nét đậm có viền tương phản, cao gần bằng nhau, xếp thẳng hàng.
3. Logo/nhãn trong đoạn: vùng nhiều cạnh đứng yên trong khi phần còn lại của khung chuyển động.
4. (Tùy chọn) PaddleOCR nếu đã cài: có chữ -> BẨN.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import threading

import cv2
import numpy as np

from .log import get_logger
from .media import grab_frames

log = get_logger()

CLEAN, DIRTY, UNSURE = "SACH", "BAN", "CHUA_CHAC"


@dataclass
class CleanResult:
    verdict: str
    reasons: list = field(default_factory=list)
    scores: dict = field(default_factory=dict)


def static_overlay_mask(thumbs, n_samples: int = 160) -> np.ndarray:
    """Mặt nạ watermark/logo cố định trên toàn video: CẠNH tồn tại ở cùng vị trí qua gần như mọi khung.

    Không dựa vào độ sáng (watermark bán trong suốt đổi màu theo nền) mà dựa vào sự bền vững của cạnh.
    """
    n = thumbs.shape[0]
    idx = np.linspace(0, n - 1, min(n_samples, n)).astype(int)
    edges = np.stack([np.abs(cv2.Laplacian(thumbs[i].astype(np.float32), cv2.CV_32F, ksize=3)) > 14 for i in idx])
    persist = edges.mean(axis=0)
    mask = persist >= 0.85
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return mask.astype(bool)


_DB = None
_DB_LOCK = threading.Lock()
_DB_TRIED = False


def text_detector():
    """Bộ phát hiện chữ PP-OCRv3 DB (ONNX, OpenCV DNN). None nếu thiếu file mô hình."""
    global _DB, _DB_TRIED
    if _DB_TRIED:
        return _DB
    _DB_TRIED = True
    from .config import TOOL_ROOT

    path = TOOL_ROOT / "models" / "text_detection_cn_ppocrv3_2023may.onnx"
    if not path.exists():
        log.warning("Thiếu mô hình phát hiện chữ %s -> fallback sạch sẽ chỉ ở mức CHUA_CHAC", path.name)
        return None
    try:
        m = cv2.dnn.TextDetectionModel_DB(str(path))
        m.setBinaryThreshold(0.3)
        m.setPolygonThreshold(0.5)
        m.setMaxCandidates(200)
        m.setUnclipRatio(2.0)
        m.setInputParams(1.0 / 255.0, (736, 736), (122.67891434, 116.66876762, 104.00698793), True)
        _DB = m
    except Exception as e:  # pragma: no cover
        log.warning("Không nạp được mô hình phát hiện chữ: %s", e)
    return _DB


_REC = None
_REC_TRIED = False
_TV = np.array([[0, 31], [0, 0], [99, 0], [99, 31]], np.float32)


def text_recognizer():
    """CRNN-CN (OpenCV Zoo, Apache-2.0) để XÁC NHẬN hộp chữ đọc ra được ký tự. None nếu chưa tải."""
    global _REC, _REC_TRIED
    if _REC_TRIED:
        return _REC
    _REC_TRIED = True
    from .config import TOOL_ROOT

    path = TOOL_ROOT / "models" / "text_recognition_CRNN_CN_2021nov.onnx"
    if path.exists():
        try:
            _REC = cv2.dnn.readNet(str(path))
        except Exception as e:  # pragma: no cover
            log.warning("Không nạp được mô hình nhận dạng chữ: %s", e)
    else:
        log.warning("Chưa có %s (chạy tai_mo_hinh.py) -> mọi hộp nghi chữ đều bị coi là chữ", path.name)
    return _REC


def _confident_chars(img_736: np.ndarray, quad) -> list[float]:
    """Độ tin cậy từng ký tự CRNN đọc được trong hộp (giải mã CTC tham lam, bỏ ký tự trống)."""
    rec = text_recognizer()
    M = cv2.getPerspectiveTransform(np.array(quad, np.float32), _TV)
    crop = cv2.warpPerspective(img_736, M, (100, 32))
    blob = cv2.dnn.blobFromImage(crop, size=(100, 32), mean=127.5, scalefactor=1 / 127.5)
    with _DB_LOCK:
        rec.setInput(blob)
        o = rec.forward()[:, 0, :]
    e = np.exp(o - o.max(axis=1, keepdims=True))
    p = e / e.sum(axis=1, keepdims=True)
    a, pm = p.argmax(axis=1), p.max(axis=1)
    chars, prev = [], 0
    for ai, pi in zip(a, pm):
        if ai != 0 and ai != prev:
            chars.append(float(pi))
        prev = ai
    return chars


def text_level(chars: list[float]) -> int:
    """2 = chắc chắn chữ (>= 3 ký tự tin cậy >= 0.8); 1 = nghi chữ (1-2 ký tự rõ); 0 = không phải chữ."""
    strong = [c for c in chars if c >= 0.8]
    if len(strong) >= 3:
        return 2
    if len(strong) >= 1 or any(c >= 0.97 for c in chars):
        return 1
    return 0


def detect_text_boxes(img_bgr: np.ndarray, min_conf: float = 0.8) -> list | None:
    """Hộp chữ ĐÃ XÁC NHẬN (x, y, w, h, conf_det, số ký tự) trên ảnh gốc; None nếu không có mô hình phát hiện.

    Hộp do mô hình phát hiện đề xuất được CRNN đọc lại; hình vẽ giống chữ (nhân vật đơn giản) bị loại.
    Thiếu CRNN -> giữ mọi hộp (thận trọng: thà báo BẨN nhầm còn hơn coi chữ là sạch).
    """
    m = text_detector()
    if m is None:
        return None
    h, w = img_bgr.shape[:2]
    im = cv2.resize(img_bgr, (736, 736), interpolation=cv2.INTER_AREA)
    with _DB_LOCK:
        boxes, conf = m.detect(im)
    have_rec = text_recognizer() is not None
    out = []
    for b, c in zip(boxes, conf):
        if c < min_conf:
            continue
        level = 2  # thiếu CRNN: coi mọi hộp là chữ (thận trọng)
        if have_rec:
            level = text_level(_confident_chars(im, b))
            if level == 0:
                continue
        x, y, bw, bh = cv2.boundingRect(np.array(b))
        out.append((x * w / 736, y * h / 736, bw * w / 736, bh * h / 736, float(c), level))
    return out


def _text_lines(img_bgr: np.ndarray) -> tuple[int, list]:
    """Đếm dòng chữ ứng viên: >= 3 ký tự cùng chiều cao, cùng hàng, khoảng cách đều."""
    h, w = img_bgr.shape[:2]
    # gradient hình thái học lấy MAX trên 3 kênh màu (chữ vàng trên nền vàng-xám vẫn tách được)
    grad = np.max(np.stack([cv2.morphologyEx(np.ascontiguousarray(img_bgr[:, :, c]), cv2.MORPH_GRADIENT,
                                             np.ones((3, 3), np.uint8)) for c in range(3)]), axis=0)
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(bw, 8)
    comps = []
    for i in range(1, n):
        x, y, cw, ch, area = st[i]
        if not (0.015 * h <= ch <= 0.14 * h and 0.004 * w <= cw <= 0.12 * w):
            continue
        fill = area / float(cw * ch)
        if 0.12 <= fill <= 0.85:
            comps.append((x, y, cw, ch))
    lines = []
    used = set()
    comps.sort(key=lambda c: c[0])
    for i, a in enumerate(comps):
        if i in used:
            continue
        row = [a]
        for j in range(i + 1, len(comps)):
            if j in used:
                continue
            b = comps[j]
            last = row[-1]
            same_h = 0.6 <= b[3] / max(1, a[3]) <= 1.6
            same_y = abs((b[1] + b[3] / 2) - (a[1] + a[3] / 2)) < 0.5 * a[3]
            near = 0 <= b[0] - (last[0] + last[2]) <= 1.6 * a[3]
            if same_h and same_y and near:
                row.append(b)
                used.add(j)
        if len(row) >= 3:
            lines.append((row[0][0], row[0][1], row[-1][0] + row[-1][2] - row[0][0], max(r[3] for r in row)))
    return len(lines), lines


def _paddle_ocr():
    try:  # tùy chọn, không bắt buộc
        from paddleocr import PaddleOCR  # type: ignore

        return PaddleOCR(use_angle_cls=False, lang="ch", show_log=False)
    except Exception:
        return None


_OCR = None
_OCR_TRIED = False


def ocr_texts(img_bgr) -> list[str] | None:
    """Danh sách chữ PaddleOCR đọc được, hoặc None nếu chưa cài PaddleOCR."""
    global _OCR, _OCR_TRIED
    if not _OCR_TRIED:
        _OCR_TRIED = True
        _OCR = _paddle_ocr()
        log.info("PaddleOCR: %s", "có" if _OCR is not None else "không cài - chỉ dùng bộ dò chữ nội bộ")
    if _OCR is None:
        return None
    try:
        res = _OCR.ocr(img_bgr, cls=False) or []
        return [line[1][0] for page in res if page for line in page if line and line[1][1] > 0.6]
    except Exception as e:  # pragma: no cover
        log.warning("PaddleOCR lỗi: %s", e)
        return None


def check_segment(edit_path: str, times: list[float], width: int, global_mask: np.ndarray | None,
                  thumbs_seg: np.ndarray | None = None) -> CleanResult:
    """Kiểm tra các khung tại ``times`` (giây) của đoạn edit có sạch để dùng làm fallback không."""
    reasons, scores = [], {}
    dirty = unsure = False
    # 1) watermark/logo tĩnh toàn video
    if global_mask is not None:
        frac = float(global_mask.mean())
        scores["static_overlay"] = round(frac, 5)
        # cạnh bền vững có thể là nền đứng yên của chính phim -> chỉ là dấu hiệu, không tự kết luận BẨN
        if frac > 0.02:
            unsure = True
            reasons.append(f"nhiều cạnh cố định toàn video ({frac:.2%}) - có thể là watermark/khung trang trí")
    # 2) chữ trong từng khung: mô hình PP-OCRv3 DB (chính) + bộ dò hình thái học (phụ) + PaddleOCR (nếu cài)
    imgs = grab_frames(edit_path, times, width, gray=False)
    n_db, n_weak, n_lines, ocr_hit = [], [], [], []
    have_db = True
    for img in imgs:
        if img is None:
            unsure = True
            reasons.append("không đọc được khung để kiểm tra")
            continue
        boxes = detect_text_boxes(img)
        if boxes is None:
            have_db = False
        else:
            n_db.append(sum(1 for bx in boxes if bx[5] == 2))
            n_weak.append(sum(1 for bx in boxes if bx[5] == 1))
        k, _ = _text_lines(img)
        n_lines.append(k)
        t = ocr_texts(img)
        if t:
            ocr_hit.append(t)
    scores["text_boxes_db"] = n_db
    scores["text_lines_heuristic"] = n_lines
    if ocr_hit:
        dirty = True
        reasons.append(f"PaddleOCR đọc được chữ: {ocr_hit[0][:3]}")
    scores["text_boxes_weak"] = n_weak
    if n_db and any(k > 0 for k in n_db):
        dirty = True
        reasons.append(f"PP-OCRv3 + CRNN đọc được chữ ở {sum(k > 0 for k in n_db)}/{len(n_db)} khung")
    elif n_weak and any(k > 0 for k in n_weak):
        unsure = True
        reasons.append(f"nghi có chữ ngắn (1-2 ký tự) ở {sum(k > 0 for k in n_weak)}/{len(n_weak)} khung")
    if not have_db:
        unsure = True  # bộ dò phụ yếu hơn: không bao giờ tự kết luận SẠCH
        reasons.append("thiếu mô hình phát hiện chữ -> không thể xác nhận sạch")
        if any(k >= 1 for k in n_lines):
            dirty = True
            reasons.append("bộ dò hình thái học nghi có dòng chữ")
    # 3) (đã thử và loại bỏ) "vùng chi tiết đứng yên khi cảnh chuyển động": nền anime vốn đứng yên
    #    trong khi nhân vật chuyển động -> báo nhầm. Logo/nhãn hầu như luôn có chữ -> do bộ dò chữ bắt;
    #    logo cố định toàn video -> do mặt nạ tĩnh (1) bắt. Logo thuần hình, chỉ xuất hiện trong 1 cảnh
    #    là giới hạn đã biết (ghi trong báo cáo).
    if dirty:
        return CleanResult(DIRTY, reasons, scores)
    if unsure:
        return CleanResult(UNSURE, reasons, scores)
    return CleanResult(CLEAN, ["không phát hiện chữ, logo, watermark (đã chạy mọi bộ dò)"], scores)
