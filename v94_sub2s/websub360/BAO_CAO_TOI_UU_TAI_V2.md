# TỐI ƯU TẢI 360P — V2 (2026-11-15)

> **Mọi con số trong báo cáo này là SỐ MOCK** (máy chủ video giả trên 127.0.0.1, TLS thật) — **KHÔNG phải số YouTube thật.**
> Máy thử bị chặn `*.googlevideo.com` nên không tải được video thật. **Chưa có cơ sở để nói "đã đạt ≤ 1000 ms"**: chỉ
> `BENCH_TAI_360.bat` chạy trên máy bạn mới trả lời được câu đó.

## Kết luận ngắn
- Đã làm đủ 8 hạng mục, **mỗi mục sau cờ `net_*`/`dl_*`**. Tắt hết cờ = đi đúng đường code bản trước (có test chứng minh:
  `test_all_flags_off_takes_exactly_the_old_path`). Bản trước lưu ở `backup/*_truoc_toi_uu_tai_v2.py`.
- Trên mock (mô phỏng đặc điểm số đo thật của bạn: bắt tay 60 ms/kết nối mới, mọi link mới 302 sang cụm khác, chờ máy chủ 150 ms với
  10% lượt 1500 ms): **⟨MOCK_HEADLINE⟩**
- #1 (SSLContext dùng chung) **không thể hiện được trên Linux**: chi phí nằm ở việc duyệt kho chứng chỉ Windows, mock không mô phỏng
  được. Chỉ `BENCH_TAI_360.bat` trên Windows mới đo được.
- Vòng review đối kháng (70 agent, 4 góc nhìn: đúng/sai, an toàn YouTube, đồng thời, bench) xác nhận **12 lỗi** ở bản đầu → **đã sửa
  hết, mỗi lỗi có test hồi quy** (mục 3). Quan trọng nhất: sau 403/429 bản đầu có thể gửi thêm 2–3 request ngay lập tức → nay **không**.

## 1. Bảng hạng mục

Số dòng: `M` = `YOUTUBE_WEBSUB_INSTANT_360P.py`, `N` = `ws360_net.py`, `B` = `backup/YOUTUBE_WEBSUB_INSTANT_360P_truoc_toi_uu_tai_v2.py`,
`BN` = `backup/ws360_net_truoc_toi_uu_tai_v2.py`. Test: `tests/test_dl_v2.py`.

| # | Phát hiện (bản trước) | Đã sửa | Bằng chứng | Cờ (mặc định → rollback) |
|---|---|---|---|---|
| 1 | `BN:141 make_pool` và `B:789 pool()` tạo `PoolManager` không có `ssl_context` → urllib3 tạo SSLContext MỚI + `load_default_certs()` cho TỪNG kết nối (trên Windows: duyệt kho chứng chỉ, giữ GIL) | 1 context lazy + khoá (`N:188 shared_ssl_context`), dùng cho cả pool fast lẫn legacy (`M:819 pool`, `N:217 pool_kwargs`); vẫn xác minh chứng chỉ + hostname. `socket_options`: `TCP_NODELAY` + `SO_RCVBUF` (`N:208`). Ghi `tls_resumed` (`N:152`) | test: 10 kết nối → 1 context (bản cũ 10); xác minh hostname vẫn bật; socket options. Mock: ⟨MOCK_1⟩ | `net_shared_ssl` true → **false**; `net_rcvbuf_mb` 4 → **0** |
| 2 | `B:1042 _follow` theo 302 bằng kết nối mới tới host đích mỗi lần (1 vòng 302 + bắt tay + chờ máy chủ lần 2) | Học `host → đích` + `cụm sn → đích` TTL 10 phút (`M:1181/1193`), GET thẳng máy chủ đích, đoán sai → URL gốc ngay trong cùng lần thử (`M:1379 _open_media`); đoán sai → chặn đoán nguồn đó 10 phút (`M:1210`) | test: lần 2 cùng máy chủ / cùng cụm → 0 chặng 302; đoán sai → về URL gốc, 1 GET thường, không đoán lại. Mock: ⟨MOCK_2⟩ | `net_redirect_memory` true → **false** |
| 3 | Không dùng tham số `mn=` (danh sách cụm ứng viên) | Mở sẵn (nền, không chặn) tối đa 3 máy chủ `rrN---<sn>` từ `mn=` (`M:1224/1257`), mỗi file 1 lần, không mở trùng; chỉ ở `net_mode` fast. Phát hiện kèm: TLS 1.3 gửi session ticket làm kết nối mở sẵn "trông như đã đứt" → urllib3 vứt đi; sửa `N:96 is_connected` | test: bật → chặng sau 302 dùng kết nối có sẵn (`new_conns` 1 vs 2); Range chỉ mở sẵn 1 lần/file; legacy không mở sẵn | `net_mn_preconnect` true → **false** |
| 4 | Không có gì chống "đuôi chậm" của máy chủ (P90 chờ máy chủ 1–2 s) | Hedged GET (`M:1309`): sau `dl_hedge_delay_ms` chưa có header → thêm 1 request tới replica `mn=`; bên trả 2xx trước thắng, bên kia `close()`. Tối đa 1 request kép; không hedge khi lần trước 403/429 hoặc vừa có 429; replica 403/404/410/429 → cấm 10 phút (ghi nhận cả khi lỗi về MUỘN, `M:1293`) | test: A chậm 800 ms, B 100 ms → < 600 ms (≈ 120+100), ≤ 2 bắt tay; 403 nhanh → không bắn kép; 429 muộn của bên thua vẫn bị cấm. Mock: ⟨MOCK_4⟩ | `dl_hedge` true → **false**; `dl_hedge_delay_ms` 120 |
| 5 | `B:969 resolve` chỉ giữ client thắng → link 403 = `RETRY_WAIT` ≥ 1 s + resolve lại | `resolve` gắn `alt_futs` (`M:1031`), `_fetch` (`M:2475`) thử link client khác ĐÃ VỀ (cùng itag + dung lượng) ngay trong lần thử, ghi `fallback_client`. Link client khác = GET thường; tối đa 1 lần mỗi video | test: client 1 → 403, client 2 ok → DONE, `attempts == 1`. Mock: ⟨MOCK_5⟩ | `dl_alt_url_on_403` true → **false** |
| 6 | `B:1863` `range_connections` chỉ là số cố định | `"auto"` (`M:2279`): < 2 MiB → 1, 2–6 MiB → 2, > 6 MiB → 4; **mặc định GIỮ 1**. Biến thể bench `warm_all_auto` (so thẳng warm_all, ngưỡng 100 ms) + `v2_full_auto` | test: 1/3/8 MB → 1/2/4 kết nối. Mock: ⟨MOCK_6⟩ | `range_connections` **1** (không đổi) |
| 7 | Chỉ size + ffprobe (`B:1903`, ~45–60 ms tạo tiến trình) | `mp4_quick_check` (`M:1565`): duyệt box cấp 1, phải có `ftyp` đầu tiên + `moov` + `mdat`, tổng box == kích thước file; chạy TRƯỚC ffprobe (giữ ffprobe), ghi `integrity_ms` | test: file cụt / 0 byte / thiếu moov đều bị phát hiện trước ffprobe, < 2 ms. Mock: ⟨MOCK_7⟩ | `dl_quick_integrity` true → **false** |
| 8 | `B:1097-1098` khúc 256 KB + đệm file 1 MiB; `B:1894` + `B:1900` ghi `VERIFYING` 2 lần liên tiếp (nhánh DASH) | Khúc 1 MiB + `buffering=0` (khúc đầu vẫn 256 KB để mốc "byte đầu" đo y như bản cũ); bỏ lần ghi VERIFYING thứ nhất ở nhánh DASH (`M:2314`). Không thêm `sleep` nào | test: kích thước khúc theo cờ; khúc đầu 256 KB ở cả 2 đường. Mock: ⟨MOCK_8⟩ | `dl_hotpath_v2` true → **false** |

⟨MOCK_SECTION⟩
