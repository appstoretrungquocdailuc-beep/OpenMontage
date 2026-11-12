# AUDIT NÚT THẮT UPLOAD TIKTOK — watcher nút Post (TAM v5.0.0 → v5.1.0)

Ngày 2026-11-12. Câu hỏi: watcher EVENT + CDP theo dõi nút Post có làm chậm việc TikTok upload và bật nút Post không?

## 1. Kết luận (dựa trên bằng chứng)

- **Trong phòng thử: KHÔNG.** Trên Chromium thật, chạy đúng mã TAM, 108 lượt (3 kịch bản × 2 dung lượng × 3 chế độ × 6 vòng, thứ tự xoay vòng). Thời gian thật tới lúc nút Post BẬT, thời gian truyền dữ liệu, độ trễ giữa các khúc upload và CPU Chrome của A (watcher hiện tại), B, C **không khác nhau có ý nghĩa thống kê**. Mọi khoảng tin cậy 95% của chênh lệch đều chứa 0 (bảng 4).
- Watcher A thêm < 1% thời gian chạy script trong trang (ví dụ kịch bản bị che: ~20 ms trên ~2,4 s) và 0 long task. Không đủ để làm chậm upload.
- A phát hiện nút sáng NHANH NHẤT (5–22 ms sau khi bật); C chậm hơn A ~10–15 ms; B chậm hơn 80–230 ms (vì đọc theo chu kỳ 250 ms).
- Vì CHƯA chứng minh được watcher làm chậm, **mặc định GIỮ NGUYÊN A** (không đổi cơ chế đang chạy tốt). B và C được thêm làm lựa chọn + công cụ đo trên TikTok thật.
- Dao động trong log của bạn (9,11 MB → 4,015 s; 2,67 MB → 4,418 s; 2,58 MB → 9,635 s) KHÔNG tỉ lệ với dung lượng: file nhỏ nhất lại chậm nhất. Watcher chạy giống hệt cả 3 lần, nên đây là dấu hiệu dao động phía TikTok/mạng (truyền + xử lý), không phải watcher. Đây là SUY LUẬN — phải xác nhận bằng `BENCH_WATCH_TIKTOK.bat` trên máy bạn.

**Giới hạn:** trang thử là trang GIẢ mô phỏng TikTok, chưa phải TikTok thật. TikTok thật có thể nặng hơn (giải mã video xem trước, tạo ảnh bìa…). Kết luận cuối cùng chỉ có sau khi chạy A/B/C trên TikTok thật (mục 6).

## 2. Audit cơ chế theo dõi trong TAM v5.0.0 (chế độ A — event)

| # | Cơ chế | Chạy khi nào | Chi phí tiềm năng | Đo được trong lab |
|---|---|---|---|---|
| 1 | `rootObs`: MutationObserver childList trên TOÀN trang (subtree) | suốt lúc upload | mỗi đợt thay đổi DOM gọi callback; nếu mất tham chiếu nút → `evaluate()` có getComputedStyle/getBoundingClientRect/elementFromPoint (ép layout) | ScriptDuration A ≈ C ≈ B ±10%; LayoutCount bằng nhau |
| 2 | `watchBtn`: observer thuộc tính trên nút + tới 40 tổ tiên (class/style/disabled/aria-disabled/hidden) | suốt lúc upload | tổ tiên đổi class/style → `evaluate()` | không thấy long task |
| 3 | `coverStart`: nút bị che → `evaluate()` lại mỗi **8 ms** | khi có lớp phủ che nút | 125 lần/s ép layout + hit-test | kịch bản 'bị che': +~20 ms script/2,4 s, CPU Chrome không khác có ý nghĩa |
| 4 | Python `execute_async_script` theo lát 400 ms | suốt lúc chờ | 1 lệnh WebDriver/400 ms | 9 lệnh (2,67 MB) / 16 lệnh (9,11 MB) mỗi lượt |
| 5 | `sync.sync(samples=3)` ngay sau gắn file | 1 lần | 3 lệnh WebDriver | không đáng kể |
| 6 | CDP: `Page.bringToFront`/`Emulation.setFocusEmulationEnabled` (mặc định TẮT), `Input.dispatchMouseEvent` (chỉ khi bấm) | ngoài lúc upload | không chạy trong lúc upload | — |
| 7 | Bộ ghi thụ động `TAM_REC` (PerformanceObserver resource/longtask) | mặc định TẮT | thấp | bật GIỐNG nhau cho mọi chế độ khi đo |

Không có vòng `while` Python nào gọi Selenium dày đặc trong chế độ A (polling 20 ms chỉ có ở chế độ `poll` cũ dùng để so sánh).

## 3. Ba chế độ đã cài vào TAM v5.1.0 (`TAM_WAIT_MODE`, hoặc `"tam_wait_mode"` trong config.json của WS360)

| Chế độ | Trong lúc upload | Khi nút Post bật |
|---|---|---|
| **A `event`** (mặc định, như cũ) | watcher đầy đủ (mục 2) | bấm ngay |
| **B `sparse`** | KHÔNG cài gì vào trang; mỗi 250 ms đọc 1 thuộc tính `disabled` của nút (querySelector, không layout) | cài watcher đầy đủ → kiểm tra hiển thị/che + bảo vệ lúc nhấn → bấm |
| **C `lite`** | 1 observer CHỈ trên nút Post + childList (không subtree) của 3 tổ tiên gần nhất; không ép layout | như B |

Cả 3 chế độ: CHỈ bấm khi nút bật + hiển thị + không bị che (cùng một hàm kiểm tra); giữ bảo vệ lúc nhấn, POST_CONFIRMED, nhật ký chống đăng trùng. Test: nút đã bật nhưng còn bị che 1,2 s → cả A, B, C đều KHÔNG bấm lúc bị che, bấm sau khi hết che, vẫn POST_CONFIRMED.

## 4. Benchmark trong phòng thử (Chromium thật, mã TAM thật, trang Upload giả)

Cách làm: `tests/lab/bench_watch_lab.py`. Trang giả upload chia khúc 512 KB bằng fetch trên main thread và cập nhật giao diện mỗi 16 ms. Máy chủ giả (process riêng) giới hạn 25 Mbps và 'xử lý' 1,2 s sau khúc cuối; trang tự hỏi trạng thái mỗi 300 ms rồi bật nút. Mỗi lượt nạp lại trang sạch; thứ tự A/B/C xoay vòng (ABC, BCA→đảo, CAB…). Mốc thời gian lấy từ trang và máy chủ, độc lập với watcher.

Cột: **Bật** = gắn file → nút Post BẬT thật (ms) · **Truyền** = máy chủ nhận khúc đầu → khúc cuối · **Hở** = tổng thời gian máy chủ rảnh chờ khúc tiếp (trễ phía trình duyệt) · **Xử lý** = upload xong → nút bật · **Phát hiện** = gắn file → Python nhận READY · **Trễ** = Phát hiện − Bật · CPU Chrome = giây CPU cả cây process trong cửa sổ gắn→READY · Lệnh = số lệnh WebDriver · Long = tổng long task ms · Script/Layout = CDP ScriptDuration ms / LayoutCount. Giá trị = median 6 lượt.

| Kịch bản | MB | Chế độ | Bật | Truyền | Hở | Xử lý | Phát hiện | Trễ | CPU Chrome s | Lệnh | Long | Script ms | Layout |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Nhẹ (60 node/16 ms) | 2.67 | A | 2328 | 1060 | 198 | 1220 | 2333 | 6 | 1.1 | 9 | 0 | 47.0 | 140 |
| Nhẹ (60 node/16 ms) | 2.67 | B | 2326 | 1057 | 196 | 1218 | 2558 | 232 | 1.1 | 13 | 0 | 43.6 | 140 |
| Nhẹ (60 node/16 ms) | 2.67 | C | 2324 | 1056 | 192 | 1222 | 2340 | 17 | 1.2 | 9 | 0 | 43.8 | 140 |
| Nhẹ (60 node/16 ms) | 9.11 | A | 4966 | 3699 | 759 | 1219 | 4972 | 5 | 2.4 | 16 | 0 | 106.2 | 298 |
| Nhẹ (60 node/16 ms) | 9.11 | B | 4966 | 3697 | 760 | 1218 | 5084 | 118 | 2.5 | 23 | 0 | 90.4 | 298 |
| Nhẹ (60 node/16 ms) | 9.11 | C | 4973 | 3705 | 764 | 1222 | 4986 | 15 | 2.4 | 16 | 0 | 94.5 | 299 |
| Nặng (800 node/16 ms + React thay node nút) | 2.67 | A | 2542 | 1118 | 255 | 1369 | 2560 | 20 | 3.4 | 9 | 0 | 413.5 | 134 |
| Nặng (800 node/16 ms + React thay node nút) | 2.67 | B | 2570 | 1108 | 240 | 1401 | 2660 | 116 | 3.5 | 13 | 0 | 380.1 | 132 |
| Nặng (800 node/16 ms + React thay node nút) | 2.67 | C | 2567 | 1118 | 255 | 1392 | 2600 | 36 | 3.4 | 9 | 0 | 381.9 | 130 |
| Nặng (800 node/16 ms + React thay node nút) | 9.11 | A | 5404 | 3918 | 974 | 1420 | 5426 | 22 | 7.2 | 16 | 0 | 854.3 | 270 |
| Nặng (800 node/16 ms + React thay node nút) | 9.11 | B | 5388 | 3935 | 987 | 1342 | 5490 | 101 | 7.2 | 24 | 0 | 778.6 | 274 |
| Nặng (800 node/16 ms + React thay node nút) | 9.11 | C | 5377 | 3920 | 973 | 1396 | 5414 | 36 | 7.2 | 16 | 0 | 800.3 | 273 |
| Bị che (lớp phủ che nút Post suốt lúc upload) | 2.67 | A | 2364 | 1070 | 204 | 1244 | 2374 | 9 | 2.1 | 9 | 0 | 173.2 | 141 |
| Bị che (lớp phủ che nút Post suốt lúc upload) | 2.67 | B | 2340 | 1065 | 203 | 1230 | 2574 | 228 | 1.9 | 13 | 0 | 148.6 | 140 |
| Bị che (lớp phủ che nút Post suốt lúc upload) | 2.67 | C | 2344 | 1062 | 200 | 1236 | 2368 | 22 | 2.0 | 9 | 0 | 153.0 | 140 |
| Bị che (lớp phủ che nút Post suốt lúc upload) | 9.11 | A | 5023 | 3742 | 794 | 1232 | 5032 | 10 | 4.3 | 16 | 0 | 368.1 | 301 |
| Bị che (lớp phủ che nút Post suốt lúc upload) | 9.11 | B | 5024 | 3739 | 794 | 1237 | 5125 | 102 | 4.4 | 23 | 0 | 349.4 | 300 |
| Bị che (lớp phủ che nút Post suốt lúc upload) | 9.11 | C | 5019 | 3724 | 783 | 1246 | 5040 | 20 | 4.3 | 16 | 0 | 330.6 | 300 |

### Chênh lệch so với A (median, khoảng tin cậy 95% bootstrap) — thời gian THẬT tới lúc nút Post bật

| Kịch bản | MB | B − A (ms) | C − A (ms) | Có ý nghĩa? |
|---|---|---|---|---|
| Nhẹ (60 node/16 ms) | 2.67 | -2 [-16; +6] | -3 [-18; +6] | không |
| Nhẹ (60 node/16 ms) | 9.11 | +0 [-26; +15] | +8 [-14; +23] | không |
| Nặng (800 node/16 ms + React thay node nút) | 2.67 | +28 [-76; +97] | +24 [-68; +104] | không |
| Nặng (800 node/16 ms + React thay node nút) | 9.11 | -15 [-163; +102] | -26 [-86; +98] | không |
| Bị che (lớp phủ che nút Post suốt lúc upload) | 2.67 | -25 [-40; +20] | -20 [-35; +6] | không |
| Bị che (lớp phủ che nút Post suốt lúc upload) | 9.11 | +0 [-24; +98] | -4 [-28; +12] | không |

Kiểm tra bấm thật trên trang GIẢ (không dry-run): A, B, C đều **POST_CONFIRMED** {"A": "CONFIRMED", "B": "CONFIRMED", "C": "CONFIRMED"}

Ghi chú: Python điều khiển Chromium qua Playwright (bộ chuyển `tests/lab/pw_adapter.py` cung cấp đúng các lệnh Selenium TAM dùng) vì máy thử không có chromedriver khớp phiên bản. Phần chạy TRONG TRANG (JS watcher, gắn file bằng DOM.setFileInputFiles) giống Selenium; chi phí mỗi lệnh phía Python thì khác, nên chỉ so SỐ LỆNH, không so ms phía Python với máy thật.

## 5. Quyết định

- Quy tắc 6 ("nếu chứng minh watcher làm chậm thì thay"): **chưa chứng minh** → không đổi mặc định. A vẫn là chế độ phát hiện nhanh nhất.
- Nếu đo trên TikTok thật (mục 6) mà C − A < 0 có ý nghĩa (C nhanh hơn dù phát hiện chậm hơn ~10–15 ms), đổi sang C bằng 1 dòng trong config.json của WS360: `"tam_wait_mode": "lite"` (hoặc `TAM_WAIT_MODE=lite` khi chạy TAM riêng).

## 6. Đo trên TikTok thật (cần làm trên máy bạn)

1. Tắt WS360/TAM đang dùng profile thử. Mở Genlogin.
2. Chép `TAM_PATCH/tam_INSTANT_POST.py` (v5.1.0) đè file TAM cũ (bản cũ đã lưu ở `backup/tam_INSTANT_POST_5.0.0_goc.py`), chép `TAM_PATCH/BENCH_WATCH_TIKTOK.bat` vào CÙNG thư mục TAM.
3. Nhấp đúp `BENCH_WATCH_TIKTOK.bat` → nhập Profile ID, kéo thả 1 video (vd file 2,58 MB đã chậm 9,6 s), 6 vòng.
   Mỗi lượt: nạp trang Upload sạch → gắn file → chờ nút Post sáng → DỪNG. **KHÔNG bấm Post, không đăng gì.**
4. Gửi file `tam_bench_watch_<ngày>.json` (cạnh file TAM). Bảng tóm tắt in ra: gắn→READY, CPU Chrome/Python/chromedriver, số lệnh WebDriver, long task, layout, và `C − A`, `B − A` kèm khoảng tin cậy 95%.
   Muốn tách 'truyền dữ liệu' và 'TikTok xử lý' trên TikTok thật: đặt `TAM_UPLOAD_URL_RE` (regex URL upload thấy trong `rec_res_top` của file JSON) rồi chạy lại.
