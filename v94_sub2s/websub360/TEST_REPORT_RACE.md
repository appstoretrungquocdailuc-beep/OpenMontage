# TEST REPORT — WS360 RACE MODE (WebSub + YouTube Data API) + tên file theo tiêu đề

Ngày: 2026-11-12 | Bộ test: `tests/test_ws360.py` + `tests/test_integration_tam.py` + `tests/test_race_api.py`
Kết quả: **117/117 PASS** (gồm 8 test trình duyệt thật của `tests/test_watch_modes_lab.py`).

> Mọi số liệu trong test là **GIẢ LẬP** (máy chủ Data API giả, hub WebSub giả, máy chủ video giả). Chúng chứng minh
> logic đúng (chống trùng, quota, thử lại, tên file), **không** chứng minh nguồn nào nhanh hơn ngoài đời.
> **Chưa có kết quả thực tế WebSub ↔ API → KHÔNG kết luận API nhanh hơn hay chậm hơn WebSub.**

## Kiểm tra với Google thật (không cần key)
Gọi `activities.list` thật tới `www.googleapis.com` bằng 1 key giả, đúng cách tool gọi (key trong header
`X-goog-api-key`): Google trả `HTTP 400 API_KEY_INVALID`. Tool nhận đúng lỗi và loại key. Log chỉ ghi
`key#13485e`, không lộ key. Định dạng lỗi thật khớp máy chủ giả dùng trong test.

## Yêu cầu → bằng chứng

| Yêu cầu | Trạng thái | Test |
|---|---|---|
| 3 chế độ WEBSUB ONLY / API ONLY / PARALLEL (giao diện + config + `--mode`) | PASS | `test_mode_parsing_and_parallel_without_keys_falls_back_to_websub`, `test_websub_only_architecture_no_polling_no_api`, `test_gui_builds_offscreen` |
| PARALLEL: nguồn tới trước thắng, tải ngay | PASS | `test_parallel_race_one_download_one_excel_one_winner[WEBSUB]`, `[API]` |
| Chung SQLite + chống trùng nguyên tử: không tải / ghi Excel / đăng trùng | PASS | như trên (1 lần resolve, 1 lần tải, 1 excel_job); `test_simultaneous_signals_exactly_one_winner_per_video` (40 video × 2 nguồn cùng lúc → đúng 1 thắng/video) |
| Nguồn thắng nhưng link chưa sẵn sàng → nguồn còn lại kích thử lại có kiểm soát | PASS | `test_second_source_triggers_controlled_early_retry_when_link_not_ready`: lịch chờ 30 s bị thay bằng thử lại ngay, DONE < 5 s, tổng 2 lần thử, lần chờ cũ bị huỷ |
| API quét tiếp sau mỗi video, không mở trình duyệt, không thêm dòng Excel | PASS | `test_api_only_detects_every_video_and_keeps_scanning` (webbrowser.open bị chặn; max_row không đổi; chỉ cột D) |
| Bỏ hoạt động playlistItem; mốc khởi động dùng cùng bộ lọc tuổi WebSub | PASS | `test_api_only_…`, `test_api_baseline_fresh_video_uses_same_age_gate_as_websub` |
| Quota theo từng project, không bao giờ vượt; quotaExceeded/key sai | PASS | `test_quota_hard_cap_per_project_and_google_errors` (2 key cùng project dùng chung 6 đơn vị; lưu SQLite, khởi động lại giữ số) |
| Lỗi 403 của KÊNH không loại key | PASS | `test_channel_level_403_does_not_disable_key` |
| Reset quota 0h giờ Thái Bình Dương (có DST) | PASS | `test_quota_day_resets_at_pacific_midnight_with_dst` |
| Quét thích ứng, KHÔNG mặc định 0,2 s | PASS | `test_adaptive_plan_never_defaults_to_continuous_fast_polling` (15 kênh/1 project → 136 s), `test_adaptive_interval_from_real_quota_state` |
| Giờ vàng: vào khung là quét nhanh ngay | PASS | `test_hot_window_starts_fast_polling_immediately_even_after_long_cold_interval`, `test_hot_window_parsing_and_overlap` |
| SELFTEST/DIAG không tiêu quota API | PASS | `test_selftest_style_start_never_spends_api_quota` |
| Không in key ra log; key không nằm trong URL | PASS | `test_api_only_…`, `test_quota_hard_cap_…`, `test_parse_keys_projects_and_never_prints_key` |
| Tên file = tiêu đề GỐC `<tiêu đề>.mp4` (chỉ đổi ký tự Windows không cho lưu) | PASS | `test_original_title_kept_only_windows_impossible_chars_changed` (`母が決めた…超えたんだが….mp4`) |
| Title từ WebSub/API, không gọi API thêm; thiếu title → title resolver (miễn phí) | PASS | `test_resolver_title_used_only_when_detection_had_none`, `test_parallel_race_…` (tên theo nguồn thắng) |
| Chống trùng tên (2 video cùng tiêu đề, file lạ có sẵn không bị dùng lại/ghi đè) | PASS | `test_same_title_two_videos_and_foreign_file_on_disk_get_unique_names` |
| Dọn file tạm không xoá nhầm tiêu đề có chữ ".Part" | PASS | `test_restart_cleanup_keeps_titles_with_part_and_removes_only_temp_files` |
| Uploader nhận ĐÚNG đường dẫn file cuối đã xác minh | PASS | `test_title_named_file_reaches_uploader_exactly_and_attach_is_logged`, `test_websub_to_download_to_tiktok_end_to_end` |
| Log API_DETECTED / WEBSUB_DETECTED / WINNER / DETECTION_TO_DOWNLOAD_DONE / DOWNLOAD_DONE_TO_TIKTOK_ATTACH / API_QUOTA_USED | PASS | kiểm trong các test trên |
| Benchmark so sánh 2 nguồn trên cùng kênh | **PENDING (cần dữ liệu thật)** | công cụ: `BAO_CAO_RACE.bat` / `--race-report` → `RACE_BENCHMARK.md/.csv`; `test_race_report_pending_without_real_pairs` |

## Chưa kiểm chứng ngoài đời (PENDING)
- Tốc độ phát hiện thật của API so với WebSub: chạy PARALLEL vài ngày, rồi `BAO_CAO_RACE.bat`.
- Quota thật theo project của bạn (Google Cloud Console → APIs → YouTube Data API v3 → Quotas) so với bộ đếm của tool.
- Tên file tiếng Nhật dài + TikTok gắn file trên máy Windows thật (Genlogin + ChromeDriver).
