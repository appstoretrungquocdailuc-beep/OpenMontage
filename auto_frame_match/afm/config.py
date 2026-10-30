"""Cấu hình mặc định và ngưỡng kiểm định của AUTO FRAME MATCH.

Mọi ngưỡng đều được hiệu chỉnh bằng bộ kiểm thử tổng hợp trong ``tests/``.
Có thể ghi đè bằng file ``afm_config.json`` đặt cạnh thư mục ``afm``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

TOOL_VERSION = "1.1.0"
# Tăng khi thay đổi cách tính fingerprint -> cache cũ tự động bị bỏ qua.
FINGERPRINT_VERSION = 6

TOOL_ROOT = Path(__file__).resolve().parent.parent


def default_cache_dir() -> Path:
    env = os.environ.get("AFM_CACHE_DIR")
    if env:
        return Path(env)
    return TOOL_ROOT / "cache"


@dataclass
class AnalysisConfig:
    # --- thumbnail dùng cho phân tích (grayscale, KHÔNG lưu ảnh PNG/BMP) ---
    source_thumb_area: int = 160 * 90
    edit_thumb_area: int = 256 * 144
    feat_side: int = 12  # vector đặc trưng feat_side x feat_side cho FAISS
    index_stride: int = 0  # lấy mẫu khung nguồn khi lập chỉ mục tìm nhanh (0 = tự động: 1, hoặc 2 nếu > 6000 khung)
    query_per_scene: int = 24  # số khung truy vấn tối đa/cảnh (căn chỉnh vẫn dùng mọi khung)
    topk: int = 12
    use_gpu_decode: bool = True
    workers: int = 0  # 0 = tự động theo số nhân CPU

    # --- phát hiện cảnh ---
    scenedetect_threshold: float = 27.0
    own_cut_ratio: float = 4.0  # điểm cắt bổ sung khi diff > ratio * median cục bộ
    min_scene_frames: int = 3

    # --- so khớp chính xác ---
    block_grid: int = 6
    block_keep: float = 0.75  # giữ 75% block tốt nhất (loại phụ đề/watermark)
    canvas_area: int = 96 * 54
    speed_min: float = 0.5
    speed_max: float = 2.0
    speed_step: float = 0.01
    speed_prior: float = 0.1  # phạt khi speed khác 1.0 (chống trôi do giữ khung)
    speed_min_frames: int = 12  # cảnh ngắn hơn -> không đủ bằng chứng để ước lượng tốc độ
    window_margin_frames: int = 36

    # --- ngưỡng kiểm định ---
    frame_ok: float = 0.80  # khung đạt (XANH)
    frame_min: float = 0.60  # dưới mức này coi là không khớp
    scene_green: float = 0.86  # median cảnh để XANH
    scene_match: float = 0.66  # median cảnh tối thiểu để không phải UNMATCHED
    min_ok_ratio: float = 0.95  # tỉ lệ khung đạt trong cảnh XANH
    match_ok_ratio: float = 0.5  # dưới mức này -> UNMATCHED (không được gán nguồn, kể cả VÀNG)
    ambiguity_margin: float = 0.015  # chênh lệch điểm giữa 2 nguồn ứng viên
    min_evidence_frames: int = 3  # không chấp nhận 1 khung giống làm bằng chứng
    static_texture_min: float = 2.0

    # --- tầng 2: SIFT + RANSAC cho đoạn UNMATCHED ---
    sift_enable: bool = True
    sift_step: float = 0.5  # giây giữa 2 khung nguồn lấy SIFT
    sift_width: int = 640
    sift_nfeat: int = 800
    sift_ratio: float = 0.7
    sift_min_inliers: int = 40
    sift_unique: float = 1.5  # phải gấp >= 1.5 lần ứng viên thứ hai
    sift_mask_bottom: float = 0.22  # bỏ phần dưới khung edit (phụ đề cứng)
    sift_window_s: float = 5.0  # nới cửa sổ tìm quanh 2 đoạn kề

    # --- DP theo thứ tự thời gian (Viterbi) ---
    dp_enable: bool = True
    dp_lambda: float = 0.02
    dp_window: float = 0.03  # chỉ xét ứng viên có điểm cách ứng viên tốt nhất <= mức này

    # --- nội suy đoạn ĐỎ từ đoạn kề (tùy chọn, mặc định TẮT) ---
    interpolate_unmatched: bool = False
    interp_speed_min: float = 0.5
    interp_speed_max: float = 1.1

    # --- xác minh độ phân giải cao ---
    hires_refine: bool = True
    hires_width: int = 640


@dataclass
class RenderConfig:
    height: int = 1440  # 2K / QHD
    codec: str = "auto"  # auto -> h264_nvenc nếu có, ngược lại libx264
    nvenc_cq: int = 18
    x264_crf: int = 16
    preset_nvenc: str = "p7"
    preset_x264: str = "slow"
    fps_mode: str = "auto"  # auto | edit | source
    crop_mode: str = "match_edit"  # match_edit | full_source
    audio_bitrate: str = "320k"
    audio_rate: int = 48000
    audio_lang: str = "jpn"
    allow_unverified: bool = False


@dataclass
class AppConfig:
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    cache_dir: str = field(default_factory=lambda: str(default_cache_dir()))

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    cfg = AppConfig()
    p = Path(path) if path else TOOL_ROOT / "afm_config.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        for section in ("analysis", "render"):
            for k, v in data.get(section, {}).items():
                if hasattr(getattr(cfg, section), k):
                    setattr(getattr(cfg, section), k, v)
        if "cache_dir" in data:
            cfg.cache_dir = data["cache_dir"]
    return cfg
