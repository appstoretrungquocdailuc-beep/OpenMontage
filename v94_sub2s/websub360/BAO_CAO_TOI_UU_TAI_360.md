# TỐI ƯU TẢI 360P — audit resolver + downloader (2026-11-12)

## Kết luận ngắn
- **Mục tiêu ≤ 1000 ms: CHƯA xác nhận được.** Máy thử của tôi bị chặn `www.youtube.com` và `*.googlevideo.com` (chính sách mạng của
  môi trường, không phải YouTube từ chối), nên KHÔNG tải được video thật ở đây. Bảng trước/sau thật phải chạy `BENCH_TAI_360.bat` trên máy bạn.
- Đo THẬT được ở đây (gọi API Innertube của Google qua `youtubei.googleapis.com`, 24+24 lượt xen kẽ):
  **resolver kết nối NGUỘI median 508 ms vs kết nối ẤM median 178 ms (−330 ms)**.
  Bản trước KHÔNG BAO GIỜ giữ kết nối ấm → resolver 627 ms của bạn gần như chắc chắn gồm phần bắt tay DNS+TCP+TLS này.
- Ước tính (CHƯA đo) cho video 8,67 MB của bạn nếu cả resolver và máy chủ video đều ấm: resolver ≈ 627 − (bắt tay) ;
  TTFB ≈ 919 − (DNS+TCP+TLS) = chỉ còn **chờ máy chủ googlevideo**. Nếu phần chờ máy chủ đó lớn (video vừa đăng, cache Google chưa có),
  tool KHÔNG thể giảm nó → khi đó dưới 1 s là không thực tế. Bảng BENCH_DL sẽ tách đúng phần này.

## Audit (bản trước)
| # | Phát hiện | Ảnh hưởng | Đã làm |
|---|---|---|---|
| 1 | `Resolver.warm()` chỉ tạo đối tượng pool, KHÔNG mở kết nối thật | lần resolve đầu sau lúc rảnh phải DNS+TCP+TLS | Bộ giữ ấm thật (`NET-WARM`): 3 kết nối tới máy chủ Innertube (= 3 client chạy song song), ping `GET /generate_204` mỗi 40 s |
| 2 | Google đóng kết nối rảnh sau vài phút → hầu như mọi video mới đều gặp kết nối nguội | như trên, cho cả resolver và tải | như trên + giữ ấm 6 máy chủ video (googlevideo) vừa dùng |
| 3 | Máy chủ video mới → DNS mỗi lần | vài–vài chục ms | DNS cache 5 phút + tải trước DNS rr1–rr20 cùng cụm `sn-…` |
| 4 | Không có số đo từng giai đoạn ("TTFB 919" gộp tất cả) | không biết mất thời gian ở đâu | Đo DNS / TCP / TLS / CHỜ MÁY CHỦ / đọc / parse cho resolver và mọi chặng tải; log `🌐 MẠNG`; lưu SQLite cột `net` |
| 5 | `_innertube_client`: import + deepcopy mỗi request | ~0,03 ms | nạp 1 lần |
| 6 | Tải chia đoạn (Range): thêm 1 request dò `bytes=0-0` trước | +1 vòng mạng + chờ máy chủ | bỏ request dò: mỗi đoạn tự kiểm 206 + Content-Range, sai → về 1 GET |
| 7 | 3 client Innertube chạy song song (race), lấy client đầu tiên có 360p | tốt — giữ | đo thời gian từng client (`race`) |
| 8 | itag 18 progressive 1 GET, không mux, `.part` → ffprobe + đúng dung lượng → đổi tên nguyên tử khi tích hợp TikTok | tốt — giữ | không đổi |

Không đổi: WebSub, API, Excel, TikTok, tên file, thứ tự ưu tiên itag 18.

## Số đo thật trên máy thử (resolver, không phải tải video)
| Kết nối | lượt | median | P90 | min | max |
|---|---|---|---|---|---|
| Nguội (như bản trước sau lúc rảnh) | 24 | 508 | 2259 | 296 | 2295 |
| Ấm (bộ giữ ấm) | 24 | 178 | 2003 | 137 | 2003 |

P90 ~2 s ở cả hai: 3/24 lượt hết giờ đọc 2 s (máy chủ/proxy chậm) — race 3 client + dự phòng yt-dlp xử lý trường hợp này.
Nhiều video trả "Sign in to confirm you're not a bot" vì máy thử là IP trung tâm dữ liệu — không lách, chỉ dùng để đo độ trễ. Số liệu đi qua proxy
của môi trường thử → chênh lệch tuyệt đối trên máy bạn sẽ khác (thường NHỎ hơn), chiều hướng giống.

## Benchmark trên máy bạn (bảng trước/sau thật)
`BENCH_TAI_360.bat` → nhập 3–4 ID video khác dung lượng → ≥ 20 lượt MỖI phương án, thứ tự xoay vòng, mỗi lượt lấy link MỚI + tải lại từ đầu:
| Phương án | Ý nghĩa |
|---|---|
| `legacy` | **bản trước** (rollback), kết nối nguội |
| `fast_cold` | lớp mạng mới, không giữ ấm (để tách tác dụng) |
| `warm_resolver` | giữ ấm Innertube, máy chủ video nguội (thực tế xấu) |
| `warm_all` | giữ ấm cả máy chủ video vừa dùng (thực tế tốt) |
| `warm_all_r2`, `warm_all_r4` | chia 2 / 4 đoạn Range |
| `warm_apihost` | resolver qua `youtubei.googleapis.com` |
| `warm_h2` | tải bằng HTTP/2 (httpx) |
Kết quả `BENCH_DL.md`: median/P90 TỔNG, tỷ lệ lỗi, số lượt ≤ 1000 ms, resolver (setup/chờ máy chủ), TTFB (setup/chờ máy chủ), truyền, ffprobe.

## Rollback
`config.json`: `"net_mode": "legacy"` (đúng lớp mạng bản trước) và/hoặc `"net_warm": false`. Bản trước đầy đủ: `backup/YOUTUBE_WEBSUB_INSTANT_360P_truoc_toi_uu_tai.py`.
Sau khi có BENCH_DL: phương án nào không nhanh hơn `legacy` thì tắt (vd giữ `range_connections: 1` nếu r2/r4 không thắng).
