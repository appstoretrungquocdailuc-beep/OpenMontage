# BENCH ARIA2 RPC vs downloader WS360 — 2026-11-16T13:31:38.644+00:00 — máy vm

CÙNG URL mỗi vòng (resolver WS360, link MỚI mỗi vòng), cùng máy, cùng dung lượng; thứ tự xoay vòng. TỔNG = tải + kiểm MP4 + ffprobe (không gồm resolver — giống nhau cho mọi phương án). TTFB aria2 = gửi addUri → byte đầu (đo mỗi 5 ms). 'kết nối' aria2 = số kết nối THỰC TẾ lớn nhất aria2 báo; ws360 = số kết nối tải (1 = 1 GET).

| Phương án | lượt | lỗi | file SAI | TỔNG med / P90 | tải med / P90 | TTFB med | kết nối thực tế med | ffprobe med | 8–10 MB dưới 1,5 s | chỉ lượt đầu vòng: TỔNG med |
|---|---|---|---|---|---|---|---|---|---|---|
| ws360 | 10 | 0 | 0 | 1679 / 1687 | 1614 / 1618 | 514 | 1 | 65 | 0/10 | 1679 (2) |
| aria2_1 | 10 | 0 | 0 | 1651 / 1690 | 1590 / 1627 | 488 | 1 | 62 | 0/10 | 1666 (2) |
| aria2_4 | 10 | 0 | 0 | 1327 / 1394 | 1272 / 1334 | 490 | 4 | 56 | 9/10 | 1313 (2) |
| aria2_8 | 10 | 0 | 0 | 1496 / 1532 | 1437 / 1472 | 488 | 8 | 56 | 5/10 | 1359 (2) |
| aria2_12 | 10 | 0 | 0 | 1264 / 1501 | 1195 / 1437 | 492 | 9 | 60 | 9/10 | 1217 (2) |

Quy tắc chọn: aria2_N chỉ được chọn khi **0 lỗi, 0 file sai** (SHA-256 trùng downloader WS360, đúng dung lượng, kiểm MP4 + ffprobe OK) VÀ nhanh hơn 'ws360' trên CÙNG URL: khoảng tin cậy 90% (bootstrap theo cặp) của mức giảm TỔNG > 0 ở CẢ median lẫn P90. Mục tiêu tham khảo: video 8–10 MB tải + xác minh < 1500 ms.

| Phương án | TỔNG med / P90 | giảm so với ws360 theo cặp: med [KTC 90%] | P90 [KTC 90%] | lỗi | file sai | 8–10 MB < 1,5 s | Kết luận |
|---|---|---|---|---|---|---|---|
| ws360 (hiện tại) | 1679 / 1687 | mốc | mốc | 0 | 0 | 0/10 | mốc |
| aria2_1 | 1651 / 1690 | +27 [+1…+29] | -3 [-13…+6] | 0 | 0 | 0/10 | ❔ chưa kết luận (chạy thêm vòng) |
| aria2_4 | 1327 / 1394 | +349 [+338…+352] | +292 [+13…+350] | 0 | 0 | 9/10 | ✅ nhanh hơn chắc chắn, file hợp lệ |
| aria2_8 | 1496 / 1532 | +187 [+177…+309] | +154 [+148…+184] | 0 | 0 | 5/10 | ✅ nhanh hơn chắc chắn, file hợp lệ |
| aria2_12 | 1264 / 1501 | +413 [+317…+468] | +185 [+151…+359] | 0 | 0 | 9/10 | ✅ nhanh hơn chắc chắn, file hợp lệ |

**Đề xuất:** `aria2_12` (median TỔNG 1264 ms). Bật THỬ trong config.json: `"dl_engine": "aria2_rpc", "aria2_connections": 12` — quay về: `"dl_engine": "http"`.

Lưu ý: cùng URL tải nhiều lần → các lượt sau có thể được cache CDN của Google phục vụ nhanh hơn; thứ tự xoay vòng chia đều ảnh hưởng này, cột 'chỉ lượt đầu vòng' trong BENCH_ARIA2.md cho số khi phương án là người tải ĐẦU TIÊN của URL.
