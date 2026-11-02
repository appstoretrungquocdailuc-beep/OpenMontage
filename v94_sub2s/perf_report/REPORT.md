# Báo cáo V9.3 AUDIO → V9.4 SUB-2S (02/11/2026)

> **Trạng thái live: CHƯA ĐO.** Container dùng để code bị network policy chặn `www.youtube.com` và
> `*.googlevideo.com` (proxy từ chối kết nối), và đây không phải máy Windows của bạn. Vì vậy **không có số
> đo KPI1/KPI2/KPI3 thật nào** trong báo cáo này, và **không tuyên bố đạt < 2 s**. Mọi con số bên dưới
> đến từ mock GoogleVideo cục bộ + ffmpeg thật; chúng chứng minh độ đúng và chi phí engine, không chứng minh
> tốc độ CDN. Runner live đã sẵn sàng: `python bench_v93.py live --urls urls.txt` (xem README_VI.md).

## 1. Đường đi thực tế (đọc từ code)

`_realtime_worker` (stat trigger mỗi 25 ms → `read_links_excel_url_only` → pandas/openpyxl đọc **toàn bộ** xlsx
→ `df.iterrows()`) → submit `Worker._process_single_url` vào `batch_executor` →
`_fast_innertube_480` (visionos + web_embedded song song, deadline 1 s) + yt-dlp FAST (watch URL ngay; Shorts sau
0.55 s) + ROBUST (Shorts 1.15 s, watch 0.45 s, lặp mỗi 1 s) trong cửa sổ 8 s → `add_candidate` khởi động **ngay**
media thread riêng cho video (244→135) và audio JA → `_download_http_range_turbo` (EAGER+STEAL, hedge mn replica,
FIRST-WAVE-RACE) → `mux_480_stream_copy` (-c copy) hoặc DIRECT-CUT (>300 s) → `_cut_after_merge` → MOVE → STATUS=DONE.

## 2. Điểm nghẽn / lỗi của V9.3 đã chứng minh bằng test hoặc đo

| # | Vấn đề V9.3 | Bằng chứng | Fix V9.4 (cờ) |
|---|---|---|---|
| 1 | **Ghi STATUS song song (3–4 job) mất DONE và có thể HỎNG check.xlsx** (mọi thread cùng ghi `check.xlsx.tmp`) | `test_concurrent_status_writes_no_lost_update[v93]`: mất 21/24 DONE; lần khác BadZipFile. Test GUI 4 job: V9.3 XFAIL (`STATUS=['DONE',None,None,None]` / "check.xlsx HỎNG") | `_V94_EXCEL_WRITE_LOCK` tuần tự hoá load→save. V9.4: 0 mất, 0 hỏng (PASS mọi lần) |
| 2 | Range chỉ đọc tổng trong Content-Range; edge trả **sai đoạn byte** vẫn được ghi → file hỏng nhưng báo OK | `test_v93_writes_wrong_range_bytes` (V9.3 ok=True, SHA256 sai); offline `bad_content_range_shift4096`: V9.3 `CORRUPT_FILE_ACCEPTED` | Kiểm tra start/end/total từng response (`V94_STRICT_CONTENT_RANGE`) |
| 3 | Size thật ≠ `contentLength` → huỷ và tải lại từ đầu | offline: size dư 8 %: 856.9 → **514.3 ms** (−40 %), 22 → 14 request; size thiếu 3 %: 615.8 → **598.3 ms**, 32 → 21 request | Co/giãn tại chỗ theo header 206 đầu, không vứt byte (`V94_RANGE_ADAPTIVE_RESIZE`) |
| 4 | Audio không biết size → DIRECT-1GET 1 kết nối, không deadline (log V7.9: 29.5 s) | offline (cùng giới hạn/kết nối cho 200 và 206): 0.4 MB 829 → **277 ms**; 2 MB 4205 → **457 ms** | Range-first 4 request đoán + fan-out (`V94_RANGE_UNKNOWN_SIZE`); DIRECT-1GET còn là fallback cuối có deadline 12 s |
| 5 | 403/410/429 trên signed URL (nguồn không phải NATIVE) → vẫn gọi aria2 với **cùng URL** | `test_smart_progressive_skips_aria2_on_403`, `test_process_fails_without_done_when_audio_403` | Bỏ aria2, 1 SAFE-REFRESH dùng chung; 429 chờ Retry-After (0.5–3 s) 1 lần (`V94_NO_ARIA2_ON_AUTH_FAIL`) |
| 6 | Trigger→URL: poll 25 ms + openpyxl + `iterrows` | trigger bench (bảng 4) | poll 10 ms, calamine (tự đối chiếu với openpyxl), duyệt cột |
| 7 | FIRST-WAVE-RACE bắn kép **mọi** segment đợt đầu (32 video + 40 audio → tới 144 socket) | đọc code `_open_hedged` (`race_now` không có trần) | Trần 4 request kép/file (`V94_FIRST_WAVE_RACE_MAX`, −1 = như V9.3) — **cần live để xác nhận** |
| 8 | Suy luận "single-track = JA" chấp nhận cả khi player ghi `defaultAudioLanguage=en` | `test_guard_default_language_non_ja_blocks_inference` (V9.3 nhận, V9.4 từ chối) | `V94_JA_INFER_GUARD` |
| 9 | Audio opus/webm vẫn đặt tên `.m4a` | `test_audio_ext` | `_v94_audio_ext` |
| 10 | Không xác thực file trước DONE (chỉ size > 256 KiB) | `test_mux_missing_audio_stream_fails`, pipeline test | Đọc stream output từ log của chính lệnh mux (0 process thêm), fallback ffprobe |

## 3. Offline engine A/B (mock: link 400 Mbps, 40 Mbps/kết nối video, 4 Mbps/kết nối audio, TTFB 40 ms, 5 lần, ABAB)

File: `offline_20261102_072619_summary.md`. Trường hợp chuẩn: V9.4 ngang V9.3 trong sai số.

| case | V9.3 p50 ms | V9.4 p50 ms | Δ |
|---|---|---|---|
| audio 0.4 MB | 243.1 | 243.2 | +0.0 % |
| audio 3 MB | 804.5 | 804.9 | +0.1 % |
| audio 11 MB | 1012.7 | 1016.4 | +0.4 % |
| video 3 MB | 150.3 | 150.8 | +0.3 % |
| video 24 MB | 552.1 | 555.6 | +0.6 % |
| video 90 MB (chạm trần link) | 1948.7 | 1956.6 | +0.4 % (p90: 1964.6 vs 1963.7) |
| video 24 MB, size ước tính −3 % | 615.8 | **598.3** | −2.8 % |
| audio 3 MB, size ước tính +8 % | 856.9 | **514.3** | −40 % |
| audio 0.4 MB, size không biết | 829.1 | **276.7** | −67 % |
| audio 2 MB, size không biết | 4204.6 | **457.4** | −89 % |
| HTTP 403 | từ chối, 6 req | từ chối, 6 req | = |
| Content-Range lệch | **nhận file hỏng** | từ chối | sửa lỗi |

Lock ghi file dùng chung (V9.4 đo `lock_wait_ms`): 90 MB/32 worker = 6.5 ms tổng trên 1957 ms (0.3 %);
24 MB = 0.9 ms. → **Giữ nguyên handle dùng chung + lock**; không đổi sang offset-write độc lập (không có lợi
để bù rủi ro). Trên Windows hãy xem `lock_wait_ms` trong telemetry live.

## 4. Trigger → danh sách URL mới sẵn sàng (30 lần, scanner ghi Excel rồi append trigger)

| Excel | V9.3 p50 / p95 | V9.4 (không calamine) p50 / p95 | V9.4 (calamine) p50 / p95 |
|---|---|---|---|
| 500 dòng | 122.7 / 171.4 ms | 76.2 / 97.4 ms | **14.7 / 17.4 ms** |
| 3000 dòng | 526.5 / 636.2 ms | 325.5 / 499.3 ms | **34.6 / 48.7 ms** |

Phát hiện trigger: 23.1 → 8.1 ms (poll 25 → 10 ms). Phần lớn lợi ích đến từ calamine
(`pip install python-calamine`); không cài thì vẫn có poll 10 ms + bỏ iterrows.

## 5. Offline pipeline A/B (Worker thật, resolver giả, mock GVS, ffmpeg thật, 14 lần ABAB)

File: `pipeline_offline_20261102_075636_summary.md`. Resolver giả trả URL ngay → **không** phản ánh Innertube.

| clip | bản | media_ready p50 | mux p50 | HOT_READY p50 / p90 | TOTAL p50 |
|---|---|---|---|---|---|
| 30 s (nhánh <60 s) | V9.3 | 214.5 | 76.5 | 296.5 / 319.3 | 559.5 |
| | V9.4 | 215.3 | 84.0 | 300.4 / 318.3 | 570.2 |
| 120 s (60–300 s) | V9.3 | 544.5 | 116.5 | 661.0 / 686.1 | 723.0 |
| | V9.4 | 541.8 | 119.5 | 664.0 / 677.4 | 728.2 |

Đường thuận lợi: V9.4 ≈ V9.3 (+0.5…+1.9 %, trong nhiễu; đã bao gồm bước xác thực file mới). Bản nháp đầu
dùng ffprobe để xác thực tốn +55–70 ms; đã thay bằng đọc log mux → `validate_ms≈0`.

## 6. Sweep số worker (mock, chỉ minh hoạ phương pháp)

`sweep_20261102_072746_summary.md`: khi có giới hạn/kết nối, thời gian giảm theo số worker tới lúc chạm trần
link (video 24 MB: 4 w = 1306 ms → 12 w = 558 ms, sau đó phẳng; audio 11 MB @4 Mbps/kết nối: 4 w = 5783 ms →
40 w = 638 ms). **Không đổi heuristic V9.3** vì giới hạn/kết nối thật của GVS trên mạng bạn chưa đo.
Chạy `python bench_v93.py live --urls urls.txt --versions v94 --workers-video 8,16,24,32` để quyết định.

## 7. Test (xem `test_results.txt`)

`py_compile V9_3_AUDIO.py V9_4_SUB2S.py bench_v93.py`: OK. `pytest`: **56 passed, 1 xfailed** — xfail là bug
V9.3 #1 được tái hiện có chủ đích. Toàn bộ suite chạy 9 lần liên tiếp không crash sau khi sửa vòng đời widget
trong test GUI. `test_v93_range_offline.py` **không có** trong gói được gửi → chưa chạy.

## 8. Chưa làm / chưa đạt

- Live A/B trên Windows (KPI1/2/3, p50/p90/p95, under_2s_rate, tỷ lệ 403/429): **chưa chạy** (bị chặn mạng).
- HTTP/2, edge/IP khác (P5): **không triển khai** — không đo được, nên không thêm thư viện vào hot path.
- IPC truyền URL trực tiếp: **không thêm** — chưa xác nhận scanner V34 ghi URL vào trigger.
- Trần FIRST-WAVE-RACE = 4 và keepalive nhường job: hợp lý theo code nhưng **chưa có số live**; tắt bằng
  `V94_FIRST_WAVE_RACE_MAX=-1`, `V94_KEEPALIVE_YIELD=0` nếu A/B live xấu hơn.
- `FASTEST_VALID_480P`: có công tắc, **mặc định TẮT**; chỉ bật khi có số đo + bạn đồng ý.
