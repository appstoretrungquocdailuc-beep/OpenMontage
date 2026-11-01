# DEPENDENCY_AUDIT — AUTO FRAME MATCH V2

Ngày kiểm tra: 2026-11-01. Môi trường kiểm thử: Linux x86_64 (máy ảo 4 nhân, **không GPU**),
Python 3.11.17 (khớp máy đích) và Python 3.13. Máy đích: Windows 11, Python 3.11, RTX 3060 12 GB.

## Thư viện Python

| Gói | v1.x đã dùng | Bản ổn định mới nhất (PyPI) | V2 khóa | Có wheel win_amd64 cp311? | Kết quả |
|---|---|---|---|---|---|
| numpy | 2.5.3 | 2.5.4 | **2.4.4** | 2.5.x: **KHÔNG** · 2.4.4: có | numpy 2.5 bỏ hỗ trợ Python 3.11 trên Windows → khóa 2.4.4 (bản mới nhất có wheel); toàn bộ test chạy trên 2.4.4/py3.11 |
| opencv-python-headless | 5.0.0.93 | 5.0.0.93 | 5.0.0.93 | có (abi3) | Giữ. Đã sửa `findTransformECC` (mask khác giữa OpenCV 4/5) từ v1.0 |
| opencv-python | (kéo theo bởi PySceneDetect) | 5.0.0.93 | 5.0.0.93 | có | Khóa CÙNG phiên bản với bản headless để tránh 2 bản cv2 lệch nhau |
| faiss-cpu | 1.15.1 | 1.15.1 | 1.15.1 | có | Giữ. IVF-SQ8 khi > 300 nghìn vector |
| scenedetect | 0.7.2 | 0.7.2 | 0.7.2 | có (thuần Python) | Giữ; sửa cảnh báo `get_seconds()` lỗi thời; `min_scene_len=1` (giữ cảnh 1–2 khung) |
| pytest | 9.1.1 | 9.2.0 | 9.2.0 | có | Chỉ dùng cho kiểm thử |

`requirements-lock.txt` = `pip freeze` của môi trường Python 3.11 đã chạy test (không dùng `pip install --upgrade` hàng loạt).

## FFmpeg / FFprobe

* Môi trường kiểm thử: FFmpeg 6.1.1 (Ubuntu). Máy Windows nên dùng bản `ffmpeg-release-full` của gyan.dev (có NVENC, CUDA).
* Những gì tool dựa vào và đã kiểm thử: `showinfo` lấy PTS từng khung khi giải mã; `-copyts` + `trim` theo PTS để lấy
  đúng dải khung (đã đối chiếu: giải mã dải 640 px khớp chỉ số khung của fingerprint); `-fps_mode passthrough`;
  `atempo` (giữ cao độ); concat demuxer; đếm khung độc lập bằng `ffprobe -count_packets/-count_frames`.
* **NVENC/CUDA: CHƯA kiểm thử trên GPU thật** (môi trường không có GPU). Đã kiểm thử nhánh dự phòng: giả lập
  "GPU báo có nhưng giải mã CUDA lỗi" → renderer tự dựng lại bằng CPU, đủ khung (`test_v2_loi_gpu_tu_chuyen_cpu`).

## Mô hình kèm theo (chạy bằng OpenCV DNN, CPU, không cần mạng sau khi tải)

| Mô hình | Nguồn | License | Kích thước | Phân phối |
|---|---|---|---|---|
| PP-OCRv3 DB phát hiện chữ (CN) | OpenCV Zoo `text_detection_ppocr` (gốc PaddleOCR) | Apache-2.0 | 2,4 MB | kèm trong `models/` |
| CRNN-CN nhận dạng chữ | OpenCV Zoo `text_recognition_crnn` | Apache-2.0 | 72,8 MB | tải bằng `tai_mo_hinh.py` (kiểm SHA-256) |

Lý do: phát hiện chữ (DB) đơn độc báo nhầm nhân vật hoạt hình đơn giản là chữ; CRNN đọc lại hộp chữ, chỉ hộp đọc ra
≥3 ký tự tin cậy mới là "chữ chắc chắn". Đánh giá trên bộ tổng hợp: mọi cửa sổ có chữ chèn/logo chữ/watermark/
phụ đề → BẨN; không có trường hợp khung có lớp phủ bị kết luận SẠCH.

## Công nghệ GitHub được đánh giá

| Repo | Quyết định | Bằng chứng / lý do |
|---|---|---|
| opencv/opencv | Dùng (5.0) | SIFT, ECC, DNN chạy model OCR |
| FFmpeg/FFmpeg | Dùng | xem trên |
| facebookresearch/faiss | Dùng CPU | FAISS GPU không có wheel pip chính thức cho Windows → giữ CPU (truy vấn chỉ chiếm phần nhỏ thời gian) |
| Breakthrough/PySceneDetect | Dùng 0.7.2 | + bộ dò diff nội bộ + dò lại ranh giới bằng so khớp |
| PaddlePaddle/PaddleOCR | Dùng **mô hình** PP-OCRv3 (qua OpenCV DNN); `paddleocr` là tùy chọn, tự dùng nếu đã cài | Cài paddlepaddle trên Windows nặng; mô hình ONNX đủ cho phát hiện chữ |
| facebookresearch/dinov2, dinov3 | **CHƯA tích hợp** | Không kiểm thử được: proxy môi trường chặn `download.pytorch.org` và `dl.fbaipublicfiles.com` (nơi phát hành PyTorch CPU và trọng số DINOv2). DINOv3 cần đăng ký tải trọng số (license riêng). Không đưa vào mã khi chưa đo được lợi ích |
| cvg/LightGlue | **CHƯA tích hợp** | Cần PyTorch (bị chặn như trên). Tầng SIFT+RANSAC hiện có đã cứu được ca phóng to trong bộ thử |
| soCzech/TransNetV2 | **CHƯA tích hợp** | Cần TensorFlow/PyTorch. Thay bằng: PySceneDetect + bộ dò diff nội bộ (giữ được cảnh 1 khung) + chia/gộp/dò lại ranh giới bằng so khớp nguồn |
| hzwer/ECCV2022-RIFE | **CHƯA tích hợp** | Khung nội suy của Douyin được phát hiện (ảnh trộn 2 khung gốc) và tái tạo bằng phép trộn 50/50 tương ứng (`SOURCE_INTERPOLATED`); RIFE chỉ cần khi nội suy dạng chuyển động, chưa có dữ liệu thử chứng minh cần |
| princeton-vl/RAFT | **CHƯA tích hợp** | Speed ramp được giải bằng DTW theo từng khung (không cần optical flow) và đạt 100% trên bộ thử |

Muốn đánh giá DINOv2/LightGlue: thêm `download.pytorch.org`, `dl.fbaipublicfiles.com` (và `github.com`) vào
Allowed domains của môi trường, hoặc chạy đánh giá trên máy Windows có mạng.
