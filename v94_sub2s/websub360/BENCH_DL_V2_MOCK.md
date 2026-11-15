# BENCH-DL — tải 360p thật, 2 video, 1 vòng/phương án, thứ tự xoay vòng (2026-11-15T17:23:31.528+00:00)

Mỗi lượt: link MỚI + xoá file trước (không dùng file có sẵn). Giá trị: median / P90 / P95 (ms). TỔNG = từ lúc bắt đầu resolve tới khi file đã tải xong + ffprobe xác minh + bàn giao (không tính TTFB 2 lần: TTFB nằm trong 'tải').

| Phương án | lượt | lỗi | TỔNG | ≤1000 ms | resolver | setup resolver | chờ máy chủ resolver | TTFB | setup tải | chờ máy chủ tải | truyền | ffprobe | tls_resumed | redirect_skip | hedge_win (gốc/replica) | fallback_client | integrity_ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm_all | 2 | 0 (0%) | 1329 / 1868 / 1936 | 1/2 | 150 / 150 / 150 | 0 / 0 / 0 | - | 1106 / 1645 / 1712 | 126 / 127 / 127 | 977 / 1516 / 1584 | 3 / 4 / 4 | 68 / 68 / 68 | 0/2 | 0 ok / 0 sai | 0/0 | 0 | - |
| v2_ssl | 2 | 0 (0%) | 1318 / 1858 / 1926 | 1/2 | 150 / 150 / 150 | 0 / 0 / 0 | - | 1106 / 1646 / 1714 | 127 / 127 / 127 | 977 / 1517 / 1585 | 3 / 3 / 3 | 59 / 60 / 60 | 0/2 | 0 ok / 0 sai | 0/0 | 0 | - |
| v2_redirect | 2 | 0 (0%) | 478 / 569 / 580 | 2/2 | 150 / 150 / 150 | 0 / 0 / 0 | - | 260 / 347 / 358 | 31 / 56 / 59 | 227 / 287 / 295 | 3 / 4 / 4 | 63 / 69 / 70 | 0/2 | 1 ok / 0 sai | 0/0 | 0 | - |
| v2_hedge | 2 | 0 (0%) | 362 / 366 / 366 | 2/2 | 150 / 150 / 150 | 0 / 0 / 0 | - | 152 / 152 / 153 | 0 / 0 / 0 | 151 / 151 / 151 | 2 / 3 / 3 | 56 / 60 / 61 | 0/2 | 2 ok / 0 sai | 0/0 | 0 | - |
| v2_full | 2 | 0 (0%) | 1032 / 1574 / 1642 | 1/2 | 150 / 150 / 150 | 0 / 0 / 0 | - | 829 / 1370 / 1438 | 0 / 0 / 0 | 826 / 1366 / 1434 | 2 / 3 / 3 | 49 / 50 / 50 | 0/2 | 2 ok / 0 sai | 0/0 | 0 | 0 / 0 / 0 |
| v2_full_auto | 2 | 0 (0%) | 507 / 619 / 633 | 2/2 | 150 / 150 / 150 | 0 / 0 / 0 | - | 294 / 407 / 421 | 34 / 61 / 64 | 152 / 153 / 153 | 4 / 7 / 8 | 58 / 61 / 62 | 0/2 | 1 ok / 0 sai | 0/0 | 0 | 0 / 0 / 0 |

Phương án: legacy = bản trước (kết nối nguội); fast_cold = lớp mạng mới không giữ ấm; warm_resolver = giữ ấm Innertube, máy chủ video nguội; warm_all = giữ ấm cả máy chủ video vừa dùng; _r2/_r4 = chia 2/4 đoạn Range; warm_apihost = resolver qua youtubei.googleapis.com.
Lưu ý: cùng 1 video tải lặp lại thì máy chủ cache của Google (CDN) đã có sẵn → 'chờ máy chủ tải' có thể thấp hơn video vừa đăng thật; thứ tự xoay vòng để mọi phương án chịu ảnh hưởng như nhau.
