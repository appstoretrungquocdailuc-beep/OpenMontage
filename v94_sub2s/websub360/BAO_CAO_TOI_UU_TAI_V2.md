# TỐI ƯU TẢI 360P — V2 (2026-11-15)

> **Mọi con số trong báo cáo này là SỐ MOCK** (máy chủ video giả trên 127.0.0.1, TLS thật) — **KHÔNG phải số YouTube thật.**
> Máy thử bị chặn `*.googlevideo.com` nên không tải được video thật. **Chưa có cơ sở để nói "đã đạt ≤ 1000 ms"**: chỉ
> `BENCH_TAI_360.bat` chạy trên máy bạn mới trả lời được câu đó.

## Kết luận ngắn
- Đã làm đủ 8 hạng mục, **mỗi mục sau cờ `net_*`/`dl_*`**. Tắt hết cờ = đi đúng đường code bản trước (có test chứng minh:
  `test_all_flags_off_takes_exactly_the_old_path`). Bản trước lưu ở `backup/*_truoc_toi_uu_tai_v2.py`.
- Trên mock (mô phỏng đặc điểm số đo thật của bạn: bắt tay 60 ms/kết nối mới, mọi link mới 302 sang cụm khác, chờ máy chủ 150 ms với
  10% lượt 1500 ms): **TỔNG (resolve giả 150 ms → tải → ffprobe) median **639 → 358 ms**, P90 **1988 → 543 ms**, số lượt > 1 s **5/20 → 0/20** (`warm_all` = bản hiện tại → `v2_full`). Phần lớn nhờ #2 (bỏ chặng 302: −274 ms median) và #4 (hedge cắt đuôi chậm: P90 1708 → 377 ms). Đây là số MOCK — trên YouTube thật có thể khác nhiều (vd nếu Google không 302, #2 không lợi gì).**
- #1 (SSLContext dùng chung) **không thể hiện được trên Linux**: chi phí nằm ở việc duyệt kho chứng chỉ Windows, mock không mô phỏng
  được. Chỉ `BENCH_TAI_360.bat` trên Windows mới đo được.
- Vòng review đối kháng (70 agent, 4 góc nhìn: đúng/sai, an toàn YouTube, đồng thời, bench) xác nhận **12 lỗi** ở bản đầu → **đã sửa
  hết, mỗi lỗi có test hồi quy** (mục 3). Quan trọng nhất: sau 403/429 bản đầu có thể gửi thêm 2–3 request ngay lập tức → nay **không**.

## 1. Bảng hạng mục

Số dòng: `M` = `YOUTUBE_WEBSUB_INSTANT_360P.py`, `N` = `ws360_net.py`, `B` = `backup/YOUTUBE_WEBSUB_INSTANT_360P_truoc_toi_uu_tai_v2.py`,
`BN` = `backup/ws360_net_truoc_toi_uu_tai_v2.py`. Test: `tests/test_dl_v2.py`.

| # | Phát hiện (bản trước) | Đã sửa | Bằng chứng | Cờ (mặc định → rollback) |
|---|---|---|---|---|
| 1 | `BN:141 make_pool` và `B:789 pool()` tạo `PoolManager` không có `ssl_context` → urllib3 tạo SSLContext MỚI + `load_default_certs()` cho TỪNG kết nối (trên Windows: duyệt kho chứng chỉ, giữ GIL) | 1 context lazy + khoá (`N:188 shared_ssl_context`), dùng cho cả pool fast lẫn legacy (`M:819 pool`, `N:217 pool_kwargs`); vẫn xác minh chứng chỉ + hostname. `socket_options`: `TCP_NODELAY` + `SO_RCVBUF` (`N:208`). Ghi `tls_resumed` (`N:152`) | test: 10 kết nối → 1 context (bản cũ 10); xác minh hostname vẫn bật; socket options. Mock: mở 1 kết nối TLS mới 2,98 → 2,70 ms (Linux, không đáng kể); số context 20 → 1 / 20 kết nối; bench `v2_ssl` vs `warm_all` median 639 → 634 ms (trong nhiễu) | `net_shared_ssl` true → **false**; `net_rcvbuf_mb` 4 → **0** |
| 2 | `B:1042 _follow` theo 302 bằng kết nối mới tới host đích mỗi lần (1 vòng 302 + bắt tay + chờ máy chủ lần 2) | Học `host → đích` + `cụm sn → đích` TTL 10 phút (`M:1181/1193`), GET thẳng máy chủ đích, đoán sai → URL gốc ngay trong cùng lần thử (`M:1379 _open_media`); đoán sai → chặn đoán nguồn đó 10 phút (`M:1210`) | test: lần 2 cùng máy chủ / cùng cụm → 0 chặng 302; đoán sai → về URL gốc, 1 GET thường, không đoán lại. Mock: `v2_redirect` vs `v2_ssl`: median 634 → 360 ms (KTC 90% của mức giảm: 262…280 ms), TTFB median 430 → 152 ms, 19/20 lượt bỏ được 302, 0 đoán sai | `net_redirect_memory` true → **false** |
| 3 | Không dùng tham số `mn=` (danh sách cụm ứng viên) | Mở sẵn (nền, không chặn) tối đa 3 máy chủ `rrN---<sn>` từ `mn=` (`M:1224/1257`), mỗi file 1 lần, không mở trùng; chỉ ở `net_mode` fast. Phát hiện kèm: TLS 1.3 gửi session ticket làm kết nối mở sẵn "trông như đã đứt" → urllib3 vứt đi; sửa `N:96 is_connected` | test: bật → chặng sau 302 dùng kết nối có sẵn (`new_conns` 1 vs 2); Range chỉ mở sẵn 1 lần/file; legacy không mở sẵn | `net_mn_preconnect` true → **false** |
| 4 | Không có gì chống "đuôi chậm" của máy chủ (P90 chờ máy chủ 1–2 s) | Hedged GET (`M:1309`): sau `dl_hedge_delay_ms` chưa có header → thêm 1 request tới replica `mn=`; bên trả 2xx trước thắng, bên kia `close()`. Tối đa 1 request kép; không hedge khi lần trước 403/429 hoặc vừa có 429; replica 403/404/410/429 → cấm 10 phút (ghi nhận cả khi lỗi về MUỘN, `M:1293`) | test: A chậm 800 ms, B 100 ms → < 600 ms (≈ 120+100), ≤ 2 bắt tay; 403 nhanh → không bắn kép; 429 muộn của bên thua vẫn bị cấm. Mock: `v2_hedge` vs `v2_redirect`: P90 1708 → 377 ms, lượt > 1 s 3/20 → 0/20, median +5 ms (trong nhiễu) | `dl_hedge` true → **false**; `dl_hedge_delay_ms` 120 |
| 5 | `B:969 resolve` chỉ giữ client thắng → link 403 = `RETRY_WAIT` ≥ 1 s + resolve lại | `resolve` gắn `alt_futs` (`M:1031`), `_fetch` (`M:2475`) thử link client khác ĐÃ VỀ (cùng itag + dung lượng) ngay trong lần thử, ghi `fallback_client`. Link client khác = GET thường; tối đa 1 lần mỗi video | test: client 1 → 403, client 2 ok → DONE, `attempts == 1`. Mock: link client thắng bị 403: cờ tắt → FAILED sau 3 lần thử (3623 ms); cờ bật → DONE 303 ms, `attempts=1` (5/5 lượt mỗi bên) | `dl_alt_url_on_403` true → **false** |
| 6 | `B:1863` `range_connections` chỉ là số cố định | `"auto"` (`M:2279`): < 2 MiB → 1, 2–6 MiB → 2, > 6 MiB → 4; **mặc định GIỮ 1**. Biến thể bench `warm_all_auto` (so thẳng warm_all, ngưỡng 100 ms) + `v2_full_auto` | test: 1/3/8 MB → 1/2/4 kết nối. Mock: `warm_all_auto` vs `warm_all`: median 649 vs 639 ms → GIỮ TẮT; `v2_full_auto` chậm hơn `v2_full` 63 ms median (mock không giới hạn băng thông mỗi kết nối → chia đoạn chỉ thêm kết nối) | `range_connections` **1** (không đổi) |
| 7 | Chỉ size + ffprobe (`B:1903`, ~45–60 ms tạo tiến trình) | `mp4_quick_check` (`M:1565`): duyệt box cấp 1, phải có `ftyp` đầu tiên + `moov` + `mdat`, tổng box == kích thước file; chạy TRƯỚC ffprobe (giữ ffprobe), ghi `integrity_ms` | test: file cụt / 0 byte / thiếu moov đều bị phát hiện trước ffprobe, < 2 ms. Mock: kiểm nhanh 0,07 ms vs ffprobe 50,9 ms (file 7,7 MB): file hỏng bị loại sớm hơn ~50 ms; file tốt chỉ tốn thêm 0,07 ms | `dl_quick_integrity` true → **false** |
| 8 | `B:1097-1098` khúc 256 KB + đệm file 1 MiB; `B:1894` + `B:1900` ghi `VERIFYING` 2 lần liên tiếp (nhánh DASH) | Khúc 1 MiB + `buffering=0` (khúc đầu vẫn 256 KB để mốc "byte đầu" đo y như bản cũ); bỏ lần ghi VERIFYING thứ nhất ở nhánh DASH (`M:2314`). Không thêm `sleep` nào | test: kích thước khúc theo cờ; khúc đầu 256 KB ở cả 2 đường. Mock: tải 8,67 MB trên loopback: 10,57 → 10,79 ms median (không khác biệt đo được trên mock) | `dl_hotpath_v2` true → **false** |

## 2. Số MOCK trước/sau — KHÔNG phải số YouTube thật

**Mô phỏng** (`tests/bench_dl_v2_mock.py`, chạy đúng `bench_dl` → `Engine.process` thật): `MockGVSHosts` (TLS thật, chứng chỉ tự ký, nhiều tên
`*.googlevideo.com` trên loopback), mỗi kết nối TLS mới tốn 60 ms, link mới ở `rrN---sn-aaa` (N ngẫu nhiên) luôn 302 sang `rrN---sn-bbb`,
chờ máy chủ 150 ms với 10% lượt 1500 ms, `mn=sn-aaa,sn-bbb,sn-ccc`, resolver giả 150 ms. 2 video (4,3 MB và 1,5 MB) × 10 vòng = **20 lượt mỗi
biến thể**, thứ tự xoay vòng, mỗi lượt link MỚI. File: `BENCH_DL_V2_MOCK.md`, `BENCH_DL_V2_MOCK_SUMMARY.md`, `BENCH_DL_V2_MOCK.csv`,
`BENCH_DL_V2_MOCK.json`, `BENCH_DL_V2_MICRO.json` (đo riêng #1/#7/#8, `tests/bench_dl_v2_micro.py`).

| Biến thể (cộng dồn) | median TỔNG | P90 TỔNG | lượt > 1 s | TTFB median / P90 | setup kết nối tải (median) | bỏ 302 | hedge (bắn kép / replica thắng) | Kết luận tự động |
|---|---|---|---|---|---|---|---|---|
| `warm_all` (= bản hiện tại, mốc) | 639 | 1988 | 5/20 | 430 / 1780 | 126 | 0 | 0 / 0 | mốc |
| `v2_ssl` (+#1) | 634 | 782* | 2/20 | 430 / 566 | 126 | 0 | 0 / 0 | ⏸ GIỮ TẮT (median không nhanh hơn — đúng dự đoán trên Linux) |
| `v2_redirect` (+#2 #3) | 360 | 1708* | 3/20 | 152 / 1502 | 0 | 19/20 | 0 / 0 | ❔ CHƯA KẾT LUẬN (median chắc chắn nhanh hơn; P90 trong nhiễu) |
| `v2_hedge` (+#4) | 365 | 377 | 0/20 | 153 / 154 | 0 | 20/20 | 20 / 1 | ✅ BẬT |
| `v2_full` (+#5 #7 #8) | 358 | 543* | 0/20 | 153 / 337 | 0 | 20/20 | 20 / 6 | ✅ BẬT |
| `v2_full_auto` (+#6) | 420 | 1708* | 3/20 | 153 / 218 | 63 | 20/20 | 10 / 1 | ❔ (chậm hơn v2_full 63 ms median) |
| `warm_all_auto` (#6 riêng) | 649 | 1982 | 7/20 | 433 / 570 | 96 | 0 | 0 / 0 | ⏸ GIỮ TẮT (ngưỡng 100 ms) |

\* P90 với 20 lượt và đuôi chậm đúng 10% nằm ngay ranh giới đuôi: 2 hay 3 lượt rơi vào đuôi làm P90 nhảy ~1,3 s. Vì vậy
`BENCH_DL_SUMMARY.md` dùng khoảng tin cậy bootstrap và trả "CHƯA KẾT LUẬN" thay vì đoán (lỗi R10 ở mục 3). `v2_redirect` chắc chắn
nhanh hơn ở median (KTC 90% của mức giảm: 268…296 ms) nhưng P90 không phân định được với 20 lượt.

Đo riêng (`BENCH_DL_V2_MICRO.json`):

| Mục | Trước | Sau | Ghi chú |
|---|---|---|---|
| #1 mở 1 kết nối TLS mới (20 lượt) | 2,98 ms, 20 SSLContext | 2,70 ms, 1 SSLContext | Linux: nạp chứng chỉ rẻ → khác biệt nhỏ; Windows phải đo bằng bench thật |
| #5 link client thắng bị 403 (5 lượt) | FAILED sau 3 lần thử, 3623 ms | DONE 303 ms, `attempts=1` | mock HTTP, 403 trả sau 200 ms |
| #7 kiểm file 7,7 MB | ffprobe 50,9 ms | + kiểm nhanh 0,07 ms trước ffprobe | ffprobe vẫn chạy (chốt cuối) |
| #8 tải 8,67 MB loopback (30 lượt) | 10,57 ms | 10,79 ms | không phân biệt được trên mock; giữ vì yêu cầu, rollback bằng `dl_hotpath_v2` |


## 3. Review đối kháng — 12 lỗi xác nhận ở bản đầu, đã sửa

Mỗi lỗi được dựng lại trên mock bởi agent kiểm chứng trước khi tính là "xác nhận"; mỗi bản sửa có test hồi quy trong `tests/test_dl_v2.py`.

| # | Lỗi (bản đầu) | Mức | Sửa | Test hồi quy |
|---|---|---|---|---|
| R0, R3 | Máy chủ đoán (302) trả 429/403 → cùng lần thử gửi lại URL gốc **kèm hedge** (tới 3 request ngay sau 429) | cao | 429 ở máy chủ đoán → ném 429 luôn, **không gửi thêm** (process() chờ ≥ 10 s như cũ); lỗi khác → đúng 1 GET thường về URL gốc (không hedge, không mở sẵn) | `test_predicted_host_429_raises_without_any_extra_request` |
| R2 | 403/429 về MUỘN từ bên thua hedge bị bỏ qua → không cấm, không quên → mọi lượt sau lại gửi tới máy chủ đang giới hạn | cao | Ghi nhận lỗi ngay tại luồng nhận lỗi (`_hedge_error`): replica 403/404/410/429 → cấm 10 phút; request chính (đoán) lỗi → báo đoán sai; 429 → tạm dừng suy đoán | `test_late_429_from_losing_replica_bans_it_and_pauses_speculation`, `test_late_failure_of_predicted_host_is_reported_and_blocked_even_if_hedge_rescued` |
| R4 | Link client khác (sau 403) vẫn chạy hedge/đoán/mở sẵn và lặp lại ở MỌI lần thử (≈ 33 request bị 403 thay vì 10) | trung bình | Link client khác = GET thường; lần thử sau một lỗi 403/410/429 có `no_alt`/`no_spec` → mỗi lần thử đúng 1 GET như cũ | `test_alt_urls_are_plain_gets_and_skipped_on_the_next_attempt` |
| R5 | Range: mỗi đoạn tự đoán 302 → đoán sai/429 nhân n lần, mỗi đoạn lại quay về ngay | trung bình | Đoán 1 lần cho cả file; đoạn đầu gặp lỗi → các đoạn sau dùng URL gốc; 429 → ném luôn | `test_range_prediction_once_per_file_and_429_is_not_retried` |
| R6 | Mở sẵn `mn=` chạy ở MỖI đoạn Range, kiểm tra kết nối rảnh bị đua → 24 kết nối thừa/DASH | trung bình | Mở sẵn 1 lần/file + tập "đang mở" có khoá (không mở trùng) | `test_range_mn_preconnect_runs_once_per_file_not_per_part` |
| R7 | Quên cặp đoán sai nhưng request về URL gốc dạy lại đúng cặp đó → lần nào cũng đoán sai lại | trung bình | Đoán sai → chặn đoán cho máy chủ + cụm đó 10 phút (`_RDR_BLOCK`) | `test_redirect_memory_skips_302_and_falls_back_on_wrong_guess` (phần cuối) |
| R8 | Mở sẵn vẫn chạy ở `net_mode: legacy` (kết nối mở sẵn bị urllib3 gốc vứt đi → chỉ tốn bắt tay) | thấp | Chỉ mở sẵn ở lớp mạng `fast` | `test_mn_preconnect_is_skipped_in_legacy_net_mode` |
| R1 | Khúc 1 MiB làm "byte đầu" = 1 MiB đầu → TTFB của v2 trông tệ hơn ~400 ms chỉ vì cách đo | thấp | Khúc đầu vẫn 256 KB (đo giống bản cũ), sau đó 1 MiB | `test_v2_ttfb_first_read_is_256k_like_old_path` |
| R9 | Thiếu biến thể `warm_all_auto`; range auto bị xét với ngưỡng 50 ms thay vì 100 ms | trung bình | Thêm `warm_all_auto` (warm_all + range auto, không cờ v2) + ngưỡng 100 ms; kết luận range auto = cả warm_all_auto và v2_full_auto | `test_summary_clear_win_and_range_auto_needs_100ms_from_warm_all_auto`, `test_bench_variants_cover_spec_and_warm_all_auto_has_no_v2_flags` |
| R10 | 20 lượt + đuôi chậm 10% → P90 nhảy ~1,3 s theo số lượt rơi vào đuôi; dung sai 25 ms làm kết luận lật ngược | trung bình | Khoảng tin cậy 90% bootstrap cho Δ median/Δ P90; kết luận 3 mức BẬT / GIỮ TẮT / **CHƯA KẾT LUẬN**; thêm cột "lượt > 1 s" | `test_summary_marks_p90_flip_from_tail_count_as_inconclusive_not_off` |
| R11 | Hedge cứu được thì đoán sai vẫn ghi là "đoán đúng"; Range không báo đoán sai | thấp | `redirect_skip_fail` ghi cả khi hedge cứu; cột bench ưu tiên `FAIL:`; Range ghi đoán sai | như R2 + R5 |

Thêm (không có trong yêu cầu, chỉ làm cho an toàn hơn): **bất kỳ 429 nào từ googlevideo → 10 phút không gửi request suy đoán nào**
(đoán 302, hedge, mở sẵn). Cờ tắt thì biến này không có tác dụng gì.

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

⟨PYTEST_SECTION⟩

## 7. Giới hạn — điều CHƯA chứng minh được
- **Không có số YouTube thật.** Mọi số ở mục 2 là mock trên loopback; độ lớn trên máy thật sẽ khác (Google có thể không 302, có thể không chậm đuôi như mô phỏng).
- **#1 trên Windows**: lợi ích dự kiến đến từ việc không duyệt kho chứng chỉ Windows cho mỗi kết nối — Linux không có chi phí đó (mục 2 cho thấy gần bằng nhau). Chỉ đo được bằng `BENCH_TAI_360.bat`.
- **TLS resumption (`tls_resumed`)**: Python chỉ resume phiên TLS khi được đưa session cũ; urllib3 không làm việc đó → cột `tls_resumed` gần như luôn `0`. Với TLS 1.3, resume cũng không bớt được vòng mạng nào (chỉ bớt tính toán), nên KHÔNG kỳ vọng lợi ích từ đây.
- **`SO_RCVBUF` 4 MiB**: đặt cố định thì hệ điều hành thôi tự co giãn bộ đệm nhận (Linux còn giới hạn theo `net.core.rmem_max`; trên mock loopback không có độ trễ/băng thông thật nên không đo được tác dụng); ảnh hưởng thật chỉ đo được trên máy bạn (Windows) → nếu bench `v2_ssl` không thắng, đặt `net_rcvbuf_mb: 0`.
- **Range + hedge**: yêu cầu ghi "đoạn đầu tiên dùng chính kết nối đang hedge" — bản này KHÔNG hedge từng đoạn Range (giữ số socket ≤ n). Range auto mặc định tắt; chỉ bật khi `BENCH_DL_SUMMARY.md` ghi BẬT cho cả `warm_all_auto` và `v2_full_auto`.
- **Mục 6 (range auto)** không đổi mặc định — đúng yêu cầu.
- **Hedge bắn kép rất thường xuyên với 120 ms**: trên mock, chờ máy chủ cơ bản 150 ms > 120 ms nên **20/20 lượt đều gửi thêm 1 request**
  (đúng giới hạn "tối đa 1 request kép/file", nhưng tăng gấp đôi số GET tới googlevideo). Trên máy thật, nếu cột "chờ máy chủ tải" trong
  `BENCH_DL.md` thường > 120 ms thì hedge cũng sẽ bắn gần như mọi video. Muốn chỉ hedge phần đuôi chậm: đặt `dl_hedge_delay_ms` ≈ P75–P90
  của "chờ máy chủ tải" (vd 300–400 ms) — đổi số này không cần sửa code.
