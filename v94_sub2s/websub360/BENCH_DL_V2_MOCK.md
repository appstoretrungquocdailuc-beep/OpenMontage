# BENCH-DL — tải 360p thật, 2 video, 10 vòng/phương án, thứ tự xoay vòng (2026-11-16T08:15:52.551+00:00)

Mỗi lượt: link MỚI + xoá file trước (không dùng file có sẵn). Giá trị: median / P90 / P95 (ms). TỔNG = từ lúc bắt đầu resolve tới khi file đã tải xong + ffprobe xác minh + bàn giao (không tính TTFB 2 lần: TTFB nằm trong 'tải').

| Phương án | lượt | lỗi | TỔNG | ≤1000 ms | resolver | setup resolver | chờ máy chủ resolver | TTFB | setup tải | chờ máy chủ tải | truyền | ffprobe | tls_resumed | redirect_skip | hedge_win (gốc/replica, trên số lượt có bắn kép) | fallback_client | integrity_ms | lượt suy đoán bị tắt (429 / thận trọng / chặn đoán) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm_all | 20 | 0 (0%) | 655 / 1982 / 2001 | 15/20 | 150 / 150 / 151 | 0 / 0 / 0 | - | 431 / 1779 / 1781 | 126 / 128 / 128 | 303 / 1652 / 1653 | 4 / 6 / 7 | 65 / 80 / 88 | 0/20 | 0 ok / 0 sai / 0 chưa rõ | 0/0 (bắn kép 0) | 0 | - | 0/20 (request thêm bị 429: 0) |
| v2_ssl | 20 | 0 (0%) | 650 / 1999 / 2004 | 16/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 430 / 1781 / 1782 | 126 / 127 / 128 | 302 / 1652 / 1653 | 3 / 6 / 7 | 66 / 70 / 72 | 0/20 | 0 ok / 0 sai / 0 chưa rõ | 0/0 (bắn kép 0) | 0 | - | 0/20 (request thêm bị 429: 0) |
| v2_redirect | 20 | 0 (0%) | 371 / 405 / 630 | 19/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 153 / 176 / 428 | 0 / 6 / 63 | 151 / 168 / 362 | 4 / 5 / 6 | 61 / 69 / 74 | 0/20 | 19 ok / 0 sai / 0 chưa rõ | 0/0 (bắn kép 0) | 0 | - | 0/20 (request thêm bị 429: 0) |
| v2_hedge | 20 | 0 (0%) | 376 / 588 / 647 | 19/20 | 150 / 150 / 151 | 0 / 0 / 0 | - | 153 / 340 / 398 | 0 / 63 / 64 | 151 / 152 / 219 | 2 / 6 / 9 | 61 / 77 / 93 | 0/20 | 17 ok / 0 sai / 3 chưa rõ | 17/3 (bắn kép 20) | 0 | - | 0/20 (request thêm bị 429: 0) |
| v2_full | 20 | 0 (0%) | 375 / 492 / 542 | 20/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 153 / 281 / 336 | 0 / 63 / 63 | 151 / 152 / 152 | 2 / 5 / 5 | 57 / 68 / 69 | 0/20 | 17 ok / 0 sai / 3 chưa rõ | 17/3 (bắn kép 20) | 0 | 0 / 0 / 0 | 0/20 (request thêm bị 429: 0) |
| v2_full_auto | 20 | 0 (0%) | 596 / 1929 / 2015 | 17/20 | 150 / 150 / 151 | 0 / 0 / 0 | - | 354 / 376 / 381 | 64 / 66 / 66 | 151 / 1501 / 1502 | 32 / 1295 / 1419 | 57 / 71 / 73 | 0/20 | 9 ok / 0 sai / 1 chưa rõ | 9/1 (bắn kép 10) | 0 | 0 / 0 / 0 | 0/20 (request thêm bị 429: 0) |
| warm_all_auto | 20 | 0 (0%) | 654 / 1999 / 2001 | 14/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 434 / 1780 / 1781 | 70 / 126 / 126 | 302 / 1652 / 1652 | 3 / 141 / 1298 | 61 / 69 / 72 | 0/20 | 0 ok / 0 sai / 0 chưa rõ | 0/0 (bắn kép 0) | 0 | - | 0/20 (request thêm bị 429: 0) |

Phương án: legacy = bản trước (kết nối nguội); fast_cold = lớp mạng mới không giữ ấm; warm_resolver = giữ ấm Innertube, máy chủ video nguội; warm_all = giữ ấm cả máy chủ video vừa dùng; _r2/_r4 = chia 2/4 đoạn Range; warm_apihost = resolver qua youtubei.googleapis.com. v2_*: mỗi biến thể bật THÊM 1 nhóm cờ tối ưu tải v2; warm_all_auto = warm_all + range_connections "auto" (không cờ v2).
Lưu ý: cùng 1 video tải lặp lại thì máy chủ cache của Google (CDN) đã có sẵn → 'chờ máy chủ tải' có thể thấp hơn video vừa đăng thật; thứ tự xoay vòng để mọi phương án chịu ảnh hưởng như nhau.
