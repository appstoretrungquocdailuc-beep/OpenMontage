# BENCH_DL_SUMMARY — 2026-11-15T20:38:14.119+00:00 — mốc 'warm_all' (TỔNG = resolve → tải → ffprobe)

Mốc warm_all: median 653.727 ms | P90 1982.733 ms | lỗi 0/20 | lượt > 1 s: 5/20

| Biến thể | median | P90 | lỗi | lượt > 1 s | Δ median / Δ P90 so với warm_all [KTC 90%] | so với biến thể liền trước [KTC 90%] | Nhóm cờ | Đề xuất | Lý do |
|---|---|---|---|---|---|---|---|---|---|
| v2_ssl | 641 | 1985 | 0/20 | 4/20 | +13 [-1…+20] / -2 [-1198…+1289] (ngưỡng 50) | - | net_shared_ssl + net_rcvbuf_mb | ⏸ GIỮ TẮT nhóm cờ này | median chắc chắn KHÔNG đạt ngưỡng; P90 trong nhiễu |
| v2_redirect | 368 | 399 | 0/20 | 1/20 | +286 [+277…+295] / +1584 [+195…+1618] (ngưỡng 50) | v2_ssl: +273 [+264…+289] / +1586 [+81…+1612] | net_redirect_memory + net_mn_preconnect | ✅ BẬT nhóm cờ này | median chắc chắn nhanh hơn; P90 chắc chắn nhanh hơn |
| v2_hedge | 374 | 550 | 0/20 | 1/20 | +279 [+230…+289] / +1432 [+233…+1550] (ngưỡng 50) | v2_redirect: -7 [-52…+4] / -151 [-1310…+1117] | dl_hedge | ❔ CHƯA KẾT LUẬN (chạy thêm vòng) | median chắc chắn nhanh hơn; P90 chắc chắn nhanh hơn; có thể chậm hơn v2_redirect (trong nhiễu) |
| v2_full | 372 | 503 | 0/20 | 0/20 | +282 [+227…+290] / +1479 [+283…+1556] (ngưỡng 50) | v2_hedge: +3 [-49…+39] / +47 [-92…+1205] | dl_alt_url_on_403 + dl_quick_integrity + dl_hotpath_v2 | ✅ BẬT nhóm cờ này | median chắc chắn nhanh hơn; P90 chắc chắn nhanh hơn |
| v2_full_auto | 599 | 1923 | 0/20 | 3/20 | +55 [-2…+280] / +60 [-1301…+1336] (ngưỡng 100) | v2_full: -227 [-286…-2] / -1420 [-2744…-116] | range_connections = "auto" | → xem dòng range auto bên dưới | median trong nhiễu; P90 trong nhiễu; chậm hơn rõ so với v2_full |
| warm_all_auto | 651 | 1997 | 0/20 | 6/20 | +3 [-11…+11] / -14 [-1210…+66] (ngưỡng 100) | - | range_connections = "auto" | → xem dòng range auto bên dưới | median chắc chắn KHÔNG đạt ngưỡng; P90 chắc chắn KHÔNG đạt ngưỡng |

**range_connections = "auto"**: ⏸ giữ range_connections = 1 (warm_all_auto: median chắc chắn KHÔNG đạt ngưỡng; P90 chắc chắn KHÔNG đạt ngưỡng)

**Cộng dồn:** v2_redirect đo khi đã bật nhóm của v2_ssl; v2_hedge khi đã bật cả v2_ssl + v2_redirect; … Bật một nhóm sau mà TẮT nhóm trước nó = cấu hình CHƯA được đo (nên giữ các nhóm trước như lúc đo, trừ nhóm bị 'chậm hơn rõ' hoặc lỗi tăng).

Δ dương = nhanh hơn. [KTC 90%] = khoảng tin cậy 90% bootstrap của chênh lệch (cần ≥ 5 lượt thành công). 'So với biến thể liền trước' chỉ tính là chậm hơn khi chắc chắn chậm hơn > 25 ms. 'Giữ tắt' = đặt các cờ của nhóm đó về false (range_connections = 1) trong config.json. 'Chưa kết luận' = chênh lệch nằm trong nhiễu lấy mẫu → giữ như hiện tại và chạy thêm vòng (BENCH_TAI_360.bat), không suy ra nhanh/chậm. Gặp 429 trong lúc đo: các tối ưu gửi thêm request bị tắt 10 phút (an toàn) → các lượt đó không đo được tác dụng v2 (cột 'đo khi đang tạm dừng vì 429' trong BENCH_DL.md).
