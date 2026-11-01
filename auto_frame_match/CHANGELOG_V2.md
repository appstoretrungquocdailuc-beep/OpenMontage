# CHANGELOG — AUTO FRAME MATCH V2 (2.0.0)

Bản sao lưu để quay lại: git tag `afm-v1.1.0-backup` (xem mục Rollback trong README_VI.md).

## Giai đoạn A — Kiểm toán: nguyên nhân video bị ngắn và cảnh ĐỎ

* **Mất khung (nguyên nhân gốc):** `pipeline._plan_segments_from_analysis` đặt `render=False` cho đoạn ĐỎ; `RenderPlan`
  chỉ cấp khung xuất cho đoạn được dựng → thành phẩm ngắn hơn bản Douyin (log thực tế: 257,37 s → 215,25 s).
  Ngoài ra `fps_mode="auto"` có thể xuất theo FPS nguồn → số khung khác bản edit.
* **Khớp sai nằm XANH (phát hiện nhờ bộ thử V2 mới):** tốc độ ngoài 0,5–2× (0,25×, ramp) bị ép vào đường thẳng sai;
  phim hoạt hình giữ khung khiến đường sai vẫn đạt ngưỡng từng khung (v1.1: 45 và 61 khung XANH sai).
* **Cảnh 1–2 khung bị nuốt:** PySceneDetect `min_scene_len=3` + gộp điểm cắt gần nhau + bộ dò nội bộ yêu cầu cực đại
  cả 2 phía; thêm lỗi lệch chỉ số trong bước dò ranh giới khi dải bị thu hẹp (có từ v1.0).
* **Cảnh ĐỎ vì phóng to:** ảnh nguồn 160×90 thiếu chi tiết; v1.1 đã thêm SIFT nhưng crop ước lượng ở 160 px lệch
  vài pixel → ở cảnh lia máy, (khung sai + crop lệch) khớp hơn (khung đúng).

## Thay đổi chính

| Hạng mục | V2 |
|---|---|
| Bất biến dòng thời gian | Mỗi khung edit có đúng 1 khung xuất (`timeline.check_invariant`), FPS xuất = FPS bản edit, QA đếm khung độc lập |
| Nguồn gốc từng khung | `SOURCE_RAW`, `SOURCE_INTERPOLATED`, `DOUYIN_CLEAN_FALLBACK`, `UNVERIFIED` |
| Xuất | **STRICT VERIFIED** (mặc định, không xuất khi còn khung chưa xác minh) / **PREVIEW FULL TIMELINE** (`*_CHUA_XAC_MINH`, khung chưa xác minh lấy từ bản Douyin) |
| Tốc độ | Dải 0,2×–6× + đứng hình; lưới log; 2 tầng cửa sổ (thường / rộng) |
| Speed ramp | DTW theo từng khung (vị trí ¼ khung × 16 lớp tốc độ, đổi dần), chỉ tinh chỉnh cảnh đã khớp |
| Đứng hình | Chỉ cho phép khi chính khung Douyin đứng yên (so với khung đầu, không bị lừa bởi khung giữ hình) |
| Khung nội suy Douyin | Phát hiện ảnh trộn 2 khung gốc → dựng lại bằng phép trộn tương ứng |
| Cứu hộ cảnh ĐỎ | Tầng 2 (Top-K×3, gấp đôi khung truy vấn) → SIFT cửa sổ ±5 s → Tầng 6 SIFT toàn bộ nguồn → Tầng 7 phân loại (quá ngắn / trơn-flash / dissolve / tĩnh / fade / chuyển động không có trong nguồn) |
| Phóng to ≥ 2× | Xác minh lại ở 640 px: tìm crop cùng lúc với khung nguồn (template matching), căn chỉnh trên lưới mịn; không khớp ở 640 px → UNMATCHED |
| Chống khớp sai | Chặn hình học méo (co giãn ngang/dọc lệch > 18%); phép thử "vùng hiển thị tĩnh" và "mơ hồ tốc độ" → VÀNG |
| Clean Douyin Fallback | PP-OCRv3 DB + CRNN (OpenCV DNN) + bộ dò phụ; SẠCH chỉ khi mọi bộ dò âm tính; 1–2 ký tự nghi ngờ → CHƯA CHẮC |
| Âm thanh | Khúc tốc độ không đổi time-stretch riêng (atempo giữ cao độ), fade 3 ms ở điểm nối, vị trí mẫu theo PTS edit; khung Douyin/đứng hình → im lặng + `AUDIO_UNRESOLVED` |
| Chế độ B | Đọc ánh xạ từng khung trong JSON V2 (giữ ramp, khung trộn); `DUYET_FALLBACK`; `BO_QUA` cũ không còn bỏ khung |
| GUI | Chế độ xuất, FAST/MAX ACCURACY, Per-Frame Speed, Deep Rescue, Clean Fallback, GPU; thống kê dòng thời gian; tab **C. DUYỆT CẢNH VÀNG/ĐỎ** (ảnh Douyin cạnh ảnh nguồn, chấp nhận/sửa nguồn/duyệt fallback, lưu & cắt) |
| Báo cáo mới | `FRAME_TIMELINE_EXACT.csv`, `SPEED_RAMP_REPORT.csv`, `CLEAN_FALLBACK_REPORT.csv`, `UNMATCHED_RESCUE_REPORT.csv` |
| Phụ thuộc | `requirements-lock.txt` (Python 3.11, đã kiểm wheel Windows), `tai_mo_hinh.py`, `DEPENDENCY_AUDIT.md` |

## Tương thích

* Giữ nguyên tên/cột cũ của `BANG_CAT_AUTO.csv/json`, `BANG_CAT_TUNG_FRAME.csv`, `SOURCE_MANIFEST.json`,
  `KIEM_TRA_THU_CONG.csv`, `QA_REPORT.html`, `QA_RESULT.json`; chỉ THÊM cột (`method`, `clean_check`, `origin`,
  `verified`, `interp_pair`). Bảng v1.x đọc được ở Chế độ B (mô hình tuyến tính theo đoạn).
* Hành vi đổi có chủ đích: "bản nháp" cũ (bỏ đoạn ĐỎ) được thay bằng PREVIEW giữ đủ khung.
