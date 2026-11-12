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

## Điều tra POST sai chữ ký HMAC (14:43:36)

**Nguyên nhân tìm được trong code và gói giao:** gói zip trước có kèm `database.sqlite` rỗng. Giải nén chép đè làm mất
secret HMAC đã đăng ký với hub, nên tool tạo secret mới. Thuê bao cũ trên hub vẫn còn hiệu lực với secret CŨ, còn lần đăng ký
lại (14:40) chỉ có hiệu lực khi hub xác minh xong. Hub vì vậy vẫn ký push bằng secret cũ, và tool từ chối.
Khớp với log: 15/15 HTTP 202 nhưng chưa thấy dòng `✅ Hub XÁC MINH`.
Kết luận này chưa chứng minh được 100% từ phía máy chủ test: cần log mới, có ghi nguồn và chữ ký, từ máy bạn.

**Đã kiểm tra trong code:**

| Yêu cầu | Kết quả |
|---|---|
| Header | `X-Hub-Signature: <thuật toán>=<hex>`; hỗ trợ sha1 (hub Google dùng), sha256, sha384, sha512 |
| HMAC tính trên | **byte thô** của body đọc từ socket (cả kiểu chunked), không parse hay serialize lại. Test: đổi 1 byte khoảng trắng thì chữ ký cũ phải sai |
| So sánh | `hmac.compare_digest` |
| Secret khi subscribe và khi kiểm POST | cùng `self.secret` (từ `websub_secret.txt`). Test kiểm `hub.secret` gửi đi bằng secret dùng để kiểm |
| Secret có đổi khi khởi động lại | không (giữ trong `websub_secret.txt`). Nếu database cũ có secret khác thì secret đó được đưa vào `websub_secret_history.txt` |
| Thuê bao cũ dùng secret khác | nhận diện được: POST ký bằng secret cũ do chính tool cấp (trong 10 ngày) là chữ ký HỢP LỆ → nhận và đăng ký lại; chữ ký không khớp → từ chối, ghi lý do, đăng ký lại kênh |
| Nguồn POST | log ghi: qua tunnel/ngrok (X-Forwarded-For) / localhost (SELFTEST) / trực tiếp, kèm User-Agent |
| Không lộ bí mật | log chỉ có dấu vân tay secret (8 ký tự) và 6 ký tự đầu chữ ký. Test kiểm log không chứa secret và chữ ký đầy đủ |
| Không tắt HMAC | POST không có chữ ký, hoặc chữ ký không khớp secret nào của tool, đều bị từ chối |

**Test mới (đều PASS):** chữ ký đúng (sha1, sha256), sai, thiếu, lệch 1 byte, body chunked, tình huống ghi đè DB
(secret cũ → nhận + đăng ký lại), secret cũ quá 10 ngày → từ chối.

## POST thật FeedFetcher-Google bị từ chối (kênh UCAqQb0yK6XkD8WS_0CKNXCQ, video tTN8iaYHO98)

Từ máy chủ test KHÔNG thể tự kiểm chữ ký của request này: cần secret, và secret chỉ nằm trên máy bạn.
Bản này giúp xác định nguyên nhân NGAY TRÊN MÁY BẠN:
- Mỗi POST được gán 1 mã: VALID_SIGNATURE / VALID_OLD_SECRET / MISSING_SIGNATURE / UNKNOWN_CHANNEL / UNKNOWN_SUBSCRIPTION /
  INVALID_SIGNATURE. Mã được ghi vào log + hub_events, kèm nguồn, User-Agent, thuật toán, 6 ký tự đầu chữ ký và số byte body.
- POST bị từ chối được lưu nguyên vẹn → `KIEM_TRA_POST.bat` chạy lại HMAC-SHA1 trên đúng byte thô với secret hiện tại và
  từng secret cũ (`hmac.compare_digest`), rồi kết luận khớp cái nào.
- Nếu kênh đó KHÔNG thuộc 15 kênh cột K → mã UNKNOWN_CHANNEL: thuê bao còn sót (bản cũ / Excel cũ / tool khác dùng chung
  callback ngrok); tool tự gửi lệnh huỷ thuê bao. Nếu kênh có trong cột K → UNKNOWN_SUBSCRIPTION: hub còn giữ thuê bao
  với secret khác → tool tự đăng ký lại.
- `hub.secret` gửi khi subscribe luôn là secret trong `websub_secret.txt`. Dấu vân tay của nó được lưu theo từng kênh
  (`subs.secret_fp`), và lúc hub xác minh thì lưu thêm (`subs.verified_secret_fp`).

## DIAG: 15/15 REQUESTED, hub không gọi xác minh, cột hub chỉ hiện "Error"

**Vì sao chỉ hiện "Error" (lỗi code chẩn đoán, đã sửa):** `hub_details()` chỉ giữ những dòng chứa một số từ khoá
(state/verif/error…). Trang của hub có tiêu đề "Error", còn câu giải thích nằm ở dòng khác không chứa từ khoá nào,
nên bị lọc mất. Hàm cũng không ghi mã HTTP. Bản mới ghi: mã HTTP, toàn bộ thông điệp (đã xoá secret và chữ ký), thử cả
có và không có `hub.secret`, và lưu trang gốc (đã xoá bí mật) vào `logs/hub_details/`.

**Yêu cầu đăng ký được ghi CHÍNH XÁC** vào `hub_events` cho từng lần gửi: hub_url, hub.mode, hub.topic
(`/xml/feeds/videos.xml?channel_id=…`), hub.callback, hub.verify=async, hub.lease_seconds, hub.secret
(chỉ ghi dấu vân tay và độ dài).

**Phân biệt rõ:** `subscribe` HTTP 202 (hub nhận yêu cầu) / `verify` (GET xác minh THẬT, chỉ tính khi đã trả đúng
hub.challenge) / `health-get` (GET không có challenge, ví dụ DIAG hay trình duyệt) / `denied` (hub từ chối) /
`verify` bị tool từ chối (kèm lý do) / `verify-probe` (đăng ký thử của --hubtest, không đụng dữ liệu thật).

**Dữ liệu thật của bạn cho thấy gì:**
- Bảng request của ngrok có GET /websub lúc 13:39–13:40 (hub xác minh thuê bao lúc đó, secret cũ). Sau đó **không có GET
  nào**, dù có 7–10 lần gửi 202 cho mỗi kênh.
- Hai POST lúc 14:43:36 và 15:00:28 bị từ chối vì ký bằng secret cũ, đã mất khi `database.sqlite` bị ghi đè.
- Nghĩa là các thuê bao CŨ vẫn hoạt động. Hub nhận yêu cầu mới (202) nhưng chưa gọi xác minh.

**Đã sửa:** callback giờ mang thế hệ secret (`?g=<dấu vân tay>`). Lần đăng ký tới là thuê bao MỚI hoàn toàn trên hub
(callback khác), nên hub phải xác minh như lần đầu, không còn là "đăng ký lại một thuê bao đã có".

**Chưa chứng minh được từ máy test** (không có mạng tới hub Google). Việc phân biệt các khả năng còn lại:
(1) hub không xác minh lại thuê bao đã có, (2) hub không gọi tới được callback, (3) hub đang trì hoãn,
được làm bằng **DIAG.bat → 3 (`--hubtest`)** trên máy bạn. Test với hub giả theo đặc tả: sync thành công thì 204;
hub không gọi tới được thì 409 kèm lý do, và báo cáo kết luận đúng.

## 860 ms không giải thích được (video IU5CqGkeK2k)

**Đối chiếu code:** total_ms = (nhận push → bắt đầu lấy link) + lấy link + tải (đã gồm TTFB) + **[tải xong → kết thúc]**.
Bản cũ chỉ đo ffprobe trong đoạn cuối. 5.4 + 516.2 + 796.9 + 53.2 = 1371.7, nên 860.1 ms nằm trong 3 thao tác chưa được
đo: `db.upd(state="VERIFYING")` (gồm chờ khoá), các bước kiểm tra, và `os.replace`.

**Không tái hiện được trên máy test.** Bench 10 lượt (Linux, máy chủ giả có TTFB 500 ms, 40 Mbps): db_verifying 0.1 ms,
rename 0.0–0.9 ms, các khoảng hở 0.0 ms, lệch tổng 0.0 ms. Các lần SELFTEST trên Windows trước đó (1352 ms, 1476 ms,
1980 ms) cũng không có khoảng hở: các bước cộng lại khớp tổng. Khoảng 860 ms chỉ xuất hiện ở lần chạy đó trên máy bạn.
Hai ứng viên: Windows Defender giữ thao tác đổi tên file .mp4 vừa ghi, hoặc SQLite chờ khoá/checkpoint.

**Đã làm:**
1. Đo riêng từng thao tác bằng perf_counter_ns và lưu vào database + log `⏱️ BƯỚC`: DB_VERIFYING_MS (kèm thời gian chờ
   khoá và TÊN LUỒNG đang giữ khoá), PRE_VERIFY_GAP_MS, FFPROBE_MS, RENAME_MS, POST_VERIFY_GAP_MS, và độ lệch tổng
   (`sum_check_ms` ≈ 0). Có test kiểm tổng khớp, và test giả lập luồng khác giữ khoá SQLite 400 ms: log ghi đúng tên luồng.
2. **Bỏ đổi tên theo yêu cầu** (`atomic_rename=false`): tải thẳng vào tên cuối. Toàn vẹn: DONE chỉ khi ffprobe đạt VÀ
   đúng dung lượng YouTube báo; tải thiếu → FAILED + xoá file; tắt giữa chừng → phát hiện file thiếu dung lượng và tải lại
   (đều có test). Bật lại đổi tên: `atomic_rename=true` (có thử lại khi Windows báo file đang bị giữ).
3. `--bench360` / BENCH360.bat: 10 lượt trên đúng đường tải thật, median/p90 từng bước.

**Số đo 10 lượt Shorts THẬT: PENDING.** Máy test không vào được YouTube. Chạy BENCH360.bat trên máy bạn.

## LIVE PUSH thật (log 16:43–17:18 trên máy bạn)

| Video | Push | Excel | Tải | TỔNG | Ghi chú |
|---|---|---|---|---|---|
| pj4F8q6H_A0 | VALID, tuổi 5.0 s | D17, 41 ms | itag 18, 2.34 MB | **1024 ms** | resolver 644, TTFB 209, tải 331, ffprobe 45; các khoảng hở = 0 |
| sLrFIdWkR0c | VALID, tuổi 4.9 s | D7, 41 ms | itag 18, 4.21 MB | **1566 ms** | TTFB 860 ms (chờ YouTube) |
| MY524yaPKUk | VALID, tuổi 7.3 s | D10, 46 ms | DASH 134+140 qua yt-dlp | **31 924 ms** | chưa có itag 18; DASH tải 1 kết nối bị bóp 0.8 Mbps → **đã sửa** |

- Khoảng 860 ms bất thường **không lặp lại**: DB 0.1 ms, đổi tên 0, các khoảng hở 0, lệch tổng ≈ 0.
- Các push `UNKNOWN_SUBSCRIPTION` "THẾ HỆ CŨ" là bản sao từ thuê bao cũ (secret đã mất), đúng như dự kiến. Bản gửi
  tới thuê bao mới đã được tải. Sau mỗi push như vậy, tool tự đăng ký lại kênh đó và hub xác minh trong < 2 s.
- **Sửa nhánh DASH:** tải hình và tiếng bằng 4 kết nối mỗi luồng (`dash_range_connections`), tự dò dung lượng khi yt-dlp
  không báo. Test với máy chủ giới hạn 300 KB/s mỗi kết nối: nhanh hơn rõ so với 1 kết nối. Nhánh itag 18 (1.0–1.5 s)
  **giữ nguyên**. Log DONE giờ ghi vì sao Innertube nhanh không dùng được (mỗi client: số format, có itag 18 hay không).

## Review đối kháng

Riêng các thay đổi WebSub đã qua 2 vòng review: vòng 3 xác nhận 13 lỗi, vòng 4 xác nhận thêm 3 lỗi do chính các bản sửa gây ra. **Đã sửa cả 16, mỗi lỗi có test hồi quy.**

## Kết quả test

| # | Bài test | Kết quả | Ghi chú |
|---|---|---|---|
| A | Bộ test tự động `tests/test_ws360.py` | **70/70 PASS**, chạy 3 lần liên tiếp đều qua | hub WebSub giả (đúng đặc tả), máy chủ video giả, Excel thật, ffmpeg thật |
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
| O | **LIVE PUSH: video mới THẬT qua WebSub thật** | **PASS trên máy bạn (12/11)** | 3 video thật: pj4F8q6H_A0 (push 5.0 s sau đăng, TỔNG 1024 ms, Excel D17 41 ms), sLrFIdWkR0c (1566 ms, Excel D7), MY524yaPKUk (31.9 s: nhánh DASH chậm, đã sửa bên dưới) |

**Phân biệt giả lập và thật:**
- Mọi sự kiện trong bộ test và trong TEST3 của SELFTEST là **giả lập**. Tiêu đề ghi `SELFTEST (giả lập, không phải video mới thật)`.
- Push thật hiện trong log dạng `🔥 [WEBSUB] VIDEO MỚI <id> | kênh <UC…>`, không có chữ SELFTEST, và nằm trong bảng `videos` với
  engine='WEBSUB' trong `database.sqlite`.
