# AUTO FRAME MATCH 2K — Phân tích & cắt ghép khung hình 100% trên máy

Đưa vào **video Douyin đã edit** + **1, 2, 3 hoặc nhiều video gốc** → nhấn **TỰ ĐỘNG PHÂN TÍCH & CẮT GHÉP** →
phần mềm tự tìm từng khung hình của bản edit trong các video gốc, lập bảng cắt, kiểm định rồi xuất video
**2K (1440p)** từ nguồn chất lượng cao, **giữ tiếng Nhật của từng tập gốc**.

* Không gửi video lên ChatGPT/Claude, không dùng API trả phí hay máy chủ phân tích bên ngoài.
* Toàn bộ Computer Vision chạy cục bộ: FFmpeg, OpenCV, PySceneDetect, FAISS, NumPy.
* Thư mục này độc lập — **không sửa, không ghi đè** `so_khung.py`, `cat_ghep.py` hay tool cũ nào.

---

## 1. Cài đặt (Windows 11, làm 1 lần)

1. **Python 3.10 – 3.13**: tải tại <https://www.python.org/downloads/>, khi cài nhớ tick
   **"Add python.exe to PATH"**.
2. **FFmpeg bản có NVENC** (để dùng GPU RTX 3060):
   * Cách nhanh: mở PowerShell → `winget install Gyan.FFmpeg`
   * Hoặc tải `ffmpeg-release-full` tại <https://www.gyan.dev/ffmpeg/builds/>, giải nén, chép
     `ffmpeg.exe` và `ffprobe.exe` vào thư mục `auto_frame_match\bin\`.
3. Nhấp đúp **`CAI_DAT.bat`** — tạo môi trường `.venv` và cài thư viện (`requirements.txt`).
   Cuối bước này cửa sổ báo có/không có **H.264 NVENC**.
4. (Tùy chọn) Nhấp đúp **`KIEM_THU.bat`** để chạy bộ kiểm thử với video tổng hợp (10–20 phút).

Chạy chương trình: nhấp đúp **`CHAY_AUTO_FRAME_MATCH.bat`**.

> Driver NVIDIA nên cập nhật mới (NVENC/CUDA decode của FFmpeg cần driver ≥ 531).

---

## 2. Chế độ A — PHÂN TÍCH & CẮT TỰ ĐỘNG (một nút)

1. **Video Douyin đã edit** → *Chọn…*
2. **Thêm video gốc…** (chọn được nhiều file một lần). Mỗi dòng có thể:
   * **Chọn bản Video2X…**: file đã làm nét bằng Video2X **cùng số khung** với bản gốc. Tool phân tích trên bản
     gốc nhưng **dựng hình từ bản Video2X**; âm thanh vẫn lấy từ bản gốc (tiếng Nhật).
   * **Độ trễ audio…**: hiệu chỉnh độ trễ riêng của nguồn (ms, dương = tiếng muộn hơn hình).
3. **Thư mục xuất** → nhấn **TỰ ĐỘNG PHÂN TÍCH & CẮT GHÉP**.

Phần mềm tự chạy 11 bước, hiển thị tiến độ từng bước:

| Bước | Việc làm |
|---|---|
| 1 | Đọc metadata mọi video (FPS, timebase, PTS, start_time, luồng âm thanh/ngôn ngữ) |
| 2 | Giải mã **mọi khung** video gốc → thumbnail xám + pHash/DCT + vector đặc trưng đa cửa sổ, **lưu cache** |
| 3 | Giải mã **mọi khung** Douyin, phát hiện cảnh bằng PySceneDetect + bộ dò diff từng khung, dò lại điểm nghi vấn |
| 4–7 | Tìm nguồn ứng viên (FAISS), ước lượng crop/zoom/lật (template matching đa tỉ lệ + ECC + tối ưu trực tiếp), căn chỉnh thời gian từng khung (dò đường thẳng thời gian + tốc độ) |
| 8 | Kiểm định: dò lại ranh giới cảnh, cảnh tĩnh, cảnh trùng nhiều nguồn, tính liên tục, phép thử lia máy |
| 9–10 | Cắt ghép chính xác từng khung từ nguồn chất lượng cao, scale lanczos lên 2K, H.264 NVENC, ghép tiếng Nhật theo `source_id` + `source_pts` |
| 11 | So thành phẩm với bản Douyin từng khung + đối chiếu âm thanh → `QA_REPORT.html` |

**Quy tắc an toàn**: chỉ xuất `THANH_PHAM_2K.mp4` (đã xác minh) khi **mọi khung đều XANH**. Nếu còn VÀNG/ĐỎ,
tool **không đoán nguồn, không chèn màn trắng**: các đoạn được đưa vào `KIEM_TRA_THU_CONG.csv`.
Muốn xem thử vẫn có thể tick **"Cho phép xuất BẢN NHÁP"** → file `THANH_PHAM_2K_CHUA_XAC_MINH.mp4`
(đoạn ĐỎ bị bỏ, không lấp bằng hình giả).

### Mới trong v1.1.0

* **Tầng 2 SIFT + RANSAC** cho đoạn ĐỎ (Douyin phóng to mạnh, phụ đề cứng): tìm trong cửa sổ giữa 2 đoạn kề
  (±5 giây), bỏ 22% dưới khung (phụ đề), cần ≥ 40 inlier và ≥ 2 khung đồng thuận cùng thời điểm. Kết quả được
  căn chỉnh lại từng khung; nếu ảnh thu nhỏ không xác minh được thì đoạn là VÀNG (`method = sift`).
* **DP theo thứ tự thời gian (Viterbi)**: phân xử các ứng viên sát điểm nhau (cảnh lặp) theo tính liền mạch.
* **Nội suy đoạn ĐỎ** (tùy chọn, tick ô *Nội suy đoạn ĐỎ từ đoạn kề*): đoạn không tìm được nguồn được lấp theo
  2 đoạn kề (hoặc giữ khung nếu nguồn không đủ dài), giữ đủ thời lượng bản Douyin. Đoạn này luôn VÀNG
  (`method = interp/freeze`), thành phẩm mang hậu tố `_CHUA_XAC_MINH`; đoạn giữ khung không có tiếng.
* Cột mới `method` trong bảng cắt: `match` | `sift` | `interp` | `freeze`.

### Ba trạng thái

* 🟩 **XANH** — khớp và đã qua kiểm định.
* 🟨 **VÀNG** — nghi vấn (điểm thấp, đoạn quá ngắn < 3 khung, cảnh tĩnh không xác định duy nhất được
  offset, hai nguồn cùng nội dung, lia máy mơ hồ…). Cột *Ghi chú* nói rõ lý do và các nguồn ứng viên.
* 🟥 **ĐỎ** — `UNMATCHED`: không tìm được nguồn, hoặc kiểm định thất bại.

Phần mềm **không bao giờ báo 100%** khi còn đoạn VÀNG/ĐỎ.

---

## 3. Chế độ B — CẮT THEO BẢNG CÓ SẴN

Không cần video Douyin. Chọn `BANG_CAT_AUTO.csv` (hoặc `.json`) + các video gốc → **CẮT GHÉP THEO BẢNG**.

* Tool tự nhận `source_id` của từng file (băm nội dung, hoặc vân tay pHash nếu file đã đổi tên/remux)
  và **từ chối** file không khớp nguồn trong bảng.
* Kiểm tra thủ công: mở `BANG_CAT_AUTO.csv` bằng Excel, ở cột **`manual_action`** ghi:
  * `OK` — đã xem, chấp nhận đoạn VÀNG đó;
  * `BO_QUA` — không dùng đoạn đó.
  Có thể sửa `source_id`, `source_frame_exact`/`source_frame_start`, `speed`, `flip`, `crop_*` rồi chạy lại.
* Thành phẩm Chế độ B được kiểm tra lại với khung nguồn theo bảng (crop, lật, lặp/thiếu khung, lệch tiếng).

---

## 4. File kết quả

| File | Nội dung |
|---|---|
| `BANG_CAT_AUTO.csv` | Bảng cắt theo đoạn: khung/PTS/thời gian edit, `source_id`, khung/PTS nguồn, `source_frame_exact`, tốc độ, lật, crop (pixel nguồn), điểm, ứng viên, lý do, `manual_action` |
| `BANG_CAT_TUNG_FRAME.csv` | **Từng khung** của bản edit: lấy từ nguồn nào, khung nào, PTS nào, độ tương đồng, trạng thái |
| `BANG_CAT_AUTO.json` | Toàn bộ thông tin trên + metadata edit, cấu hình phân tích, điểm cắt nghi vấn |
| `SOURCE_MANIFEST.json` | Mỗi nguồn: đường dẫn, bản Video2X, băm nội dung, FPS, timebase, start_time, số khung, luồng audio, **offset audio**, vân tay pHash mẫu |
| `KIEM_TRA_THU_CONG.csv` | Các đoạn VÀNG/ĐỎ cần xem tay |
| `QA_REPORT.html` | Báo cáo kiểm định: tóm tắt, ảnh đối chiếu từng đoạn, kết quả QA thành phẩm, lệch âm thanh |
| `QA_RESULT.json` | Kết quả QA dạng máy đọc |
| `THANH_PHAM_2K.mp4` | Chỉ có khi đã xác minh hoàn toàn |
| `logs\afm_*.log` | Log chi tiết mỗi lần chạy (thư mục của tool) |

---

## 5. Video & âm thanh

* 2K/QHD: khung ngang → cao 1440 (16:9 = 2560×1440); khung dọc → rộng 1440. Tỉ lệ khung theo vùng nguồn
  hiển thị trong bản Douyin (*Giống bản Douyin*) hoặc toàn khung nguồn (*Toàn khung nguồn*).
* Mã hóa mặc định **H.264 NVENC** (preset p7, CQ 18). Không có NVENC → x264 CRF 16 (CPU).
* Chỉ dùng bộ lọc scale lanczos thông thường; **không chạy AI làm nét lại** (dùng bản Video2X bạn đưa vào).
* Âm thanh: luôn lấy **luồng tiếng Nhật** (nhãn `jpn`; không có nhãn → luồng đầu) của **video gốc** tương ứng
  `source_id`, cắt đúng `source_pts` + offset riêng; đổi tốc độ bằng `atempo`. Ghép theo **số mẫu tích lũy**
  nên không lệch hình–tiếng tại điểm nối. **Không dùng tiếng Douyin.**
* FPS xuất: *tự động* — giữ FPS gốc nếu mọi đoạn cùng FPS và tốc độ 1.0, ngược lại dùng FPS bản Douyin.

---

## 6. Tối ưu cho PC (RTX 3060 12GB, RAM 32GB)

* Giải mã bằng GPU (`-hwaccel cuda`) khi có, tự quay về CPU nếu lỗi. Mã hóa NVENC.
* FAISS chạy CPU (bản GPU của FAISS không có sẵn qua pip trên Windows) — đủ nhanh vì chỉ số nhỏ gọn.
* **Cache fingerprint theo nội dung file** (`cache\`): gặp lại cùng tập Shin-chan (kể cả đổi tên) không quét lại.
  Mỗi tập 25 phút chiếm khoảng 0,8–1 GB cache (thumbnail xám 160×90 dạng memmap + đặc trưng), **không** lưu
  khung 1080p/2K dạng BMP/PNG. Nút *Xóa cache fingerprint…* trong GUI.
* Chỉ tạo ma trận so khớp **cục bộ** trong cửa sổ ứng viên của từng cảnh (không có ma trận toàn cục).
* **Tiếp tục khi bị gián đoạn**: fingerprint, kết quả so khớp và từng đoạn video đã dựng đều được lưu; chạy lại
  với cùng đầu vào sẽ bỏ qua phần đã xong. Nút *Hủy / Tạm dừng* dừng ở điểm an toàn.
* So khớp các cảnh chạy song song theo số nhân CPU.
* Đo thực tế (máy ảo 4 nhân, **không GPU**): 2 nguồn 1080p dài 6 + 5 phút, bản Douyin dọc 50 giây
  (1.504 khung) → lập vân tay nguồn ≈ 165 khung/giây, phân tích + dựng 2K + QA ≈ 5,5 phút, RAM đỉnh ≈ 0,85 GB.
  Trên RTX 3060 + CPU desktop sẽ nhanh hơn (giải mã CUDA, NVENC).

---

## 7. Dòng lệnh (tùy chọn)

```bat
.venv\Scripts\python.exe -m afm phan-tich --edit douyin.mp4 --source tap1.mp4 --source tap2.mp4 ^
    --video2x S1=tap1_video2x.mp4 --audio-offset S2=40 --out D:\ketqua
.venv\Scripts\python.exe -m afm cat-theo-bang --table D:\ketqua\BANG_CAT_AUTO.csv --source tap1.mp4 --out D:\ketqua_B
```

Thêm `--draft` để cho phép bản nháp, `--crop full_source`, `--codec libx264`. Ngưỡng/tham số nâng cao:
chép `afm_config.example.json` thành `afm_config.json` rồi sửa.

---

## 8. Giới hạn đã biết (nói thẳng)

* Cảnh **hoàn toàn tĩnh** hoặc **lia máy nền không nhân vật** có thể khớp như nhau ở nhiều offset → VÀNG,
  trừ khi tính liên tục với cảnh kề xác định được (khi đó ghi chú `LIÊN TỤC`).
* Hai nguồn chứa **cùng một đoạn phim** (OP/ED, đoạn tóm tắt) → VÀNG kèm danh sách nguồn ứng viên nếu
  tính liên tục không phân định được.
* Khung chuyển cảnh kiểu hòa tan (dissolve), hiệu ứng che phủ toàn khung, khung đen/trắng thuần: không có
  đủ thông tin hình để xác minh → VÀNG/ĐỎ để kiểm tra tay.
* Hỗ trợ crop/zoom/lật/đổi màu/phụ đề/watermark/khung dọc nền mờ và tốc độ không đổi trong từng đoạn; **không**
  hỗ trợ xoay hình, biến dạng phối cảnh, hay tốc độ thay đổi liên tục trong một đoạn (sẽ bị chia nhỏ/VÀNG).
* Bản Video2X phải giữ nguyên số khung (không bật nội suy khung hình).
