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

---

# V9.4.1 — bản vá sau log live đầu tiên (Windows, 02/11/2026 17:09)

## A. Log live đầu tiên (Shorts M1sbvQq1DAk, 135 + 139 [SINGLE-TRACK-JA], <60s)

| Bước | Thời gian | Ghi chú |
|---|---|---|
| resolve (NATIVE_VISIONOS) | 276 ms | ~80% là thời gian server /player |
| video 2.88 MB, 6 worker | 1515 ms | TTFB 0.49s có 1 redirect 302; ~5 Mbps/kết nối sau TTFB; alt-win=3 |
| audio 0.34 MB | 594 ms | xong ở ~0.87s, không nằm trên đường găng |
| **KPI1 (đủ video+audio)** | **≈1792 ms** | **đã < 2s** |
| mux | **602 ms** | ~10× một lần stream-copy nóng |
| **KPI2 HOT_READY** | **2394 ms** | trượt 2s đúng ~394 ms, nằm trọn trong mux |
| hậu xử lý <60s | 705 ms | ffprobe đầu tiên (không đo), Excel, prepend 110ms, MOVE, ghi DONE |

Cùng giây đó bước <60s chạy ffmpeg concat + ffprobe chỉ mất **0.11s**, và offline cùng lệnh mux ~110ms; mọi nguyên nhân bên
trong ffmpeg (`-loglevel info`, probe fMP4 DASH, `-shortest`) đo được ≤5ms. Điểm khác duy nhất: đó là lần spawn ffmpeg.exe
**đầu tiên** của job sau một quãng rảnh, và là lần duy nhất mở file python vừa đóng → giả thuyết số 1: **ffmpeg.exe nguội**
(nạp image/DLL, kiểm tra AV khi tạo process); số 2: **AV quét-khi-mở** file vừa tải; số 3: GIL stall phía Python.
Một con số "MUX COPY" duy nhất không phân biệt được → V9.4.1 thêm cả **bản sửa** lẫn **đo tách pha**.

## B. Phân tích độc lập 5 góc (workflow 6 agent, 51 phát hiện → 13 làm ngay, 13 cần số live, 11 bác bỏ)
Bác bỏ có lý do: gộp mux+prepend 1 lệnh (lệch audio ~1.5s), PyAV in-process (phụ thuộc 35MB, output khác byte),
controller tăng worker động (p90 tệ hơn ~130ms trong sim), hedge /player (tăng tải 429), duration-hint từ log input
(lệch 1 sample ở 3/16 ca), bỏ fh.truncate (NTFS zero-fill trong lock), dời mốc HOT_READY (mất so sánh với V9.3)...

## C. Đã sửa (đều sau cờ `V94_*`, xem README mục 3)

| Hạng mục | Vấn đề | Bằng chứng | Kết quả |
|---|---|---|---|
| ff-prewarm | ffmpeg.exe nguội ở lần mux đầu | log live 0.602 vs 0.11s; offline: evict page-cache 126→210ms, 1 lần warm-up xoá chênh lệch | làm nóng nền (ưu tiên thấp) lúc media bắt đầu tải; job không chờ (test: prewarm 2s không làm chậm HOT_READY) |
| mux-diag | không phân biệt được nguyên nhân | — | dòng MUX có `pre/popen/→in0/copy/exit/life/cpu/lag`; dòng `FF BASELINE` cold/warm lúc START |
| **race mặc định** | V9.4.0 trần race 4 → segment còn lại chờ host chính + 302 | bench 2 host (race_20261102_112312): Shorts 2.88MB **V9.3 223 / V9.4.0 365 / V9.4.1 221 ms**; 24MB 609 / 739 / 631 ms. Kịch bản ngược (replica chậm, race_…112346): trần không nhanh hơn, chỉ bớt socket | mặc định trở lại **race hết như V9.3** (`V94_FIRST_WAVE_RACE_MAX=-1`) |
| alt-ban-strict | request thua cuộc timeout MUỘN (sau khi job xong) bật cấm replica 10 phút cho mọi job sau, không log | tái hiện: audio xong 362ms, 1.8s sau ban 599s, video sau không còn race | chỉ cấm khi lỗi chắc chắn; luôn log `mn-race OFF 600s` |
| validate-strict | V9.4.0 đọc "Duration" của INPUT (header) → input cắt cụt 60% vẫn OK | tái hiện: v=58.17 ok=True trong khi output thật 34.87s | đọc `time=`/`video: audio:` cuối của OUTPUT + dòng lỗi; thiếu → ffprobe; input cụt → KHÔNG DONE (test pipeline) |
| ja-infer-strict | track `audioTrack.id=en.4`, 2 track không metadata, tiêu đề dịch microformat → vẫn đoán JA | 3/5 ca probe | từ chối, ghi lý do; ca JA thật vẫn nhận |
| excel atomic + row verify | `shutil.move` trên Windows = copy đè (đọc thấy zip dở); DONE theo vị trí có thể ghi nhầm dòng | giả lập Windows: 7 lần đọc '' / 26.984 | `os.replace` + retry; DONE chỉ vào dòng chứa đúng video id |
| ffprobe timeout | ffprobe thời lượng không timeout (treo giữ slot) | code | timeout 20s, dùng đúng path |
| Popen.wait polling | `_v94_ff_run` bản đầu dùng `wait(timeout)` → POSIX ngủ tới 50ms | pipeline bench: mux +35-45ms | chờ EOF stderr rồi `wait()` → mux 105 vs V9.3 109 ms |

## D. Số đo offline sau bản vá (V9.3 vs V9.4.1, cùng máy)

- Engine Range (offline_20261102_112537): ngang V9.3 ở size đúng (±1%); size sai/không biết vẫn nhanh hơn như V9.4.0
  (size dư 8%: 857→517ms; 0.4MB không biết size: 829→278ms; 2MB: 4205→458ms); Content-Range lệch: V9.3 nhận file hỏng.
- Pipeline (pipeline_offline_20261102_112917, 12 lần ABAB): HOT_READY p50 dur30 **294 vs 297 ms**, dur120 **654 vs 658 ms**;
  mux 81 vs 76 / 107 vs 111 ms; TOTAL 564 vs 575 / 724 vs 725 ms. Trên Linux ffmpeg không bao giờ "nguội" nên lợi ích
  prewarm không thể hiện ở đây — chỉ log live Windows mới xác nhận được (dòng FF BASELINE + MUX diag).
- Test: 108 passed + 1 xfailed (bug V9.3), ổn định qua nhiều lần chạy.

## E. Cần số live (không đổi mặc định khi chưa đo)
A/B số worker Shorts (`V94_VIDEO_SMALL_WORKERS_AB=6,11`), quét AV khi mở file, GIL/GC stall, cờ spawn DETACHED,
tối ưu hậu xử lý theo dòng POST, preallocate NTFS, redirect/reuse kết nối, trần race riêng cho audio, hàng đợi realtime,
kết nối YouTube nguội sau thời gian bận, lỗi đọc Excel thoáng qua, phủ quyết ASR cho suy luận JA.

## F. Review độc lập sau V9.4.1 (4 góc + kiểm chứng phản biện, 10 agent)
5 lỗi được xác nhận và đã sửa, mỗi lỗi có test hồi quy (`tests/test_review_*.py`):
1. Kiểm dòng STATUS ghi theo vị trí khi ô chứa URL khác ở dạng regex không nhận (host viết hoa, `/v/`, nocookie, URL
   không phải YouTube) → nay mọi ô không rỗng (trừ công thức không có id) mà không chứa video id của job đều đi tìm lại dòng.
2. Nhánh >300s DIRECT-CUT không qua xác thực chặt → input cụt vẫn DONE. Nay kiểm từng part (có video+audio, hai luồng dài
   tương đương); lỗi → quay về MUX (có xác thực chặt) → không DONE.
3. `os.replace` check.xlsx bị Windows từ chối khi reader đang mở file → nay reader đọc cả file vào RAM rồi parse (handle mở
   vài ms) và replace chạy dưới khoá cache.
4. `Duration: N/A` của 1 input làm lệch thời lượng sang input kia → nay gắn theo từng `Input #N`; thiếu → ffprobe, không đặt sàn.
5. Fallback ffprobe dùng thời lượng video của resolver làm sàn cho output `-shortest` → bỏ sàn khi không biết đủ 2 input.
Thêm: xoá file tạm khi xác thực thất bại; đọc thống kê dưới khoá; nhãn A/B chỉ gắn với video ≤ 4MiB.
Test sau review: 120 passed + 1 xfailed (2/2 lần). Pipeline offline sau review (pipeline_offline_20261102_130907):
HOT_READY p50 295 vs 301 ms (dur30), 665 vs 666 ms (dur120) — ngang V9.3.

---

# V9.5 — lựa chọn 360p nhanh (itag 18), 10/11/2026

**Số đo live: CHƯA CÓ** (môi trường này không ra được YouTube). Dưới đây chỉ là mock cục bộ; con số < 1s trên máy bạn
chỉ khả thi với Shorts vài MB trên mạng nhanh và phải đo bằng `python bench_v93.py live --quality both` / `report`.

Hàm đã sửa/thêm: `_v95_audio_lang_meta` (mới), `_fast_player_request` (+lang/duration), `_yt_info_to_direct` (+lang),
`_download_direct_single_get` (+info), `PremiumDownloaderApp.quality_mode` (mới), `_build_ui` (radio), `_save_config`,
`_load_config`, `Worker.__init__` (quality_mode), `Worker._process_single_url_360` (mới), `_process_single_url_impl` (rẽ nhánh),
`_cut_after_merge` (ghi `final_file` cho ffprobe nền), KPI line (`KPI-360`); `bench_v93.py`: `report`, `--quality`.

Mock pipeline (pipeline_offline_20261110_185951 / _190045; link 400Mbps, 40Mbps/kết nối, TTFB 40ms, resolver giả):

| Clip | Cách tải | HOT_READY p50 | TOTAL p50 |
|---|---|---|---|
| 30s, p18 1.3MB | 360p 1 GET | 308 ms | 496 ms |
| 30s, p18 1.3MB | 360p Range (V95_360P_RANGE=1) | 101 ms | 284 ms |
| 30s, 480p 1.3MB+0.24MB | 480p (mux) | 282 ms | 511 ms |
| 120s, p18 5.1MB | 360p 1 GET | **1094 ms** | 1094 ms |
| 120s, p18 5.1MB | 360p Range | 183 ms | 184 ms |
| 120s, 480p 4.4MB+0.97MB | 480p (mux) | 659 ms | 713 ms |

Kết luận trung thực: 1 GET bị giới hạn bởi tốc độ **một** kết nối. Với GVS thật (~5 Mbps/kết nối theo log 17:09) 1 GET cho
file 2-3 MB có thể mất 4-5 s, chậm hơn 480p Range. Vì vậy có công tắc `V95_360P_RANGE` để A/B; mặc định vẫn 1 GET theo yêu cầu.
Test: `tests/test_v95_360p.py` (15 test: 3 nhánh cắt, fallback JA→480, lỗi GET→link dự phòng, 403, không link → không DONE,
480 không đổi, tắt cờ, config round-trip, Range flag).

### Review độc lập V9.5 (3 góc + kiểm chứng, 9 agent) — 5 lỗi xác nhận, đã sửa + test
1. Tiếng mặc định KHÔNG BIẾT (yt-dlp không ghi `language` cho itag 18) nhưng video ≥2 track có tiếng Nhật → trước đây vẫn tải
   itag 18 im lặng. Nay: coi là không an toàn → chuyển 480p+JA; `_yt_info_to_direct` lấy tiếng mặc định từ track `(default)/(original)`;
   link dự phòng cũng qua kiểm tra ngôn ngữ.
2. `filesize_approx` (ước lượng tbr×duration) bị dùng làm sàn kiểm size chính xác → file đủ bị từ chối và không DONE. Nay: chỉ chặt
   khi `length_exact`; ước lượng → dung sai 4%; không mang size của resolver A sang URL của B.
3. Sau fallback aria2/IDM, KPI-360/span ghi dữ liệu cũ của lần 1-GET lỗi → nay ghi `engine=aria2/idm/1get`, `+alt`, bytes thật.
4. ffprobe nền không chạy cho nhánh >300s (không có `final_file`) → nay kiểm mọi part trong thư mục cắt; test cả 3 nhánh có span.
5. `bench report` đếm sai số job lỗi → sửa Counter.
Test sau review: 144 passed + 1 xfailed (2 lần).

---

# V9.6 — giảm thời gian tải 360p, 11/11/2026

Hàm sửa/thêm: `_v96_env`, `_v96_host/_v96_swap_host/_v96_remember_redirect/_v96_predicted_url/_v96_mark_host_used` (cache 302),
`_v96_preconnect` (đo DNS, TCP+TLS; kết nối vào pool), `_v96_keepalive_worker` (HEAD /generate_204 tới host đích, chỉ khi rảnh),
`_v96_h2_client/_v96_download_h2` (httpx HTTP/2, N Range), `_download_direct_single_get` (+host đích đã nhớ, +req1/dns/tls/xfer),
`_download_http_range_turbo_v94` (+`workers_override`, nhớ redirect), `Worker._process_single_url_360` (chọn engine theo cờ,
Range/H2 lỗi → 1-GET cùng URL), `bench_v93.py live360`.

**Số đo mock** (perf_report/live360_20261111_013605; 5.1MB H.264+AAC, 302 → host khác, TTFB 250ms, 40Mbps/kết nối; KHÔNG phải CDN thật):

| Cấu hình | first_byte p50 | hot_ready p50 | Mbps | 302 bỏ được |
|---|---|---|---|---|
| 1get (V9.5) | 354 ms | 1305 ms | 32 | 0% |
| 1get+pool | **103 ms** | 1054 ms | 40 | 75% (lần đầu phải học) |
| range2 | 27 | 532 | 79 | — |
| range4 | 26 | 271 | 156 | — |
| range8 | 27 | **146** | 290 | — |
| h2x1 | 2 | 1093 | 39 | — (mock chỉ HTTP/1.1) |
| h2x4 | 2 | 310 | 136 | — |

Mock chỉ chứng minh: (a) cache host sau 302 bỏ đúng 1 RTT; (b) chia Range có lợi khi tốc độ bị giới hạn **theo kết nối**.
Trên CDN thật log cho thấy 1 GET đã đạt 57–63Mbps với 8.9MB → lợi ích Range không chắc; HTTP/2 chưa đo được ở đây.
**Mặc định giữ**: `V96_HOST_POOL=1` (không có bất lợi đo được: sai cache chỉ tốn 1 RTT rồi về URL gốc), `V96_RANGE_N=0`, `V96_HTTP2=0`
cho tới khi `python bench_v93.py live360 --urls urls.txt` trên máy bạn chỉ ra cấu hình thắng. Chưa có số live → chưa đổi.

**Log live 10:51 (V9.6, qEo6ZvVJy4Y 9.29MB):** TTFB 578ms = 302 RTT 67 + DNS 68 + TCP+TLS 106 + chờ server ≈337; xfer 527ms
(148Mbps/1 kết nối); queue 488ms chỉ vì job đến đúng lúc START (import yt-dlp 445ms giữ GIL) — các job sau queue ≈0.
→ thêm `V96_MN_PRECONNECT` (mở sẵn DNS+TLS tới host mn trước GET; đo hit bằng `mn_pre=N✓/✗`). Chờ server 337ms không giảm được
bằng kết nối; phần truyền chỉ A/B `live360` mới quyết được Range N.
**Log live 10:59 (HjD7LnMKbVQ 8.63MB):** `mn_pre=1✗` — host đích 302 KHÔNG nằm trong `mn=`; TTFB 775 = req1 182 (bắt tay + 302
ở host đầu) + DNS 65 + TLS 114 + chờ server ≈414; xfer chỉ 263ms (262Mbps/1 kết nối) → Range N còn tối đa ~150ms để lợi.
→ thêm log cặp cụm `rrN/sn-A → rrM/sn-B` và dự đoán cấp 2 theo cụm sn (giữ rrN) để kiểm tra host đích có quy luật không.
