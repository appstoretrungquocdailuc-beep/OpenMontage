# Độ trễ bấm Post: WS360 + TikTok (TAM 5.4.0-TEST-FASTCLICK) — 2026-11-14

**Trạng thái: BẢN THỬ.** Chưa có số đo trên Windows VPS, nên **chưa tuyên bố thành công**. Số liệu dưới đây đo trên Chromium thật,
trang Upload GIẢ, trong máy thử Linux. Bước tiếp theo bắt buộc là chạy `BENCH_CLICK_TIKTOK.bat` trên VPS.

## 1. Đường đi thực tế của bản cũ (5.3.0), từ lúc nút Post bật tới lúc trình duyệt nhận click
| # | Bước | Ở đâu | Có thể chậm vì |
|---|---|---|---|
| 1 | MutationObserver thấy nút đổi thuộc tính, tính `readyAt` | trang | ~0 ms. Nếu nút bị che: kiểm tra lại bằng hẹn giờ 8 ms, mà tab ẩn thì Chrome giãn hẹn giờ |
| 2 | Trả promise cho Python đang chờ (`execute_async_script`, lát 0,4 s) | chromedriver | **khe hở giữa 2 lát**: nút sáng đúng lúc đó thì chờ lát sau (log cũ "watcher→Python≈247ms") |
| 3 | `mark_chrome`, `fsm.to`, `_finish_attach`, log READY, `before_click` (ghi nhật ký POSTING) | Python | < 1 ms (đo được 0,05–0,08 ms) |
| 4 | `mouseMoved` qua chromedriver, **chờ xác nhận** | chromedriver → Chrome | **tab không hiển thị: Chrome chỉ xác nhận khi vẽ khung kế tiếp → 136–546 ms (đo được)** |
| 5 | `mousePressed` rồi `mouseReleased`, mỗi lệnh chờ xác nhận | chromedriver → Chrome | 2–20 ms mỗi lệnh |
| 6 | Trang nhận mousedown (`clickAt`), tức điểm cuối của READY→CLICK | trang | |

Không có `sleep`, debounce (`TAM_READY_STABLE_MS` mặc định 0) hay retry trên đường này. Chỉ thử lại khi chắc chắn chuột
chưa chạm nút. Thứ làm chậm là **2 chỗ chờ ngầm**: khe hở giữa các lát (bước 2) và lệnh `mouseMoved` chờ khung hình (bước 4).

**Đối chiếu log của bạn:**
- READY in lúc 30.685, đã cộng độ trễ watcher→Python 247 ms, nên nút thật sự sẵn sàng khoảng 30.438.
- READY→CLICK = 735 ms, nên trang nhận nhấn khoảng 31.173.
- Từ lúc Python nhận kết quả tới lúc trang nhận nhấn là **≈ 490 ms**. Bước 3 tốn < 1 ms, nên gần như toàn bộ nằm ở các lệnh
  chuột qua chromedriver.
- Điều này khớp với hiện tượng `mouseMoved` chờ khung hình ở cửa sổ không hiển thị (VPS, RDP thu nhỏ/ngắt).

Đây là **giả thuyết mạnh**, cần xác nhận trên VPS. `--bench-click` in ms của **từng** lệnh CDP để kiểm chứng.

## 2. Sửa gì (chỉ đường watcher → click; Browse/downloader/Excel/SQLite/PREWARM_ALL không đổi)
- **TAM_CLICK_PATH=ws** (mặc định của bản thử):
  - Kết nối CDP **riêng** (WebSocket) tới đúng tab của driver: lấy targetId qua chính driver, mở trước khi gắn file.
  - Chờ nút bằng **một promise không cắt lát**, nên không còn khe hở.
  - Nút sáng: kiểm tra an toàn rồi gửi **liền** move + nhấn + thả, không chờ xác nhận `mouseMoved`.
  - Log và đo đạc ghi **sau** khi đã bấm.
- **Nút bị che:** kiểm tra lại theo **thay đổi DOM**, không phụ thuộc hẹn giờ có thể bị giãn.
- **Đo chi tiết mỗi lần bấm** (dòng `⏱️ [CLICK_PATH]`, kèm trường `cp_*` trong tam_perf_log.jsonl):
  - SÁNG→CLICK, thời gian chờ hết che, READY→CLICK (đồng hồ trang);
  - watcher→trả kết quả (`flush` = đang có người chờ, `immediate` = rơi vào khe hở);
  - kiểm tra an toàn, gửi lệnh, Chrome xác nhận nhấn, RTT CDP;
  - với đường driver: ms từng lệnh CDP.
- **Dự phòng:** không mở được WebSocket thì dùng đường cũ. Quay lại hẳn cách cũ: `"tam_click_path": "driver"`.

### Đánh giá "click ngay trong trình duyệt" (JavaScript tự gọi `button.click()`)
Không dùng. Sự kiện do JavaScript tạo có `isTrusted=false`:
- trang TikTok có thể bỏ qua hoặc đánh dấu tự động hoá;
- bảo vệ lúc nhấn của watcher cũng chỉ áp cho sự kiện thật.

Đường ws đạt ~2–3 ms từ READY tới lúc trang nhận nhấn **thật** (trusted). Nhanh hơn nữa không đáng đổi lấy rủi ro.

## 3. An toàn — có test trên Chromium thật (`tests/test_fast_click_lab.py`, 9/9 PASS; toàn bộ bộ test 167/167 PASS)
| Yêu cầu | Kết quả |
|---|---|
| Không bấm khi nút tắt / bị che | lớp che 900 ms: bấm **sau** khi hết che (≤ 150 ms), không bấm lúc che |
| Sai tab / sai tài liệu | WebSocket trỏ tab khác: **0 lệnh chuột qua ws**, dùng chromedriver bấm đúng tab, đăng 1 lần |
| Chống bấm đôi / đăng trùng | đúng 3 lệnh chuột/lượt; máy chủ giả nhận /post đúng 1 lần ở **mọi** lượt lab |
| Kết quả chỉ đọc sau khi Chrome xác nhận thả chuột | test hồi quy kiểm thứ tự lệnh |
| Trang không ghi nhận gì sau khi thả | coi như **đã bấm**, không bấm lại |
| DRY-RUN | **0** lệnh chuột, không postClick |
| Hard Stop / nhật ký chặn | 0 lệnh chuột |
| Rollback `driver` | vẫn đăng được |

**Lỗi tìm thấy trong lúc thử:**
- Bản ws đầu tiên gửi lệnh đọc kết quả **chung lô** với nhấn/thả. Lệnh đọc có thể chạy trong trang **trước** khi sự kiện chuột
  tới DOM, nên tool tưởng chưa trúng và bấm lại.
- Lab bắt được **3/10 lượt đăng 2 lần**.
- Đã sửa: chỉ đọc sau khi Chrome xác nhận thả, chờ tối đa 500 ms nếu trang chưa có dấu vết, không chắc thì không bấm lại.
- Chạy lại sau khi sửa: **0 lần trùng / 110 lượt** (90 lượt lab đầy đủ + 20 lượt visible riêng).

## 4. Benchmark lab — Chromium thật, trang giả, mã TAM thật, bấm thật vào trang giả
10 vòng mỗi ô, thứ tự xoay vòng. OLD = TAM 5.3.0 gốc, DRV = 5.4 đường driver, WS = 5.4 đường ws.
Thước đo do **trang tự ghi**: nút bật → hàm onclick của trang chạy (ms).

| Kịch bản | OLD P50 / P90 | DRV P50 / P90 | **WS P50 / P90** | CONFIRMED | đăng trùng | bấm lúc bị che |
|---|---|---|---|---|---|---|
| tab hiển thị | 25,5 / 29,4 | 27,0 / 29,0 | **7,2 / 8,9** | 30/30 | 0 | 0 |
| tab bị che (ẩn) | 138,1 / 150,7 | 151,3 / 160,3 | **6,8 / 10,0** | 30/30 | 0 | 0 |
| tab ẩn + nút bị che 600 ms: hết che → click | 559,8 / 565,8 | 556,0 / 564,7 | **3,1 / 3,6** | 30/30 | 0 | 0 |

Ở tab ẩn, riêng lệnh `mouseMoved` qua driver tốn **136 ms** (median); khi có lớp che là **546 ms**. Đường ws không phải chờ lệnh này.

**Giới hạn của lab:**
- Lab dùng Playwright thay chromedriver, nên đường cũ ở lab **nhanh hơn** trên máy thật (không có bước HTTP của chromedriver).
- Số thật của bạn (735 ms) phải được đo lại trên VPS.
- Lab chỉ chứng minh **cơ chế** và độ an toàn, không phải con số trên VPS.

## 5. Bước bắt buộc trên VPS (không đăng, không mở TikTok)
1. Giải nén `WS360_FAST_CLICK_TEST.zip` vào **thư mục mới**.
2. Chép `TAM_PATCH\tam_INSTANT_POST.py` và `BENCH_CLICK_TIKTOK.bat` vào thư mục TAM của **bản thử**, cạnh Genlogin.py
   và chromedriver.
3. Chạy `BENCH_CLICK_TIKTOK.bat`:
   - tool mở **tab mới**, nạp trang giả 127.0.0.1, xen kẽ driver/ws 20 vòng;
   - in P50/P90/P95, khoảng tin cậy 95 %, p-value, số lần đăng trùng và số lần bấm khi bị che.
4. Gửi `tam_bench_click_*.json` và `.md`.
5. Chỉ khi ws thắng có ý nghĩa thống kê, 0 lần trùng và 0 lần bấm khi bị che, mới thử đăng thật. Việc đăng thật cần bạn cho
   phép và chỉ tính thành công khi có POST_CONFIRMED.
