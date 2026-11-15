# BENCH_DL_SUMMARY — 2026-11-15T19:03:57.475+00:00 — mốc 'warm_all' (TỔNG = resolve → tải → ffprobe)

Mốc warm_all: median 639.385 ms | P90 1988.033 ms | lỗi 0/20 | lượt > 1 s: 5/20

| Biến thể | median | P90 | lỗi | lượt > 1 s | Δ median / Δ P90 so với warm_all [KTC 90%] | so với biến thể liền trước [KTC 90%] | Nhóm cờ | Đề xuất | Lý do |
|---|---|---|---|---|---|---|---|---|---|
| v2_ssl | 634 | 782 | 0/20 | 2/20 | +5 [-1…+21] / +1206 [-111…+1358] (ngưỡng 50) | warm_all: +5 [-1…+21] / +1206 [-111…+1358] | net_shared_ssl + net_rcvbuf_mb | ⏸ GIỮ TẮT nhóm cờ này | median chắc chắn KHÔNG đạt ngưỡng; P90 trong nhiễu |
| v2_redirect | 360 | 1708 | 0/20 | 3/20 | +279 [+268…+296] / +280 [-919…+1609] (ngưỡng 50) | v2_ssl: +274 [+262…+280] / -926 [-1067…+1487] | net_redirect_memory + net_mn_preconnect | ❔ CHƯA KẾT LUẬN (nhiễu lớn — chạy thêm vòng) | median chắc chắn nhanh hơn; P90 trong nhiễu |
| v2_hedge | 365 | 377 | 0/20 | 0/20 | +275 [+266…+293] / +1611 [+391…+1629] (ngưỡng 50) | v2_redirect: -5 [-13…+9] / +1331 [-0…+1337] | dl_hedge | ✅ BẬT nhóm cờ này | median chắc chắn nhanh hơn; P90 chắc chắn nhanh hơn |
| v2_full | 358 | 543 | 0/20 | 0/20 | +282 [+225…+297] / +1445 [+235…+1511] (ngưỡng 50) | v2_hedge: +7 [-55…+14] / -166 [-179…+12] | dl_alt_url_on_403 + dl_quick_integrity + dl_hotpath_v2 | ✅ BẬT nhóm cờ này | median chắc chắn nhanh hơn; P90 chắc chắn nhanh hơn |
| v2_full_auto | 420 | 1708 | 0/20 | 3/20 | +219 [+213…+271] / +280 [-4…+1559] (ngưỡng 100) | v2_full: -63 [-68…-4] / -1165 [-1246…+113] | range_connections = "auto" | ❔ CHƯA KẾT LUẬN (nhiễu lớn — chạy thêm vòng) | median chắc chắn nhanh hơn; P90 trong nhiễu |
| warm_all_auto | 649 | 1982 | 0/20 | 7/20 | -9 [-653…+8] / +6 [-1205…+71] (ngưỡng 100) | - | range_connections = "auto" | ⏸ GIỮ TẮT nhóm cờ này | median chắc chắn KHÔNG đạt ngưỡng; P90 chắc chắn KHÔNG đạt ngưỡng |

**range_connections = "auto"**: ⏸ giữ range_connections = 1

**Cộng dồn:** v2_redirect đo khi đã bật nhóm của v2_ssl; v2_hedge khi đã bật cả v2_ssl + v2_redirect; … Bật một nhóm sau mà TẮT nhóm trước nó = cấu hình CHƯA được đo (nên giữ các nhóm trước như lúc đo, trừ nhóm bị 'chậm hơn rõ' hoặc lỗi tăng).

Δ dương = nhanh hơn. [KTC 90%] = khoảng tin cậy 90% bootstrap của chênh lệch (cần ≥ 5 lượt thành công). 'So với biến thể liền trước' chỉ tính là chậm hơn khi chắc chắn chậm hơn > 25 ms. 'Giữ tắt' = đặt các cờ của nhóm đó về false (range_connections = 1) trong config.json. 'Chưa kết luận' = chênh lệch nằm trong nhiễu lấy mẫu → giữ như hiện tại và chạy thêm vòng (BENCH_TAI_360.bat), không suy ra nhanh/chậm.
