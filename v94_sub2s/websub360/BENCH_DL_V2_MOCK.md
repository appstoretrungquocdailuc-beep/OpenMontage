# BENCH-DL — tải 360p thật, 2 video, 10 vòng/phương án, thứ tự xoay vòng (2026-11-15T17:26:11.233+00:00)

Mỗi lượt: link MỚI + xoá file trước (không dùng file có sẵn). Giá trị: median / P90 / P95 (ms). TỔNG = từ lúc bắt đầu resolve tới khi file đã tải xong + ffprobe xác minh + bàn giao (không tính TTFB 2 lần: TTFB nằm trong 'tải').

| Phương án | lượt | lỗi | TỔNG | ≤1000 ms | resolver | setup resolver | chờ máy chủ resolver | TTFB | setup tải | chờ máy chủ tải | truyền | ffprobe | tls_resumed | redirect_skip | hedge_win (gốc/replica) | fallback_client | integrity_ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm_all | 20 | 0 (0%) | 638 / 1991 / 1992 | 15/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 430 / 1780 / 1781 | 126 / 127 / 127 | 303 / 1652 / 1652 | 3 / 6 / 6 | 55 / 62 / 62 | 0/20 | 0 ok / 0 sai | 0/0 | 0 | - |
| v2_ssl | 20 | 0 (0%) | 649 / 1984 / 1988 | 14/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 430 / 1779 / 1779 | 126 / 126 / 127 | 302 / 1653 / 1653 | 3 / 5 / 5 | 61 / 68 / 73 | 0/20 | 0 ok / 0 sai | 0/0 | 0 | - |
| v2_redirect | 20 | 0 (0%) | 359 / 452 / 637 | 19/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 152 / 231 / 426 | 0 / 63 / 63 | 151 / 167 / 362 | 2 / 4 / 4 | 52 / 68 / 69 | 0/20 | 19 ok / 0 sai | 0/0 | 0 | - |
| v2_hedge | 20 | 0 (0%) | 370 / 1715 / 1776 | 16/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 152 / 1509 / 1566 | 0 / 6 / 63 | 151 / 1501 / 1501 | 3 / 5 / 5 | 53 / 67 / 70 | 0/20 | 20 ok / 0 sai | 0/0 | 0 | - |
| v2_full | 20 | 0 (0%) | 369 / 1705 / 1706 | 17/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 153 / 1502 / 1503 | 0 / 6 / 63 | 151 / 1501 / 1501 | 2 / 4 / 5 | 56 / 65 / 68 | 0/20 | 20 ok / 0 sai | 0/0 | 0 | 0 / 0 / 0 |
| v2_full_auto | 20 | 0 (0%) | 539 / 1929 / 1936 | 17/20 | 150 / 150 / 150 | 0 / 0 / 0 | - | 324 / 436 / 436 | 63 / 66 / 68 | 152 / 1500 / 1501 | 3 / 1288 / 1288 | 59 / 67 / 68 | 0/20 | 10 ok / 0 sai | 0/0 | 0 | 0 / 0 / 0 |

Phương án: legacy = bản trước (kết nối nguội); fast_cold = lớp mạng mới không giữ ấm; warm_resolver = giữ ấm Innertube, máy chủ video nguội; warm_all = giữ ấm cả máy chủ video vừa dùng; _r2/_r4 = chia 2/4 đoạn Range; warm_apihost = resolver qua youtubei.googleapis.com.
Lưu ý: cùng 1 video tải lặp lại thì máy chủ cache của Google (CDN) đã có sẵn → 'chờ máy chủ tải' có thể thấp hơn video vừa đăng thật; thứ tự xoay vòng để mọi phương án chịu ảnh hưởng như nhau.
