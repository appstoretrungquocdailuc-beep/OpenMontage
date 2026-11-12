# TEST REPORT — YOUTUBE_WEBSUB_INSTANT_360P (WS360-1.0)

Ngày: 2026-11-12. Môi trường test: máy chủ Linux trong cloud.

**Giới hạn quan trọng nhất của lần test này:** máy chủ test **không truy cập được YouTube, googlevideo, hub WebSub
(pubsubhubbub.appspot.com) và ngrok**: mọi kết nối tới các địa chỉ đó đều bị chặn ở tầng mạng. Vì vậy:
- Mọi bài test liên quan tới mạng thật được ghi **BLOCKED** hoặc **PENDING**, không có cái nào ghi PASS giả.
- Toàn bộ luồng còn lại đã chạy thật với **hub WebSub giả** (làm đúng đặc tả: trả 202, gọi lại xác minh, push có ký
  HMAC), **máy chủ video giả** (mock_gvs: hỗ trợ Range, 302, 403/429, Content-Range sai, cắt dở),
  **Excel thật** (openpyxl), **ffmpeg/ffprobe thật** và **giao diện PySide6 thật** (chế độ offscreen).
- Các sự kiện trong test đều là **giả lập**, bơm qua đúng webhook / event processor mà WebSub thật dùng.
  Chúng không phải video mới thật.

Để có số đo thật, chạy **SELFTEST.bat** và **BENCHMARK.bat** trên máy Windows của bạn. Kết quả sẽ ghi vào
`TEST_REPORT_MAY_BAN.md` và `BENCHMARK.csv`.

## 1. Danh sách PASS / FAIL / PENDING / BLOCKED

| # | Bài test | Kết quả | Ghi chú |
|---|---|---|---|
| T1 | Đọc Channel ID cột K (trùng nhiều dòng, giá trị sai, URL kênh, file gốc không đổi) | **PASS** (Excel mẫu) | Excel thật của bạn: **PENDING** (chạy SELFTEST.bat) |
| T2a | Webhook trả đúng `hub.challenge` | **PASS** | |
| T2b | Callback HTTPS công khai (ngrok/Cloudflare) | **BLOCKED** | máy test không có ngrok; tool tự đọc URL từ ngrok 127.0.0.1:4040 |
| T2c | Đăng ký hub THẬT + hub gọi xác minh + lease | **BLOCKED** | hub thật không truy cập được. Hub giả: 3/3 kênh xác minh, lease 432000 s |
| T2d | POST sai chữ ký bị chặn; POST đúng chữ ký nhưng là video cũ → nhận, không tải | **PASS** | |
| T2e | Xác minh giả mạo (tool chưa gửi yêu cầu) bị từ chối; lệnh huỷ cũ không cắt đăng ký mới; hub gửi lại xác minh vẫn OK; `denied` được ghi | **PASS** | |
| T2f | Thân push > 256 KB → 413; tối đa 32 kết nối; hạn tổng 15 s/request | **PASS** (413) | giới hạn 32 kết nối / 15 s: kiểm qua review code, chưa có test tải nặng |
| T3 | Sự kiện → chống trùng → hàng đợi tải → resolver → HTTP → (mux) → ffprobe → DONE, song song Excel → ghi → đọc lại ô | **PASS** (hub + video giả) | **Download xong TRONG KHI Excel đang khoá**: chứng minh tải không chờ Excel. Với YouTube thật: **BLOCKED** (chạy SELFTEST.bat --video ID) |
| T4 | Benchmark nhiều cấu hình | **PASS harness / mock** | số YouTube thật: **PENDING** (BENCHMARK.bat) |
| T5.1 | Video trùng; 3 nguồn × 5 lần cùng 1 video | **PASS** | 1 lần tải, 14 lần `dup` |
| T5.2 | Excel đang mở / bị khoá | **PASS** (file khoá `~$`) | ghi qua COM pywin32: **PENDING** (chỉ chạy được trên Windows có Excel) |
| T5.3 | Mất internet / webhook ngắt | **PASS một phần** | lỗi mạng → RETRY_WAIT có giới hạn; tự kết nối lại: kiểm qua review code |
| T5.4 | Download timeout / 403 / 429 / media chưa sẵn sàng | **PASS** | 403 → thử lại thành công; 429 tôn trọng Retry-After, chờ ≥10 s; NotReady 2 lần → DONE; quá giới hạn → FAILED |
| T5.5 | Video không có audio; không có progressive 360p (DASH + mux; DASH không audio → đóng gói lại MP4) | **PASS** | |
| T5.6 | Tắt đột ngột giữa lúc tải → mở lại | **PASS** | xoá `.part`, tải tiếp, ghi Excel tiếp, không tải trùng |
| T5.7 | Sai Channel ID; thêm/xoá kênh trong Excel | **PASS** | kênh mới → đăng ký; kênh xoá → huỷ đăng ký; ID sai → cảnh báo |
| T5.8 | Nhiều video gần như cùng lúc (12 video / 4 kênh) | **PASS** | ô D luôn là video mới nhất của kênh |
| T5.9 | Không ghi đè link mới bằng link cũ (kể cả sau khi khởi động lại) | **PASS** | |
| T5.10 | File tải cụt / hỏng không bao giờ DONE; file có sẵn hợp lệ không tải lại, hỏng thì tải lại | **PASS** | |
| T5.11 | Chạy 2 bản cùng thư mục; STOP trong lúc đang START | **PASS** | bản thứ 2 bị từ chối; dừng/khởi động lại sạch |
| T5.12 | Giao diện PySide6 dựng được | **PASS** (offscreen) | trên Windows thật: **PENDING** |
| T6 | Video mới THẬT qua WebSub thật | **PENDING** | chỉ xác minh được khi một kênh trong Excel đăng video lúc tool đang chạy |

Bộ test tự động: `tests/test_ws360.py` **34/34 PASS** (đã chạy 3 lần liên tiếp, không có bài nào chập chờn).
Lệnh chạy: `python -m pytest -q tests/test_ws360.py`.

## 2. Số liệu nghiệm thu

| # | Mục | Kết quả |
|---|---|---|
| 1 | Số kênh đọc từ Excel | Excel mẫu: 3 kênh / 3 dòng (+1 giá trị sai). **Excel thật: PENDING** |
| 2 | Số kênh WebSub được xác minh | hub giả: 3/3. **Hub thật: BLOCKED** |
| 3 | Số kênh lỗi | 0 (hub giả) |
| 4 | Số sự kiện giả lập thành công | 300 (đo độ trễ) + 200 (đo resolver start) + toàn bộ bộ test |
| 5 | Số push thật nhận được | **0, BLOCKED** |
| 6 | Số file 360p tải thành công | chỉ từ máy chủ giả; **từ YouTube thật: 0, BLOCKED** |
| 7 | Độ trễ nội bộ webhook nhận → vào hàng đợi (300 POST có chữ ký) | **p50 1.1 ms · p90 8.2 ms · p99 15.2 ms · max 16.1 ms** (mục tiêu < 100 ms: đạt) |
| 7b | Vào hàng đợi → worker bắt đầu resolver (200 sự kiện) | **p50 0.55 ms · p90 1.49 ms · p99 1.81 ms · max 8.2 ms** |
| 8 | Thời gian resolver | YouTube thật: **PENDING** (máy chủ giả mô phỏng 150 ms) |
| 9 | Thời gian download | máy chủ giả, xem `BENCHMARK_MOCK_LOCAL.csv`; **YouTube thật: PENDING** |
| 10 | Thời gian mux | test DASH: có đo (`mux_ms` > 0); YouTube thật: **PENDING** |
| 11 | Thời gian tổng | mock (giả lập TTFB 150 ms, 25 Mbps/kết nối, link 100 Mbps): file 1.5 MB 1 GET 811 ms, Range x8 628 ms; file 8.6 MB 1 GET 3108 ms, Range x4–x16 ~1.2 s |
| 12 | So sánh downloader cũ (V9) và mới | harness có cấu hình `v9_1get_cu` (điền `v9_path`); **PENDING**: cần mạng YouTube thật |
| 13 | Ghi Excel | ghi đúng cột D mọi dòng có kênh; các cột khác không đổi; sắp xếp lại dòng vẫn đúng; gom nhiều video vào 1 lần lưu |
| 14 | Chống trùng | PASS (RAM + SQLite, 3 nguồn) |
| 15 | Khôi phục lỗi | PASS (crash giữa chừng, 403/429/NotReady, Excel khoá, lỗi bất ngờ trước khi tải) |

**Lưu ý về benchmark giả lập:** máy chủ giả giới hạn 25 Mbps cho mỗi kết nối, nên Range nhiều kết nối thắng rõ.
YouTube thật có thể khác hẳn (ở V9 trước đây, 1 GET thường thắng với file 360p nhỏ). Vì vậy mặc định vẫn là
`range_connections: 1`. Chỉ đổi sau khi chạy BENCHMARK.bat trên mạng của bạn.

## 3. Review đối kháng (2 vòng)

- **Vòng 1:** 33 agent, xác nhận 25 lỗi, **đã sửa cả 25** và thêm test hồi quy. Hai lỗi nặng:
  1. Video vừa đăng ngay trước lúc khởi động bị đánh dấu "đã thấy" nên không tải.
  2. Khởi động lại có thể ghi link cũ đè link mới ở cột D.
- **Vòng 2** (chỉ soát các bản sửa): 14 agent, xác nhận 10 lỗi, **đã sửa cả 10**. Nặng nhất: Excel đang mở trong
  OneDrive / ổ mạng / 2 cửa sổ Excel thì có thể không ghi được cột D qua COM.
- Benchmark giả lập còn bắt thêm 1 lỗi do bản sửa gây ra: kết nối không dùng lại được sau lần dò Range. Đã sửa và có test.
- Không có tuyên bố "không còn lỗi". Các phần chưa chạy thật (COM với Excel thật, hub thật, YouTube thật)
  vẫn có thể còn lỗi chưa lộ.

## 4. Giới hạn đã biết

- Ghi bằng openpyxl giữ nguyên dữ liệu và định dạng ô, nhưng có thể làm mất hình ảnh / biểu đồ / form control
  nhúng trong file. File `.xlsm` được giữ macro (keep_vba). File `.xlsb` chỉ ghi được qua COM (Excel phải đang mở).
- Mỗi dòng chỉ có 1 ô D. Nếu một kênh ra nhiều video gần nhau, ô D giữ video **mới nhất theo giờ đăng**; mọi video vẫn được tải.
- Bắt video mới bị giới hạn bởi lúc YouTube phát tín hiệu. WebSub thật có thể trễ từ vài giây tới vài phút; API và
  feed chạy song song để bù.
- Resolver dùng cùng cách gọi Innertube với V9 (3 client chạy đua) và yt-dlp làm dự phòng. Không vượt DRM, không
  dùng cookie / tài khoản.
