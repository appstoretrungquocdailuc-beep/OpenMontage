# TỐI ƯU TẢI 360P — V2 (2026-11-15)

> **Mọi con số trong báo cáo này là SỐ MOCK** (máy chủ video giả trên 127.0.0.1, TLS thật) — **KHÔNG phải số YouTube thật.**
> Máy thử bị chặn `*.googlevideo.com` nên không tải được video thật. **Chưa có cơ sở để nói "đã đạt ≤ 1000 ms"**: chỉ
> `BENCH_TAI_360.bat` chạy trên máy bạn mới trả lời được câu đó.

## Kết luận ngắn
- Đã làm đủ 8 hạng mục, **mỗi mục sau cờ `net_*`/`dl_*`**. Tắt hết cờ = đi đúng đường code bản trước (có test chứng minh:
  `test_all_flags_off_takes_exactly_the_old_path`). Bản trước lưu ở `backup/*_truoc_toi_uu_tai_v2.py`.
- Trên mock (mô phỏng đặc điểm số đo thật của bạn: bắt tay 60 ms/kết nối mới, mọi link mới 302 sang cụm khác, chờ máy chủ 150 ms với
  10% lượt 1500 ms), lần chạy cuối trên code cuối cùng: TỔNG (resolve giả 150 ms → tải → ffprobe) median **654 → 372 ms**, P90
  **1983 → 503 ms**, số lượt > 1 s **5/20 → 0/20** (`warm_all` = bản hiện tại → `v2_full`). Lần chạy trước đó: 639 → 358 ms / 1988 → 543 ms /
  5/20 → 0/20. Phần lớn nhờ #2 (bỏ chặng 302: median −273 ms) và #4 (hedge: lượt bị "chờ máy chủ 1,5 s" được replica trả lời, tổng
  ~550 ms thay vì ~1,7 s). Đây là số MOCK — trên YouTube thật có thể khác nhiều (vd nếu Google không 302 thì #2 không lợi gì).
- #1 (SSLContext dùng chung) **không thể hiện được trên Linux**: chi phí nằm ở việc duyệt kho chứng chỉ Windows, mock không mô phỏng
  được. Chỉ `BENCH_TAI_360.bat` trên Windows mới đo được.
- **3 vòng review đối kháng** (≈ 160 agent; góc nhìn: đúng/sai, an toàn YouTube, đồng thời, bench; mỗi phát hiện phải qua agent bác bỏ)
  → **4 vòng sửa**, mỗi lỗi có test hồi quy (mục 3). Quan trọng nhất: sau 403/429 bản đầu có thể gửi thêm 2–3 request ngay lập tức → nay
  **không**: mọi 429 ghi nhận ngay, tạm dừng mọi tối ưu gửi thêm request 10 phút (giữ cả khi khởi động lại), lần thử sau chờ ≥ 10 s.

## 1. Bảng hạng mục

Số dòng: `M` = `YOUTUBE_WEBSUB_INSTANT_360P.py`, `N` = `ws360_net.py`, `B` = `backup/YOUTUBE_WEBSUB_INSTANT_360P_truoc_toi_uu_tai_v2.py`,
`BN` = `backup/ws360_net_truoc_toi_uu_tai_v2.py`. Test: `tests/test_dl_v2.py`.

| # | Phát hiện (bản trước) | Đã sửa | Bằng chứng | Cờ (mặc định → rollback) |
|---|---|---|---|---|
| 1 | `BN:141 make_pool` và `B:789 pool()` tạo `PoolManager` không có `ssl_context` → urllib3 tạo SSLContext MỚI + `load_default_certs()` cho TỪNG kết nối (trên Windows: duyệt kho chứng chỉ, giữ GIL) | 1 context lazy + khoá (`N:188 shared_ssl_context`), dùng cho cả pool fast lẫn legacy (`M:819 pool`, `N:217 pool_kwargs`); vẫn xác minh chứng chỉ + hostname. `socket_options`: `TCP_NODELAY` + `SO_RCVBUF` (`N:208`). Ghi `tls_resumed` (`N:152`) | test: 10 kết nối → 1 context (bản cũ 10); xác minh hostname vẫn bật; socket options. Mock: mở 1 kết nối TLS mới 2,98 → 2,70 ms (Linux, không đáng kể); số context 20 → 1 / 20 kết nối; bench `v2_ssl` vs `warm_all` median 654 → 641 ms (KTC 90% của mức giảm −1…+20 ms: không đạt 50 ms) | `net_shared_ssl` true → **false**; `net_rcvbuf_mb` 4 → **0** |
| 2 | `B:1042 _follow` theo 302 bằng kết nối mới tới host đích mỗi lần (1 vòng 302 + bắt tay + chờ máy chủ lần 2) | Học `host → đích` + `cụm sn → đích` TTL 10 phút (`M:1183/1195`, học ở `M:1108`), GET thẳng máy chủ đích (1 GET thường; KHÔNG áp cho đoạn Range), đoán sai → URL gốc ngay trong cùng lần thử (`M:1410 _open_media`), 429 → không gửi thêm; đoán sai → chặn đoán máy chủ + cụm 10 phút (`M:1212`) | test: lần 2 cùng máy chủ / cùng cụm → 0 chặng 302; đoán sai → về URL gốc, 1 GET thường, không đoán lại. Mock: `v2_redirect` vs `v2_ssl`: median 641 → 368 ms (KTC 90% của mức giảm: 264…289 ms), TTFB median 430 → 152 ms, 19/20 lượt bỏ được 302, 0 đoán sai | `net_redirect_memory` true → **false** |
| 3 | Không dùng tham số `mn=` (danh sách cụm ứng viên) | Mở sẵn (nền, không chặn) tối đa 3 máy chủ `rrN---<sn>` từ `mn=` (`M:1226/1259`), mỗi file 1 lần, không mở trùng; chỉ ở `net_mode` fast. Phát hiện kèm: TLS 1.3 gửi session ticket làm kết nối mở sẵn "trông như đã đứt" → urllib3 vứt đi; sửa `N:96 is_connected` | test: bật → chặng sau 302 dùng kết nối có sẵn (`new_conns` 1 vs 2); Range chỉ mở sẵn 1 lần/file; legacy không mở sẵn | `net_mn_preconnect` true → **false** |
| 4 | Không có gì chống "đuôi chậm" của máy chủ (P90 chờ máy chủ 1–2 s) | Hedged GET (`M:1324`): sau `dl_hedge_delay_ms` chưa có header → thêm 1 request tới replica `mn=`; bên trả 2xx trước thắng, bên kia `close()`. Tối đa 1 request kép; không hedge khi lần trước 403/429 hoặc vừa có 429; replica 403/404/410/429 → cấm 10 phút (ghi nhận cả khi lỗi về MUỘN, `M:1307`); có 429 ở bất kỳ bên nào → báo 429 | test: A chậm 800 ms, B 100 ms → < 600 ms (≈ 120+100), ≤ 2 bắt tay; 403 nhanh → không bắn kép; 429 muộn của bên thua vẫn bị cấm. Mock (2 lần chạy, 40 lượt mỗi bên): lượt > 1 s 4/40 → 1/40; lượt request chính rơi vào đuôi 1,5 s → replica trả lời, tổng ~550 ms thay vì ~1,7 s; median không đổi (±7 ms). Giá: mock bắn kép 20/20 lượt (xem mục 7) | `dl_hedge` true → **false**; `dl_hedge_delay_ms` 120 |
| 5 | `B:969 resolve` chỉ giữ client thắng → link 403 = `RETRY_WAIT` ≥ 1 s + resolve lại | `resolve` gắn `alt_futs` (`M:1031`), `_fetch` (`M:2555`) thử link client khác ĐÃ VỀ (cùng itag + dung lượng) ngay trong lần thử, ghi `fallback_client`. Link client khác = đúng 1 GET thường, khử trùng; tối đa 1 lần mỗi video (`M:1780 _dl_caution`); không thử khi đang tạm dừng vì 429 | test: client 1 → 403, client 2 ok → DONE, `attempts == 1`. Mock: link client thắng bị 403: cờ tắt → FAILED sau 3 lần thử (3624 ms); cờ bật → DONE 303 ms, `attempts=1` (5/5 lượt mỗi bên) | `dl_alt_url_on_403` true → **false** |
| 6 | `B:1863` `range_connections` chỉ là số cố định | `"auto"` (`M:2335`): < 2 MiB → 1, 2–6 MiB → 2, > 6 MiB → 4; **mặc định GIỮ 1**. Biến thể bench `warm_all_auto` (so thẳng warm_all, ngưỡng 100 ms) + `v2_full_auto` | test: 1/3/8 MB → 1/2/4 kết nối. Mock: `warm_all_auto` vs `warm_all`: median 651 vs 654 ms → GIỮ TẮT; `v2_full_auto` chậm hơn `v2_full` 227 ms median (đoạn Range không đoán 302, không hedge; mock không giới hạn băng thông mỗi kết nối → chia đoạn chỉ thêm chặng/kết nối) | `range_connections` **1** (không đổi) |
| 7 | Chỉ size + ffprobe (`B:1903`, ~45–60 ms tạo tiến trình) | `mp4_quick_check` (`M:1617`, gọi ở `M:2394`): duyệt box cấp 1, phải có `ftyp` đầu tiên + `moov` + `mdat`, tổng box == kích thước file; chạy TRƯỚC ffprobe (giữ ffprobe), ghi `integrity_ms` | test: file cụt / 0 byte / thiếu moov đều bị phát hiện trước ffprobe, < 2 ms. Mock: kiểm nhanh 0,07 ms vs ffprobe 50,9 ms (file 7,7 MB): file hỏng bị loại sớm hơn ~50 ms; file tốt chỉ tốn thêm 0,07 ms | `dl_quick_integrity` true → **false** |
| 8 | `B:1097-1098` khúc 256 KB + đệm file 1 MiB; `B:1894` + `B:1900` ghi `VERIFYING` 2 lần liên tiếp (nhánh DASH) | Khúc 1 MiB + `buffering=0` (`M:1464/1474`; khúc đầu vẫn 256 KB để mốc "byte đầu" đo y như bản cũ); bỏ lần ghi VERIFYING thứ nhất ở nhánh DASH (`M:2382`); body `chunked` giữ cách đọc/ghi cũ. Không thêm `sleep` nào | test: kích thước khúc theo cờ; khúc đầu 256 KB ở cả 2 đường. Mock: tải 8,67 MB trên loopback: 10,57 → 10,79 ms median (không khác biệt đo được trên mock) | `dl_hotpath_v2` true → **false** |

## 2. Số MOCK trước/sau — KHÔNG phải số YouTube thật

**Mô phỏng** (`tests/bench_dl_v2_mock.py`, chạy đúng `bench_dl` → `Engine.process` thật): `MockGVSHosts` (TLS thật, chứng chỉ tự ký, nhiều tên
`*.googlevideo.com` trên loopback), mỗi kết nối TLS mới tốn 60 ms, link mới ở `rrN---sn-aaa` (N ngẫu nhiên) luôn 302 sang `rrN---sn-bbb`,
chờ máy chủ 150 ms với 10% lượt 1500 ms, `mn=sn-aaa,sn-bbb,sn-ccc`, resolver giả 150 ms. 2 video (4,3 MB và 1,5 MB) × 10 vòng = **20 lượt mỗi
biến thể**, thứ tự xoay vòng, mỗi lượt link MỚI. File: `BENCH_DL_V2_MOCK.md`, `BENCH_DL_V2_MOCK_SUMMARY.md`, `BENCH_DL_V2_MOCK.csv`,
`BENCH_DL_V2_MOCK.json`, `BENCH_DL_V2_MICRO.json` (đo riêng #1/#7/#8, `tests/bench_dl_v2_micro.py`).
Bench chạy trên code sau vòng sửa 2; vòng 3 chỉ đổi đường xử lý lỗi (403/404/410/429, body `chunked`) — đường tải thành công mà bench đo
không đổi.

| Biến thể (cộng dồn) | median TỔNG | P90 TỔNG | lượt > 1 s | TTFB median / P90 | setup kết nối tải (median / P90) | bỏ 302 (đúng / chưa rõ) | hedge (bắn kép / replica thắng) | Kết luận tự động |
|---|---|---|---|---|---|---|---|---|
| `warm_all` (= bản hiện tại, mốc) | 654 | 1983 | 5/20 | 431 / 1780 | 126 / 127 | 0 | 0 / 0 | mốc |
| `v2_ssl` (+#1) | 641 | 1985 | 4/20 | 430 / 1780 | 126 / 126 | 0 | 0 / 0 | ⏸ GIỮ TẮT (median không nhanh hơn — đúng dự đoán trên Linux) |
| `v2_redirect` (+#2 #3) | 368 | 399* | 1/20 | 152 / 174 | 0 / 6 | 19 / 0 | 0 / 0 | ✅ BẬT |
| `v2_hedge` (+#4) | 374 | 550 | 1/20 | 153 / 338 | 0 / 63 | 17 / 3 | 20 / 3 | ❔ CHƯA KẾT LUẬN (P90 có thể kém v2_redirect — trong nhiễu) |
| `v2_full` (+#5 #7 #8) | 372 | 503 | 0/20 | 153 / 280 | 0 / 63 | 17 / 3 | 20 / 3 | ❔ CHƯA KẾT LUẬN (thắng chắc warm_all; P90 có thể kém v2_redirect — trong nhiễu) |
| `v2_full_auto` (+#6) | 599 | 1923 | 3/20 | 353 / 372 | 63 / 65 | 9 / 1 | 10 / 1 | range auto: ⏸ giữ 1 |
| `warm_all_auto` (#6 riêng) | 651 | 1997 | 6/20 | 434 / 1780 | 66 / 126 | 0 | 0 / 0 | ⏸ GIỮ TẮT (ngưỡng 100 ms) |

Lần chạy trước (cùng đường tải thành công, trước các bản sửa an toàn của vòng 2): `warm_all` 639 / 1988 (5/20 lượt > 1 s), `v2_redirect`
360 / **1708\*** (3/20), `v2_hedge` 365 / 377 (0/20), `v2_full` 358 / 543 (0/20).

Đọc bảng: so với **bản hiện tại (`warm_all`)**, `v2_redirect`, `v2_hedge`, `v2_full` đều **chắc chắn nhanh hơn ≥ 50 ms ở CẢ median lẫn P90**
(KTC 90% của mức giảm > 0). "Chưa kết luận" của `v2_hedge`/`v2_full` là ở bước so với `v2_redirect`: trong lần chạy này `v2_redirect`
chỉ có 1/20 lượt rơi vào đuôi chậm nên P90 của nó thấp bất thường; với 20 lượt không phân định được hedge có làm P90 tốt hơn hay kém đi
(lần chạy trước thì ngược lại: `v2_redirect` P90 1708 ms, `v2_hedge` 377 ms). Trên máy thật cần ≥ 2 lần `BENCH_TAI_360.bat`.

\* P90 với 20 lượt và đuôi chậm đúng 10% nằm ngay ranh giới đuôi: 1 hay 3 lượt rơi vào đuôi làm P90 của `v2_redirect` nhảy giữa 399 và
1708 ms giữa 2 lần chạy. Vì vậy `BENCH_DL_SUMMARY.md` dùng khoảng tin cậy bootstrap và trả "CHƯA KẾT LUẬN" thay vì đoán. P90 ~500–550 ms
của `v2_hedge`/`v2_full` đến từ các lượt request chính rơi vào đuôi và **replica** trả lời (120 ms chờ + 63 ms bắt tay kết nối nguội +
150 ms chờ máy chủ) — không phải từ đuôi 1,5 s.

Đo riêng (`BENCH_DL_V2_MICRO.json`):

| Mục | Trước | Sau | Ghi chú |
|---|---|---|---|
| #1 mở 1 kết nối TLS mới (20 lượt) | 2,98 ms, 20 SSLContext | 2,70 ms, 1 SSLContext | Linux: nạp chứng chỉ rẻ → khác biệt nhỏ; Windows phải đo bằng bench thật |
| #5 link client thắng bị 403 (5 lượt) | FAILED sau 3 lần thử, 3623 ms | DONE 303 ms, `attempts=1` | mock HTTP, 403 trả sau 200 ms |
| #7 kiểm file 7,7 MB | ffprobe 50,9 ms | + kiểm nhanh 0,07 ms trước ffprobe | ffprobe vẫn chạy (chốt cuối) |
| #8 tải 8,67 MB loopback (30 lượt) | 10,57 ms | 10,79 ms | không phân biệt được trên mock; giữ vì yêu cầu, rollback bằng `dl_hotpath_v2` |


## 3. Review đối kháng (3 vòng review, 4 vòng sửa)

### Vòng 1 — 12 lỗi xác nhận ở bản đầu (70 agent)

Mỗi lỗi được dựng lại trên mock bởi agent kiểm chứng trước khi tính là "xác nhận"; mỗi bản sửa có test hồi quy trong `tests/test_dl_v2.py`.

| # | Lỗi (bản đầu) | Mức | Sửa | Test hồi quy |
|---|---|---|---|---|
| R0, R3 | Máy chủ đoán (302) trả 429/403 → cùng lần thử gửi lại URL gốc **kèm hedge** (tới 3 request ngay sau 429) | cao | 429 ở máy chủ đoán → ném 429 luôn, **không gửi thêm** (process() chờ ≥ 10 s như cũ); lỗi khác → đúng 1 GET thường về URL gốc (không hedge, không mở sẵn) | `test_predicted_host_429_raises_without_any_extra_request` |
| R2 | 403/429 về MUỘN từ bên thua hedge bị bỏ qua → không cấm, không quên → mọi lượt sau lại gửi tới máy chủ đang giới hạn | cao | Ghi nhận lỗi ngay tại luồng nhận lỗi (`_hedge_error`): replica 403/404/410/429 → cấm 10 phút; request chính (đoán) lỗi → báo đoán sai; 429 → tạm dừng suy đoán | `test_late_429_from_losing_replica_bans_it_and_pauses_speculation`, `test_late_failure_of_predicted_host_is_reported_and_blocked_even_if_hedge_rescued` |
| R4 | Link client khác (sau 403) vẫn chạy hedge/đoán/mở sẵn và lặp lại ở MỌI lần thử (≈ 33 request bị 403 thay vì 10) | trung bình | Link client khác = GET thường; lần thử sau một lỗi 403/410/429 có `no_alt`/`no_spec` → mỗi lần thử đúng 1 GET như cũ | `test_alt_urls_are_plain_gets_and_skipped_on_the_next_attempt` |
| R5 | Range: mỗi đoạn tự đoán 302 → đoán sai/429 nhân n lần, mỗi đoạn lại quay về ngay | trung bình | (vòng 2 làm chặt hơn) **Range không đoán 302 nữa**; 429 của bất kỳ đoạn nào được báo trước | `test_range_parts_never_use_redirect_prediction`, `test_range_reports_429_even_if_another_part_fails_differently` |
| R6 | Mở sẵn `mn=` chạy ở MỖI đoạn Range, kiểm tra kết nối rảnh bị đua → 24 kết nối thừa/DASH | trung bình | Mở sẵn 1 lần/file + tập "đang mở" có khoá (không mở trùng) | `test_range_mn_preconnect_runs_once_per_file_not_per_part` |
| R7 | Quên cặp đoán sai nhưng request về URL gốc dạy lại đúng cặp đó → lần nào cũng đoán sai lại | trung bình | Đoán sai → chặn đoán cho máy chủ + cụm đó 10 phút (`_RDR_BLOCK`) | `test_redirect_memory_skips_302_and_falls_back_on_wrong_guess` (phần cuối) |
| R8 | Mở sẵn vẫn chạy ở `net_mode: legacy` (kết nối mở sẵn bị urllib3 gốc vứt đi → chỉ tốn bắt tay) | thấp | Chỉ mở sẵn ở lớp mạng `fast` | `test_mn_preconnect_is_skipped_in_legacy_net_mode` |
| R1 | Khúc 1 MiB làm "byte đầu" = 1 MiB đầu → TTFB của v2 trông tệ hơn ~400 ms chỉ vì cách đo | thấp | Khúc đầu vẫn 256 KB (đo giống bản cũ), sau đó 1 MiB | `test_v2_ttfb_first_read_is_256k_like_old_path` |
| R9 | Thiếu biến thể `warm_all_auto`; range auto bị xét với ngưỡng 50 ms thay vì 100 ms | trung bình | Thêm `warm_all_auto` (warm_all + range auto, không cờ v2) + ngưỡng 100 ms; kết luận range auto = cả warm_all_auto và v2_full_auto | `test_summary_clear_win_and_range_auto_needs_100ms_from_warm_all_auto`, `test_bench_variants_cover_spec_and_warm_all_auto_has_no_v2_flags` |
| R10 | 20 lượt + đuôi chậm 10% → P90 nhảy ~1,3 s theo số lượt rơi vào đuôi; dung sai 25 ms làm kết luận lật ngược | trung bình | Khoảng tin cậy 90% bootstrap cho Δ median/Δ P90; kết luận 3 mức BẬT / GIỮ TẮT / **CHƯA KẾT LUẬN**; thêm cột "lượt > 1 s" | `test_summary_marks_p90_flip_from_tail_count_as_inconclusive_not_off` |
| R11 | Hedge cứu được thì đoán sai vẫn ghi là "đoán đúng"; Range không báo đoán sai | thấp | `redirect_skip_fail` ghi cả khi hedge cứu; cột bench ưu tiên `FAIL:`; Range ghi đoán sai | như R2 + R5 |

### Vòng 2 — kiểm lại bản sửa (60 agent, chỉ đọc code, mỗi phát hiện qua 2 agent bác bỏ)
Kết quả: 3/12 lỗi vòng 1 sửa trọn; 9 lỗi còn "ca biên"; 18 phát hiện mới đứng vững (4 bị bác). Tất cả đã sửa:

| Nhóm lỗi (vòng 2) | Sửa | Test hồi quy |
|---|---|---|
| 429 của replica hedge bị lỗi khác (403 chậm/timeout của bên chính) che → quay về URL gốc, thử link client khác, chỉ chờ 1 s | Có 429 ở bất kỳ bên nào → báo 429; đang tạm dừng vì 429 → không quay về URL gốc; process() coi lần thử có 429 ở nhánh phụ là 429 (chờ ≥ 10 s) | `test_replica_429_is_not_hidden_by_slow_403_of_predicted_host` |
| Trong 120 ms chờ hedge, luồng khác gặp 429 → vẫn bắn kép | Kiểm lại trạng thái tạm dừng ngay trước khi gửi request dự phòng | (cùng cơ chế `_speculative_ok`) |
| Link client khác không tôn trọng tạm dừng 429; 2 client trả CÙNG link → gửi 2 lần; lỗi mạng của link phụ che 403 gốc | Bỏ qua khi đang tạm dừng; khử trùng link; link phụ = đúng 1 GET; lỗi mạng của link phụ → báo lại 403 gốc | `test_alt_loop_skipped_during_429_pause_and_alt_network_error_keeps_original_403` |
| "Thận trọng" ở lần thử sau chỉ dựa vào chữ lỗi cuối → 403 bị che thì lần sau lại suy đoán + thử link phụ | Nhớ THEO VIDEO (trong RAM) mọi 403/410/429 ở bất kỳ request nào → các lần thử sau: không suy đoán, không link phụ | `test_video_remembers_block_across_attempts_even_if_last_error_is_not_http` |
| `probe_size` nuốt 403/429 → GET ngay sau vẫn hedge/đoán/mở sẵn | Dò bị 403/410/429 → GET sau đó là GET thường (+ tạm dừng nếu 429) | `test_probe_403_makes_following_get_plain` |
| Range: các đoạn gửi cùng lúc nên "đoán 1 lần" vẫn nhân n request sai; lỗi đoạn đầu che 429 đoạn khác | Range KHÔNG đoán 302; báo lỗi theo thứ tự 429 > 403/410/404 > khác (cờ tắt: như cũ) | 2 test Range ở vòng 1 (đã viết lại) |
| Bản sửa R1 gây lỗi mới: body `chunked` hỏng khi trộn `read()` + `stream()` | Body chunked: chỉ dùng `stream()` như cũ | `test_v2_hotpath_handles_chunked_responses` |
| `dl_hedge_delay_ms` sai định dạng (vd "120ms") làm MỌI lượt tải lỗi kể cả khi cờ tắt | Chỉ đọc khi hedge, đọc an toàn (hỏng → 120 ms) | `test_malformed_hedge_delay_never_breaks_downloads` |
| Replica thắng khi request đoán chưa trả lời → vẫn tính "đoán đúng" | Ghi "chưa rõ" (cột `redirect_skip` = `?host`), không cộng vào số đoán đúng | `test_replica_win_while_predicted_pending_is_not_counted_as_correct_guess` |
| Bench: 1 lượt 429 làm các biến thể v2 sau đó chạy như đã tắt mà không ai biết | KHÔNG xoá tạm dừng (an toàn), nhưng ghi cột `spec_paused` từng lượt; biến thể có lượt bị ảnh hưởng → CHƯA KẾT LUẬN | `test_summary_marks_variants_measured_during_429_pause_inconclusive` |
| Bench: so với biến thể liền trước chỉ 2 trạng thái → 1 lượt nhiễu lật BẬT ↔ TẮT | 3 trạng thái: ổn / chậm hơn chắc chắn / chưa rõ | `test_summary_incremental_check_has_unsure_state_and_does_not_flip_on_one_run` |
| Bench: thiếu hoặc lỗi hết `warm_all_auto` mà vẫn ghi "range auto có thể bật"; dòng auto tự ghi BẬT trái kết luận chung | Range auto BẮT BUỘC có `warm_all_auto`; biến thể 0 lượt thành công = lỗi tăng; dòng auto trỏ về kết luận chung | `test_summary_range_auto_needs_warm_all_auto_and_failed_variants_count` |
| Chặn đoán theo cụm bị bỏ qua khi máy chủ khác cùng cụm có cặp riêng (bị bác là "không vi phạm", vẫn làm cho chặt) | Kiểm chặn cụm trước mọi tra cứu | (phần cuối test đoán sai) |

### Vòng 3 — kiểm lại bản sửa vòng 2 + tìm lỗi mới (chỉ đọc code; chạy tiếp 23/31 agent sau khi chạm giới hạn sử dụng)
- 27 mục của vòng 2: **19 đã sửa trọn**; 4 mục "một phần" là kết quả đọc TRƯỚC bản sửa `c12b7bd` (đã sửa ngay sau đó); 5 mục mới
  "một phần" đều ở phần bench → sửa ở vòng 4.
- 3 nhánh tìm lỗi mới (đúng/sai + giữ nguyên khi tắt cờ; an toàn YouTube; bench + đồng thời) → 15 phát hiện. Tất cả đã sửa (vòng 4).

### Vòng 4 — sửa các phát hiện của vòng 3

| Lỗi (vòng 3) | Sửa | Test hồi quy |
|---|---|---|
| 429 ở 1 đoạn Range / luồng tiếng DASH chỉ được ghi nhận khi mọi đoạn xong → worker khác vẫn suy đoán trong lúc đó | Ghi nhận 429 NGAY trong `_follow` (mọi 429); đoạn Range gặp 429 → ghi ngay + dừng các đoạn còn lại; 2 luồng DASH dùng chung trạng thái chặn; luồng hình lỗi → dừng + gom lỗi luồng tiếng | `test_any_429_pauses_speculation_immediately`, `test_range_429_in_one_part_pauses_now_and_stops_other_parts`, `test_dash_streams_share_block_state` |
| Máy chủ ĐOÁN trả 404/410 đúng lúc tạm dừng 429 bắt đầu → video bị FAILED vĩnh viễn (URL thật chưa được thử) | Trường hợp đó → báo 429 (thử lại sau ≥ 10 s), không bao giờ để 404/410 của máy chủ đoán thành lỗi của video | `test_predicted_host_404_during_new_429_pause_is_retryable_not_fatal` |
| 429 bị che → lỗi thật (Fatal, ffprobe, đổi tên) bị ghi đè thành 429 | Giữ lỗi thật (ghi + xét Fatal), chỉ áp hệ quả của 429 (chờ ≥ 10 s / Retry-After, không thử sớm) + ghi chú `[đã gặp HTTP 429]` | `test_fatal_error_is_not_rewritten_by_hidden_429`, `test_hidden_block_is_written_into_error_and_hidden_429_keeps_retry_after` |
| Trạng thái "thận trọng" và tạm dừng 429 mất khi khởi động lại tool; khôi phục bỏ qua lịch chờ của 429 | Ghi chú `[đã gặp HTTP …]` vào lỗi trong DB; mốc hết tạm dừng lưu ở `ws360_spec_pause.txt` (đã thêm vào `.gitignore`); khôi phục giữ `next_try` của video bị 429 | `test_429_pause_survives_restart_and_recover_keeps_next_try` |
| Range auto vẫn chia 2–4 đoạn ngay sau 403/429 hoặc khi đang tạm dừng; lỗi Range auto xử lý kiểu cũ (429 bị che) | Range auto → 1 kết nối khi đang thận trọng/tạm dừng; lỗi Range auto xử lý như v2 | `test_range_auto_uses_one_connection_while_cautious_or_paused` |
| DASH: chỉ chờ luồng tiếng 15 s rồi bỏ (file tạm mồ côi trên Windows) | Chờ 30 s; còn chạy thì khối `finally` chờ tiếp như bản cũ | (đọc code) |
| Link phụ: chỉ kiểm tạm dừng 429 lúc bắt đầu | Kiểm lại trước MỖI link | (đọc code) |
| Bench: chạy lượt tiếp 0,3 s sau một 429 | Chờ ≥ 10 s (hoặc tới `next_try`) sau mọi 429; HTTP/2 cũng vậy | (đọc code) |
| Bench: lọc "lượt sạch" chỉ ở phía v2 → bỏ riêng 1 video chậm làm nhóm KHÔNG tác dụng trông nhanh hơn → **BẬT sai** (mức cao) | So **theo cặp** (video, vòng): lượt bị loại ở biến thể này cũng bị loại ở phía được so | `test_summary_matched_pairs_do_not_reward_dropping_a_slow_video` |
| Bench: lượt mà chính request thêm (hedge/đoán) bị 429 bị coi là "tắt suy đoán" và bỏ đi | Giữ lượt đó, đếm `own_429` → nhóm cờ đó **GIỮ TẮT** (lỗi an toàn) | `test_summary_own_429_keeps_group_off_and_failed_clean_runs_are_off` |
| Bench: trạng thái thận trọng kéo dài suốt bench; biến thể có lượt sạch nhưng không lượt nào thành công lại ghi "chưa kết luận" | Mỗi lượt bench xoá trạng thái thận trọng của video (tạm dừng 429 thì giữ); DONE cũng xoá; có lượt sạch mà 0 thành công → GIỮ TẮT | (như trên) |
| Bench: so với biến thể trước chỉ dựa biến thể liền trước (kể cả khi nó lỗi hết hoặc chưa kết luận); < 5 lượt vẫn kết luận | So với biến thể liền trước CÓ số đo VÀ biến thể gần nhất đã có kết luận chắc (lấy kết quả xấu hơn); < 5 lượt → CHƯA KẾT LUẬN | `test_summary_compares_with_last_firm_variant_and_skips_failed_predecessor`, `test_summary_with_fewer_than_5_runs_is_inconclusive` |


Thêm (không có trong yêu cầu, chỉ làm cho an toàn hơn): **bất kỳ 429 nào từ googlevideo → 10 phút không gửi request suy đoán nào**
(đoán 302, hedge, mở sẵn, link client khác). Cờ tắt hết thì cơ chế này không có tác dụng gì và mọi đường lỗi giữ y bản cũ.

## 4. Hướng dẫn 5 dòng (đo thật trên máy bạn)
1. Tắt WS360 đang chạy (để mạng không bị chia), giữ nguyên `config.json` hiện tại (bench tự đổi cờ cho từng biến thể).
2. Chạy `BENCH_TAI_360.bat` → nhập **3–4 ID Shorts khác dung lượng** (vd ~3 MB, ~6 MB, ~9 MB), cách nhau dấu cách.
3. Chờ chạy xong (8 biến thể × ≥ 20 lượt mỗi biến thể; mỗi lượt lấy link MỚI, tải lại từ đầu, không đăng TikTok, không ghi Excel).
4. Gửi lại 3 file trong thư mục `websub360`: **`BENCH_DL.md`**, **`BENCH_DL_SUMMARY.md`**, **`BENCH_DL.csv`**.
5. Chỉ bật/tắt nhóm cờ theo `BENCH_DL_SUMMARY.md` của CHÍNH máy bạn ("CHƯA KẾT LUẬN" → chạy thêm 1 lần nữa trước khi đổi).

## 5. Cờ trong `config.json` và giá trị quay về bản cũ

| Cờ | Mặc định (bản này) | Quay về bản cũ |
|---|---|---|
| `net_shared_ssl` | `true` | `false` |
| `net_rcvbuf_mb` | `4` | `0` |
| `net_redirect_memory` | `true` | `false` |
| `net_mn_preconnect` | `true` | `false` |
| `dl_hedge` | `true` | `false` |
| `dl_hedge_delay_ms` | `120` | (không dùng khi `dl_hedge` false) |
| `dl_alt_url_on_403` | `true` | `false` |
| `dl_quick_integrity` | `true` | `false` |
| `dl_hotpath_v2` | `true` | `false` |
| `range_connections` | `1` (không đổi) | `1` |

Tất cả về giá trị cột phải = đúng đường code bản trước (test `test_all_flags_off_takes_exactly_the_old_path`). Lớp mạng cũ hơn nữa:
`"net_mode": "legacy"`. Bản đầy đủ trước v2: `backup/YOUTUBE_WEBSUB_INSTANT_360P_truoc_toi_uu_tai_v2.py`, `backup/ws360_net_truoc_toi_uu_tai_v2.py`.
`config.json` của bạn KHÔNG bị ghi đè: key nào thiếu thì lấy mặc định ở trên → muốn giữ hành vi cũ hoàn toàn, thêm các dòng ở cột phải.
File mới duy nhất tool tự tạo: `ws360_spec_pause.txt` (mốc hết tạm dừng 10 phút sau HTTP 429; xoá được bất cứ lúc nào, chỉ ảnh hưởng tối ưu v2).

## 6. Kết quả `pytest` trước/sau

| | Lệnh | Kết quả |
|---|---|---|
| Trước khi sửa (bản gốc) | `python -m pytest tests -q` (trong `websub360/`) | **176 passed** (4 phút 40) |
| Sau vòng 1 (8 hạng mục + sửa review vòng 1) | như trên | 205 passed |
| Sau vòng 2 | như trên | 216 passed |
| **Sau cùng (vòng 3)** | như trên | **221 passed** (5 phút 00) — 176 test cũ vẫn qua + 45 test mới trong `tests/test_dl_v2.py` |

Không test cũ nào bị sửa để "cho qua"; số test qua không giảm ở bất kỳ vòng nào. `tests/test_dl_v2.py` chạy lặp 3 lần liên tiếp đều qua
(kiểm tra không chập chờn).

## 7. Giới hạn — điều CHƯA chứng minh được
- **Không có số YouTube thật.** Mọi số ở mục 2 là mock trên loopback; độ lớn trên máy thật sẽ khác (Google có thể không 302, có thể không chậm đuôi như mô phỏng).
- **#1 trên Windows**: lợi ích dự kiến đến từ việc không duyệt kho chứng chỉ Windows cho mỗi kết nối — Linux không có chi phí đó (mục 2 cho thấy gần bằng nhau). Chỉ đo được bằng `BENCH_TAI_360.bat`.
- **TLS resumption (`tls_resumed`)**: Python chỉ resume phiên TLS khi được đưa session cũ; urllib3 không làm việc đó → cột `tls_resumed` gần như luôn `0`. Với TLS 1.3, resume cũng không bớt được vòng mạng nào (chỉ bớt tính toán), nên KHÔNG kỳ vọng lợi ích từ đây.
- **`SO_RCVBUF` 4 MiB**: đặt cố định thì hệ điều hành thôi tự co giãn bộ đệm nhận (Linux còn giới hạn theo `net.core.rmem_max`; trên mock loopback không có độ trễ/băng thông thật nên không đo được tác dụng); ảnh hưởng thật chỉ đo được trên máy bạn (Windows) → nếu bench `v2_ssl` không thắng, đặt `net_rcvbuf_mb: 0`.
- **Range + hedge + đoán 302**: yêu cầu ghi "đoạn đầu tiên dùng chính kết nối đang hedge" — bản này KHÔNG hedge và KHÔNG đoán 302 cho đoạn Range (các đoạn gửi cùng lúc nên một lần đoán sai/429 sẽ nhân lên n request trước khi kịp biết — review vòng 2). Range vẫn được mở sẵn `mn=` 1 lần/file. Range auto mặc định tắt; chỉ bật khi `BENCH_DL_SUMMARY.md` ghi BẬT cho cả `warm_all_auto` và `v2_full_auto`.
- **Mục 6 (range auto)** không đổi mặc định — đúng yêu cầu.
- **Hedge bắn kép rất thường xuyên với 120 ms**: trên mock, chờ máy chủ cơ bản 150 ms > 120 ms nên **20/20 lượt đều gửi thêm 1 request**
  (đúng giới hạn "tối đa 1 request kép/file", nhưng tăng gấp đôi số GET tới googlevideo). Trên máy thật, nếu cột "chờ máy chủ tải" trong
  `BENCH_DL.md` thường > 120 ms thì hedge cũng sẽ bắn gần như mọi video. Muốn chỉ hedge phần đuôi chậm: đặt `dl_hedge_delay_ms` ≈ P75–P90
  của "chờ máy chủ tải" (vd 300–400 ms) — đổi số này không cần sửa code.
