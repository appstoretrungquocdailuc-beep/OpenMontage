# TEST REPORT — WS360 + InnerTube BROWSE (từ V39) — 2026-11-13

Bộ test: **152/152 PASS** (gồm 15 test Browse mới + 1 test toàn tuyến Browse → TikTok chạy thử, chạy với file TAM thật).

## Đo trên dữ liệu THẬT của YouTube (máy thử gọi được `youtubei.googleapis.com`, cùng API `/youtubei/v1/browse`)
Payload V39 gửi cho 3 kênh công khai × 2 bề mặt = 6 phản hồi thật:

| Bề mặt | Kích thước | Phản hồi (qua proxy Mỹ) | Đọc JSON đầy đủ | Đọc nhanh (mới) | Giống hệt? |
|---|---|---|---|---|---|
| BROWSE_UPLOADS | 1 235–1 331 KB | 453–820 ms | 12,8–18,0 ms | 1,2–1,6 ms | ✅ 3/3 |
| BROWSE_VIDEOS_TAB | 446–466 KB | 180–206 ms | 4,1–4,9 ms | 0,6–1,0 ms | ✅ 3/3 |

- Parser V39 đọc đúng 12 video đầu ở cả 2 bề mặt. **Chữ tuổi có trong phản hồi thật** ("… • 3 hours ago", "… • 1 day ago") →
  xác minh độ mới thường không tốn request thêm.
- Phát hiện quan trọng: Uploads 2026 dùng `itemSectionRenderer.contents = [lockupViewModel…]` (100 video, ~1,3 MB); parser V39 phải
  quét cả cây (26–39 ms/lượt). Đã thêm đường nhanh cho hình dạng này + bộ đọc JSON thô (chỉ 12 khối đầu), đối chiếu với giải mã
  đầy đủ mỗi 50 lượt, lệch → tự tắt.
- Ảnh hưởng tới đường tải (yêu cầu "cấm làm chậm"): tải 9 MiB ở 11,4 MiB/s bằng `download_single` production, 8 lượt mỗi trường hợp:
  không Browse **786 ms** | Browse giải mã đầy đủ 16 lượt/s **786 ms** (1 lượt lệch +21 ms) | Browse đọc nhanh 16 lượt/s **786 ms**.
  → không làm chậm đo được; đọc nhanh giảm CPU Browse ~10 lần (~24% → ~2% một nhân ở 16 lượt/s).
- Player (để xác minh khi thiếu chữ tuổi) từ IP trung tâm dữ liệu của máy thử trả `LOGIN_REQUIRED` → không kiểm được ở đây;
  khi đó tool ghi UNVERIFIED (không tải). Kiểm trên VPS bằng `BROWSE_PROBE.bat`.

## Kiểm thử bắt buộc → bằng chứng (máy chủ InnerTube GIẢ đúng hình dạng JSON)
| Yêu cầu | KQ | Test |
|---|---|---|
| BROWSE ONLY phát hiện video mới, KHÔNG gọi Data API, không WebSub/ngrok | PASS | `test_browse_only_detects_new_video_without_data_api_or_websub` (tải 360p, ghi D, 0 request xác minh, 2 bề mặt → tải 1 lần) |
| API + BROWSE song song, nguồn nhanh hơn thắng | PASS | `test_api_plus_browse_race_single_download_and_duplicate_ignored[BROWSE/API]` |
| Video trùng không tải/đăng 2 lần | PASS | như trên (1 lần tải, 1 excel_job, 1 winner) + `test_browse_only_detection_flows_into_same_tiktok_pipeline_dry_run` (1 job TikTok) |
| Restart không nhận nhầm video cũ; video đăng lúc tắt vẫn bắt | PASS | `test_restart_keeps_baseline_catches_gap_video_and_never_takes_old_reappearing` |
| Video cũ hiện lại / đổi kệ không thành mới | PASS | như trên (REAPPEAR "3 years ago", SHAPE_CHANGE 6 ID) |
| Không xác minh được → không suy đoán | PASS | `test_unverifiable_candidates_are_never_treated_as_new` (không chữ tuổi + player không ngày / chỉ có ngày / sắp công chiếu → UNVERIFIED) |
| Lần chạy đầu: video vừa đăng trước START vẫn bắt nếu chữ tuổi chứng minh | PASS | `test_first_ever_start_takes_video_published_seconds_before_start_only_if_text_proves_it` |
| 403/429 → nghỉ đúng (kênh riêng vs chung), giảm RPS | PASS | `test_global_429_trips_breaker_and_lowers_rps_channel_403_isolated` |
| Không chồng request cùng kênh | PASS | `test_never_overlaps_requests_for_same_channel` (máy chủ chậm 0,5 s, nhịp 0,2 s → tối đa 1 request/kênh) |
| JSON đổi cấu trúc → báo lỗi, không im lặng | PASS | `test_json_structure_change_is_reported_not_silently_empty` |
| Bộ đọc nhanh = giải mã đầy đủ; lệch → tự tắt | PASS | `test_fast_raw_reader_equals_full_json_decode_including_escapes`, `test_fast_reader_mismatch_disables_itself` |
| Không đổi logic đăng TikTok, DRY-RUN không bấm Post | PASS | toàn bộ test TAM cũ + test Browse→TikTok chạy thử (nhật ký không có POSTING) |
| Không xung đột SQLite/Excel production | THIẾT KẾ | bản thử chạy ở THƯ MỤC MỚI (database/check.xlsx riêng); `--browse-probe` dùng database_PROBE riêng |
| Không in key/cookie | PASS | log không chứa `AIzaSy` (test BROWSE ONLY) |

## Benchmark ≥ 10 video thật — **CHƯA CÓ (0 video thật)**
Máy thử bị chặn `www.youtube.com`/`googlevideo.com`, không có video mới thật nào. Không tuyên bố Browse phát hiện dưới 1–3 s.
Cách lấy số thật: chạy bản thử (API + BROWSE) trên VPS song song bản cũ vài ngày → `BAO_CAO_BROWSE.bat` → BROWSE_REPORT.md
(thắng/thua, dẫn trước P50/P90/P95, bỏ sót, xác minh, 403/429, quota, thời gian tải/đăng theo nguồn thắng).

## Danh sách thay đổi mã nguồn
- **MỚI `ws360_browse.py`**: payload/parse V39 (+ chữ tuổi, đường nhanh hình dạng 2026, đọc JSON thô có đối chiếu), BrowseGuard V39,
  bộ lập lịch theo kênh (kênh lâu nhất chưa quét đi trước, RPS tổng, không chồng request, job treo > 6 s), cooldown kênh, watchdog
  dựng lại bộ lập lịch, baseline SQLite (`browse_seen`), xác minh 3 lớp (`browse_verify`), thống kê mỗi phút (`browse_stats`).
- `YOUTUBE_WEBSUB_INSTANT_360P.py`: 2 chế độ `API_BROWSE`, `BROWSE_ONLY` (chế độ cũ giữ nguyên); nguồn BROWSE dùng luật tuổi nghiêm
  như API; log `source=`, `duplicate_ignored=`; đếm thắng Browse; GUI (2 lựa chọn + "kèm WebSub" + 3 ô số liệu Browse);
  `--browse-report`, `--browse-probe`; 18 khoá cấu hình `browse_*`. Downloader, Excel, TAM/TikTok: **không đổi**.
- `ws360_api_scanner.py`: thêm `video_published()` (Data API videos.list, tính quota) — chỉ dùng xác minh ở API + BROWSE.
- BAT: `START_BROWSE_TEST.bat`, `START_BROWSE_ONLY_24_7.bat` (tự chạy lại), `START_API_BROWSE.bat`, `BROWSE_PROBE.bat`, `BAO_CAO_BROWSE.bat`.
- Sao lưu bản trước: `backup/YOUTUBE_WEBSUB_INSTANT_360P_truoc_browse.py`, `backup/ws360_api_scanner_truoc_browse.py`.
