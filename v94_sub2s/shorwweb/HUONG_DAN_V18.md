# shorwweb v18 (fast360) — sửa từ `shorwweb_v17_sub1s_70mb.py`

> Máy thử ở đây **không vào được YouTube**. Mọi thay đổi đã chạy trên máy chủ YouTube giả (17 test đạt). Thời gian lấy link
> THẬT trên máy bạn: chạy `BENCH_LINK_V18.bat` (mục 4). Không tuyên bố "0,2 s cứng" khi chưa đo trên máy bạn.

## 1. Lỗi aria2c ("❌ Lỗi Tải: " trống) — đã tìm ra

- v17 đọc `stderr` của aria2c, nhưng **aria2c in lỗi ra `stdout`** (đã thử: `stderr` rỗng, `stdout` có
  `errorCode=22 … status=403`). Vì vậy log trống, không biết lỗi gì.
- Lỗi xảy ra ~0,1 s sau khi chạy aria2c → máy chủ từ chối ngay, không phải lỗi mạng. Nguyên nhân khả dĩ nhất: v17 **khoá cứng
  `android_vr`**, và theo chính cấu hình bên trong yt-dlp, link tải thẳng (HTTPS) của `android_vr` **bắt buộc có "GVS PO token"**
  (`GVS_PO_TOKEN_POLICY: https required=True`). Không có token → googlevideo trả **403**. `visionos` và `web_embedded` không cần
  token — bản V9.8 của bạn đang tải được bằng link `NATIVE_VISIONOS` cũng vì vậy.
- v18 in đúng lỗi: `[ARIA2C] ❌ Lỗi Tải sau 0.11s | exit=22 | HTTP 403 | errorCode=22 | …` (không in link có chữ ký).

## 2. Lấy link 1,1–1,3 s → mục tiêu ~0,1–0,25 s

v17 gọi `yt_dlp.extract_info` (tải trang, xử lý nhiều bước) → 1,1–1,3 s mỗi video. v18:
- Gọi **thẳng** `youtubei/v1/player` (giống WS360 và V9.8 của bạn — log thật V9.8: 0,11–0,19 s).
- **Kết nối TLS mở sẵn** lúc bật tool (3 kết nối) và **giữ nóng** (ping 25 s/lần) → lấy link chỉ còn 1 vòng mạng tới YouTube.
- 3 client **chạy đua**: `visionos`, `web_embedded`, `android_vr`. Client không cần PO token trả link trước là thắng, **không chờ** các
  client còn lại. `android_vr` chỉ dùng khi 2 client kia không có 360p (log ghi `⚠️ client cần PO token`).
- Thời gian lấy link = thời gian YouTube trả lời 1 request (vòng mạng + máy chủ xử lý), nên **không thể "cứng" 0,2 s** —
  phụ thuộc mạng. Log mỗi video ghi rõ từng client: `race: visionos=142ms web_embedded=158ms android_vr=171ms`.

## 3. Các thay đổi khác (giao diện giữ nguyên 100%)

| | v17 | v18 |
|---|---|---|
| Link bị 403/410 | thất bại | thử **link của client khác** trong cùng lượt (mỗi link 1 request) → yt-dlp dự phòng. 429 → không thử thêm |
| Không có aria2c / aria2c lỗi mạng | thất bại | tải bằng Python (8 Range song song, kiểm Content-Range + dung lượng, không để file dở) |
| Không có itag 18 | thất bại | tải video 360p + audio gốc song song → ghép stream-copy (không mã hoá lại), kiểm đủ 2 luồng |
| yt-dlp dự phòng | khoá `android_vr` | client mặc định của yt-dlp (tự xử lý PO token / chữ ký) |
| aria2c | đúng dung lượng không kiểm | kiểm đúng dung lượng; lỗi → xoá file dở + `.aria2` |

Cắt video, MOVE, Excel, Realtime monitor: **không đổi**.

## 4. Chạy

1. Chép `shorwweb_v18_fast360.py`, `RUN_V18.bat`, `BENCH_LINK_V18.bat` vào thư mục tool cũ (cạnh `aria2c.exe`, thư mục `ffmpeg\`).
   Bản v17 gốc vẫn giữ trong `backup\` (không sửa).
2. **Đo trước:** chạy `BENCH_LINK_V18.bat`, dán 1 URL (vd `https://www.youtube.com/watch?v=9tbnXFHVqNs`). Mỗi dòng in:
   `LẤY LINK … ms | thắng: visionos itag 18 | thử 1KB: HTTP 206 OK` (link tải được) hoặc `HTTP 403 BỊ TỪ CHỐI`,
   rồi 1 dòng so với cách v17: `v17 (yt-dlp android_vr…): … ms | thử 1KB: HTTP 403 BỊ TỪ CHỐI` (nếu đúng như phân tích ở mục 1).
3. Dùng tool: `RUN_V18.bat`. Log mỗi video:
   `⏱️ LẤY LINK [INNERTUBE_VISIONOS · itag 18]: 0.1xxs | race: …` → `[ARIA2C] ✅ HOÀN TẤT - …MB | …s | ≈…Mbps`.
4. Gửi lại vài dòng log (hoặc kết quả bench) nếu còn chậm / còn lỗi.

## 5. Kiểm thử (máy chủ giả)

`python -m pytest tests -q` → **17 passed**: chọn client sạch thắng không chờ client chậm; client cần PO token chờ thêm 0,15 s cho client
sạch; link mã hoá (cipher) không dùng; không có 18 → video+audio đúng track gốc; lỗi aria2c đọc đúng `HTTP 403`; 403 → link client khác
(đúng 1 request vào link bị từ chối); Python Range đúng từng byte, từ chối Content-Range sai; 403 không thử lại bằng Python; yt-dlp dự
phòng; nguyên luồng Worker tải → cắt 3 part; giao diện mở + mở sẵn kết nối; chế độ `--bench-link`. Cú pháp đã kiểm bằng **Python 3.11**.
