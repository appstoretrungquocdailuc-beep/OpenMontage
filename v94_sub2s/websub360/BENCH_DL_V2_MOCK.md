# BENCH-DL — tải 360p thật, 2 video, 10 vòng/phương án, thứ tự xoay vòng (2026-11-15T19:00:34.234+00:00)

Mỗi lượt: link MỚI + xoá file trước (không dùng file có sẵn). Giá trị: median / P90 / P95 (ms). TỔNG = từ lúc bắt đầu resolve tới khi file đã tải xong + ffprobe xác minh + bàn giao (không tính TTFB 2 lần: TTFB nằm trong 'tải').

| Phương án | lượt | lỗi | TỔNG | ≤1000 ms | resolver | setup resolver | chờ máy chủ resolver | TTFB | setup tải | chờ máy chủ tải | truyền | ffprobe | tls_resumed | redirect_skip | hedge_win (gốc/replica, trên số lượt có bắn kép) | fallback_client | integrity_ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm_all | 20 | 0 (0%) | 639 / 1988 / 2003 | 15/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 430 / 1780 / 1780 | 126 / 127 / 127 | 302 / 1652 / 1653 | 4 / 6 / 6 | 54 / 68 / 69 | 0/20 | 0 ok / 0 sai | 0/0 (bắn kép 0) | 0 | - |
| v2_ssl | 20 | 0 (0%) | 634 / 782 / 1985 | 18/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 430 / 566 / 1779 | 126 / 126 / 126 | 302 / 438 / 1652 | 3 / 5 / 6 | 52 / 64 / 67 | 0/20 | 0 ok / 0 sai | 0/0 (bắn kép 0) | 0 | - |
| v2_redirect | 20 | 0 (0%) | 360 / 1708 / 1708 | 17/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 152 / 1502 / 1502 | 0 / 0 / 3 | 151 / 1501 / 1501 | 2 / 5 / 5 | 52 / 68 / 68 | 0/20 | 19 ok / 0 sai | 0/0 (bắn kép 0) | 0 | - |
| v2_hedge | 20 | 0 (0%) | 365 / 377 / 394 | 20/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 153 / 154 / 164 | 0 / 0 / 3 | 151 / 151 / 152 | 2 / 4 / 4 | 58 / 68 / 71 | 0/20 | 20 ok / 0 sai | 19/1 (bắn kép 20) | 0 | - |
| v2_full | 20 | 0 (0%) | 358 / 543 / 545 | 20/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 153 / 337 / 337 | 0 / 63 / 63 | 151 / 151 / 151 | 2 / 4 / 4 | 50 / 62 / 65 | 0/20 | 20 ok / 0 sai | 14/6 (bắn kép 20) | 0 | 0 / 0 / 0 |
| v2_full_auto | 20 | 0 (0%) | 420 / 1708 / 1713 | 17/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 153 / 218 / 225 | 63 / 63 / 64 | 152 / 1501 / 1502 | 33 / 1288 / 1295 | 53 / 68 / 69 | 0/20 | 20 ok / 0 sai | 9/1 (bắn kép 10) | 0 | 0 / 0 / 0 |
| warm_all_auto | 20 | 0 (0%) | 649 / 1982 / 1990 | 13/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 433 / 570 / 1780 | 96 / 127 / 129 | 302 / 1516 / 1652 | 3 / 1289 / 1295 | 53 / 65 / 67 | 0/20 | 0 ok / 0 sai | 0/0 (bắn kép 0) | 0 | - |

Phương án: legacy = bản trước (kết nối nguội); fast_cold = lớp mạng mới không giữ ấm; warm_resolver = giữ ấm Innertube, máy chủ video nguội; warm_all = giữ ấm cả máy chủ video vừa dùng; _r2/_r4 = chia 2/4 đoạn Range; warm_apihost = resolver qua youtubei.googleapis.com. v2_*: mỗi biến thể bật THÊM 1 nhóm cờ tối ưu tải v2; warm_all_auto = warm_all + range_connections "auto" (không cờ v2).
Lưu ý: cùng 1 video tải lặp lại thì máy chủ cache của Google (CDN) đã có sẵn → 'chờ máy chủ tải' có thể thấp hơn video vừa đăng thật; thứ tự xoay vòng để mọi phương án chịu ảnh hưởng như nhau.
