# TEST REPORT — WS360-1.0 (chỉ WebSub)

Cập nhật: 2026-11-12.

## Kiến trúc (theo yêu cầu, không đổi)

```
WebSub POST hợp lệ (HMAC) → Video ID → hàng đợi tải → tải 360p ngay
                          └→ song song: hàng đợi Excel → ghi URL cột D (dòng có Channel ID ở cột K)
```

- **Đã gỡ bỏ hoàn toàn** feed polling, YouTube Data API và mọi cơ chế quét định kỳ. Bài test
  `test_websub_only_architecture_no_polling_no_api` kiểm tra điều này: trong code không còn hàm hay URL polling/API, và lúc chạy
  không có luồng nào tên FEED / API / POLL.
- Trang kênh chỉ còn được dùng trong **DIAG.bat** (chạy tay) để kiểm tra một Channel ID có tồn tại không, **không** dùng để phát hiện video.

## Kiểm tra topic và callback (yêu cầu 1–4)

| Mục | Kết quả |
|---|---|
| Topic đăng ký với hub | `https://www.youtube.com/xml/feeds/videos.xml?channel_id=<ID>`, **đúng** dạng `/xml/feeds/...` theo tài liệu YouTube. Từ đầu tool đã dùng dạng này, không phải sửa |
| Hub | `https://pubsubhubbub.appspot.com/subscribe` (hub chính thức của YouTube/Google) |
| Callback | URL HTTPS của ngrok, tool tự đọc từ `http://127.0.0.1:4040`, hoặc lấy `callback_url` trong config |
| Đăng ký lại cả 15 kênh | Tự làm khi mở tool, khi callback đổi và khi secret HMAC đổi |

## Kênh thứ 15 chưa xác minh (yêu cầu 5–6): đã sửa gì

Tool cũ có 4 chỗ khiến không thể biết, hoặc báo sai, vì sao một kênh chưa được xác minh:
1. Khi tool **từ chối** một lần hub gọi xác minh, nó không ghi log gì. Bản mới ghi mọi lần xác minh vào bảng `hub_events` và log
   `🚫 XÁC MINH BỊ TỪ CHỐI | kênh … | lý do …`.
2. Kênh chưa được xác minh phải chờ tới 10 phút mới được gửi lại. Bản mới gửi lại sau 2, 5, 10, 30, 60 phút và log từng lần.
3. SELFTEST tạo một **secret HMAC mới mỗi lần chạy**, rồi đăng ký lại cùng callback ngrok, nên ghi đè secret của tool chính trên hub.
   Push thật sau đó sẽ bị coi là sai chữ ký. Bản mới dùng một `websub_secret.txt` chung cho cả thư mục. Nếu phát hiện secret đã đổi,
   tool tự đăng ký lại toàn bộ.
4. Ô "đã xác minh" đếm cả kênh đã bị xoá khỏi cột K, còn SELFTEST đếm cả lần xác minh giả lập của chính nó, nên **có thể báo 15/15 sai**.
   Bản mới chỉ đếm kênh đang có trong Excel, đúng callback, còn lease, và (với SELFTEST) chỉ tính xác minh từ hub thật sau lần gửi.
   Kênh đã rời cột K được gửi lệnh huỷ đăng ký.

**Chưa xác định được nguyên nhân cụ thể của kênh 15 trên máy bạn.** Máy chủ test của tôi không truy cập được hub Google và ngrok.
Muốn biết chính xác, chạy **DIAG.bat → chọn 2** (tắt tool chính, bật ngrok). Báo cáo `DIAG_REPORT.md` ghi cho từng kênh:
Channel ID, mã HTTP lúc gửi đăng ký, số lần gửi, các lần hub gọi xác minh (chấp nhận hoặc từ chối, kèm lý do),
**trạng thái do chính hub Google báo** (State / Last verification error / Expiration), và trang kênh có tồn tại hay không.

## Review đối kháng

Riêng các thay đổi WebSub đã qua 2 vòng review: vòng 3 xác nhận 13 lỗi, vòng 4 xác nhận thêm 3 lỗi do chính các bản sửa gây ra. **Đã sửa cả 16, mỗi lỗi có test hồi quy.**

## Kết quả test

| # | Bài test | Kết quả | Ghi chú |
|---|---|---|---|
| A | Bộ test tự động `tests/test_ws360.py` | **48/48 PASS**, chạy 3 lần liên tiếp đều qua | hub WebSub giả (đúng đặc tả), máy chủ video giả, Excel thật, ffmpeg thật |
| B | Chỉ dùng WebSub, không polling/API | **PASS** | |
| C | Đăng ký → hub gọi xác minh → VERIFIED, lease 432000 s | **PASS** (hub giả) | hub thật: chạy SELFTEST / DIAG trên máy bạn |
| D | Kênh hub không xác minh: không bao giờ tính là đã xác minh, tự gửi lại theo lịch | **PASS** | |
| E | Xác minh giả mạo hoặc phát lại sau thời hạn bị từ chối và được ghi lý do | **PASS** | |
| F | Kênh bị xoá khỏi cột K lúc tool tắt → không được đếm, bị huỷ đăng ký | **PASS** | |
| G | POST đúng chữ ký → hàng đợi tải + hàng đợi Excel; POST sai chữ ký bị chặn và ghi nhật ký | **PASS** | |
| H | Tải 360p + ghi Excel song song (tải xong ngay cả khi Excel đang khoá) | **PASS** | |
| I | Hub gửi lại push muộn: video đăng ĐÚNG lúc tool tắt → vẫn tải; video đăng trước đó hoặc sửa video cũ → không tải | **PASS** | |
| I2 | Lần đọc Excel đầu bị lỗi (OneDrive/đang lưu) → KHÔNG huỷ đăng ký kênh nào | **PASS** | |
| J | Push THẬT tới đúng lúc SELFTEST đang giữ cổng → chuyển sang database của tool chính, tool chính tải khi mở lại (sửa video cũ / video đã biết thì không chuyển) | **PASS** | |
| K | SELFTEST khi tool chính đang chạy: không gửi gì lên hub | **PASS** | |
| L | SELFTEST không sửa `check.xlsx` gốc (so hash SHA-256) | **PASS** | |
| M | SELFTEST trên Windows của bạn với YouTube thật (bản trước, cùng bộ tải) | **PASS**: TỔNG **1352 ms**, tải 900 ms, verify 112 ms, Excel 34 ms | bộ tải 360p **không đổi** trong bản này |
| N | Hub THẬT xác minh đủ 15/15 | **PENDING** | chạy SELFTEST.bat hoặc DIAG.bat (chọn 2) với ngrok đang chạy |
| O | **LIVE PUSH: video mới THẬT qua WebSub thật** | **PENDING, CHƯA KIỂM CHỨNG** | chỉ xác minh được khi một trong 15 kênh đăng video lúc tool chính đang chạy |

**Phân biệt giả lập và thật:**
- Mọi sự kiện trong bộ test và trong TEST3 của SELFTEST là **giả lập**. Tiêu đề ghi `SELFTEST (giả lập, không phải video mới thật)`.
- Push thật hiện trong log dạng `🔥 [WEBSUB] VIDEO MỚI <id> | kênh <UC…>`, không có chữ SELFTEST, và nằm trong bảng `videos` với
  engine='WEBSUB' trong `database.sqlite`.
