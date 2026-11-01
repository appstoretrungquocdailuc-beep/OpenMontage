# BENCHMARK v1.1 → V2 (đo thật, 2026-11-01)

## Điều kiện đo — đọc trước khi dùng số liệu

* **Dữ liệu: video TỔNG HỢP** sinh bởi `tests/scenarios.py` (hoạt hình giả lập có đáp án từng khung).
  **KHÔNG có cặp video Douyin/anime thật** trong môi trường đo → chưa có số liệu trên video thật.
* Máy: Linux x86_64, 4 vCPU, 15 GB RAM, **không GPU** (FAISS CPU, giải mã CPU). Python 3.11.17 + đúng phiên bản trong
  `requirements-lock.txt`.
* Chỉ đo bước **phân tích/so khớp** (Chế độ A tới khi ra bảng cắt). Không đo thời gian dựng 2K/NVENC.
* v1.1 = tag `afm-v1.1.0-backup`; V2 = 2.0.0. Cùng dữ liệu, cùng bộ chấm điểm, mỗi kịch bản 1 tiến trình riêng, chạy tuần tự,
  cache trống. Riêng `ba_nguon_bien_doi` của V2 được đo lại một mình (lần đầu trùng lúc chạy test khác); độ đúng 2 lần như nhau.
* Lặp lại: `python tests/benchmark.py <thư mục afm> <thư mục dữ liệu> <nhãn> <kịch bản...>`.

Định nghĩa cột:

* **Khớp đúng (nhìn)**: đúng nguồn và khung trông giống hệt đáp án (kể cả khung giữ hình trùng ảnh).
* **Đúng chỉ số**: đúng nguồn và đúng số khung.
* **Sai**: khung có nguồn nhưng tool chỉ sai nguồn/khung.
* **XANH sai**: khung sai mà tool vẫn báo XANH (nguy hiểm nhất: không ai kiểm lại).
* **Bỏ sót**: khung có nguồn mà tool không tìm ra.
* **Khớp nhầm ngoài nguồn**: khung không có trong nguồn mà tool vẫn gán nguồn.
* **Đã xác minh**:
  * V2: số khung `verified` (SOURCE_RAW/INTERPOLATED XANH, hoặc DOUYIN_CLEAN_FALLBACK).
  * v1.1: số khung XANH (v1.1 chưa có khái niệm xác minh từng khung).

## Tổng hợp 11 kịch bản (3064 khung edit; 2801 có nguồn, 263 không có trong nguồn)

| Chỉ số | v1.1 | V2 |
|---|---|---|
| Khớp đúng (nhìn) / khung có nguồn | 2615 / 2801 (93,4%) | **2801 / 2801 (100%)** |
| Khớp đúng chỉ số khung | 2518 | **2682** |
| Khung khớp sai | 175 | **0** |
| Khung sai nhưng báo XANH | 106 | **0** |
| Khung bỏ sót (có nguồn mà không tìm ra) | 11 | **0** |
| Khớp nhầm khung ngoài nguồn | 0 | 0 |
| Khung ĐỎ | 277 (263 ngoài nguồn + 14 cứu được ở V2) | 263 (đúng bằng số khung ngoài nguồn) |
| Khung dùng được, đã xác minh đúng | 2421 / 3064 (79,0%) — 2527 XANH trừ 106 XANH sai | **2710 / 3064 (88,4%)** |
| Khung bị bỏ khỏi thành phẩm | 277 khung ĐỎ (theo logic v1.1 `render=False`, xem CHANGELOG_V2 mục A) | **0** (bất biến dòng thời gian; STRICT từ chối xuất thay vì cắt ngắn) |
| Tổng thời gian phân tích | 422 s | 642 s (**+52%**) |
| RAM đỉnh (1 tiến trình) | 551 MB | 830 MB |

## Chi tiết từng kịch bản

| Kịch bản | Bản | Khớp đúng (nhìn) | Đúng chỉ số | Sai | XANH sai | Bỏ sót | Khớp nhầm | Đã xác minh | Thời gian (s) | RAM (MB) |
|---|---|---|---|---|---|---|---|---|---|---|
| mot_nguon | v1.1 | 233/233 | 233 | 0 | 0 | 0 | 0 | 233/233 | 25,4 | 348 |
|  | V2 | 233/233 | 233 | 0 | 0 | 0 | 0 | 233/233 | 32,6 | 407 |
| hai_nguon_fps_phude | v1.1 | 309/309 | 309 | 0 | 0 | 0 | 0 | 309/309 | 29,2 | 418 |
|  | V2 | 309/309 | 309 | 0 | 0 | 0 | 0 | 309/309 | 36,0 | 540 |
| ba_nguon_bien_doi | v1.1 | 485/486 | 426 | 1 | 0 | 0 | 0 | 426/546 | 72,7 | 551 |
|  | V2 | 486/486 | 426 | 0 | 0 | 0 | 0 | 426/546 | 112,5 | 830 |
| doc_letterbox | v1.1 | 255/255 | 255 | 0 | 0 | 0 | 0 | 255/255 | 24,4 | 396 |
|  | V2 | 255/255 | 255 | 0 | 0 | 0 | 0 | 255/255 | 31,1 | 480 |
| doc_crop | v1.1 | 120/120 | 120 | 0 | 0 | 0 | 0 | 120/120 | 37,3 | 299 |
|  | V2 | 120/120 | 120 | 0 | 0 | 0 | 0 | 120/120 | 72,2 | 519 |
| lap_va_ngoai_nguon | v1.1 | 180/180 | 180 | 0 | 0 | 0 | 0 | 180/248 | 38,6 | 398 |
|  | V2 | 180/180 | 180 | 0 | 0 | 0 | 0 | 180/248 | 65,6 | 689 |
| zoom_manh_noi_suy | v1.1 | 255/255 | 254 | 0 | 0 | 0 | 0 | 255/300 | 54,1 | 408 |
|  | V2 | 255/255 | 253 | 0 | 0 | 0 | 0 | **242/300** | 101,1 | 747 |
| v2_toc_do (0,25×–5×) | v1.1 | 177/270 | 160 | **82** | **45** | 11 | 0 | 210/270 | 44,7 | 367 |
|  | V2 | **270/270** | **270** | 0 | 0 | 0 | 0 | 270/270 | 50,1 | 406 |
| v2_ramp_freeze | v1.1 | 241/333 | 221 | **92** | **61** | 0 | 0 | 179/333 | 37,1 | 352 |
|  | V2 | **333/333** | 276 | 0 | 0 | 0 | 0 | 285/333 | 40,4 | 413 |
| v2_fallback | v1.1 | 180/180 | 180 | 0 | 0 | 0 | 0 | 180/270 | 41,6 | 376 |
|  | V2 | 180/180 | 180 | 0 | 0 | 0 | 0 | **210/270** | 80,2 | 616 |
| v2_vfr | v1.1 | 180/180 | 180 | 0 | 0 | 0 | 0 | 180/180 | 17,1 | 343 |
|  | V2 | 180/180 | 180 | 0 | 0 | 0 | 0 | 180/180 | 20,4 | 437 |

## Nhận xét trung thực

* **Cải thiện lớn ở tốc độ bất thường / speed ramp.** v1.1 có 106 khung sai nhưng vẫn báo XANH. V2 không còn khung sai
  nào trên cả 11 kịch bản.
* **v2_fallback.**
  * V2 dùng được 30 khung Douyin đã kiểm sạch (`DOUYIN_CLEAN_FALLBACK`).
  * 60 khung có chữ/logo vẫn `UNVERIFIED`: STRICT không xuất, PREVIEW xuất đủ 270 khung.
  * v1.1 bỏ cả 90 khung khỏi bản nháp.
* **V2 thận trọng hơn → nhiều VÀNG hơn ở 2 kịch bản.** Các khung này đều khớp đúng, nhưng STRICT bắt duyệt (tab C)
  trước khi xuất chính thức:
  * `zoom_manh_noi_suy`: 13 khung VÀNG; v1.1 báo XANH và cũng đúng.
  * `v2_ramp_freeze`: 48 khung VÀNG (đoạn đứng hình/mơ hồ tốc độ).
* **"Đúng chỉ số" thấp hơn "khớp đúng (nhìn)" ở V2** (ramp_freeze 276/333, zoom 253/255). Nguyên nhân: khung giữ hình
  trùng ảnh, hoặc lệch 1 khung nhưng ảnh giống hệt. Thành phẩm không khác, nhưng đây không phải "đúng chỉ số 100%".
* **Chậm hơn ~52%, RAM cao hơn ~50%.** Nguyên nhân: DTW từng khung, xác minh lại 640 px, kiểm sạch OCR, các phép thử mơ hồ.
  Hồ sơ FAST trong GUI làm như sau:
  * giảm khung truy vấn và Top-K;
  * tắt DTW từng khung và tinh chỉnh độ phân giải cao, nên mất phần cải thiện speed ramp ở trên.

  Hồ sơ FAST chưa được đo riêng trong benchmark này.
* **Chưa đo:** video thật, GPU/NVENC, thời gian dựng 2K, video dài (> vài phút).
