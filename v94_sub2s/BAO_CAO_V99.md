# V9.9 — SMART AUTO FASTEST 480P — báo cáo ngày 2026-11-17

> **Đọc trước.** Máy thử ở đây **không ra được YouTube / googlevideo**. Vì vậy:
> - Chưa có **số LIVE** nào trong báo cáo này. Mục 6 (bảng benchmark format thật) và mục 8 (trước/sau thật) **để trống có chủ đích** —
>   `BENCH_V99_480.bat` trên máy bạn sinh ra đúng các bảng đó.
> - Số có nhãn **MOCK** = máy chủ GoogleVideo giả (127.0.0.1, tiến trình riêng). Chứng minh cơ chế và độ đúng, KHÔNG phải tốc độ YouTube.
> - Số có nhãn **CỤC BỘ** = ffmpeg thật + pipeline cắt thật của tool trên video tổng hợp. Là bằng chứng thật về cách cắt, không phải về mạng.
> - **Không tuyên bố đạt SUB-2S.** Bản chính V9.8.1 (`V9_4_SUB2S.py`) **không bị sửa một byte** (SHA-256 trùng `backup/V9_8_1_SUB2S_truoc_v99.py`).

## 0. Giao gì

| File | Vai trò |
|---|---|
| `V9_4_SUB2S.py` | Bản chính **V9.8.1 đang chạy — KHÔNG đổi**. `RUN_V9_8_GUI.bat` |
| `backup/V9_8_1_SUB2S_truoc_v99.py` | Bản sao lưu V9.8.1 |
| `V9_9_CANDIDATE.py` | **Bản ứng viên V9.9** (mọi thay đổi nằm ở đây). `RUN_V9_9_CANDIDATE_GUI.bat` |
| `bench_v99.py` | `env`, `autopsy`, `cutlab`, `formats`, `live480` (theo tổ hợp format), `verify_cut`, `mock480`, `live_dl`, `linkcap` |
| `BENCH_V99_480.bat` | 5 bước: env → autopsy → cutlab → formats → live480 theo tổ hợp |
| `BENCH_V99_SWEEP.bat` | Quét LIVE: kết nối video 32/48/64/80, worker audio 8/16/24/32, hedge 0/8/16/40, range 1/2/4 MiB, HTTP/2, kết nối giữ nóng |
| `tests/test_v99.py` | 17 test cho bản ứng viên |

Thay bản cũ **chỉ khi** số live của B tốt hơn A: chép `V9_9_CANDIDATE.py` đè lên `V9_4_SUB2S.py` (bản cũ vẫn còn trong `backup/`).
Rollback: chạy lại `RUN_V9_8_GUI.bat` (bản chính chưa hề bị đổi).

## 1. Nguyên nhân audio 139 (9,09 MiB) mất 3,5 s

### 1.1 Điều suy ra CHẮC CHẮN từ số bạn đưa (không phỏng đoán)

- Audio chia 24 đoạn bằng nhau: 9,09 MiB ÷ 24 = **0,379 MiB / đoạn**.
- Lúc 2,07 s đã có 8,71 MiB = 23 × 0,379 MiB. Còn thiếu 0,38 MiB = **đúng 1 đoạn**.
- Tức là: 23/24 range xong trước 2,07 s; **1 range duy nhất** chưa có byte nào và làm cả file chờ thêm 1,43 s.
  Đây không phải "audio tải chậm" (23 range kia xong bình thường) mà là **1 request bị kẹt + engine cứu quá muộn**.

### 1.2 Điều đã chứng minh bằng code + tái hiện (MOCK)

Trong `V9_4_SUB2S.py` (V9.8.1), một range chưa có header chỉ được gửi lại ("takeover") sau
`max(0,7 s ; 1,5 × TTFB trung bình + 0,3 s)`. TTFB càng chậm thì ngưỡng càng muộn. Audio còn có `read timeout = 1,5 s`, và ở
chế độ race "learn:probe" request dự phòng (hedge) được gửi trễ nên timeout của nó cũng trễ.

Tái hiện: audio 9,09 MiB / 24 worker, đợt đầu TTFB ≈ 1,3 s (lạnh + tranh chấp), request sau ≈ 0,25 s, **1 range treo 3 s trên cả 2 host**
(kịch bản `S7_astall_slowttfb`, 8 lượt, video 102,10 MiB tải song song):

| | audio P50 | audio P90 | audio tệ nhất | request cứu |
|---|---|---|---|---|
| A — V9.8.1 | 1967 ms | 1980 ms | **3931 ms** | takeover muộn, rơi tiếp vào host đang treo |
| B — ứng viên (tail-rescue) | 2001 ms | 2020 ms | **2036 ms** | cứu ngay khi các range khác xong, đua cả 2 host |

Cùng thí nghiệm nhưng TTFB nhanh (`S6`): audio A 1211 / 1220 ms → B 1066 / 1081 ms. Không có range treo (`S4`, `S5`): A ≈ B.
→ Engine hiện tại **có thể** biến 1 range kẹt thành ~3,9 s; mức 3,5 s bạn thấy nằm đúng trong vùng này.

### 1.3 Điều CHƯA biết — và cách lấy bằng chứng thật (không cần tải lại)

Chưa biết **vì sao** range đó kẹt trên máy bạn (mất gói lúc bắt tay TLS? host trả header chậm? timeout 1,5 s rồi retry?).
Tool đã tự ghi dòng thời gian **từng HTTP Range** của lần tải đó vào `perf_report\telemetry_*.jsonl` (span `gvs_req`: lúc gửi, lúc
có 302, lúc có header, lúc xong, số byte, host, race/hedge, takeover/retry, kết nối mới hay dùng lại).

`python bench_v99.py autopsy` đọc các file đó và in cho từng lượt tải: range xong cuối là request gì, chờ header bao lâu, tải thân
bao lâu, có request cứu không và phát lúc nào, bao nhiêu request lỗi (vd `error:ReadTimeoutError`), byte tải dư. Đã chạy thử trên
telemetry mock: nó chỉ đúng range treo và request cứu. **Bước 2 của `BENCH_V99_480.bat` làm việc này — gửi lại file `v99_autopsy_*.md`.**

Bản ứng viên ghi thêm cho mỗi range: `dns_ms / tcp_ms / tls_ms` của kết nối mới, số lần retry, thế hệ cứu; và in thẳng vào log
khi đuôi ≥ 300 ms: `🔬 ĐUÔI CHẬM AUDIO: 90% byte lúc …s, xong lúc …s | range xong cuối: … chờ header …ms, tải thân …ms | kết nối mới: dns= tcp= tls=`.

## 2. Thay đổi trong bản ứng viên (đều có cờ `V99_*`; mặc định = hành vi V9.8.1 trừ 2 dòng đầu)

| Cờ | Mặc định | Tác dụng |
|---|---|---|
| `V99_TAIL_RESCUE` | **1** | Giai đoạn đuôi (số đoạn dở ≤ worker/8): đoạn chưa có header quá 1,15 × p90 TTFB đợt đầu → gửi lại ngay, **đua cả 2 host**; request cứu kiên nhẫn theo TTFB kết nối ấm. Tối đa `V99_TAIL_RESCUE_MAX=4` request / file |
| `V99_NET_TRACE` | **1** | Đo DNS / TCP / TLS từng kết nối mới, gắn vào telemetry từng range (vá urllib3 1 lần; vá lỗi → tự bỏ qua) |
| `V99_FORMAT_MODE` | `fixed` | `fixed` = 244 → 135 như V9.8.1. `smart` = chọn format dự đoán xong sớm nhất trong các codec cho phép |
| `V99_ALLOW_VIDEO_CODECS` | `vp9,avc1` | **Không có `av01`** cho tới khi bạn kiểm file AV1 trong workflow TikTok (xem mục 5) |
| `V99_FORCE_VIDEO_ITAG` / `V99_FORCE_AUDIO_ITAG` | rỗng | Chỉ để benchmark từng tổ hợp. Không có đúng itag → coi như không có (không âm thầm đổi). Audio ép vẫn **chỉ** lấy track đã xác nhận tiếng Nhật |
| `V99_OUTPUT_H264` | 0 | 1 = file ra bắt buộc H.264 → smart chỉ chọn itag 135 (tool không chuyển mã) |
| `V99_FORMAT_LEARN` | 1 | Ghi `v99_format_stats.json`: Mbps thật (EMA) + số lần thành công / bị từ chối **theo codec** |
| `V99_AUDIO_WORKERS`, `V99_HEDGE_MAX` | 0, −1 | Chỉ để quét benchmark |

Không đổi: Excel watcher, wake-up, luật audio JA (strict), DIRECT-CUT, 403-GUARD, SAFE-REFRESH, fallback, dual, failover.

## 3. SMART AUTO FASTEST — cơ chế tự học (mặc định TẮT, bật bằng `V99_FORMAT_MODE=smart`)

1. **Phát hiện**: mỗi lần resolve, liệt kê MỌI format video 480p (không HDR) mà nguồn trả về: itag, codec, dung lượng, có link trực tiếp
   không. Áp dụng cho cả nguồn native (visionos / web_embedded) lẫn yt-dlp. Log: `… codec=vp9 chọn=smart:397=1.55s,244=1.93s có=135:avc1:148.61MiB,…`
2. **Dự đoán thời gian xong** của từng ứng viên = `0,35 s + dung lượng ÷ Mbps đã đo của codec đó` `+ 3 s` nếu codec vừa bị từ chối
   trong 30 phút `+ 2,5 s × tỉ lệ lỗi lịch sử`. Chưa có số đo cho codec → dùng trung vị các codec đã đo (hoặc 400 Mbps).
3. **Chọn** ứng viên nhỏ nhất theo dự đoán; hoà → thứ tự đã kiểm chứng (vp9, avc1, av01).
4. **Học** sau mỗi lần tải thật: Mbps tổng (EMA 0,7/0,3; bỏ qua file < 8 MiB vì bị TTFB chi phối), thành công / thất bại theo codec.
5. **Không benchmark trước khi tải**: lần đầu chưa có dữ liệu → với codec cho phép mặc định (`vp9,avc1`) kết quả = 244, tức cấu hình đã kiểm chứng.
6. Chi phí cắt/mux: stream-copy, tỉ lệ với dung lượng → đã nằm trong "file nhỏ hơn thắng". Chi phí chuyển mã: tool **không** chuyển
   mã; nếu cần H.264 đầu ra thì đặt `V99_OUTPUT_H264=1` (chỉ còn 135).

Với video của bạn (135 = 148,61 MiB; 244 = 102,10 MiB; 397 = 78,35 MiB): AV1 nhỏ hơn VP9 **23%**. Ở cùng tốc độ truyền, phần
truyền video ngắn đi cùng tỉ lệ (ví dụ ở 600 Mbps: 1,43 s → 1,10 s). Đây là **phép tính**, chưa phải số đo, và phụ thuộc 2 điều
chưa biết: nguồn native có trả 397 với link trực tiếp không (bước `formats` trả lời), và file AV1 cắt ra có dùng được không (mục 5).

Audio: 139 (9,09 MiB) đã là track JA nhỏ nhất (249 = 9,82; 140 = 24,11; 251 = 25,60) → giữ 139. Audio không nằm trên đường găng khi
không bị kẹt (mục 1).

## 4. Quét RANGE-TURBO (MOCK, bản ứng viên, video 102,10 MiB + audio 9,09 MiB, 4 lượt; READY = cả 2 file xong)

`S4` = giới hạn theo kết nối 16,4 Mbps, link 700 Mbps, 2 host ngang nhau. `S5` = host chính nhanh, replica chậm 1,4 s, link 900 Mbps.
SHA-256 đúng 100%, 0 lỗi ở mọi cấu hình.

| Kết nối video | 32 | 48 | 64 (mặc định) | 80 |
|---|---|---|---|---|
| S4 READY P50 / P90 (ms) | 2030 / 2060 | 1699 / 1703 | 1711 / 1728 | 1689 / 1745 |
| S5 READY P50 / P90 (ms) | 2099 / 2110 | 1639 / 1664 | 1404 / 1416 | 1352 / 1381 |
| request (S4 / S5) | 132 / 146 | 156 / 186 | 180 / 226 | 204 / 266 |

| Worker audio | 8 | 16 | 24 (mặc định) | 32 |
|---|---|---|---|---|
| S4 audio xong (ms) | 1261 | 840 | 696 | 625 |
| S5 audio xong (ms) | 766 | 512 | 473 | 453 |
| S4 READY P50 (ms) — video quyết định | 1681 | 1684 | 1705 | 1690 |
| request (S4) | 120 | 144 | 168 | 192 |

| Hedge tối đa | 0 | 8 | 16 | 40 | luật hiện tại |
|---|---|---|---|---|---|
| S4 READY P50 (ms) | 1704 | 1696 | 1705 | 1712 | 1696 |
| S5 READY P50 / P90 (ms) | 2056 / 2071 | 1611 / 1719 | 1634 / 1654 | 1411 / 1513 | 1425 / 1451 |

| Range đợt đầu | tự động (1,6 MiB) | 1 MiB | 2 MiB | 4 MiB |
|---|---|---|---|---|
| S4 READY P50 (ms) / request | 1696 / 180 | 1990 / 245 | 1700 / 180 | 1717 / 180 |
| S5 READY P50 (ms) / request | 1425 / 226 | 1634 / 297 | 1417 / 226 | 1422 / 226 |

Đọc bảng (chỉ đúng cho mock):
- 32 kết nối chậm rõ; **48 → 80 gần như ngang nhau khi 2 host khoẻ** (S4), nhiều kết nối chỉ thắng khi 1 host chậm (S5). Request tăng đều theo số kết nối → **không có lý do tăng mù quáng lên 80**.
- Worker audio không đổi READY (video quyết định); 8–16 worker bớt 24–48 request, đổi lại audio xong muộn hơn nhưng vẫn trước video.
- Hedge = 0 mất bảo vệ khi 1 host chậm (S5: +630 ms). Luật hiện tại ≈ hedge 40.
- Range 1 MiB chậm hơn và nhiều request hơn; 2 / 4 MiB = tự động.
- HTTP/2: mock không có TLS/ALPN nên **không đo được**; `BENCH_V99_SWEEP.bat` bước 5 đo `httpx` h2 trên link thật. HTTP/3: không có thư viện QUIC trong tool → không thử.
- Kết nối tái sử dụng: mock không có chi phí TLS → không đo được; bước 5 của SWEEP chạy `--warm` (giữ kết nối giữa các lượt) so với lạnh.

**Chưa đổi mặc định nào từ bảng này** — cần số live.

## 5. Pipeline cắt video — kiểm bằng ffmpeg thật (CỤC BỘ)

`python bench_v99.py cutlab`: tạo nguồn 854×480 24 fps (VP9 / AV1 / H.264) + audio (AAC / Opus), chạy **nguyên pipeline của tool**
(resolve giả → tải qua máy chủ cục bộ → DIRECT-CUT hoặc MUX → MOVE → STATUS), rồi kiểm **từng part**: có video + audio, gói đầu là
keyframe, lệch A/V, **giải mã toàn bộ**, đếm khung hình. ffmpeg 6.1.1, `cutsec=71`.

| Tổ hợp | Nguồn | Nhánh | STATUS | Part OK | Codec ra | Mã hoá lại? | Khung giải mã / mong đợi | Khung thừa tối đa / part | Part có timestamp lùi | Lệch A/V | Lỗi giải mã | Part thiếu audio |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| VP9 + AAC | 330 s | DIRECT-CUT | DONE | 4/4 | vp9 + aac (mp4) | không | 6672 / 6672 | 0 | 0 | 0 ms | 0 | 0 |
| **AV1 + AAC** | 330 s | DIRECT-CUT | DONE | 4/4 | av1 + aac (mp4) | không | **6864 / 6672** | **96** | **3/4** | 0 ms | 0 | 0 |
| H.264 + AAC | 330 s | DIRECT-CUT | DONE | 4/4 | h264 + aac (mp4) | không | 6672 / 6672 | 0 | 0 | 0 ms | 0 | 0 |
| VP9 + Opus | 330 s | DIRECT-CUT | DONE | 4/4 | vp9 + opus (mp4) | không | 6672 / 6672 | 0 | 0 | 1 ms | 0 | 0 |
| **AV1 + Opus** | 330 s | DIRECT-CUT | DONE | 4/4 | av1 + opus (mp4) | không | **6864 / 6672** | **96** | **3/4** | 1 ms | 0 | 0 |
| cả 5 tổ hợp | 120 s | MUX | DONE | 1/1 | giữ codec gốc | không | 2880 / 2880 | 0 | 0 | 0 ms | 0 | 0 |
| VP9 / AV1 / H.264 + AAC | 40 s | MUX + nối đuôi cho đủ ≥ 60 s | DONE | 1/1 | giữ codec gốc | không (stream-copy) | 1560 / 1560 | 0 | 0 | 23 ms | 0 | 0 |

Trả lời từng câu hỏi:

- **File ra có video + audio không?** Có, 100% part ở mọi tổ hợp; audio đúng track đưa vào. (Trên link thật, track đó là track JA do
  luật strict chọn — `live480` in `a_lang` của từng part.)
- **Có phải mã hoá lại AV1/VP9 sang H.264 không?** **Không.** Tool chỉ stream-copy (`-c copy`): codec ra = codec vào. Ngoại lệ duy nhất
  trong code: video < 60 s, nếu bước nối đuôi bằng stream-copy thất bại thì mới fallback `libx264 ultrafast` (trong thử nghiệm: không xảy ra). Nếu workflow
  cần H.264 thì chỉ itag 135 đáp ứng, và 135 là file LỚN nhất (148,61 MiB).
- **AV1 (397) cắt trực tiếp được không?** Cắt được, file phát được, không lỗi giải mã — **nhưng không sạch**: 3/4 part (mọi part không
  bắt đầu đúng keyframe) lộ ra tới 96 khung (4 s) "pre-roll" trước điểm cắt và ffmpeg báo timestamp lùi. Nguyên nhân: lệnh cắt dùng
  `-ss` trước `-i` + `-c copy` nên file bắt đầu từ keyframe đứng trước; với VP9/H.264 ffmpeg giấu đoạn đó bằng edit list, với AV1
  (ffmpeg 6.1.1) thì không. Trình phát khác có thể xử lý khác → **phải kiểm trong TikTok trước khi cho phép `av01`**. Bản ffmpeg của
  bạn (2025-07) có thể khác → chạy `cutlab` trên máy bạn.
- **Mất khung hình / lệch tiếng?** Với VP9/H.264: số khung giải mã = đúng thời lượng part × fps; lệch A/V ≤ 1 ms.
  Nhưng **theo thiết kế cũ của hàm cắt**, tổng thời lượng ra < nguồn: bỏ 6 s đầu, part 2 trở đi lùi +2 s (mất 2 s giữa part 1 và 2), và
  **phần dư cuối ngắn hơn `cutsec` không được xuất** (`num_parts = thời lượng // cutsec`). Nguồn 330 s → 4 part = 278 s. Video 1608 s /
  81 s của bạn → 19 part = 1533 s (thiếu ~75 s: 6 s đầu + 2 s giữa part 1–2 + ~67 s cuối). Mình **không sửa** phần này (không được yêu cầu, và là hành vi
  bạn đang dùng) — chỉ báo để bạn biết.
- **Thời gian cắt 19 part?** Chưa đo được trên máy bạn. Log của bạn: `PIPELINE TOTAL − HOT READY` = 0,44–0,72 s cho 15–33 part (gồm cả
  MOVE và ghi Excel). `live480` in riêng cột `CUT/MUX` và chỉ tính OK khi **mọi part** đã được kiểm.
- **Phát được trong trình xem / TikTok?** Chỉ kiểm được bằng ffmpeg/ffprobe (giải mã toàn bộ). TikTok: **chưa kiểm, không tuyên bố**.

## 6. Bảng benchmark tất cả format (LIVE) — **CHƯA CÓ, chờ máy bạn**

`BENCH_V99_480.bat` bước 4–5 tạo `perf_report\v99_formats_*.md` và `v99_live480_combos_*_summary.md` với đúng các cột yêu cầu:
resolve / video / audio / HOT READY / cut-mux / TOTAL (median + p95), số GET video + audio, retry, 403, codec + container file ra,
part OK. Tổ hợp: A (V9.8.1 hiện tại), 244+139, 397+139, 397+249, 244+249, 135+139; thêm `--smart` để đo chế độ tự chọn.
Thứ tự ngẫu nhiên, nghỉ 4 s, 429 → dừng. MiB = 1.048.576 byte, Mbps = megabit/giây.

## 7. Cặp video + audio nhanh nhất đã xác minh

**Chưa xác minh được cặp nào bằng số live.** Theo dung lượng thì ứng viên là 397 + 139 (87,44 MiB) so với 244 + 139 (111,19 MiB);
nhưng 397 chỉ được dùng nếu (a) nguồn native trả link trực tiếp, (b) `live480` cho HOT READY / TOTAL thấp hơn, (c) file AV1 cắt ra
chạy được trong workflow của bạn.

## 8. So sánh trước / sau bằng số thật — **CHƯA CÓ, chờ máy bạn**

Baseline bạn đưa: video 244 = 2,707 s (64 worker, 176 GET), audio 139 = 3,500 s (24 worker, 48 GET), HOT READY 3,707 s, TOTAL 4,103 s.
HOT READY ở lượt đó = lúc **audio** xong (3,5 s), không phải video (2,7 s) → sửa đuôi audio là thứ kéo HOT READY xuống gần 2,9 s
(= resolve + video), còn muốn dưới 2 s thì phải giảm cả thời gian video (ít byte hơn hoặc Mbps cao hơn).

## 9. yt-dlp: CMD báo 2026.07.04, log tool báo 2026.08.19

Cả hai đều đúng, vì là **hai bản cài khác nhau**:
- Tool lấy link bằng `import yt_dlp` — module trong Python đang chạy tool. Dòng `⚡ V6 WARM: yt-dlp=…` in `yt_dlp.version.__version__`
  của module đó (hàm `_youtube_runtime_summary`). → **Bản tool thật sự dùng là 2026.08.19.**
- `yt-dlp --version` trong CMD chạy file `yt-dlp.exe` tìm thấy trên PATH (hoặc cạnh tool) — tool không dùng file này để lấy link.

`python bench_v99.py env` in cả hai kèm đường dẫn. Bản ứng viên in thêm lúc START: `🔎 yt-dlp MODULE tool đang dùng: … @ đường_dẫn | python=…`.
Cập nhật bản mà tool dùng: `python -m pip install -U yt-dlp` (đúng python đó).

## 10. Kiểm thử

- Bản chính V9.8.1 (không đổi code): **__MAIN__**.
- Bản ứng viên (`set V94_TEST_MODULE=V9_9_CANDIDATE`): **__CAND__** — gồm toàn bộ test cũ (Excel, wake-up, JA strict, DIRECT-CUT,
  403-GUARD, dual, failover…) chạy trên bản ứng viên + 17 test mới: tail-rescue (đúng từng byte, có trần request, không thêm request khi
  khoẻ, 403 vẫn lỗi cứng), telemetry từng range, chọn format (fixed giữ 244; smart mặc định không bao giờ chọn AV1; học tốc độ và
  lỗi; ép itag đúng hoặc không có; `OUTPUT_H264`), yt-dlp bundle, **ép audio không bao giờ lấy track không phải tiếng Nhật**.

## 11. Việc bạn cần làm (theo thứ tự)

1. Giải nén vào thư mục tool. `V9_4_SUB2S.py` trong zip **giống hệt** bản bạn đang chạy.
2. Chạy `BENCH_V99_480.bat` (bước 1–3 không cần mạng; bước 4–5 cần `urls.txt`, nên có `https://www.youtube.com/watch?v=hOk-H2PoKq0`).
3. Gửi lại `perf_report\v99_*.md` + `.csv`. Quan trọng nhất: `v99_autopsy_*.md` (nguyên nhân thật của audio 3,5 s),
   `v99_formats_*.md` (native có 397 không), `v99_live480_combos_*_summary.md` (bảng mục 6), `v99_cutlab_*.md` (ffmpeg của bạn cắt AV1 thế nào).
4. Muốn quét worker/hedge/range trên link thật: `BENCH_V99_SWEEP.bat`, chạy từng bước, nghỉ vài phút giữa các bước.
5. Tự mở 1 file part AV1 (nếu `formats` cho thấy có 397) trong trình xem và thử trong workflow TikTok trước khi bật `av01`.
