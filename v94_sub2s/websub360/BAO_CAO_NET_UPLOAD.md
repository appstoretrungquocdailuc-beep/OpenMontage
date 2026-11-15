# WS360 + TAM 5.5.0-TEST-NET — từ phát hiện tới TikTok xác nhận (hiện ≈ 6,365 s) — 2026-11-14

**Trạng thái: BẢN THỬ. CHƯA chạy trên Windows VPS / proxy Nhật / TikTok thật** (máy thử không có Genlogin, proxy, TikTok,
googlevideo). Số liệu dưới đây là **LAB** (Chromium thật, trang Upload giả, độ trễ proxy **mô phỏng**).
**Không tuyên bố đã giảm được 6,365 s.** Muốn có số thật, chạy các BAT ở mục 5 trên VPS.

## 1. Thời gian 6,365 s đi đâu (ước lượng từ các log bạn gửi trước đây — cần CSV thật để chắc)
| Chặng | Ước lượng | Nguồn |
|---|---|---|
| Phát hiện → tải xong 360p (resolver + CDN + ffprobe) | ≈ 1,45 s | log `DONE 1453 ms` |
| Tải xong → gắn file vào TikTok | ≈ 0,1–0,3 s | log cũ |
| **Gắn file → nút Post sáng (upload qua proxy + TikTok xử lý)** | **≈ 3,5 s** | log `Post ready 5002 ms` từ lúc phát hiện |
| Nút sáng → click (FASTCLICK ws) | ≈ 0,01 s | lab FASTCLICK |
| Click → TikTok xác nhận | ≈ 0,9 s | log `INSTANT CLICK` 31.423 → `CONFIRMED` 32.289 |

Chặng lớn nhất là **upload qua proxy + TikTok xử lý**. Trước bản này, tool không tách được hai phần đó. Bản này tách được bằng
CDP (mục 2).

## 2. Đã thêm — bộ đo (không đổi hành vi đăng)
- **TAM — NetRecorder:** kết nối CDP WebSocket riêng, luồng riêng, chạy ngoài đường bấm Post. Ghi lại:
  - lúc gắn file;
  - request upload đầu;
  - từng request: proxy / TCP / TLS / thời gian Chrome tự báo đã gửi / chờ phản hồi / xong hoặc lỗi;
  - phản hồi upload cuối;
  - nút Post sáng, click, phản hồi API đăng, xác nhận.
- **Phân biệt upload mạng và TikTok xử lý:**
  - Upload mạng = request upload đầu bắt đầu → phản hồi request upload cuối xong.
  - TikTok xử lý = phản hồi upload cuối → nút Post sáng.
  - **Gửi HẾT body = UNKNOWN.** CDP không cho biết lúc máy chủ nhận byte cuối; Chrome chỉ biết đã ghi vào socket hoặc tunnel
    proxy.
- **Không ghi** cookie, header, body hay query.
  - Body bị chặn ngay từ Chrome bằng `maxPostDataSize=1`.
  - Review phát hiện bản đầu dùng `0`; đã kiểm trên Chromium thật rằng `0` = không giới hạn, tức Chrome gửi body sang bộ đo.
    Body chưa bao giờ bị ghi ra log, nhưng đây là lỗi thật và đã sửa.
  - Các sự kiện mang Cookie bị bỏ ngay khi nhận.
  - Chỉ ghi host và path đã che.
  - Có test kiểm cookie giả, chữ ký giả trong URL và body JSON giả đều không lọt vào log hay vào bộ đo.
- **Chi phí của bộ đo (lab, 10 + 10 lượt):** −1,9 ms, KTC95% [−14; +9], p=0,43. Không phân biệt được với 0.
- `--bench-net` (DRY-RUN, A/B, CSV từng lượt + CSV tóm tắt median / P90 / tỷ lệ lỗi / POST_READY).
- Proxy bench thêm cột mạng và file `*_summary.csv`.
- WS360 `--first-dl-report`: số đo của các video MỚI tải lần đầu (resolver, CDN DNS/TCP/TLS/TTFB, truyền, ffprobe), đọc từ
  database thật, không dùng lần tải lại từ cache.

## 3. Đã tối ưu — mở sẵn kết nối upload TikTok (`TAM_UPLOAD_PRECONNECT=1`)
**Cách làm:**
- Lần upload đầu, tool học tên máy chủ upload thật (chỉ tên máy chủ).
- Từ lần sau, ngay khi WS360 **phát hiện** video (lúc còn đang tải YouTube khoảng 1–2 s), tool thêm `<link rel=preconnect>`
  vào trang Upload.
- Chrome mở sẵn CONNECT qua proxy + TCP + TLS tới máy chủ đó. Request upload đầu dùng lại kết nối đã mở, nên bỏ được một lần
  bắt tay qua proxy Nhật.
- Lệnh này đi qua kết nối WebSocket ngắn hạn, không qua chromedriver, nên không tranh lệnh với luồng đăng.

**Lab A/B:**
- Thiết lập: 10 vòng A-B-B-A, DRY-RUN.
- Máy chủ upload đặt sau bộ mô phỏng proxy ở xa: kết nối mới mất 300 ms, rảnh 1,5 s thì bị đóng.

| Nhánh | POST_READY P50 / P90 | upload mạng P50 | gắn → upload xong P50 / P90 | TikTok xử lý P50 | kết nối đầu MỚI |
|---|---|---|---|---|---|
| base (như cũ) | 1176 / 1194 ms | 556 ms | 941 / 990 ms | 624 ms | 10/10 |
| **pre (mới)** | **872 / 884 ms** | **255 ms** | **650 / 688 ms** | 624 ms | **0/10** |

Chênh POST_READY = **−305 ms**, KTC95% [−320; −290], p=0,0002. Mức này đúng bằng độ trễ bắt tay đã mô phỏng. "TikTok xử lý"
không đổi, đúng như kỳ vọng.

**Giới hạn thấy được trong lab:** preconnect mở sẵn **một** kết nối mỗi máy chủ. Nếu trang mở **song song** nhiều request tới cùng
máy chủ upload qua HTTP/1.1, chỉ request đầu được lợi. HTTP/2 dùng chung một kết nối nên không bị giới hạn này.

**Trên VPS** mức lợi phụ thuộc vào số vòng đi-về qua proxy Nhật tới máy chủ upload TikTok. Nếu kết nối có sẵn đã còn sống thì
lợi bằng 0. Cột "kết nối đầu MỚI" trong `--bench-net` cho biết điều này trực tiếp.

## 4. Đã xem nhưng KHÔNG đổi (và lý do)
- **Proxy Nhật — băng thông:** tool không tăng được băng thông proxy. Chỉ có cách đo A/B và chọn proxy nhanh hơn (proxy bench
  đã có CSV tóm tắt). Upload mạng tỷ lệ với dung lượng file chia băng thông proxy.
- **TikTok xử lý:** nằm phía máy chủ TikTok, tool không rút được. Bản này chỉ đo tách riêng ra.
- **Tải YouTube lần đầu:** đã có giữ kết nối nóng (NetWarmer), DNS prefetch và đo DNS/TCP/TLS/TTFB từ các bản trước. Không đổi
  thêm khi chưa có số thật (`BAO_CAO_TAI_LAN_DAU.bat`).
- **Đưa ffprobe ra khỏi đường găng:** lợi khoảng 40–150 ms nhưng phải đổi thứ tự "tải xong → kiểm file → giao TikTok". Đây là
  thay đổi thuật toán tải, cần số thật `FFPROBE_MS` trước khi làm.
- **Click → xác nhận (≈ 0,9 s):** bộ đo giờ ghi cả lúc API đăng phản hồi (`net_click_to_publish_resp_ms`). Nếu phần lớn 0,9 s
  là chờ giao diện, bản sau có thể xác nhận sớm hơn. Hiện chưa đổi vì chưa biết dạng phản hồi thật của TikTok.

**Kết luận thật thà:** từ 6,4 s xuống dưới 4 s cần cắt khoảng 2,4 s. Phần lớn nằm ở upload qua proxy và phía TikTok, tool không
điều khiển được. Preconnect chỉ cắt được phần bắt tay, vài trăm ms nếu proxy ở xa. Đạt dưới 4 s hay không phụ thuộc vào proxy
(bench A/B) và kích thước file. Phải đo trên VPS mới biết.

## 4b. Review đối kháng (4 góc nhìn × 3 người phản biện mỗi phát hiện) — 10 lỗi được xác nhận, đã sửa hết
1. `maxPostDataSize=0` = không giới hạn, nên body đi sang bộ đo. Đổi thành `1`. Có test trên Chromium thật kiểm cả hai giá trị.
2. Phần tổng hợp đọc danh sách request trong lúc luồng ghi vẫn đang chạy. Giờ dừng luồng trước, dùng khoá và bản chụp.
3. Mốc thời gian lấy từ tài liệu trang khác bị đổi giờ sai. Giờ kiểm `__tamDoc`, ưu tiên mốc Python, không khớp thì để UNKNOWN.
4. `performance.timeOrigin` trôi theo thời gian. Giờ đo `Date.now() − performance.now()` ngay lúc bắt đầu.
5. Không có số đo timing mà vẫn báo "dùng lại kết nối". Giờ báo UNKNOWN.
6. Lỗi `connectStart or -1` (giá trị 0 bị đếm sai).
7. Coi POST đầu tiên sau click là API đăng, trong khi thường đó là beacon. Giờ chỉ nhận request khớp `TAM_PUBLISH_URL_RE`, không
   khớp thì UNKNOWN.
8. Worker có sẵn trước khi bật đo bị bỏ sót. Giờ bật auto-attach sau khi luồng đọc đã chạy.
9. Ghi nhật ký bền vững bị chậm vì bộ đo chạy trước. Giờ nhật ký ghi trước, bộ đo chạy sau.
10. Kết nối WebSocket lỗi thì job phải chờ tới khoảng 8 s. Giờ timeout 1 s và bỏ đo trong 5 phút sau khi lỗi.

Ngoài ra đã sửa:
- Preflight CORS mở kết nối nhưng Chrome báo nó sau POST khoảng 1 ms. Đã nhận đúng là "MỚI (OPTIONS)", có test.
- Gợi ý nhận dạng upload giờ đọc trên URL gốc (chỉ tên tham số), sau đó bỏ URL gốc đi.
- File tạm riêng cho mỗi luồng khi ghi danh sách máy chủ.
- Gộp các lần làm nóng kết nối: tối đa 1 lần mỗi profile trong 2 s.
- `--bench-net` cảnh báo khi nhánh base không lạnh (so sánh không công bằng).

## 5. Cần chạy trên VPS (DRY-RUN — không đăng gì)
1. `TAM_PATCH\BENCH_NET_TIKTOK.bat`: A/B base vs pre trên TikTok thật, 10 vòng.
   - Gửi `tam_bench_net_*.csv`, `*_summary.csv` và `.md`.
   - Chỉ giữ preconnect khi có kết luận "GIỮ preconnect". Nếu không, đặt `TAM_UPLOAD_PRECONNECT=0`.
2. `TAM_PATCH\PROXY_BENCH_TIKTOK.bat`: so 2 proxy, giờ có thêm upload mạng, TikTok xử lý và `*_summary.csv`.
3. `BAO_CAO_TAI_LAN_DAU.bat`: tách chặng tải YouTube lần đầu.
4. Log `🌐 [UPLOAD_NET]` của vài lần đăng thật (khi bạn cho phép đăng) để biết tỷ lệ upload và xử lý thật.

## 5b. Bộ test
- Lab mới:
  - `tests/test_net_upload_lab.py` 9/9;
  - `tests/test_fast_click_lab.py` 9/9 (FASTCLICK giữ nguyên).
- Toàn bộ: **176/176 PASS**.

## 6. Khôi phục
- Bản ổn định trước vòng này:
  - `backup\tam_INSTANT_POST_5.4.0_fastclick_truoc_net.py`
  - `backup\ws360_tam_bridge_truoc_net.py`
  - `backup\YOUTUBE_WEBSUB_INSTANT_360P_truoc_net.py`
- Tắt từng phần, không cần chép file:
  - `TAM_UPLOAD_PRECONNECT=0` (tắt tối ưu);
  - `TAM_NET_TRACE=0` (tắt bộ đo).
