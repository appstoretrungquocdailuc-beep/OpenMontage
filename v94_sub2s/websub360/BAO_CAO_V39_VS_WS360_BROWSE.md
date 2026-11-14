# V39 gốc ↔ WS360 + BROWSE — vì sao "phát hiện muộn hơn"? — 2026-11-14

Phạm vi: CHỈ bộ Browse (`ws360_browse.py`, lệnh `--browse-compare`, 4 khoá cấu hình `browse_*`). Downloader, FFmpeg, SQLite
production, Excel, TAM/TikTok: **không sửa**. Chưa có video mới THẬT nào chạy qua bản này → **không tuyên bố cải thiện**.

## 1. Hai dòng log bạn gửi KHÔNG so sánh được với nhau
| | V39 | WS360 + BROWSE |
|---|---|---|
| Video | CHAiv0tP9z8 | sL016q-WV_w (**video khác**) |
| Giờ in ra | 04:04:11.950 | 11:04:11.112 |
| "Tuổi" | 2,953 s = đồng hồ Google lúc nhận − publishedAt (Data API) → **đo được** | "8 seconds ago" = chữ YouTube in trên dòng video → **không phải số đo** |

- Hai ID khác nhau → không có cùng một video để so sánh lệch thời gian.
- Phút:giây trùng khít (…04:11), lệch đúng 7 giờ. Nếu số của V39 lấy từ `scanner_ledger.jsonl` (giờ **UTC**, có chữ `Z`)
  còn của WS360 là giờ máy (Việt Nam **UTC+7**), thì hai dòng ở **cùng một thời điểm**. Khi đó WS360 thấy sL016q-WV_w
  khoảng 0,8 s **trước** lúc V39 thấy CHAiv0tP9z8. Đây chỉ là **giả thuyết**, cần kiểm lại nguồn của số 04:04:11.950.
- "8 seconds ago" là chữ YouTube làm tròn và có thể lấy từ dữ liệu đã cache. Không dùng nó làm tuổi, ở cả log lẫn kết luận
  (log mới ghi rõ `row_text='…' (chữ của YouTube, KHÔNG phải tuổi đo được)`).

## 2. So sánh bộ Browse V39 ↔ WS360
| Thành phần | V39 | WS360 (sau bản vá này) |
|---|---|---|
| Endpoint, payload, browseId `VLUU…`, tab Videos `EgZ2aWRlb3PyBgQKAjoA`, `hl=en` | ✓ | giống |
| Nhịp Uploads 0,75 s/kênh, tab Videos 3 s, RPS tổng ≤ 16 / ≤ 4, 8 + 4 luồng | ✓ | giống (vd 2 kênh → 2,67 RPS, khớp RPS 2.7 trong log của bạn) |
| Chọn kênh lâu nhất chưa quét, không chồng request cùng kênh, job treo 6 s | ✓ | giống |
| BrowseGuard 403/429 (kênh riêng 5→60 s; ≥ 2 kênh/5 s → nghỉ chung, RPS × 0,6) | ✓ | giống |
| Đọc key/clientVersion mới từ trang chủ (10 phút/lần) | ✓ | **thiếu → đã thêm** |
| HTTP 400/401 → đọc lại key/clientVersion ngay | ✓ | **thiếu → đã thêm** (≤ 1 lần/30 s) |
| Header `Pragma: no-cache` | ✓ | **thiếu → đã thêm** |
| Phát hiện theo TẬP ID (không phụ thuộc vị trí), 12 dòng đầu | ✓ | giống |
| Baseline lúc khởi động | Data API playlistItems + Browse | Browse lượt đầu + chữ tuổi (BROWSE ONLY không dùng Data API) |
| Xác minh tuổi | Data API publishedAt + đồng hồ Google (V39 gọi là AGE-GATE) | chữ tuổi trên dòng (0 request) → player → (API + BROWSE) Data API |
| Ứng viên chưa xác minh được ở lượt đầu | xếp hàng, xác minh lại (VERIFY_DEFER) | **trước đây: thành baseline, KHÔNG BAO GIỜ xét lại → đã sửa** |
| Bộ đọc JSON | json đầy đủ | đọc nhanh (bản 2026), lượt nào cũng kiểm chéo ID mới với json đầy đủ |
| HTTP | requests.Session / luồng | urllib3 pool riêng (giữ kết nối) |

## 3. Lỗi tìm thấy và đã vá (chỉ trong bộ Browse)
1. **Video có thể bị mất hẳn.** Nếu ở lượt đầu xuất hiện video chưa có chữ tuổi và player chưa có ngày đăng, nó bị ghi
   UNVERIFIED rồi vào baseline. Các lượt sau không bao giờ xét lại, kể cả khi chữ tuổi đã hiện. Bản vá:
   - Video ở trạng thái chờ được xét lại **mỗi lượt** bằng chữ tuổi (0 request).
   - Player được gọi lại sau 2 / 5 / 10 / 30 / 60 s.
   - Tối đa 15 phút (`browse_unverified_window_sec`), sau đó mới ghi `BROWSE_UNVERIFIED_FINAL`.
   - Video vẫn không tải khi chưa chứng minh được là mới.
2. **Bộ đọc nhanh chỉ được kiểm 50 lượt/lần.** Nay **mỗi lượt** đều so ID trong JSON thô với ID bộ đọc trả ra. Nếu có ID mới
   mà bộ đọc nhanh bỏ sót:
   - giải mã đầy đủ **ngay lượt đó**, nên video vẫn được bắt ở lượt đầu;
   - ghi `❗ BROWSE_FAST_MISS`;
   - tắt hẳn bộ đọc nhanh.
3. Thiếu đọc lại key/clientVersion và thiếu `Pragma: no-cache` so với V39 (xem bảng ở mục 2).

Đã kiểm trên 6 phản hồi THẬT của YouTube (3 kênh × 2 bề mặt):
- Bộ đọc ID thô tìm đúng 100 / 30 ID, mất 0,7–2,5 ms mỗi lượt.
- 12 dòng đầu luôn nằm trong tập đó.
- Bộ đọc nhanh cho kết quả giống hệt json đầy đủ.

## 4. Đo mới — để trả lời "muộn ở đâu" bằng số thật
- `logs/browse_trace.jsonl`, mỗi lượt Browse một dòng, gồm:
  - `seq`;
  - giờ **monotonic** gửi/nhận (ns) và giờ thật;
  - header `Date` của Google, HTTP status, ms, bytes, cách đọc;
  - `top` (ID đã đọc) và `new_in_json` (ID **lần đầu** có trong JSON HTTP 200 của kênh/bề mặt đó).

  Lượt đầu có ID trong `new_in_json` là thời điểm YouTube bắt đầu trả video đó. Các lượt trước không có ID nghĩa là YouTube
  chưa đưa video vào danh sách, không phải tool chậm.
- `⏱️ BROWSE_TIMELINE` (lưu ở bảng `browse_timeline`) gồm các trường sau:

  | Trường | Ý nghĩa |
  |---|---|
  | HTTP_RECEIVED_MS | thời gian phản hồi HTTP |
  | FIRST_JSON_VISIBLE | giờ, bề mặt, lượt # đầu tiên ID có trong JSON |
  | FIRST_PARSED | từ lúc đó đến khi bộ đọc trả ra ID (ms) |
  | FIRST_CANDIDATE | từ lúc đó đến khi ID thành ứng viên (ms) |
  | AGE_VERIFY_MS | thời gian xác minh tuổi |
  | DETECTED_TO_QUEUE_MS | từ phát hiện đến khi vào hàng đợi tải |
  | số lượt từ lúc có trong JSON | **0 = bắt ngay lượt đầu** |
- `🕒 BROWSE_AGE_AUDIT` chạy **nền sau khi đã vào hàng đợi tải**, nên không làm chậm phát hiện.
  - Giờ đăng chính xác lấy từ Data API (API + BROWSE) hoặc player (BROWSE ONLY).
  - Kèm đồng hồ Google (header Date).
  - Kết quả: tuổi thật lúc ID có trong JSON lần đầu và lúc vào hàng đợi.
- `SO_SANH_V39_BROWSE.bat` (`--browse-compare <thư mục V39>`) **chỉ đọc**:
  - đọc `scanner_ledger.jsonl*` của V39 (DISCOVERED, nguồn, pub, srv_first) và `browse_timeline` / `detections` của WS360;
  - ghi BROWSE_COMPARE.md: Δ từng video, P50/P90, video chỉ một bên thấy, tuổi đo của V39;
  - nếu có dưới 10 video cả hai cùng thấy thì ghi "CHƯA ĐỦ DỮ LIỆU".

## 5. Bằng chứng trên máy thử (máy chủ InnerTube GIẢ, cùng hình dạng JSON 2026) — KHÔNG phải số thật
- Video mới được bắt **ngay lượt đầu** có ID: `polls_between = 0`, FIRST_PARSED = +0 ms (bộ đọc nhanh).
- Các lượt trước trong trace không có ID đó.
- Khi giả lập bộ đọc nhanh bỏ sót: vẫn bắt ngay lượt đầu (`json(fast-miss)`) và bộ đọc nhanh tự tắt.
- Video thiếu chữ tuổi ở lượt đầu: không bị mất, được bắt ở lượt có chữ tuổi (log ghi "sau khi xét lại").
- Kiểm tra tuổi bằng giờ đăng chính xác cộng header Date: sai số đồng hồ ≤ 1 s.
- `--browse-compare` tính đúng Δ = +350 ms trên ledger V39 dựng sẵn.
- Bộ test Browse: **20/20 PASS**. Toàn bộ: **158/158 PASS**.

## 6. Cách so sánh thật trên VPS (cùng video, cùng kênh, cùng lúc)
1. **V39**, ở thư mục riêng (ví dụ `C:\V39_SO_SANH`):
   - dùng check.xlsx **riêng** (bản sao);
   - không có tool upload/TikTok nào theo dõi thư mục này;
   - **không** chép `websub_callback.txt` / `websub_secret.txt`. Nếu chép, V39 sẽ đăng ký lại URL ngrok với secret khác
     và làm hỏng HMAC của bản production.
2. **WS360**, ở `C:\WS360_BROWSE_TEST`:
   - chọn BROWSE ONLY (hoặc API + BROWSE, không kèm WebSub);
   - tick **Chạy thử (KHÔNG bấm Post)**;
   - dùng cùng danh sách kênh với V39.
3. Cho hai bản chạy cùng lúc. Nếu thấy 429, giảm `browse_max_rps` của WS360 vì hai tool dùng chung một IP.
4. Khi đã có từ 10 video mới trở lên: chạy `SO_SANH_V39_BROWSE.bat`, nhập `C:\V39_SO_SANH`, mở `BROWSE_COMPARE.md`.
   - Nếu WS360 vẫn muộn hơn: xem `polls_between` và `logs/browse_trace.jsonl` của video đó.
     - `polls_between > 0` nghĩa là chậm do xác minh, có log lý do.
     - `= 0` nhưng FIRST_JSON_VISIBLE muộn hơn nghĩa là JSON YouTube trả về muộn hơn (do mạng, nhịp quét hoặc cache phía
       YouTube), không phải do bộ đọc.

## 7. Bộ test
- Browse: 20/20 PASS.
- Toàn bộ: **158/158 PASS** (181 s), gồm test tải / Excel / TAM / TikTok cũ. Các phần đó không đổi.
