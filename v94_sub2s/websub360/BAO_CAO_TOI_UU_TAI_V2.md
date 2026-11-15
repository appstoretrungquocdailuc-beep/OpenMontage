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
