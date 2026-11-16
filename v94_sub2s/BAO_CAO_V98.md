# V9.8-SUB2S — TỐI ƯU TẢI 480P (video 244 + audio JA) — 2026-11-16

> **Đọc trước:** mọi số trong mục 4–6 là **SỐ MOCK** — máy chủ GoogleVideo GIẢ (127.0.0.1, chạy ở tiến trình riêng,
> mô phỏng theo log thật của bạn). Máy thử ở đây **không tải được googlevideo**. Số mock chứng minh **độ đúng** và **tác
> dụng tương đối** của từng thay đổi, KHÔNG phải tốc độ YouTube trên máy bạn. Chưa có cơ sở để nói "đã đạt ≤ 1,5 s" —
> chỉ `BENCH_V98_480.bat` chạy trên máy bạn trả lời được (mục 7).

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
| `V98_PREALLOC_ASYNC` | `1` | `0` | __PA_NOTE__ |
| `V98_RACE_MODE` | `learn` | `fixed` | `adaptive` (luôn chỉ thăm dò) bị LOẠI: P90 +153 ms khi CDN chính chậm |
| `V98_WORKERS_LEARN` | __WL_DEFAULT__ | `0` | mức "hi" = 20 × 1,6 = 32 (≤ `V98_SCALE_MAX`); file < 8 MiB và khi đã ép số worker → không áp dụng |
| `V98_WRITE_MODE` | `lock` | `lock` | `split`/`pwrite` không nhanh hơn trên Linux; chờ số Windows |
| `V98_SCALE_UP` | `0` | `0` | LOẠI: P90 tăng (S1 1946→2035 ms) + 20 request |
| `V98_PART_MIB` | `0` | `0` | LOẠI: đoạn đầu ≤ 1 MiB chậm hơn rõ (__PART_NOTE__) |
| `V98_ARIA2_SPLIT` / `V98_ARIA2_MAX_TRIES` | `0` (=cũ) / `2` (=cũ) | | chỉ bench dùng; downloader sản xuất KHÔNG đổi |

Đặt cờ trên Windows: trong `RUN_V9_4_GUI.bat`, trước dòng `python ...` thêm ví dụ `set V98_WORKERS_LEARN=0`.
Log khi bấm START có dòng `⚙️ V9.8 RANGE: prealloc=… | ghi=… | race=… | worker=… | scale-up=…`; mỗi lần tải có thêm
`race=learn:probe|learn:all`, `wl=lo|hi`, `prealloc_bg=…ms`, `lock_wait=… lock_hold=… write=…ms/Nx(mode)`.

__RESULTS__
