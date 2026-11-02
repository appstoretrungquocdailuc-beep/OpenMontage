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
| `V94_FIRST_WAVE_RACE_MAX` | 4 | -1 | Số request kép đợt đầu mỗi file (0 = tắt race) |
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

## 4. Test (không cần mạng)

```bat
python -m py_compile V9_3_AUDIO.py V9_4_SUB2S.py bench_v93.py
python -m pytest -q -rfxX
```
Kết quả mong đợi: `56 passed, 1 xfailed`. XFAIL là **bug V9.3 được tái hiện có chủ đích** (4 job ghi STATUS
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

## 6. Tiêu chí để dùng V9.4 làm bản chính

So `live_*_summary.md`: success_rate V9.4 ≥ 95 % và không thấp hơn V9.3; p50/p90 `hot_ready_ms` không tệ hơn;
không có `fail_reasons` mới (đặc biệt 403/429, `bad_content_range`, `output_invalid`). Nếu một tối ưu làm xấu
đi, tắt riêng cờ đó (mục 3) rồi đo lại.

## 7. Giới hạn trung thực

- Chưa có số đo live: môi trường phát triển bị chặn YouTube/GoogleVideo. **Không** có tuyên bố "< 2 s".
- KPI < 2 s phụ thuộc trễ Innertube, giới hạn per-connection của GVS và băng thông thực; với video lớn
  (> 80 MiB) trên đường < 400 Mbps thì về lý thuyết không thể < 2 s.
- Không lách đăng nhập/DRM/token; 403/429 được dừng có giới hạn, không spam retry.
