# V9.4 SUB-2S — Hướng dẫn chạy, đo và rollback

Thư mục này chứa:

| File | Vai trò |
|---|---|
| `V9_3_AUDIO.py` | Bản gốc, **không sửa một byte** (SHA256 `c1760ef5…7ecfe`). Dùng để rollback và làm baseline. |
| `V9_4_SUB2S.py` | Bản mới (GUI, Excel, realtime, cắt <60/60–300/>300, MOVE, STATUS giữ nguyên). |
| `bench_v93.py` | Benchmark: `offline`, `sweep`, `trigger`, `pipeline`, `live` (A/B V9.3 vs V9.4). |
| `benchmark_config.example.json`, `urls.example.txt` | Mẫu cấu hình / danh sách URL cho benchmark live. |
| `tests/` | 57 test pytest + mock GoogleVideo (`tests/mock_gvs.py`). |
| `perf_report/` | Kết quả đã chạy (JSONL + CSV + summary.md), `REPORT.md`, `test_results.txt`. |
| `RUN_V9_4_GUI.bat`, `RUN_V9_3_GUI.bat`, `RUN_BENCHMARK.bat` | Chạy trên Windows bằng double-click. |

## 1. Cài đặt (Windows)

1. Chép **cả thư mục** cạnh `aria2c.exe`, thư mục `ffmpeg\` (có `ffmpeg.exe`, `ffprobe.exe`) như bản V9.3 đang dùng.
2. Gói giống V9.3: `pip install PySide6 yt-dlp urllib3 pandas openpyxl`
3. Khuyến nghị thêm (không bắt buộc): `pip install python-calamine` → đọc `check.xlsx` nhanh hơn ~8×.
   Lần đầu đọc mỗi file, V9.4 tự đối chiếu calamine với openpyxl; khác một ô là tự quay về openpyxl cho file đó
   (để chỉ số dòng ghi DONE luôn đúng).
4. Chỉ để chạy test/benchmark: `pip install pytest` (tuỳ chọn `psutil` để log RAM).

## 2. Chạy GUI

- Double-click `RUN_V9_4_GUI.bat` (hoặc `python V9_4_SUB2S.py`). Giao diện, nút, cấu hình giống V9.3.
- Log có thêm 1 dòng KPI mỗi video, ví dụ:
  `📊 V9.4 KPI [id] resolve_v=…ms resolve_a=…ms | video=…ms audio=…ms | max(v,a)=…ms | mux=…ms | HOT_READY=…ms | TOTAL=…ms`
  (mốc 0 = lúc worker nhận URL).
- Telemetry chi tiết (span `trigger_detected`, `excel_read_begin/end`, `worker_submit/start`,
  `player_post_begin/end`, `resolve_first_video_url`, `resolve_first_ja_audio_url`, `gvs_request_begin`,
  `gvs_206_headers`, `video_first_byte`, `audio_first_byte`, `video/audio_download_complete`, `mux_begin/end`,
  `file_validated`, `postprocess_complete`, `status_done`, `job_summary`) ghi vào
  `perf_report\telemetry_YYYYMMDD_HHMMSS.jsonl` bởi thread nền (không print trên hot path).
  URL media có chữ ký luôn bị redact (`https://rr1---sn-***.googlevideo.com/videoplay?<redacted>`); không ghi
  cookie/visitorData/token.

## 3. Rollback

- Toàn bộ: chạy `RUN_V9_3_GUI.bat`.
- Từng tối ưu: đặt biến môi trường trước khi chạy (trong `.bat`: `set V94_...=...`):

| Biến | Mặc định V9.4 | Giá trị = hành vi V9.3 | Ý nghĩa |
|---|---|---|---|
| `V94_RANGE_ENGINE` | `v94` | `v93` | Dùng nguyên engine Range V9.3 |
| `V94_STRICT_CONTENT_RANGE` | 1 | 0 | Kiểm start/end Content-Range |
| `V94_RANGE_ADAPTIVE_RESIZE` | 1 | 0 | Size sai → co/giãn tại chỗ (0 = tải lại như V9.3) |
| `V94_RANGE_UNKNOWN_SIZE` | 1 | 0 | Size không biết → Range-first thay vì DIRECT-1GET/aria2 |
| `V94_UNKNOWN_FIRST_WORKERS` | 4 | – | Số request đoán song song khi chưa biết size |
| `V94_DIRECT_1GET_DEADLINE_SEC` | 12 | 0 | Deadline tổng DIRECT-1GET |
| `V94_NO_ARIA2_ON_AUTH_FAIL` | 1 | 0 | 403/410/429 → không aria2 cùng URL, SAFE-REFRESH 1 lần |
| `V94_FIRST_WAVE_RACE_MAX` | -1 (từ V9.4.1) | -1 | Số request kép (host chính + replica mn) đợt đầu mỗi file. -1 = không giới hạn = V9.3; 0 = tắt race; 4 = trần của V9.4.0 (đo được: chậm hơn khi replica nhanh) |
| `V94_HEDGE_POOL_MAX` | 192 | 192 | Trần thread hedge |
| `V94_RANGE_WORKERS_VIDEO/AUDIO` | 0 | 0 | Ép số worker (thí nghiệm); 0 = heuristic V9.3 |
| `V94_VALIDATE_MUX` | 1 | 0 | Xác thực video+audio trước khi đi tiếp/DONE |
| `V94_VALIDATE_MODE` | `mux_log` | – | `ffprobe` = luôn spawn ffprobe (+50–70 ms) |
| `V94_WAKEUP_CHECK_SEC` | 0.010 | 0.025 | Chu kỳ stat trigger |
| `V94_EXCEL_CALAMINE` | 1 | 0 | Engine calamine nếu đã cài |
| `V94_KEEPALIVE_YIELD` | 1 | 0 | Keepalive không bắn HEAD khi đang có job |
| `V94_JA_INFER_GUARD` | 1 | 0 | Chặn suy luận JA khi `defaultAudioLanguage` khác ja |
| `V94_FASTEST_VALID_480P` | 0 | 0 | **Đề xuất, mặc định TẮT**: cả 244 và 135 cùng có → lấy file nhỏ hơn. Chỉ bật khi đã đo và bạn đồng ý |
| `V94_TELEMETRY` / `V94_TELEMETRY_DIR` | 1 / `perf_report` | 0 | Bật/tắt và thư mục telemetry |
| **V9.4.1** `V94_FF_PREWARM` | 1 | 0 | Làm nóng ffmpeg/ffprobe (1 lần remux thật trên mẫu 12KB nhúng sẵn, trong `%TEMP%\v94_ffwarm`) ngay khi media bắt đầu tải, nếu ffmpeg đã rảnh ≥ `V94_FF_PREWARM_IDLE_SEC` (3s). Job không bao giờ chờ nó |
| `V94_FF_WARMUP_START` | 1 | 0 | Làm nóng 2 lần lúc bấm START và in dòng `FF BASELINE` (chi phí nguội vs nóng trên máy bạn) |
| `V94_FF_KEEPWARM_SEC` | 0 | 0 | >0: giữ ffmpeg nóng định kỳ khi rảnh (chỉ bật nếu log cho thấy job đầu sau quãng rảnh vẫn chậm) |
| `V94_PREWARM_INNERTUBE` | 1 | 0 | Import bảng client Innertube của yt-dlp lúc START (bớt ~40-150ms cho job đầu) |
| `V94_MUX_DIAG` | 1 | 0 | Đo tách pha lệnh mux (pre-read input, spawn, mở input, copy, thoát, thời gian kernel Windows). 2 = thêm `-benchmark`. Lệnh và file output không đổi |
| `V94_MUX_PREREAD` | 1 | – | Đọc trước 64KiB mỗi input để quy thời gian chờ quét antivirus |
| `V94_GIL_PROBE` | 0 | 0 | Chỉ để chẩn đoán: đo GIL/GC stall trong lúc mux |
| `V94_VALIDATE_MUX_STRICT` | 1 | 0 | Xác thực file OUTPUT bằng báo cáo cuối của ffmpeg (`time=`, `video:/audio:` kB, không có dòng lỗi đọc input); thiếu dữ liệu → ffprobe. Input bị cắt cụt → KHÔNG DONE |
| `V94_FFPROBE_TIMEOUT` | 20 | 0 | Timeout ffprobe đo thời lượng (V9.3 có thể treo vĩnh viễn) |
| `V94_RACE_ALL_IF_WORKERS_LE` | 8 | – | Chỉ có tác dụng khi bạn tự đặt trần race > 0: file nhỏ vẫn race hết |
| `V94_ALT_BAN_STRICT` | 1 | 0 | Chỉ cấm replica mn 10 phút khi lỗi chắc chắn (401/403/404/410/429, từ chối kết nối, DNS); timeout muộn của request thua cuộc không còn tắt race cho các job sau. Mỗi lần cấm đều có log `mn-race OFF 600s` |
| `V94_VIDEO_SMALL_WORKERS` | 0 | 0 | Số worker cho video ≤ 4MiB (0 = 6 như cũ) |
| `V94_VIDEO_SMALL_WORKERS_AB` | (trống) | – | A/B trong app, ví dụ `6,11`: job lẻ 6, job chẵn 11, có nhãn `ab=` trong log |
| `V94_EXCEL_ATOMIC_REPLACE` | 1 | 0 | Ghi check.xlsx bằng `os.replace` (nguyên tử, retry 10×30ms, rơi về `shutil.move`) |
| `V94_STATUS_VERIFY_URL` | 1 | 0 | DONE chỉ ghi vào dòng VẪN chứa video id của job (dòng bị chèn/xoá/sắp xếp → tìm lại hoặc không ghi) |
| `V94_JA_INFER_STRICT` | 1 | 0 | Suy luận [SINGLE-TRACK-JA] tôn trọng `audioTrack.id/displayName`, track trùng, bỏ tiêu đề dịch của microformat |

## 3b. Đọc các dòng log mới của V9.4.1

- `🔥 FF BASELINE | ffmpeg version … | cold ffmpeg=X ms ffprobe=Y ms | warm ffmpeg=… | exe: … | ổ: …`: in 1 lần sau START.
  `cold` là chi phí ffmpeg.exe "nguội" trên máy bạn. Nếu cold lớn hơn warm hàng trăm ms thì giả thuyết spawn nguội là đúng.
- `🔥 FF PREWARM [id] ffmpeg=… ffprobe=… idle=…s (JOB)`: làm nóng nền khi job bắt đầu tải (chỉ khi ffmpeg đã rảnh ≥ 3s).
- `⚡ MUX COPY 480P+JA: 0.602s | … | seq= idle= pre=v/a popen= →in0= →in1= →out= copy= exit= life= cpu= lag=`:
  `pre` lớn thì antivirus quét file vừa tải. `popen`/`lag` lớn thì tạo process chậm. `→in0` lớn thì mở input chậm.
  `life` cao trong khi `cpu` thấp nghĩa là ffmpeg đang chờ (đĩa/AV).
- `✅ VALIDATE mux-log: … | out=58.28s | strict=OK`. `strict=FALLBACK(...)` nghĩa là đã kiểm bằng ffprobe; `❌ VALIDATE thất bại (...)` nghĩa là KHÔNG DONE.
- `[RANGE-TURBO] ✅ HOÀN TẤT … | xfer≈ conn_p50≈ ttfb_late= hedge_real= gets= conc_peak= splits= takeovers= prealloc=`
  (tốc độ thật sau byte đầu, tốc độ mỗi kết nối, số GET đồng thời). `alt=n/a` nghĩa là replica chưa trả lời lần nào.
  `alt_lost=ok/err/pending` là kết cục các request thua cuộc.
- `📮 POST: rename= probe= excel= prepend_ff= prepend_probe= move= done_lock= done_load= done_save= done_replace=` (ms sau HOT_READY).
- Dòng `📊 V9.4 KPI` có thêm `KPI1=` (đủ video + audio), `KPI3=` (phát hiện tới GET media đầu tiên), `queue=`
  (chờ slot Max Threads), `detect=`, `valid=`, `post=`, `done=`, `prewarm=`, `ab=` và tên bản `[V9.4.1-SUB2S]`.

## 4. Test (không cần mạng)

```bat
python -m py_compile V9_3_AUDIO.py V9_4_SUB2S.py bench_v93.py
python -m pytest -q -rfxX
```
Kết quả mong đợi (V9.5): `171 passed, 1 xfailed` (đôi khi `172 passed` vì bug V9.3 là race, có lần không lộ). XFAIL là **bug V9.3 được tái hiện có chủ đích** (4 job ghi STATUS
song song làm mất DONE / hỏng `check.xlsx`). Nội dung test: 206 chuẩn (SHA256), 302, 403/410/429, 200 bỏ
Range, size ước tính sai (4 kiểu), size không biết, Content-Range sai, đứt kết nối giữa chừng, header trễ,
đuôi chậm (steal), 8 tải đồng thời, huỷ + dọn thread/file, 416, rollback cờ, chọn audio JA, đuôi file, Excel,
trigger, telemetry/redact, mux VP9+AAC / VP9+Opus, mux thiếu audio phải FAIL, end-to-end 3 nhánh cắt + MOVE +
DONE đúng dòng, 403 audio không DONE và không aria2, GUI realtime 4 job qua Excel+trigger rồi STOP.

## 5. Benchmark

Double-click `RUN_BENCHMARK.bat`, hoặc từng lệnh:

```bat
python bench_v93.py offline --reps 5
python bench_v93.py trigger --reps 30 --rows 500
python bench_v93.py pipeline --reps 10
```

**Live A/B (quan trọng nhất — chỉ chạy được trên máy/mạng của bạn):**

1. Copy `urls.example.txt` → `urls.txt`, điền URL công khai, đủ nhóm: Shorts 1–5 MiB, 5–20 MiB, 20–80 MiB,
   >80 MiB, audio nhỏ và audio >8 MiB, video vừa đăng và video cũ.
2. `python bench_v93.py live --urls urls.txt --reps 3 --gap 5`
   - Chạy NGUYÊN `Worker._process_single_url` của từng bản, xen kẽ ABBA, 1 lượt warm-up mỗi bản (không tính),
     nghỉ `--gap` giây giữa các lần để không gây 429. `--cutsec 0` (mặc định) để luôn đo MUX.
   - Không có Excel → không ghi STATUS vào `check.xlsx` thật. Output vào thư mục tạm rồi xoá.
   - Mỗi file kết quả được ffprobe (phải có video + audio), nếu không → tính là FAIL.
3. A/B số worker (mỗi lần đổi **một** nhóm tham số):
   `python bench_v93.py live --urls urls.txt --versions v94 --workers-video 8,16,24,32`
4. A/B một cờ: ví dụ `set V94_FIRST_WAVE_RACE_MAX=-1` rồi chạy lại live với `--versions v94` và so sánh với lần
   mặc định.

Kết quả: `perf_report\live_<thời gian>.jsonl/.csv/_summary.md` với `resolve_video_ms`, `video_ms`, `audio_ms`,
`media_ready_ms` (KPI1), `mux_ms`, `hot_ready_ms` (KPI2), `pipeline_total_ms`, `success_rate`, p50/p90/p95,
`under_2s_*`, `threads_after`, `fail_reasons`. KPI3 (detection → media GET đầu) = `gvs_request_begin` trừ
`worker_start`/`trigger_detected` trong telemetry JSONL.

## 5b. Gửi lại gì sau khi chạy V9.4.1

1. File `perf_report\telemetry_*.jsonl` của phiên chạy (cạnh `V9_4_SUB2S.py`). Không chứa URL có chữ ký hay token.
2. Log GUI của **10+ job liên tiếp**, gồm cả các dòng sau START (`FF BASELINE`).
3. Dòng đầu của `ffmpeg -version`, ffmpeg.exe nằm ở đâu, ổ SSD hay HDD/USB, có OneDrive/antivirus nào quét thư
   mục tool/download/`_temp_yt480p` không. Chỉ cần báo lại, chưa cần đổi gì.
4. (Tuỳ chọn) A/B số worker cho Shorts: thêm dòng `set V94_VIDEO_SMALL_WORKERS_AB=6,11` vào `RUN_V9_4_GUI.bat` trước
   dòng `python`, chạy 20+ Shorts mới, gửi log + telemetry. Chỉ đổi mặc định khi nhánh 11 nhanh hơn ≥200ms p50, không
   tệ hơn p90 và không tăng 403/429.

## 6. Tiêu chí để dùng V9.4 làm bản chính

So `live_*_summary.md`: success_rate V9.4 ≥ 95 % và không thấp hơn V9.3; p50/p90 `hot_ready_ms` không tệ hơn;
không có `fail_reasons` mới (đặc biệt 403/429, `bad_content_range`, `output_invalid`). Nếu một tối ưu làm xấu
đi, tắt riêng cờ đó (mục 3) rồi đo lại.

## 7. Giới hạn trung thực

- Chưa có số đo live: môi trường phát triển bị chặn YouTube/GoogleVideo. **Không** có tuyên bố "< 2 s".
- KPI < 2 s phụ thuộc trễ Innertube, giới hạn per-connection của GVS và băng thông thực; với video lớn
  (> 80 MiB) trên đường < 400 Mbps thì về lý thuyết không thể < 2 s.
- Không lách đăng nhập/DRM/token; 403/429 được dừng có giới hạn, không spam retry.

## 8. V9.5 — lựa chọn "360p nhanh" (cờ `V95_360P`, mặc định bật)

Trong mục **Chất lượng** có thêm radio **"360p nhanh — itag 18 (hình + tiếng 1 file, không ghép)"**. Lựa chọn được lưu
vào config (`quality_mode = "360fast"` / `"480hard"`) và đọc lại đúng khi mở tool (V9.4 luôn ép 480p).
Chọn **480p** → luồng cũ chạy y như V9.4 (test `test_480_mode_unchanged`).

Luồng 360p: `_quick_fetch_native_360p` (race 3 client InnerTube → yt-dlp nhanh → yt-dlp robust) → `_download_direct_single_get`
(1 GET, không HEAD/Range preflight) → kiểm `size >= content_length` → `os.replace` ra file cuối (KHÔNG ffmpeg) → cắt/MOVE như
480p → DONE → ffprobe chạy nền chỉ cảnh báo. Lỗi GET → link dự phòng (yt-dlp) → aria2/IDM.

itag 18 chỉ có **tiếng mặc định**: nếu `defaultAudioLanguage` không phải tiếng Nhật mà video có track Nhật → log `🈁` và
tự chuyển video đó sang 480p + audio Nhật (`V95_360P_JA_FALLBACK=0` để tắt).

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `V95_360P` | 1 | 0 = ẩn radio, mọi job đi 480p (hành vi V9.4) |
| `V95_360P_JA_FALLBACK` | 1 | tự chuyển sang 480p+JA khi tiếng mặc định ≠ Nhật nhưng có track Nhật |
| `V95_360P_RANGE` | 0 | 1 = tải itag 18 bằng engine Range nhiều kết nối (A/B). Mock 40Mbps/kết nối: 1 GET 5MB = 1094ms, Range = 183ms. Live GVS ~5Mbps/kết nối nên 1 GET với file 2-3MB có thể mất 4-5s — hãy đo |
| `V95_360P_BG_PROBE` | 1 | ffprobe nền sau DONE, chỉ cảnh báo |

Dòng log mới: `=== [START 360P-FAST]`, `⚡ 18 READY [...] | lang= tracks=`, `🚀 HOT 360P READY`, và
`📊 V9.5 KPI-360 [...] resolve= first_byte= download= (MB ≈Mbps) HOT_READY= post= done= TOTAL=`.
Span telemetry: `q360_resolve_begin/end`, `q360_get_begin`, `q360_first_byte`, `q360_download_complete`, `q360_file_ready`,
`q360_ja_fallback`, `q360_bg_probe`; `job_summary` có `mode`.

Báo cáo P50/P90/P99 từ telemetry: `python bench_v93.py report` (đọc mọi `perf_report\telemetry_*.jsonl`, tách 360fast / 480hard,
kèm `size_mb`, `mbps`, `under_1s_hot`). Live A/B: `python bench_v93.py live --urls urls.txt --versions v94 --quality both`.

## 9. V9.6 — giảm thời gian tải 360p (cờ `V96_*`, tắt = V9.5)

Từ log live: resolve ≈0.14s, **TTFB ≈0.69–0.75s (có 302 đổi host)**, truyền 5–9MB ≈0.5–0.6s.

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `V96_HOST_POOL` | 1 | Nhớ host đích sau 302 (TTL 10 phút) → video sau GET thẳng host đích (bỏ 1 RTT); sai → về URL gốc 1 lần. Kết nối TLS tới host đích được mở ngay khi thấy 302 và giữ trong pool; sau khi tải xong, HEAD `/generate_204` mỗi 25s giữ nóng (chỉ khi không có job). Mock: first_byte 354 → 103ms khi trúng cache |
| `V96_MN_PRECONNECT` | 1 | Ngay khi có link, mở DNS+TLS song song tới các host trong `mn=` (ứng viên đích 302). Log `mn_pre=2✓` = 302 trỏ đúng host đã mở sẵn (bỏ ~dns+tls ≈170ms theo log 10:51), `✗` = trượt (không mất gì) |
| `V96_RANGE_N` | 0 | Chia N đoạn Range song song (2/4/8) khi biết content_length; lỗi 403/410/429 → dừng chia, 1-GET cùng URL, không retry thêm |
| `V96_HTTP2` | 0 | Tải qua httpx HTTP/2 (`pip install httpx[http2]`), N = `V96_RANGE_N` (1 = 1 GET h2). Không có httpx → tự về 1-GET urllib3 |

Dòng `[DIRECT-1GET] ✅ …` và `KPI-360` có thêm `req1=` (RTT request đầu: 302 hoặc 200), `dns=`, `tls=` (tới host đích sau 302),
`xfer=` (thời gian truyền sau byte đầu), `302skip` (đã bỏ 302 nhờ cache).

**A/B tự động (bắt buộc chạy live để chọn mặc định):**
```bat
pip install httpx[http2]
python bench_v93.py live360 --urls urls.txt --reps 3
```
Chạy 7 cấu hình (`1get`, `1get+pool`, `range2/4/8`, `h2x1`, `h2x4`) × URL × 3 lần, in bảng `first_byte / xfer / download /
hot_ready p50/p90 / Mbps / 302skip_rate` và kết luận cấu hình nhanh nhất (phải nhanh hơn `1get` ≥50ms p50 và không tệ hơn p90).
Sau đó ghi vào `RUN_V9_4_GUI.bat`, ví dụ `set V96_RANGE_N=4`. Không có URL → chạy mock (chỉ kiểm harness).

## 10. V9.7 — giao thức HTTP/2, HTTP/3 cho 360p (cờ `V97_PROTO`, mặc định `h1` = V9.6)

| Biến | Giá trị | Ý nghĩa |
|---|---|---|
| `V97_PROTO` | `h1` (mặc định) | A: urllib3 HTTP/1.1, 1-GET — như V9.6 |
| | `h2` | B: httpx HTTP/2 (`pip install httpx[http2]`), client nóng dùng chung; `V97_H2_RANGE_N`=1/2/4 đoạn Range trên cùng kết nối |
| | `h3` | C: `curl --http3` (curl phải có HTTP3: `curl -V` → Features có `HTTP3`; tải build từ curl.se/windows, đặt `curl.exe` cạnh tool hoặc `set V97_CURL_PATH=...`). Theo redirect, ghi thẳng file tạm; `--write-out` cho DNS/connect/TLS/TTFB/truyền |
| `V97_H2_RANGE_N` | 1 | số đoạn Range cho `h2` |
| `V97_CURL_PATH` | (trống) | đường dẫn curl.exe có HTTP3 |

Lỗi bất kỳ ở h2/h3 (không cài, bắt tay thất bại, HTTP ≠ 200/206, file thiếu) → về A ngay trong cùng job, không thử lại.
Dòng KPI-360 có `proto=h3/3 cpu=12.5%` (phiên bản HTTP đã thương lượng + % CPU tiến trình trong lúc tải).
D (aioquic thuần Python) **không** thử: chỉ có ý nghĩa khi B/C đã thắng và chưa có số đo.

**Đo & quyết định:** `python bench_v93.py live360 --urls urls.txt --reps 3` chạy A, A+pool, range2/4/8, h2x1/h2x2/h2x4, h3
xen kẽ thứ tự; bảng có dns/tls/first_byte/xfer/download/hot_ready p50–p90/Mbps/cpu%/errors/http_versions. Luật chọn:
P50 **và** P90 `hot_ready` tốt hơn A ≥ 30 ms, lỗi không nhiều hơn A. curl không có HTTP3 → bỏ C và ghi rõ trong báo cáo.
