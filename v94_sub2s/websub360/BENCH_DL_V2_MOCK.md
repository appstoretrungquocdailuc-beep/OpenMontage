# BENCH-DL — tải 360p thật, 2 video, 10 vòng/phương án, thứ tự xoay vòng (2026-11-15T20:38:14.116+00:00)

Mỗi lượt: link MỚI + xoá file trước (không dùng file có sẵn). Giá trị: median / P90 / P95 (ms). TỔNG = từ lúc bắt đầu resolve tới khi file đã tải xong + ffprobe xác minh + bàn giao (không tính TTFB 2 lần: TTFB nằm trong 'tải').

| Phương án | lượt | lỗi | TỔNG | ≤1000 ms | resolver | setup resolver | chờ máy chủ resolver | TTFB | setup tải | chờ máy chủ tải | truyền | ffprobe | tls_resumed | redirect_skip | hedge_win (gốc/replica, trên số lượt có bắn kép) | fallback_client | integrity_ms | đo khi đang tạm dừng vì 429 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm_all | 20 | 0 (0%) | 654 / 1983 / 1994 | 15/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 431 / 1780 / 1780 | 126 / 127 / 127 | 302 / 1652 / 1652 | 3 / 6 / 6 | 66 / 69 / 69 | 0/20 | 0 ok / 0 sai / 0 chưa rõ | 0/0 (bắn kép 0) | 0 | - | 0/20 |
| v2_ssl | 20 | 0 (0%) | 641 / 1985 / 1986 | 16/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 430 / 1780 / 1780 | 126 / 126 / 127 | 302 / 1652 / 1652 | 3 / 5 / 5 | 53 / 68 / 69 | 0/20 | 0 ok / 0 sai / 0 chưa rõ | 0/0 (bắn kép 0) | 0 | - | 0/20 |
| v2_redirect | 20 | 0 (0%) | 368 / 399 / 649 | 19/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 152 / 174 / 429 | 0 / 6 / 63 | 151 / 166 / 362 | 2 / 4 / 5 | 62 / 68 / 69 | 0/20 | 19 ok / 0 sai / 0 chưa rõ | 0/0 (bắn kép 0) | 0 | - | 0/20 |
| v2_hedge | 20 | 0 (0%) | 374 / 550 / 613 | 19/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 153 / 338 / 396 | 0 / 63 / 64 | 151 / 151 / 219 | 2 / 5 / 5 | 57 / 69 / 70 | 0/20 | 17 ok / 0 sai / 3 chưa rõ | 17/3 (bắn kép 20) | 0 | - | 0/20 |
| v2_full | 20 | 0 (0%) | 372 / 503 / 541 | 20/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 153 / 280 / 337 | 0 / 63 / 63 | 151 / 151 / 151 | 2 / 4 / 5 | 60 / 68 / 69 | 0/20 | 17 ok / 0 sai / 3 chưa rõ | 17/3 (bắn kép 20) | 0 | 0 / 0 / 0 | 0/20 |
| v2_full_auto | 20 | 0 (0%) | 599 / 1923 / 2003 | 17/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 353 / 372 / 376 | 63 / 65 / 66 | 151 / 1500 / 1501 | 33 / 1294 / 1418 | 66 / 70 / 70 | 0/20 | 9 ok / 0 sai / 1 chưa rõ | 9/1 (bắn kép 10) | 0 | 0 / 0 / 0 | 0/20 |
| warm_all_auto | 20 | 0 (0%) | 651 / 1997 / 1999 | 14/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 434 / 1780 / 1780 | 66 / 126 / 126 | 302 / 1652 / 1652 | 3 / 137 / 1295 | 57 / 68 / 69 | 0/20 | 0 ok / 0 sai / 0 chưa rõ | 0/0 (bắn kép 0) | 0 | - | 0/20 |

Phương án: legacy = bản trước (kết nối nguội); fast_cold = lớp mạng mới không giữ ấm; warm_resolver = giữ ấm Innertube, máy chủ video nguội; warm_all = giữ ấm cả máy chủ video vừa dùng; _r2/_r4 = chia 2/4 đoạn Range; warm_apihost = resolver qua youtubei.googleapis.com. v2_*: mỗi biến thể bật THÊM 1 nhóm cờ tối ưu tải v2; warm_all_auto = warm_all + range_connections "auto" (không cờ v2).
Lưu ý: cùng 1 video tải lặp lại thì máy chủ cache của Google (CDN) đã có sẵn → 'chờ máy chủ tải' có thể thấp hơn video vừa đăng thật; thứ tự xoay vòng để mọi phương án chịu ảnh hưởng như nhau.
