# TEST REPORT — Tích hợp WS360 + TAM (đăng TikTok)

Cập nhật: 2026-11-12. Bộ test: `tests/` **87/87 PASS** (70 test WS360 cũ + 17 test tích hợp), chạy 2 lần liên tiếp.

**Cách test:** dùng **chính file TAM của bạn** (`tests/fixtures/tam_INSTANT_POST.py`, bản 5.0.0). JOURNAL, PostStatus,
UploadResult, PerfTrace, khoá profile và FSM đều là code thật. CHỈ 3 hàm chạm trình duyệt
(`setup_and_navigate_profile`, `upload_video_to_tiktok_ex`, `prepare_upload_page`) được thay bằng bản giả, vì máy test không
có Genlogin, Chrome hay tài khoản TikTok, và **không được bấm Post thật khi chưa có sự cho phép của bạn**.
Mọi số đo dưới đây là GIẢ LẬP, không phải tốc độ đăng thật.

## Hàm TAM được dùng lại (không sửa file TAM)

| Hàm / đối tượng TAM | Dùng để |
|---|---|
| `setup_and_navigate_profile` | mở profile Genlogin, gắn Selenium (debuggerAddress), vào trang Upload |
| `drivers`, `drivers_lock` | giữ driver đang mở (dùng chung với TAM) |
| `get_profile_lock` | khoá thực thi riêng từng profile |
| `upload_video_to_tiktok_ex` (+ `JobHooks`) | toàn bộ quy trình gắn file → POST_READY → bấm (CDP) → xác nhận bằng giao diện |
| `JOURNAL` (`PostJournal`) | POSTING trước khi bấm; CONFIRMED / UNKNOWN / FAILED_VISIBLE / NOT_POSTED sau; chặn đăng lại |
| `get_fsm`, `PState` | UNKNOWN chặn đăng tiếp cho tới khi người kiểm tra (`reset_unknown`) |
| `prepare_upload_page` | đưa trang về Upload sẵn sàng cho video kế tiếp |
| `PerfTrace` | mốc SEND_KEYS_START/END, UPLOAD_COMPLETED, PROCESSING_READY, POST_CLICKED, POST_CONFIRMED |

Tác dụng phụ khi import TAM: trên Windows, TAM tự đặt **HIGH priority cho cả tiến trình**. Khi chạy tích hợp,
tool đặt `TAM_HIGH_PRIORITY=0` trước khi import để không ảnh hưởng tới WebSub và bộ tải.

## Kiến trúc

```
WebSub POST (HMAC) → on_event → [hàng đợi tải] → tải .<id>.part.mp4 → ffprobe + dung lượng → os.replace → <id>_360p.mp4
                              └→ [hàng đợi Excel] → cột D                                       │ DOWNLOAD_DONE
                                                                                                 ▼
          SQLite upload_jobs (ghi BỀN VỮNG trước) → hàng đợi RIÊNG từng profile (deque + Condition, trong bộ nhớ)
                                                                                                 ▼
          worker của profile (luôn sẵn) → kiểm tra file + nhật ký TAM → khoá profile → upload_video_to_tiktok_ex
```

Cầu nối chạy CÙNG TIẾN TRÌNH: không qua mạng hay file, không khởi động Python mới, không mở lại profile đang READY.
Mọi mốc dùng chung `perf_counter_ns`.

## Danh sách kiểm thử (mục XI)

| # | Bài test | Kết quả | Test / bằng chứng |
|---|---|---|---|
| 1 | Ánh xạ cột C ↔ K | **PASS** | `test_mapping_C_to_K_rules` |
| 2 | PROFILE ID thiếu / sai / không mở được | **PASS** (giả lập) | thiếu/sai → HELD; Genlogin không mở → RETRY, không đăng nhầm |
| 3 | Nhiều kênh dùng chung 1 profile | **PASS** | e2e: 2 video cùng profile đăng tuần tự, tối đa 1 phiên |
| 4 | Một kênh nhiều dòng | **PASS** | cùng profile → 1 đích; khác profile → đăng cho tất cả (log rõ) |
| 5 | Mở profile bằng backend TAM gốc | **PASS** (giả lập) / **PENDING** (Genlogin thật) | gọi đúng `setup_and_navigate_profile`, ghi vào `tam.drivers` |
| 6 | Selenium kết nối đúng Chrome đã mở | **PENDING** | cần Genlogin thật: chạy TEST_TICH_HOP_KHONG_DANG.bat |
| 7 | Không mở trùng profile | **PASS** | 3 lệnh mở song song → mở đúng 1 lần |
| 8 | File `.part.mp4` không được upload | **PASS** | job trỏ tới .part → HELD; tải dùng `.<id>.part.mp4` rồi đổi tên |
| 9 | DOWNLOAD_DONE kích hoạt job đúng profile | **PASS** | e2e WebSub → tải → đúng profile theo cột C |
| 10 | Không đọc Excel / quét thư mục cho mỗi job | **PASS** | đếm số lần đọc ánh xạ và quét DOWNLOADS = 0 |
| 11 | Tải vẫn chạy khi uploader lỗi | **PASS** | TAM ném lỗi → video vẫn DONE + Excel DONE, job RETRY |
| 12 | Uploader khởi động lại không mất job | **PASS** | job QUEUED → khởi động lại → khôi phục → CONFIRMED |
| 13 | Một video không bị đăng trùng | **PASS** | đưa lại cùng video → không đăng lần 2; nhật ký UNKNOWN chặn đăng lại |
| 14 | Hai video cùng lúc cho cùng profile | **PASS** | tối đa 1 phiên / profile |
| 15 | Profile mất kết nối Selenium | **PASS** (giả lập) | phiên chết → mở lại có kiểm soát → CONFIRMED |
| 16 | TikTok không xác nhận → UNKNOWN | **PASS** (giả lập) | UNKNOWN giữ job; profile chặn video kế tiếp (WAIT_USER) tới khi bấm "Đã kiểm tra" |
| 17 | STOP trong lúc đang đăng | **PASS** | việc đang đăng làm xong (CONFIRMED), không nhận job mới, không đóng trình duyệt |
| 18 | Excel bị khoá | **PASS** | vẫn đăng TikTok; Excel chờ |
| 19 | WebSub HMAC hợp lệ / không hợp lệ | **PASS** | bộ test WS360 (đúng / sai / thiếu / thế hệ cũ) |
| 20 | ngrok + subscription tiếp tục chạy | **PASS** (log thật 12/11 trước khi tích hợp) / **PENDING** sau tích hợp | cần chạy thật |
| 21 | Không Feed Polling / Data API | **PASS** | `test_websub_only_architecture_no_polling_no_api` |
| 22 | SELFTEST không sửa dữ liệu production | **PASS** | so hash check.xlsx; dùng DB / thư mục riêng |
| + | FAILED_SAFE thử lại 1 lần, FAILED_VISIBLE không thử lại | **PASS** | |
| + | Chạy thử không bấm Post (dry-run) | **PASS** | DRY_RUN_DONE, nhật ký không có POSTING |
| + | Không đăng hàng loạt video cũ khi mới bật tích hợp | **PASS** | video tải trước khi bật → không tạo job |
| ★ | **Đăng TikTok THẬT (POST_CONFIRMED)** | **PENDING** | chưa được phép bấm Post thật; chạy TEST_TICH_HOP_KHONG_DANG.bat trước, sau đó START_INTEGRATED.bat |

## Độ trễ nội bộ DONE → gắn file (giả lập, 40 lượt, profile READY)

| Đoạn | p50 | p90 | max |
|---|---|---|---|
| DONE → vào hàng đợi | 0.004 ms | 0.006 ms | 0.008 ms |
| hàng đợi → worker nhận (gồm ghi SQLite) | 0.233 ms | 0.280 ms | 0.367 ms |
| worker nhận → bắt đầu gắn file | 0.298 ms | 0.370 ms | 0.697 ms |
| **DONE → bắt đầu gắn file** | **0.546 ms** | **0.662 ms** | **0.934 ms** |

Trên máy thật còn cộng thêm thời gian Selenium kiểm tra trang và tìm ô nhập file (TAM `ensure_upload_page`). Số này
đo được bằng TEST_TICH_HOP_KHONG_DANG.bat, xem dòng `DONE→gắn file … ms`.

## Chưa có trong lần này

- "File GUI uploader" mà bạn nhắc tới chưa được gửi. Tôi chỉ đọc được `tam_INSTANT_POST.py` (backend + GUI tích hợp bên
  trong). Nếu GUI riêng có logic quan trọng (ví dụ token Genlogin, chế độ CUM/TT theo profile), hãy gửi để tích hợp.
- Chế độ CUM (gom nhiều video vào 1 lần đăng) của TAM không dùng trong tích hợp: mỗi video đăng ngay khi xong, để nhanh nhất.
