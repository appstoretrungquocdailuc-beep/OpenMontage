# BENCH_DL_SUMMARY — 2026-11-16T08:15:52.553+00:00 — mốc 'warm_all' (TỔNG = resolve → tải → ffprobe)

Mốc warm_all: median 655.412 ms | P90 1982.158 ms | lỗi 0/20 | lượt > 1 s: 5/20

| Biến thể | median | P90 | lỗi | lượt > 1 s | Δ median / Δ P90 so với warm_all [KTC 90%] | so với biến thể trước [KTC 90%] | Nhóm cờ | Đề xuất | Lý do |
|---|---|---|---|---|---|---|---|---|---|
| v2_ssl | 650 | 1999 | 0/20 | 4/20 | +6 [-10…+31] / -17 [-1180…+1277] (ngưỡng 50) | - | net_shared_ssl + net_rcvbuf_mb | ⏸ GIỮ TẮT nhóm cờ này | median chắc chắn KHÔNG đạt ngưỡng; P90 trong nhiễu |
| v2_redirect | 371 | 405 | 0/20 | 1/20 | +284 [+270…+308] / +1577 [+203…+1625] (ngưỡng 50) | v2_ssl: +279 [+268…+289] / +1594 [+114…+1628] | net_redirect_memory + net_mn_preconnect | ✅ BẬT nhóm cờ này | median chắc chắn nhanh hơn; P90 chắc chắn nhanh hơn |
| v2_hedge | 376 | 588 | 0/20 | 1/20 | +280 [+234…+301] / +1394 [+221…+1558] (ngưỡng 50) | v2_redirect: -5 [-47…+4] / -183 [-1314…+1078] | dl_hedge | ❔ CHƯA KẾT LUẬN (chạy thêm vòng) | median chắc chắn nhanh hơn; P90 chắc chắn nhanh hơn; có thể chậm hơn v2_redirect (trong nhiễu) |
| v2_full | 375 | 492 | 0/20 | 0/20 | +280 [+235…+307] / +1490 [+321…+1565] (ngưỡng 50) | v2_hedge: +1 [-39…+43] / +96 [-98…+1227] ; v2_redirect: -4 [-53…+10] / -87 [-166…+1237] | dl_alt_url_on_403 + dl_quick_integrity + dl_hotpath_v2 | ❔ CHƯA KẾT LUẬN (chạy thêm vòng) | median chắc chắn nhanh hơn; P90 chắc chắn nhanh hơn; có thể chậm hơn v2_redirect (trong nhiễu) |
| v2_full_auto | 596 | 1929 | 0/20 | 3/20 | +60 [-4…+291] / +53 [-1309…+1337] (ngưỡng 100) | v2_full: -221 [-285…+9] / -1437 [-2750…-121] ; v2_redirect: -225 [-283…+5] / -1524 [-2722…-85] | range_connections = "auto" | → xem dòng range auto bên dưới | median trong nhiễu; P90 trong nhiễu; chậm hơn rõ so với v2_full |
| warm_all_auto | 654 | 1999 | 0/20 | 6/20 | +1 [-22…+24] / -16 [-1182…+55] (ngưỡng 100) | - | range_connections = "auto" | → xem dòng range auto bên dưới | median chắc chắn KHÔNG đạt ngưỡng; P90 chắc chắn KHÔNG đạt ngưỡng |

**range_connections = "auto"**: ⏸ giữ range_connections = 1 (warm_all_auto: median chắc chắn KHÔNG đạt ngưỡng; P90 chắc chắn KHÔNG đạt ngưỡng)

**Cộng dồn:** v2_redirect đo khi đã bật nhóm của v2_ssl; v2_hedge khi đã bật cả v2_ssl + v2_redirect; … Bật một nhóm sau mà TẮT nhóm trước nó = cấu hình CHƯA được đo (nên giữ các nhóm trước như lúc đo, trừ nhóm bị 'chậm hơn rõ' hoặc lỗi tăng).

Δ dương = nhanh hơn. [KTC 90%] = khoảng tin cậy 90% bootstrap của chênh lệch (cần ≥ 5 lượt thành công). 'So với biến thể trước' = biến thể liền trước có số đo và biến thể gần nhất đã có kết luận chắc; chỉ tính là chậm hơn khi chắc chắn chậm hơn > 25 ms. 'Giữ tắt' = đặt các cờ của nhóm đó về false (range_connections = 1) trong config.json. 'Chưa kết luận' = chênh lệch nằm trong nhiễu lấy mẫu hoặc không đủ lượt → giữ như hiện tại và chạy thêm vòng (BENCH_TAI_360.bat). Lượt mà tối ưu suy đoán bị tắt (gặp 429 → tạm dừng 10 phút; video từng bị 403/429 → thận trọng; chặn đoán sau khi đoán sai; mọi replica bị cấm) không được dùng để chấm các nhóm v2 có suy đoán (cột cuối của BENCH_DL.md).
