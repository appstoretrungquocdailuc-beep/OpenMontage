# BENCH_DL_SUMMARY — 2026-11-15T17:26:11.234+00:00 — so với mốc 'warm_all' (TỔNG = resolve → tải → ffprobe)

Mốc warm_all: median 638.34 ms | P90 1990.55 ms | lỗi 0/20

| Biến thể | median | P90 | lỗi | Δ median | Δ P90 | Đề xuất |
|---|---|---|---|---|---|---|
| v2_ssl | 649 | 1984 | 0/20 | -11 | +7 | ⏸ GIỮ TẮT (chưa thắng đủ ≥ 50 ms ở cả median và P90, hoặc lỗi tăng) |
| v2_redirect | 359 | 452 | 0/20 | +279 | +1538 | ✅ BẬT mặc định |
| v2_hedge | 370 | 1715 | 0/20 | +268 | +276 | ✅ BẬT mặc định |
| v2_full | 369 | 1705 | 0/20 | +269 | +285 | ✅ BẬT mặc định |
| v2_full_auto | 539 | 1929 | 0/20 | +99 | +62 | ✅ BẬT mặc định |

Δ dương = nhanh hơn mốc. Biến thể v2_* cộng dồn (mỗi biến thể thêm đúng 1 nhóm so với biến thể trước): nhóm nào không làm biến thể của nó thắng biến thể liền trước thì không đáng bật. Cờ tương ứng trong config.json: v2_ssl → net_shared_ssl/net_rcvbuf_mb; v2_redirect → net_redirect_memory/net_mn_preconnect; v2_hedge → dl_hedge; v2_full → dl_alt_url_on_403/dl_quick_integrity/dl_hotpath_v2; v2_full_auto → range_connections="auto".
