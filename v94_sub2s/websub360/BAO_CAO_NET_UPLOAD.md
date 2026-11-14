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
- **Không ghi** cookie, header, body hay query. Body bị chặn ngay từ Chrome (`maxPostDataSize=0`). Chỉ ghi host và path đã che.
  Có test kiểm cookie giả và chữ ký giả trong URL đều không lọt vào log.
- **Chi phí của bộ đo (lab, 10 + 10 lượt):** +3,7 ms, KTC95% [−12; +12], p=0,73. Không phân biệt được với 0.
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
| base (như cũ) | 1173 / 1198 ms | 558 ms | 926 / 953 ms | 628 ms | 10/10 |
| **pre (mới)** | **875 / 886 ms** | **256 ms** | **634 / 657 ms** | 628 ms | **0/10** |

Chênh POST_READY = **−298 ms**, KTC95% [−310; −285], p=0,0002. Mức này đúng bằng độ trễ bắt tay đã mô phỏng. "TikTok xử lý"
không đổi, đúng như kỳ vọng.

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

## 5. Cần chạy trên VPS (DRY-RUN — không đăng gì)
1. `TAM_PATCH\BENCH_NET_TIKTOK.bat`: A/B base vs pre trên TikTok thật, 10 vòng.
   - Gửi `tam_bench_net_*.csv`, `*_summary.csv` và `.md`.
   - Chỉ giữ preconnect khi có kết luận "GIỮ preconnect". Nếu không, đặt `TAM_UPLOAD_PRECONNECT=0`.
2. `TAM_PATCH\PROXY_BENCH_TIKTOK.bat`: so 2 proxy, giờ có thêm upload mạng, TikTok xử lý và `*_summary.csv`.
3. `BAO_CAO_TAI_LAN_DAU.bat`: tách chặng tải YouTube lần đầu.
4. Log `🌐 [UPLOAD_NET]` của vài lần đăng thật (khi bạn cho phép đăng) để biết tỷ lệ upload và xử lý thật.

## 6. Khôi phục
- Bản ổn định trước vòng này:
  - `backup\tam_INSTANT_POST_5.4.0_fastclick_truoc_net.py`
  - `backup\ws360_tam_bridge_truoc_net.py`
  - `backup\YOUTUBE_WEBSUB_INSTANT_360P_truoc_net.py`
- Tắt từng phần, không cần chép file:
  - `TAM_UPLOAD_PRECONNECT=0` (tắt tối ưu);
  - `TAM_NET_TRACE=0` (tắt bộ đo).
