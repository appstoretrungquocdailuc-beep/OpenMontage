# V9.8.1-SUB2S — TỐI ƯU TẢI 480P (video 244 + audio JA) — 2026-11-17 (mục 0 = mới nhất; mục 1–10 = bản 16/11)

> **Đọc trước:** mọi số trong mục 4–6 là **SỐ MOCK** — máy chủ GoogleVideo GIẢ (127.0.0.1, chạy ở tiến trình riêng,
> mô phỏng theo log thật của bạn). Máy thử ở đây **không tải được googlevideo**. Số mock chứng minh **độ đúng** và **tác
> dụng tương đối** của từng thay đổi, KHÔNG phải tốc độ YouTube trên máy bạn. Chưa có cơ sở để nói "đã đạt ≤ 1,5 s" —
> chỉ `BENCH_V98_480.bat` chạy trên máy bạn trả lời được (mục 7).

## 0. CẬP NHẬT V9.8.1 (17/11) — sau khi đọc log THẬT của bạn

**Log thật (V9.8, Windows) nói gì**

| Video | Dung lượng | Thời gian video | Tốc độ truyền | Mỗi kết nối | TTFB | Chẻ đoạn |
|---|---|---|---|---|---|---|
| gQT1bf3iPHw | 145,7 MB | 4,70 s | 307 Mbps | 15,8 Mbps | 0,65 s | 53 |
| V98lX3y8D3w | 200,7 MB | 4,25 s | 542 Mbps | 23,3 Mbps | 1,01 s | 59 |
| QI8YPV9pnVo | 158,3 MB | 2,80 s | **694 Mbps** | 70,3 Mbps | 0,64 s | 36 |
| Zfi4nk_EWyM | 83,5 MB | 2,85 s | 325 Mbps | 16,4 Mbps | 0,66 s | 49 |

- Các video này nặng **83–200 MB**, không phải 40–60 MB của mục tiêu ≤ 1,5 s. 158 MB ở 694 Mbps (gần trần đường truyền đo
  được) cần tối thiểu ≈ 1,9 s truyền + 0,64 s TTFB + 0,2 s resolve ≈ 2,75 s (thực tế 2,99 s). Muốn 158 MB trong 1–2 s cần
  > 1,3 Gbps — **không downloader nào** làm được trên đường truyền này.
- Video 83 MB thì engine **lãng phí thật** (2,85 s trong khi ~2,0 s là khả thi): 3 lỗi dưới đây.

**3 lỗi tìm ra từ log thật — đã sửa**

| # | Lỗi | Bằng chứng trong log | Sửa (cờ rollback) |
|---|---|---|---|
| 1 | Mở 64 kết nối (32 host chính + 32 replica để race) nhưng **32 kết nối thua bị đóng bỏ**; chỉ 32 tải dữ liệu, mỗi cái ~16 Mbps | `conc_peak=64 … race=32 … alt_lost=32/0/0`, `conn_p50≈16.4Mbps` | **dual**: chia 2N đoạn, chẵn → host chính, lẻ → replica; vẫn 2N kết nối nhưng TẤT CẢ đều tải. Chỉ video; audio giữ `learn`. (`V98_RACE_MODE=learn` hoặc `fixed`) |
| 2 | Tốc độ mỗi đoạn tính bằng byte ÷ (thời gian **kể cả lúc chờ máy chủ**) → đoạn vừa bắt đầu bị coi là chậm → chẻ đôi thừa, mỗi lần chẻ tốn thêm 1 vòng TTFB ~0,2 s | `splits=49 steal=55`; 8 MB cuối mất 0,79 s (75,22 MB lúc 2,06 s → xong lúc 2,85 s) | ETA tính từ lúc có header (`V98_ETA_FIRST_BYTE=0` để quay về) |
| 3 | Cập nhật tiến độ gọi `path.resolve()` (syscall) cho **mỗi khối 128 KiB ngay trong khoá chung** | video 200 MB: `lock_hold=1430ms` nhưng `write=168ms`; `lock_wait=8565ms` | key tính 1 lần/job. **Đã xác nhận trên máy bạn**: log 20:13 `lock_hold=197ms write=189ms` |

Ngoài ra: worker tự học trước đây so `xfer_mbps` (không thấy TTFB) → nay so Mbps tổng; khi dual bật thì không cộng thêm mức "hi".

**MOCK A/B bản V9.8.1 mặc định so với V9.7.1** (R8, 6 vòng; số MOCK — KHÔNG phải YouTube; SHA-256 đúng 100%, 0 lỗi)

| Kịch bản | Video 49,2 MiB: V9.7.1 → V9.8.1 (P50 / P90, ms) | request | Video 83,5 MiB: V9.7.1 → V9.8.1 (P50 / P90, ms) | request |
|---|---|---|---|---|
| S1 giới hạn theo kết nối (14 Mbps, link 600) | 1953 / 1968 → **1233 / 1236** | 102 → 90 | 2107 / 2161 → **1679 / 1690** | 168 → 146 |
| S4 theo log 17/11 (16,4 Mbps, link 700) | 1657 / 1703 → **1008 / 1017** | 102 → 102 | 1811 / 1940 → **1451 / 1455** | 168 → 168 |
| S2 giới hạn đường truyền 380 Mbps | 1638 / 1657 → 1584 / 1592 | 102 → 90 | 2415 / 2470 → 2369 / 2379 | 168 → 146 |
| S3 host chính chậm, replica nhanh | 1762 / 1799 → **1474 / 1480** | 102 → 122 | 1977 / 2006 → **1554 / 1562** | 168 → 200 |

- Riêng sửa ETA (dual có/không): S3 49 MiB 1590 / 1663 → 1474 / 1480, S3 83 MiB 1645 / 1850 → 1554 / 1562, request 217 → 200;
  các kịch bản khác P50 ngang nhau, P90 thấp hơn hoặc bằng.
- Khi đường truyền là giới hạn (S2) dual chỉ lợi nhờ prealloc nền — đúng như dự đoán: thêm kết nối không tạo thêm băng thông.
- S3: request tăng ~20% (hedge sang host nhanh) — đổi lại nhanh hơn 16–21%.

**aria2c — vì sao không dùng làm mặc định, và cách tự kiểm**

- aria2c giới hạn CỨNG **16 kết nối / máy chủ** (`max-connection-per-server must be between 1 and 16` — đã thử trên 1.37.0).
  Với ~16 Mbps/kết nối như log 83 MB → trần ≈ 260 Mbps, thấp hơn 325 Mbps tool đang đạt.
- Số aria2 thật của chính bạn (360pok): 101,1 MB trong 2,91 s ≈ 278 Mbps. V9.8 vừa tải 158,3 MB trong 2,80 s ≈ 475 Mbps.
- Lỗi cũ tìm thấy: `_aria2_tuned_split` xin 20/24 kết nối cho file > 48 MiB → aria2 từ chối → luôn rơi về aria2c kiểu cũ. Đã chặn ở 16.
- **Tự so trên máy bạn:** trong `RUN_V9_8_GUI.bat` bỏ `REM` ở dòng `set V98_FORCE_ARIA2=1`. Log sẽ có
  `[ARIA2-RPC] ✅ VIDEO | …MB | …s | ≈…Mbps | ttfb=… | kết nối thật=N/16`. 403/429: `max-tries=1`, không thử lại cùng link;
  lỗi khác → tự quay về RANGE-TURBO. Tải 3–4 video mỗi chế độ rồi so dòng `HOÀN TẤT VIDEO` / `HOT 480P READY`.

**Cần bạn gửi lại:** vài dòng log `[RANGE-TURBO] ✅ HOÀN TẤT VIDEO` của bản V9.8.1 (có `race=dual`), tốt nhất gồm 2–3 video
**40–60 MB**. Nhìn `workers=`, `xfer≈`, `ttfb=`, `splits=`: nếu `xfer` tăng rõ so với bảng trên mà `ttfb` không tăng → dual
có tác dụng thật; nếu `ttfb` tăng → đặt `V98_DUAL_MAX_CONNS=48`.

## 1. Đường chạy 480p (đã đọc code)

```
_process_single_url (480hard)
 ├─ _BUNDLE_RESOLVE_EXECUTOR: _fast_innertube_480 (visionos + web_embedded song song, 244 trước, 135 dự phòng)
 │   └─ + yt-dlp race (_YTDLP_RESOLVER_EXECUTOR)                         ← log thật: 142/144 ms
 ├─ add_candidate → 244 READY → _submit_media_dedicated(_video_job)      (luồng riêng, không chờ audio)
 │                 → JA AUDIO READY → _submit_media_dedicated(_audio_job) (JA nghiêm ngặt, không EN/unknown)
 │   └─ _download_http_range_turbo → _download_http_range_turbo_v94
 │        ├─ _range_worker_count (49 MiB → 20; audio 5 MiB → 14)
 │        ├─ mở file + PREALLOC (truncate)       ← log thật 96 ms, TRƯỚC GET đầu (V9.7.1)
 │        ├─ đợt đầu: N đoạn bằng nhau, FIRST-WAVE-RACE: mỗi đoạn bắn kép host chính (302) + replica mn
 │        │    _open_hedged → _v94_range_request → _get_direct_media_pool (urllib3, 40 pool × 160)
 │        ├─ đọc 128 KiB → [khoá chung] seek+write → work-stealing / split / takeover
 │        └─ Content-Range kiểm chặt, co/giãn tại chỗ, 403/416/429 an toàn
 ├─ _v94_maybe_prewarm (ffmpeg 59,7 ms / ffprobe 270 ms ở luồng nền, job không chờ)
 └─ DIRECT-CUT (>300 s) hoặc MUX → KPI: HOT 480P READY / PIPELINE TOTAL
```

Log thật của bạn (fJ8--LQXd-Q): HOT READY 2,120 s ≈ resolve 0,144 + video 1,976 s → **video 244 là đường găng**;
audio (1,249 s) xong trước video ~0,7 s nên **không** nằm trên đường găng.

## 2. Nút thắt (có bằng chứng)

| # | Nút thắt | Bằng chứng | Xử lý V9.8 |
|---|---|---|---|
| 1 | **Preallocation trên đường găng**: `truncate` (Windows `_chsize` ghi số 0) chạy XONG mới gửi GET đầu | log thật `prealloc 96 ms`; code: `fh.truncate(total0)` trước `_spawn_workers_locked` | `V98_PREALLOC_ASYNC`: truncate ở luồng nền, GET đi ngay; lần ghi đầu / co-giãn / đóng file chờ nó xong |
| 2 | **Request thừa**: FIRST-WAVE-RACE bắn kép mọi đoạn đợt đầu, replica không bao giờ thắng | log thật video `20 race / alt-win 0`, audio `14 race / alt-win 0`; conc peak 40 | `V98_RACE_MODE=learn`: nhớ job gần đây; replica không thắng → chỉ 2 đoạn thăm dò; replica thắng → race hết như cũ |
| 3 | **Băng thông mỗi kết nối thấp** (giới hạn theo kết nối hay theo đường truyền?) | log thật: video 20 kết nối ≈ 268 Mbps (≈13 Mbps/kết nối), audio 14 ≈ 103 Mbps (≈7 Mbps/kết nối) | `V98_WORKERS_LEARN`: luân phiên 20 ↔ 32 kết nối, đo Mbps thật, giữ mức nhanh hơn; `linkcap` đo trần đường truyền |
| 4 | Khoá chung cho cả lập lịch lẫn ghi đĩa | mock Linux: lock_wait 1–17 ms / ~400 lần ghi → **không phải nút thắt trên Linux**; Windows chưa đo | `V98_WRITE_MODE=split/pwrite` (đúng offset, có nhánh handle riêng cho Windows) — chỉ bật nếu bước 1 của BAT trên Windows chứng minh |

(Các mục khác đã kiểm: TTFB audio 770 ms không nằm trên đường găng; prewarm ffmpeg/ffprobe chạy ở tiến trình riêng — có
biến thể `noprewarm` trong `live480` để đo; DNS/TCP/TLS từng kết nối do `NetTrace` của bench đo trên máy bạn.)

## 3. Thay đổi (đều có cờ, đặt biến môi trường để quay về V9.7.1)

| Cờ | Mặc định V9.8 | Quay về V9.7.1 | Ghi chú |
|---|---|---|---|
| `V98_PREALLOC_ASYNC` | `1` | `0` | mock (mô phỏng 1,95 ms/MiB như log thật): −50…−80 ms P50 **và** P90 ở cả 3 kịch bản; GET đầu đi ở ~20 ms thay vì ~100 ms |
| `V98_RACE_MODE` | `dual` (từ 17/11; 16/11: `learn`) | `fixed` | `adaptive` (luôn chỉ thăm dò) bị LOẠI: P90 +153 ms khi CDN chính chậm |
| `V98_WORKERS_LEARN` | `1` | `0` | mức "hi" = 20 × 1,6 = 32 (≤ `V98_SCALE_MAX`); file < 8 MiB và khi đã ép số worker → không áp dụng |
| `V98_WRITE_MODE` | `lock` | `lock` | `split`/`pwrite` không nhanh hơn trên Linux; chờ số Windows |
| `V98_SCALE_UP` | `0` | `0` | LOẠI: P90 tăng (S1 1946→2035 ms) + 20 request |
| `V98_PART_MIB` | `0` | `0` | LOẠI: đoạn đầu nhỏ chậm hơn rõ (0,5/1/2 MiB: +450…+830 ms, request 102→125–181; 4 MiB = không đổi) |
| `V98_ARIA2_SPLIT` / `V98_ARIA2_MAX_TRIES` | `0` (=cũ) / `2` (=cũ) | | chỉ bench dùng; downloader sản xuất KHÔNG đổi |

Đặt cờ trên Windows: chạy GUI bằng `RUN_V9_8_GUI.bat` — bỏ `REM` ở dòng `set V98_...` muốn quay về (đã ghi sẵn).
Bản V9.7.1 nguyên vẹn: **chép** `backup\V9_7_1_SUB2S_truoc_v98.py` ra thư mục tool rồi chạy file đó (ffmpeg\, aria2c.exe, config được tìm theo thư mục chứa file .py).
Log khi bấm START có dòng `⚙️ V9.8 RANGE: prealloc=… | ghi=… | race=… | worker=… | scale-up=…`; mỗi lần tải có thêm
`race=learn:probe|learn:all`, `wl=lo|hi`, `prealloc_bg=…ms`, `lock_wait=… lock_hold=… write=…ms/Nx(mode)`.

## 4. Kết quả A/B (MOCK — xem cảnh báo đầu trang)

Mô phỏng theo log thật của bạn: video 49,20 MiB + audio 5,21 MiB tải **song song** như production; host chính trả 302 rồi
mới trả dữ liệu; replica mn chậm hơn (alt-win 0 như log thật); preallocation Windows giả lập 1,95 ms/MiB (= 96 ms/49 MiB).
Máy chủ giả chạy ở **tiến trình riêng** (GIL của nó không làm méo số đo). Mỗi lượt kiểm **SHA-256** cả 2 file.
Thứ tự cấu hình **ngẫu nhiên** mỗi vòng. `P50/P90` = lúc CẢ video lẫn audio xong; `RESOLVE_MS = 0` (mock không có resolver);
`HOT_READY_MS / PIPELINE_TOTAL_MS = -` vì mock không chạy DIRECT-CUT/MUX (số thật: chế độ `live480`).

3 kịch bản: **S1** YouTube giới hạn THEO KẾT NỐI (14 Mbps video / 7,4 Mbps audio mỗi kết nối, đường truyền 600 Mbps) —
khớp log thật (≈13 Mbps/kết nối); **S2** giới hạn ĐƯỜNG TRUYỀN 380 Mbps; **S3** CDN chính chậm (302 + 650 ms), replica nhanh.

### 4.1 TRƯỚC / SAU — bản V9.8 mặc định (R6, 10 vòng mỗi kịch bản, ĐÃ TÍNH cả 4 lượt "học" đầu tiên)

| SCENARIO | ENGINE | WORKERS_VIDEO | WORKERS_AUDIO | HEDGE | FILE_MB | RESOLVE_MS | TTFB_MS | VIDEO_MS | AUDIO_MS | HOT_READY_MS | PIPELINE_TOTAL_MS | P50 | P90 | REQUESTS | ERRORS | SHA256_OK |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S1_perconn | B — V9.8 MẶC ĐỊNH (prealloc nền + race learn + worker tự học) | auto | auto | -1 | 54 | 0 | 345 | 1309 | 752 | 1310 | - | 1310 | 1859 | 96 | 0 | 10/10 |
| S1_perconn | B — prealloc nền + race learn | auto | auto | -1 | 54 | 0 | 342 | 1852 | 753 | 1852 | - | 1852 | 1857 | 72 | 0 | 10/10 |
| S1_perconn | B — chỉ prealloc nền | auto | auto | -1 | 54 | 0 | 344 | 1855 | 750 | 1855 | - | 1855 | 1866 | 102 | 0 | 10/10 |
| S1_perconn | A — V9.7.1 (TRƯỚC) | auto | auto | -1 | 54 | 0 | 424 | 1935 | 756 | 1935 | - | 1935 | 1952 | 102 | 0 | 10/10 |
| S2_linkcap | B — prealloc nền + race learn | auto | auto | -1 | 54 | 0 | 339 | 1542 | 619 | 1543 | - | 1543 | 1549 | 72 | 0 | 10/10 |
| S2_linkcap | B — V9.8 MẶC ĐỊNH (prealloc nền + race learn + worker tự học) | auto | auto | -1 | 54 | 0 | 342 | 1561 | 626 | 1561 | - | 1561 | 1587 | 72 | 0 | 10/10 |
| S2_linkcap | B — chỉ prealloc nền | auto | auto | -1 | 54 | 0 | 344 | 1565 | 630 | 1566 | - | 1566 | 1592 | 102 | 0 | 10/10 |
| S2_linkcap | A — V9.7.1 (TRƯỚC) | auto | auto | -1 | 54 | 0 | 425 | 1632 | 573 | 1632 | - | 1632 | 1636 | 102 | 0 | 10/10 |
| S3_mainslow | B — V9.8 MẶC ĐỊNH (prealloc nền + race learn + worker tự học) | auto | auto | -1 | 54 | 0 | 193 | 1155 | 588 | 1156 | - | 1156 | 1675 | 138 | 0 | 10/10 |
| S3_mainslow | B — chỉ prealloc nền | auto | auto | -1 | 54 | 0 | 178 | 1668 | 581 | 1669 | - | 1669 | 1677 | 102 | 0 | 10/10 |
| S3_mainslow | B — prealloc nền + race learn | auto | auto | -1 | 54 | 0 | 176 | 1669 | 582 | 1669 | - | 1669 | 1676 | 102 | 0 | 10/10 |
| S3_mainslow | A — V9.7.1 (TRƯỚC) | auto | auto | -1 | 54 | 0 | 256 | 1750 | 587 | 1751 | - | 1751 | 1757 | 102 | 0 | 10/10 |

- **S1 (giới hạn theo kết nối, giống log thật): P50 1935 → 1310 ms (−32%)**. P90 1952 → 1859 ms (P90 là các lượt còn
  thử mức 20 worker; sau giai đoạn học, cứ 10 job mới thử lại mức thua 1 lần).
- **S2 (giới hạn đường truyền): 1632 → 1561 ms**, request 102 → 72 (worker tự học tự quay về 20 vì 32 không nhanh hơn).
- **S3 (CDN chính chậm): 1751 → 1156 ms**; race learn tự chuyển về race tất cả (replica thắng) → không mất bảo vệ P90.
- 0 lỗi, SHA-256 đúng 120/120 lượt.

### 4.2 Từng cờ riêng lẻ (R1, 8 vòng × 3 kịch bản, video 20 / audio 14 worker cố định)

| Cờ | S1 P50 / P90 | S2 P50 / P90 | S3 P50 / P90 | Request | Kết luận |
|---|---|---|---|---|---|
| A V9.7.1 | 1938 / 1941 | 1634 / 1635 | 1751 / 1774 | 102 | mốc |
| B cờ tắt | 1937 / 1946 | 1635 / 1636 | 1751 / 1754 | 102 | = A (rollback đúng) |
| `PREALLOC_ASYNC` | **1859 / 1866** | **1581 / 1585** | **1671 / 1699** | 102 | ✅ GIỮ |
| `WRITE_MODE=split` | 1943 / 1947 | 1634 / 1641 | 1751 / 1754 | 102 | không lợi trên Linux |
| `WRITE_MODE=pwrite` | 1935 / 1944 | 1633 / 1636 | 1752 / 1759 | 102 | không lợi trên Linux |
| `SCALE_UP` | 1923 / **2035** | 1624 / **1664** | 1588 / 1595 | 122 | ❌ LOẠI (P90 tăng, +20 request) |
| `RACE_MODE=adaptive` | 1938 / 1963 | 1613 / 1627 | **1904** / 1907 | 72 | ❌ LOẠI (S3 +153 ms) → thay bằng `learn` |

Khoá: lock_wait video 1–17 ms, lock_hold ~55 ms, thời gian ghi ~30 ms cho 49 MiB / ~400 lần ghi → khoá **không** phải nút
thắt trên Linux. Windows (NTFS, Defender) có thể khác → bước 1 của BAT đo đúng điều này trên máy bạn.

### 4.3 Số worker video (R2a) — KHÔNG mặc định "32 > 16"

| Worker video | 8 | 12 | 16 | 20 | 24 | 32 |
|---|---|---|---|---|---|---|
| S1 theo kết nối P50 / P90 | 4132 / 4151 | 2899 / 2909 | 2304 / 2311 | 1944 / 1954 | 1715 / 1716 | **1377 / 1395** |
| S2 theo đường truyền P50 / P90 | 1766 / 1767 | 1629 / 1635 | 1620 / 1622 | 1633 / 1641 | 1622 / 1630 | 1605 / 1611 |
| Request (video+audio) | 66 | 78 | 90 | 102 | 114 | 138 |

→ Nhiều kết nối chỉ có lợi khi YouTube giới hạn theo kết nối; khi giới hạn là đường truyền thì 12–32 như nhau (chỉ tốn
request). Không đoán trước được mạng bạn thuộc loại nào → **worker tự học** đo thật từng job rồi chọn.

### 4.4 Số worker audio (R2b): 4 / 8 / 12 / 14 / 16

READY gần như không đổi với 8–16 (S1 1933–1944, S2 1603–1631 ms) vì audio xong trước video; 4 worker làm S2 chậm hơn
(1668 ms). → **giữ nguyên** mặc định audio (14 cho 5 MiB). Audio TTFB 770 ms trong log thật **không** nằm trên đường găng.

### 4.5 FIRST-WAVE-RACE (R3): −1 (tất cả) / 0 / 2 / 4 / adaptive

| Race | S1 P50 / P90 | S2 P50 / P90 | S3 P50 / P90 | Request S1/S2 |
|---|---|---|---|---|
| −1 (V9.7.1) | 1942 / 1971 | 1633 / 1638 | **1749 / 1764** | 102 |
| 0 | 1941 / 1945 | 1604 / 1631 | 2089 / 2133 | 102 |
| 2 | 1941 / 1947 | 1630 / 1641 | 1934 / 1938 | 102 |
| 4 | 1937 / 1957 | 1634 / 1643 | 1935 / 1939 | 102 |
| adaptive | 1936 / 1947 | 1614 / 1630 | 1903 / 1908 | **72** |

→ Race 0/2/4 KHÔNG bớt request (hedge sau 150 ms vẫn bắn sang replica) mà còn chậm hơn khi CDN chính kém. Chỉ "chờ host chính,
chỉ hedge khi replica đã thắng" mới bớt request; nhưng luôn làm vậy thì mất bảo vệ ở S3 → **`learn`**: dựa vào job gần đây
(R6: S1/S2 request 102→72 mà không chậm hơn; S3 giữ race tất cả 1669 ms).

### 4.6 Kích thước đoạn đầu (R4): 0 (=tổng/worker ≈ 2,46 MiB) / 0,5 / 1 / 2 / 4 MiB

S1: 1941 / **2769 / 2564 / 2576** / 1958 ms; S2: 1633 / **2286 / 2085 / 2123** / 1636 ms; request 102 / 181 / 141 / 125 / 102.
→ đoạn nhỏ = thêm vòng TTFB (302 + chờ máy chủ) cho mỗi đoạn → **loại**, giữ mặc định.

## 5. So sánh engine (R5, MOCK, cùng file, 4 vòng)

| SCENARIO | ENGINE | WORKERS_VIDEO | WORKERS_AUDIO | HEDGE | FILE_MB | RESOLVE_MS | TTFB_MS | VIDEO_MS | AUDIO_MS | HOT_READY_MS | PIPELINE_TOTAL_MS | P50 | P90 | REQUESTS | ERRORS | SHA256_OK |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S1_perconn | A RANGE-TURBO V9.7.1 | 20 | 14 | -1 | 54 | 0 | 425 | 1932 | 762 | 1933 | - | 1933 | 1936 | 102 | 0 | 4/4 |
| S1_perconn | B (cờ V9.8 tắt) | 20 | 14 | -1 | 54 | 0 | 426 | 1944 | 756 | 1945 | - | 1945 | 1959 | 102 | 0 | 4/4 |
| S1_perconn | D / httpx-h2 / n=16 | 16 | 16 | - | 54 | 0 | 326 | 2480 | 908 | 2480 | - | 2480 | 2481 | 36 | 0 | 4/4 |
| S1_perconn | C / aria2rpc / split=16 | 16 | 16 | - | 54 | 0 | 326 | 3042 | 1783 | 3043 | - | 3043 | 3084 | 46 | 0 | 4/4 |
| S1_perconn | C / aria2rpc / split=12 | 12 | 12 | - | 54 | 0 | 326 | 3607 | 1788 | 3608 | - | 3608 | 3686 | 46 | 0 | 4/4 |
| S1_perconn | D / httpx-h2 / n=8 | 8 | 8 | - | 54 | 0 | 326 | 4321 | 1285 | 4321 | - | 4321 | 4483 | 20 | 0 | 4/4 |
| S1_perconn | C / aria2rpc / split=8 | 8 | 8 | - | 54 | 0 | 326 | 4887 | 1786 | 4887 | - | 4887 | 4918 | 30 | 0 | 4/4 |
| S2_linkcap | B (cờ V9.8 tắt) | 20 | 14 | -1 | 54 | 0 | 425 | 1631 | 578 | 1632 | - | 1632 | 1659 | 102 | 0 | 4/4 |
| S2_linkcap | A RANGE-TURBO V9.7.1 | 20 | 14 | -1 | 54 | 0 | 425 | 1634 | 587 | 1634 | - | 1634 | 1638 | 102 | 0 | 4/4 |
| S2_linkcap | D / httpx-h2 / n=16 | 16 | 16 | - | 54 | 0 | 327 | 1764 | 736 | 1764 | - | 1764 | 1777 | 36 | 0 | 4/4 |
| S2_linkcap | D / httpx-h2 / n=8 | 8 | 8 | - | 54 | 0 | 327 | 1940 | 836 | 1940 | - | 1940 | 1958 | 20 | 0 | 4/4 |
| S2_linkcap | C / aria2rpc / split=12 | 12 | 12 | - | 54 | 0 | 327 | 1998 | 1125 | 1998 | - | 1998 | 2030 | 45 | 0 | 4/4 |
| S2_linkcap | C / aria2rpc / split=16 | 16 | 16 | - | 54 | 0 | 326 | 2006 | 1140 | 2007 | - | 2007 | 2012 | 47 | 0 | 4/4 |
| S2_linkcap | C / aria2rpc / split=8 | 8 | 8 | - | 54 | 0 | 326 | 2245 | 1132 | 2246 | - | 2246 | 2259 | 29 | 0 | 4/4 |

- **C (aria2 RPC daemon)** chậm hơn rõ trên mock: aria2 mở kết nối dần dần (máy chủ chỉ thấy 3–12 kết nối thật) và mỗi
  kết nối tự đi lại chuỗi 302. → **không** chuyển sang aria2. `live_dl` trên máy bạn đo lại C (8/16) với link thật.
- **D (httpx)**: trên mock là HTTP/1.1 (máy chủ giả không có TLS/ALPN h2) → chỉ đo được chi phí httpx, chậm hơn. HTTP/2 thật
  dồn mọi đoạn vào 1 kết nối TCP — nếu YouTube giới hạn theo kết nối thì h2 bất lợi. HTTP/3: Python không có thư viện QUIC
  ổn định trong tool → **không thử**. Không đưa D vào sản xuất.
- **A ≈ B (cờ tắt)** ở mọi kịch bản → đường rollback đúng.

## 6. Giới hạn vật lý (tính từ log thật của bạn — CHƯA phải số đo mới)

- Log thật: video 49,20 MiB = 412,7 Mbit tải trong 1,976 s, TTFB 410 ms (đo từ lúc engine bắt đầu — gồm cả 96 ms
  preallocation) → phần truyền 1,566 s ≈ **264 Mbps**; audio 43,7 Mbit song song ≈ 91–103 Mbps → tổng ≈ **360–370 Mbps**.
- HOT READY ≈ resolve (144) + video. Với V9.8: TTFB mạng ≈ 410 − 96 = ~314 ms.
- **≤ 1,5 s**: còn 1,5 − 0,144 − 0,314 ≈ 1,04 s cho 412,7 Mbit video (+43,7 Mbit audio cùng lúc) → cần **≥ ~400 Mbps cho
  video, ~440 Mbps tổng**. Nếu 370 Mbps hiện tại đã là TRẦN đường truyền → HOT READY tối thiểu ≈ 0,144 + 0,314 + 456/370 ≈
  **1,69 s** — không thể ≤ 1,5 s bằng cách đổi downloader. Nếu là giới hạn THEO KẾT NỐI (~13 Mbps/kết nối) và đường truyền
  ≥ 450 Mbps → 32 kết nối ≈ 420 Mbps → ≈ 1,45 s: **có thể**.
- **≤ 1,0 s**: còn ~0,54 s → cần **≥ ~760 Mbps** cho riêng video (≈ 840 Mbps tổng) → chỉ khả thi với đường truyền ~1 Gbps
  và CDN đáp ứng được.
- Câu trả lời thật nằm ở bước `linkcap` (tải cùng video 20/32/40 kết nối, đo Mbps tối đa) — report của nó in sẵn
  "HOT_READY tối thiểu" cho 40 / 49,2 / 60 MiB theo số đo của chính máy bạn.

## 7. Chạy trên Windows: `BENCH_V98_480.bat`

Tạo `urls.txt` (mỗi dòng 1 URL, nên có video 40–60 MiB ở 480p), rồi chạy BAT. 4 bước, kết quả ở `perf_report\v98_*`:

| Bước | Lệnh | Trả lời câu hỏi |
|---|---|---|
| 1 | `mock480 --b-variants preset:flags` | Trên ổ đĩa / Windows THẬT: prealloc nền lợi bao nhiêu; khoá + ghi (lock/split/pwrite) có phải nút thắt? |
| 2 | `linkcap` | Trần Mbps của đường truyền + CDN (20/32/40 kết nối) → giới hạn vật lý, HOT_READY tối thiểu |
| 3 | `live_dl --engines A,B,C --b-variants preset:live` | CÙNG link đã ký mỗi vòng: A V9.7.1 / B V9.8 / B tắt cờ / B+scale / B 32 worker / aria2 8,16 — TTFB, video, audio, request, DNS/TCP/TLS từng kết nối, **SHA-256** trùng nhau + ffprobe |
| 4 | `live480 --b-variants preset:pipeline` | NGUYÊN pipeline (resolve → tải → DIRECT-CUT/MUX): HOT_READY / PIPELINE_TOTAL thật; có / không prewarm ffmpeg; opt-in FASTEST_VALID_480P (itag 135 khi nhỏ hơn 244) |

An toàn: thứ tự ngẫu nhiên, nghỉ 3–4 s giữa lượt; **429 → dừng ngay**, 403/410 → bỏ vòng, 2 lần → dừng (mã thoát 3, BAT
báo "đợi 15–30 phút"). Không ghi URL có chữ ký / cookie / token vào báo cáo (DNS/TCP/TLS không ghi tên host).
Kết nối GVS được **đóng trước mỗi lượt** (công bằng giữa các cấu hình); `--warm` để đo kiểu giữ kết nối nóng.

Gửi lại `perf_report\v98_*_summary.md` + `.csv` → mình chốt mặc định theo số thật (ví dụ: nếu `linkcap` cho thấy 32 không
nhanh hơn 20, worker tự học sẽ tự giữ 20; nếu Windows cho thấy `pwrite` nhanh hơn `lock` → bật `V98_WRITE_MODE=pwrite`).

## 8. Kiểm thử

- Hồi quy toàn bộ: V9.7.1 gốc **173 passed** → V9.8 **212 passed + 1 xfailed** (= 173 test cũ + 40 test mới; xfail là test V9.3 cũ về ghi Excel song song, vốn chập chờn, không liên quan engine). 5 test cũ phải sửa vì ghi cứng chữ `[V9.7.1-SUB2S]` → nay đọc `V94_VERSION`.
- `tests/test_v98.py` (40 test): đúng từng byte ở 3 kiểu ghi (cả nhánh Windows mỗi luồng 1 handle) với chẻ đoạn / mất kết nối
  / size sai / 416 / huỷ; handle đóng hết (đổi tên + xoá được file); Content-Range sai và 403 vẫn an toàn, không để file dở;
  prealloc nền (GET đầu < 100 ms, co/giãn chờ prealloc, lỗi prealloc = lỗi cứng, sạch file); race adaptive / learn (đổi chế độ
  theo lịch sử, replica 403 an toàn); scale-up (thêm khi giới hạn theo kết nối, dừng khi giới hạn đường truyền); worker tự học
  (chọn 32 khi giới hạn theo kết nối, giữ 20 khi giới hạn đường truyền); 4 cờ cùng bật + 6 job song song; aria2 RPC thật
  (đếm kết nối thật, 403 đúng 1 request); mặc định V9.8 và rollback bằng biến môi trường.
- Đã sửa trong lúc làm: (1) chế độ ghi ngoài khoá phải **chờ mọi lần ghi xong** trước khi đóng file (nếu không, fd đã đóng
  có thể bị cấp lại cho file khác — ví dụ file audio — và lần ghi trễ rơi nhầm vào đó); (2) lỗi preallocation = lỗi cứng,
  không retry; (3) bảng markdown của bench bị vỡ do nhãn có ký tự `|`.

## 9. Chưa làm / không tuyên bố

- **Chưa có số live**: máy thử không ra được googlevideo. Không tuyên bố "đạt ≤ 1,5 s" hay "≤ 1,0 s".
- 480p chưa dùng "nhớ host đích để bỏ 302" như 360p (V9.6) — đoán sai host = 403/404 giữa chừng engine nhiều đoạn; cần số
  `live_dl` (cột redirect/TTFB) trước khi làm.
- Ghi file Windows (`split`/`pwrite`) để TẮT tới khi bước 1 trên máy bạn chứng minh có lợi.
- Downloader sản xuất **không** đổi sang aria2 / httpx.

## 10. File

| File | |
|---|---|
| `V9_4_SUB2S.py` | bản V9.8 (`V94_VERSION = "V9.8-SUB2S"`) |
| `backup/V9_7_1_SUB2S_truoc_v98.py` | bản V9.7.1 gốc (không ghi đè) — engine A của bench |
| `bench_v98.py`, `tests/mock_gvs_proc.py` | bench mock480 / linkcap / live_dl / live480 |
| `BENCH_V98_480.bat` | chạy 4 bước trên Windows |
| `RUN_V9_8_GUI.bat` | chạy GUI V9.8, có sẵn dòng rollback từng cờ |
| `tests/test_v98.py` | 40 test mới |
| `perf_report/v98_mock480_*` | toàn bộ số mock (jsonl / csv / json / summary.md) |

