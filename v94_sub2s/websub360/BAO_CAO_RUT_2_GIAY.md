# RÚT 2 GIÂY (6047 ms → ~4000 ms) — kết quả 2026-11-13

## Trạng thái từng yêu cầu

| # | Yêu cầu | Kết quả | Bằng chứng |
|---|---|---|---|
| 1 | Giữ gắn file 13 ms + POST_CONFIRMED | **Giữ nguyên** — không đổi đường DOWNLOAD_DONE→ATTACH, bảo vệ lúc nhấn, nhật ký chống trùng | test e2e + 3 test trình duyệt "không bấm khi nút bị che, vẫn POST_CONFIRMED" |
| 2 | Downloader 1533 → ~1000 ms | Lớp mạng giữ kết nối ấm đã giao hôm qua (resolver ấm −330 ms trong lab). **Chưa có số đo trên máy bạn** | chạy `BENCH_TAI_360.bat` → `BENCH_DL.md` |
| 3 | event/lite/sparse + trạng thái trang → POST_READY ~2000 ms | Công cụ đo 20 vòng, median/P90/P95, thêm trạng thái trang (vừa nạp / mở sẵn lâu). **Sửa 1 lỗi làm chậm thật** (xem dưới). Chế độ mặc định giữ `event` tới khi có số đo TikTok thật | `BENCH_WATCH_TIKTOK.bat`, `BENCH_TOAN_TUYEN.bat` |
| 4 | Prewarm + READY, video tới lúc khởi động không mất | **Đã có + chứng minh bằng test** | test `video_during_profile_startup…` |
| 5 | API báo video cũ 19 giờ là VIDEO MỚI | **Đã sửa** + chặn đăng video cũ ở mọi đường | 3 test mới |
| 6 | PROFILE_WAIT_MS / PAGE_RELOAD_MS / FILE_ATTACH_MS / POST_READY_MS / POST_CONFIRMED_MS | **Đã có** (log + SQLite + báo cáo) | test e2e |
| 7 | Không sleep cố định, không refresh sau DOWNLOAD_DONE nếu không cần, không đăng sớm | **Sửa lỗi nạp trang 2 lần**; không thêm sleep nào vào đường đăng | test trình duyệt `page_reloaded = 0` |
| 8 | ≥ 20 lượt, median/P90/P95, rollback | Công cụ mặc định 20 vòng + P90/P95; bản production trước lưu ở `backup/*_truoc_rut_2s.py` | — |

**Mục tiêu ~4 giây: CHƯA chứng minh được.** Máy thử không vào được TikTok/googlevideo; mọi con số TikTok phải đo trên máy bạn.

## Lỗi đã sửa (có bằng chứng)

### A. API: video cũ 19 giờ thành "VIDEO MỚI" lúc khởi động
Nguyên nhân: lần quét mốc của API dùng chung luật của WebSub: video mới nếu ≤ 30 phút **hoặc đăng trong lúc tool tắt**.
Tool tắt ~19+ giờ → video 19 giờ nằm trong "lúc tool tắt" → bị coi là mới. Cửa sổ đó chỉ đúng cho WebSub (hub gửi lại sau sự cố).
Sửa: API (mốc và cả sau mốc) **chỉ theo tuổi ≤ `max_age_minutes`**. Video mới thật ≤ 30 phút vẫn bắt (test: video 5 phút được tải, video 19 giờ bị bỏ).

### B. Không có lớp chặn TikTok cho video cũ
Sửa: `tiktok_max_age_minutes` (mặc định 30). Video cũ hơn → job **HELD "video cũ"**, KHÔNG tự đăng — kiểm cả lúc nhận job và
lúc chạy job (job khôi phục sau khi tool tắt lâu cũng bị chặn). WebSub/Excel vẫn chạy như cũ (tải + ghi link), chỉ không tự đăng.

### C. Nạp lại trang Upload 2 lần sau lỗi / chạy thử (tăng PAGE_RELOAD sau DOWNLOAD_DONE)
Sau lần chạy thử hoặc lỗi an toàn, TAM để profile ở `ERROR`. Bridge đã nạp sẵn trang sạch, nhưng job sau thấy `ERROR` →
**nạp lại lần nữa sau DOWNLOAD_DONE** (thêm 1 lần tải trang vào đường nóng). Sửa: `prepare_upload_page` thấy ô nạp file → `ERROR→READY`.
`UNKNOWN` không bao giờ tự xoá. Bằng chứng: test trình duyệt thật — trang mở sẵn sau lượt chạy thử có `page_reloaded = 0`.
(Ảnh hưởng đăng thật: sau mỗi lần FAILED_SAFE, video kế tiếp không còn bị nạp trang thêm ~1–3 s.)

### D. Thông báo "sẵn sàng" đếm sai
Profile đã mở và đang đăng video chờ (`BUSY`) bị đếm là chưa sẵn sàng. Sửa + test.

## Profile khởi động (yêu cầu 4)
- Bấm START: WS360 bắt đầu theo dõi NGAY; profile mở song song (`open_concurrency`).
- Giao diện có ô **"Trạng thái profile"**: `ĐANG CHUẨN BỊ x/y` → `SẴN SÀNG TỐC ĐỘ TỐI ĐA (y/y)`; log `🚀 TikTok: SẴN SÀNG TỐC ĐỘ TỐI ĐA — y/y profile READY sau … ms`.
- Video tới trong lúc profile còn mở: job ghi SQLite (QUEUED) → worker chờ đúng lần mở đang chạy (không mở trùng) → đăng NGAY khi READY;
  `PROFILE_WAIT_MS` ghi lại thời gian chờ. Tool tắt giữa chừng → job khôi phục khi mở lại (nếu video chưa quá cũ).

## Log mới mỗi job
`⏱️ TIKTOK_STAGES <ID> <profile> CONFIRMED | PROFILE_WAIT_MS … | PAGE_RELOAD_MS … (nạp lại: không) | FILE_ATTACH_MS … | POST_READY_MS … | POST_CONFIRMED_MS … | chế độ event`

## Đo trên máy bạn (thứ tự khuyến nghị)
1. `BENCH_TAI_360.bat` — tải 360p (7 phương án mạng, ≥ 20 lượt mỗi phương án) → `BENCH_DL.md`.
2. `TAM_PATCH/BENCH_WATCH_TIKTOK.bat` — event/sparse/lite 20 vòng trên TikTok thật (+ tuỳ chọn trang mở sẵn 60 s) → `tam_bench_watch_*.json`.
3. `BENCH_TOAN_TUYEN.bat` — toàn tuyến chạy thử: tải → TikTok → POST_READY, 3 chế độ xoay vòng, ≥ 20 lượt → `BENCH_PIPELINE.md`.
4. Chạy thật vài ngày rồi `BAO_CAO_TOAN_TUYEN.bat` → `PIPELINE_REPORT.md` (gồm CLICK→POST_CONFIRMED thật, median/P90/P95).
Tất cả chạy thử: KHÔNG bấm Post. Gửi các file kết quả; tôi chỉ bật tối ưu nào thắng `legacy`/`event` có ý nghĩa thống kê.

## Rollback
- `config.json`: `"net_mode": "legacy"`, `"net_warm": false`, `"tam_wait_mode": ""`, `"tiktok_max_age_minutes": 0` (tắt chặn tuổi).
- Mã nguồn production trước hôm nay: `backup/YOUTUBE_WEBSUB_INSTANT_360P_truoc_rut_2s.py`, `backup/ws360_tam_bridge_truoc_rut_2s.py`,
  `backup/ws360_api_scanner_truoc_rut_2s.py`, `backup/tam_INSTANT_POST_5.1.0_truoc_rut_2s.py`.

Bộ test: 132/132 qua (gồm 9 test Chromium thật trên trang Upload giả).
