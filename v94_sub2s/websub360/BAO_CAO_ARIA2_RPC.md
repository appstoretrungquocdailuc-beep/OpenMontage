# ARIA2 RPC (THỬ NGHIỆM) trong WS360 — 2026-11-16

> Số trong mục 4 là **SỐ MOCK** (máy chủ video giả trên 127.0.0.1) — **KHÔNG phải YouTube**. Máy thử không tải được googlevideo.
> Chưa có cơ sở để nói "đã đạt < 1,5 s" — chỉ `BENCH_ARIA2_360.bat` trên máy bạn trả lời được.

## 1. Đã đọc chính xác 3 hàm của `360pok(1).py`

| Hàm | Làm gì (đúng theo code) |
|---|---|
| `_start_aria2_rpc_daemon` (dòng 1001) | Chạy **1 tiến trình aria2c cố định**: `--enable-rpc --rpc-listen-all=false` (chỉ 127.0.0.1), cổng trống ngẫu nhiên, `--rpc-secret`, `--file-allocation=none --disk-cache=64M --max-concurrent-downloads=16 --allow-overwrite`. Chờ `aria2.getVersion` tối đa 1,2 s; `atexit` → `aria2.shutdown`. Lợi ích: không tốn thời gian tạo tiến trình aria2c cho mỗi video. RPC qua `http.client` giữ kết nối (mỗi luồng 1 kết nối). |
| `_aria2_tuned_split` (dòng 1072) | ≤ 5 MiB → **8** kết nối; ≤ 15 MiB → **12**; lớn hơn → **16**. |
| `_download_aria2c_rpc` (dòng 1081) | `aria2.addUri` với `split = max-connection-per-server = N`, `min-split-size 1M`, `max-tries 2`, `retry-wait 0`, `connect-timeout 2`, `timeout 4`, `stream-piece-selector default`, `reuse-uri true`; hỏi `aria2.tellStatus` mỗi 15 ms; thành công = `complete` **và file ≥ min_bytes**; lỗi/hết giờ → `forceRemove` (+ chờ 20 ms) → `removeDownloadResult`; nếu RPC lỗi → `download_aria2c_ultimate` quay về aria2c tạo tiến trình 16 luồng. |

Log của bạn (video DÀI, 101,1 MB tải trong 2,91 s ≈ 290 Mbit/s với ≤ 16 kết nối) cho thấy: googlevideo giới hạn tốc độ **theo từng kết nối**, nhiều kết nối song song cộng dồn được băng thông.

## 2. Đã tích hợp vào WS360 (THỬ NGHIỆM, MẶC ĐỊNH TẮT)

- `ws360_aria2.py`: daemon + tải, giữ nguyên thiết kế 360pok (daemon cố định, `tuned_split` y hệt), **chỉnh cho an toàn**:
  - `max-tries = 1` (360pok: 2, `retry-wait 0`): 403/429 **không** bị aria2 thử lại ngay trên mọi kết nối — đo trên mock: đúng **1 request** bị từ chối.
  - Mã HTTP đọc từ `errorMessage` (`status=403/429`, `errorCode 3` = 404) → WS360 áp đúng quy tắc cũ: 429 → chờ ≥ 10 s + tạm dừng tối ưu 10 phút; 403/404/410/429 → **không** tải lại bằng đường khác trong cùng lần thử.
  - Thành công **chỉ khi đúng dung lượng `clen`** (360pok: ≥ min_bytes); sau đó WS360 vẫn kiểm MP4 + ffprobe như mọi đường tải. File sai/dở → xoá cả file lẫn `.aria2`.
  - Đo thêm: **TTFB** (addUri → byte đầu, hỏi mỗi 5 ms), **số kết nối THỰC TẾ** (lớn nhất của `connections` trong `tellStatus`).
  - Secret ngẫu nhiên (`secrets.token_hex`), không ghi URL/secret vào log.
- `YOUTUBE_WEBSUB_INSTANT_360P.py`:
  - Cờ mới `"dl_engine"`: `"http"` (**mặc định = downloader hiện tại, không đổi**) | `"aria2_rpc"`; `"aria2_connections"`: `"auto"` (= 360pok) / 1 / 4 / 8 / 12 / 16.
  - Chỉ dùng aria2 cho **itag 18 (1 file)**; không dùng khi video đang "thận trọng" (vừa 403/429), đang tạm dừng vì 429, hoặc khi dùng link client khác. Lỗi aria2 KHÔNG phải HTTP (daemon chết, quá giờ…) → quay về downloader WS360, 1 GET thường. Resolver Innertube, Excel, TikTok, SQLite, tên file: **không đổi**.
  - Bật `aria2_rpc` → daemon được khởi động sẵn lúc mở tool (như 360pok). Log khởi động: `⚙️ TẢI: … | downloader aria2_rpc (THỬ NGHIỆM, aria2c có, kết nối …)`; mỗi video: `| v2: aria2 RPC N kết nối yêu cầu / M thực tế`.
  - Lệnh đo mới `--bench-aria2` + `BENCH_ARIA2_360.bat`.

## 3. Benchmark CÙNG URL (chạy trên máy bạn)

`BENCH_ARIA2_360.bat` → nhập 3–4 ID (nên có video 8–10 MB và 1 video dài). Mỗi vòng: resolver WS360 lấy **1 link MỚI**, rồi tải **CHÍNH link đó**
bằng `ws360` (downloader hiện tại, đúng `config.json`) và `aria2_1/4/8/12`, **thứ tự xoay vòng** (cache CDN chia đều; có cột "chỉ lượt đầu vòng").
Mỗi lượt đo: TTFB, số kết nối thực tế, tải, kiểm MP4, ffprobe, TỔNG (= tải + kiểm + ffprobe), lỗi. **Mọi file phải đúng dung lượng + MP4 + ffprobe +
SHA-256 trùng nhau** giữa các phương án của vòng. 429 → bench **tự dừng** (không dồn request). Kết quả: `BENCH_ARIA2.md`, `BENCH_ARIA2_SUMMARY.md`, `BENCH_ARIA2.csv`.

**Quy tắc chọn** (tự động trong `BENCH_ARIA2_SUMMARY.md`): `aria2_N` chỉ được chọn khi **0 lỗi, 0 file sai** VÀ nhanh hơn `ws360` trên cùng URL —
khoảng tin cậy 90% (bootstrap theo cặp) của mức giảm TỔNG > 0 ở **cả median lẫn P90**. Có nhiều phương án đạt → chọn median nhỏ nhất.

## 4. Số MOCK (127.0.0.1 — KHÔNG phải YouTube)

Mô phỏng theo log thật của bạn (video 9,86 MB): mỗi request chờ máy chủ 160 ms, link chuyển hướng 2 lần (3 chặng), mỗi kết nối ~68 Mbit/s;
file MP4 thật 9,5 MB; 10 vòng × 5 phương án, cùng URL mỗi vòng (`tests/bench_aria2_mock.py` → `BENCH_ARIA2_MOCK*.md/.csv/.json`).

| Phương án | TỔNG median / P90 (ms) | giảm so với ws360 theo cặp: median [KTC 90%] | kết nối thực tế | request / lần tải | 8–10 MB < 1,5 s | tự kết luận |
|---|---|---|---|---|---|---|
| ws360 (hiện tại) | 1679 / 1687 | mốc | 1 | 3 | 0/10 | mốc |
| aria2_1 | 1651 / 1690 | +27 [+1…+29] | 1 | 3 | 0/10 | ❔ chưa kết luận |
| aria2_4 | 1327 / 1394 | +349 [+338…+352] | 4 | **14** | 9/10 | ✅ |
| aria2_8 | 1496 / 1532 | +187 [+177…+309] | 8 | **26** | 5/10 | ✅ |
| aria2_12 | 1264 / 1501 | +413 [+317…+468] | 9 | **27** | 9/10 | ✅ (nhanh nhất) |

0 lỗi, 0 file sai (SHA-256 trùng) ở mọi phương án. `aria2_12` chỉ mở 9 kết nối thật (file 9,5 MB, mỗi đoạn tối thiểu 1 MiB).

**Điều quan trọng phải biết — số request:** mỗi kết nối aria2 **tự đi lại toàn bộ chuỗi chuyển hướng 302**. Với 2 lần chuyển hướng:
`ws360` = 3 request/video; `aria2_12` = **27** request/video (gấp 9). Trên YouTube thật, nhiều kết nối + nhiều chặng có thể làm tăng khả năng bị
429 — vì vậy aria2 dùng `max-tries=1`, bench tự dừng khi gặp 429, và chế độ này **mặc định tắt**.

**Ước tính cho máy bạn (CHƯA đo — chỉ suy từ log):** TTFB 888 ms (3 chặng, kết nối mới) gần như không đổi với aria2 (mỗi kết nối cũng đi 3
chặng); phần truyền 1151 ms (≈ 68 Mbit/s/kết nối) chia cho ~9 kết nối còn khoảng 150–300 ms nếu đường truyền đủ (video dài của bạn đạt ≈ 290 Mbit/s).
→ tải + xác minh 9,86 MB khoảng **1,1–1,4 s** — CHỈ khi giới hạn đúng là theo từng kết nối. Bench thật sẽ trả lời.

## 5. Cờ và quay về bản cũ

| Cờ | Mặc định | Bật thử | Quay về |
|---|---|---|---|
| `dl_engine` | `"http"` | `"aria2_rpc"` | `"http"` |
| `aria2_connections` | `"auto"` | số mà `BENCH_ARIA2_SUMMARY.md` đề xuất | (không dùng khi `http`) |

Cần `aria2c.exe` cạnh `YOUTUBE_WEBSUB_INSTANT_360P.py` (hoặc `bin\`, hoặc PATH) — dùng đúng file `aria2c.exe` của 360pok.
Bản trước: `backup/YOUTUBE_WEBSUB_INSTANT_360P_truoc_aria2_rpc.py`.

## 6. Hướng dẫn 5 dòng
1. Chép `aria2c.exe` (của 360pok) vào thư mục WS360 (cạnh `START.bat`).
2. Tắt WS360 đang chạy; chạy `BENCH_ARIA2_360.bat` → nhập 3–4 ID (video 8–10 MB + 1 video dài).
3. Chờ xong (mỗi vòng tải cùng 1 link 5 lần; ~20 vòng mỗi phương án; gặp 429 sẽ tự dừng).
4. Gửi `BENCH_ARIA2.md`, `BENCH_ARIA2_SUMMARY.md`, `BENCH_ARIA2.csv`.
5. Chỉ khi SUMMARY ghi ✅: đặt `"dl_engine": "aria2_rpc", "aria2_connections": N` trong `config.json`; muốn quay về: `"dl_engine": "http"`.

## 7. pytest
`python -m pytest tests -q`: trước **232 passed** → sau **244 passed** (+12 test trong `tests/test_aria2_rpc.py`, chạy aria2c THẬT với mock:
tải đúng từng byte, đếm kết nối thật, 403/429 đúng 1 request, sai dung lượng/huỷ không để lại file dở, chế độ mặc định không gọi aria2,
lỗi aria2 → quay về downloader WS360, 429 không bị tải lại, bench cùng URL + quy tắc chọn).
