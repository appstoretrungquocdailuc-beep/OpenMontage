# BENCH_DL_SUMMARY — 2026-11-15T17:23:31.528+00:00 — so với mốc 'warm_all' (TỔNG = resolve → tải → ffprobe)

Mốc warm_all: median 1328.753 ms | P90 1868.338 ms | lỗi 0/2

| Biến thể | median | P90 | lỗi | Δ median | Δ P90 | Đề xuất |
|---|---|---|---|---|---|---|
| v2_ssl | 1318 | 1858 | 0/2 | +11 | +10 | ⏸ GIỮ TẮT (chưa thắng đủ ≥ 50 ms ở cả median và P90, hoặc lỗi tăng) |
| v2_redirect | 478 | 569 | 0/2 | +851 | +1300 | ✅ BẬT mặc định |
| v2_hedge | 362 | 366 | 0/2 | +967 | +1503 | ✅ BẬT mặc định |
| v2_full | 1032 | 1574 | 0/2 | +297 | +294 | ✅ BẬT mặc định |
| v2_full_auto | 507 | 619 | 0/2 | +822 | +1249 | ✅ BẬT mặc định |

Δ dương = nhanh hơn mốc. Biến thể v2_* cộng dồn (mỗi biến thể thêm đúng 1 nhóm so với biến thể trước): nhóm nào không làm biến thể của nó thắng biến thể liền trước thì không đáng bật. Cờ tương ứng trong config.json: v2_ssl → net_shared_ssl/net_rcvbuf_mb; v2_redirect → net_redirect_memory/net_mn_preconnect; v2_hedge → dl_hedge; v2_full → dl_alt_url_on_403/dl_quick_integrity/dl_hotpath_v2; v2_full_auto → range_connections="auto".
