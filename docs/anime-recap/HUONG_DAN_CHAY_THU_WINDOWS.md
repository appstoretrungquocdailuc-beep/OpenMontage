# Hướng dẫn chạy thử phân tích anime thật trên Windows (GPU NVIDIA)

Bộ chạy thử này dùng lại Giai đoạn 1–2 của OpenMontage để biến một tập anime MP4
(không cần phụ đề, tiếng Nhật gốc) thành `anime_source_index.json`, thư mục keyframe và
`review_packet.md`, kèm báo cáo thời gian và kiểm tra. **Chưa có TTS, GUI hay render video.**

> Bộ công cụ này mới chỉ được kiểm thử trên Linux bằng video tổng hợp. Các file `.bat`
> chưa từng được chạy trên Windows thật. Lần chạy đầu trên máy bạn chính là lần kiểm thử
> thực tế đầu tiên — nếu gặp lỗi, gửi lại file `trial_report.md` và nội dung cửa sổ đen.

---

## 1. Cần chuẩn bị (một lần)

| Thứ cần có | Tải ở đâu | Ghi chú |
|---|---|---|
| **Python 3.11** (64-bit) | https://www.python.org/downloads/windows/ | Khi cài, **tick "Add python.exe to PATH"**. Giữ mặc định "tcl/tk" (cần cho hộp chọn file). Python 3.13 chưa khuyên dùng. |
| **Driver NVIDIA** mới | GeForce Experience hoặc https://www.nvidia.com/drivers | RTX 3060 cần driver hỗ trợ CUDA 12 (bản 2024 trở đi là đủ). Không cần cài CUDA Toolkit — thư viện CUDA được cài qua pip. |
| **FFmpeg** ≥ 5.1 | `1_CAI_DAT.bat` sẽ hỏi cài bằng `winget` (Gyan.FFmpeg) | Hoặc tự tải tại https://www.gyan.dev/ffmpeg/builds/ rồi thêm thư mục `bin` vào PATH. |
| **Mã nguồn OpenMontage** | Trang nhánh `claude/charming-meitner-s9tzcb` trên GitHub → nút **Code → Download ZIP** | Giải nén vào ví dụ `D:\OpenMontage`. Đường dẫn nên ngắn, không cần tiếng Việt có dấu. |
| Dung lượng trống | — | ~5 GB: thư viện CUDA (~1.5 GB) + model Whisper `large-v3` (~3 GB) + kết quả. |
| Mạng | — | Lần đầu cần tải từ `pypi.org` và `huggingface.co` (model Whisper). Sau đó chạy offline được. |

Không dùng API trả phí nào. Faster-Whisper chạy hoàn toàn trên máy bạn.

---

## 2. Các file sẽ chạy

Tất cả nằm trong `scripts\anime_trial\` — **bấm đúp** để chạy:

| File | Làm gì | Khi nào |
|---|---|---|
| `1_CAI_DAT.bat` | Tìm Python 3.11 → tạo `.venv-anime` → (hỏi) cài FFmpeg → cài `faster-whisper`, `jsonschema`, và nếu có GPU NVIDIA thì `nvidia-cublas-cu12`, `nvidia-cudnn-cu12` → kiểm tra FFmpeg/GPU/CUDA → tải model `large-v3` và chạy thử 1 giây im lặng trên GPU (lỗi GPU thì thử CPU). | Một lần, hoặc khi báo lỗi môi trường |
| `2_CHAY_THU_DOAN_NGAN.bat` | Mở hộp chọn video → cắt **đoạn thử 45 giây** (bắt đầu ở giây 60 nếu video dài ≥ 3 phút, để bỏ qua phần mở đầu) → phân tích. | Luôn chạy cái này trước |
| `3_CHAY_TOAN_BO.bat` | Phân tích **toàn bộ** video (~8 phút). | Chỉ sau khi đoạn thử ổn |
| `4_CHAM_DIEM.bat` | Đọc 2 file CSV bạn đã điền, tính độ chính xác thoại / tỉ lệ cắt nhầm, cập nhật báo cáo. | Sau khi tự nghe & xem kiểm tra |

Mẹo: có thể **kéo thả file video lên** `2_...bat` / `3_...bat` để bỏ qua hộp chọn file;
kéo thả **thư mục kết quả** lên `4_CHAM_DIEM.bat` để chấm đúng lần chạy đó.

Tùy chọn nâng cao (chạy trong cửa sổ cmd tại thư mục gốc OpenMontage):

```bat
.venv-anime\Scripts\python.exe -m lib.anime_trial check
.venv-anime\Scripts\python.exe -m lib.anime_trial run --mode clip --clip-start 120 --clip-seconds 60
.venv-anime\Scripts\python.exe -m lib.anime_trial run --mode full --device cpu --model-size medium
```

`--device auto|cuda|cpu` chọn thiết bị phiên âm (mặc định `auto` = GPU nếu dùng được, lỗi GPU sẽ tự chuyển CPU và ghi rõ trong báo cáo).

---

## 3. Đặt video ở đâu

```
D:\OpenMontage\
└── anime_trial\
    └── video_vao\          ← ĐẶT FILE MP4 VÀO ĐÂY (tự tạo ở lần chạy đầu)
```

Hộp chọn file mở sẵn thư mục `anime_trial\video_vao`, nhưng bạn có thể chọn video ở ổ đĩa
khác. Không cần phụ đề. Tên file có tiếng Nhật/tiếng Việt vẫn được (công cụ tự chép sang tên an toàn).

Thư mục `anime_trial\` đã được đưa vào `.gitignore` — **video, keyframe, transcript và kết
quả không bao giờ bị đưa lên GitHub**.

---

## 4. Kết quả nằm ở đâu

Mỗi lần chạy tạo một thư mục mới:

```
anime_trial\ket_qua\<ten-video>_<clip|full>_<ngay-gio>\
├── trial_report.md                ← ĐỌC FILE NÀY TRƯỚC (báo cáo tiếng Việt)
├── trial_report.json              (cùng nội dung, dạng máy đọc)
├── anime_source_index.json        ← kết quả chính, đúng định dạng Giai đoạn 1
├── review_packet.md               ← danh sách cảnh + keyframe + thoại để AI xem hình
├── visual_annotations.template.json
├── keyframes\sc001_kf01.jpg …     ← ảnh từng cảnh
├── kiem_tra_loi_thoai.csv         ← bạn điền khi nghe kiểm tra thoại (mở bằng Excel)
├── kiem_tra_canh.csv              ← bạn điền khi xem kiểm tra cảnh
└── work\                          (đoạn clip, proxy, transcript gốc của Whisper)
```

**Lưu ý đoạn thử:** mọi timestamp trong kết quả đoạn thử tính từ **đầu đoạn thử**.
Báo cáo ghi rõ cần cộng bao nhiêu giây để ra thời điểm trong phim gốc.

---

## 5. Báo cáo kiểm tra những gì

`trial_report.md` gồm:

1. **Thời gian xử lý từng công đoạn** (chuẩn bị file, cắt đoạn thử, đọc thông số, proxy 360p,
   phát hiện cảnh, trích keyframe, phiên âm, gán thoại, đo độ to, ghi file, kiểm tra) và **tổng**.
   Thiết bị thật sự dùng để phiên âm (GPU hay CPU) cũng được ghi lại.
2. **Kiểm tra tự động**: schema JSON; cảnh liền mạch, khớp lưới frame; keyframe tồn tại và
   nằm trong cảnh; thoại nằm trong cảnh; không trùng ID; thống kê thoại (độ tin cậy, câu nghi
   ảo giác của Whisper, câu nói nhanh bất thường); số cảnh, độ dài TB, số lần cắt/phút và
   **điểm cắt nghi nhầm** (hai keyframe hai bên điểm cắt giống nhau, SSIM ≥ 0.92).
3. **Các bước CHƯA tự động hoàn thành** — luôn hiện rõ, không bao giờ báo xong giả:

| Bước | Trạng thái ban đầu | Ai làm |
|---|---|---|
| Phân tích hình ảnh bằng AI (nhân vật, hành động, phản ứng) | **PENDING** | Claude: gửi `review_packet.md` + thư mục `keyframes` cho Claude, Claude điền `visual_annotations.template.json`, rồi chạy `python -m lib.anime_source_indexer annotate` |
| Nghe kiểm tra lời thoại tiếng Nhật | **PENDING** | Bạn: `kiem_tra_loi_thoai.csv` |
| Xem kiểm tra điểm cắt cảnh | **PENDING** | Bạn: `kiem_tra_canh.csv` |
| Chạy toàn bộ tập | **NOT_RUN** (ở chế độ đoạn thử) | Bạn: `3_CHAY_TOAN_BO.bat` |

Trạng thái chung chỉ có thể là `FAIL` (có lỗi) hoặc `PENDING` (còn việc thủ công) cho đến khi
mọi bước trên được làm thật.

---

## 6. Tự kiểm tra độ chính xác (khoảng 10–15 phút cho đoạn 45 giây)

**Lời thoại** — mở `kiem_tra_loi_thoai.csv` bằng Excel, mở video đoạn thử (`work\clip.mp4`)
bằng trình phát có hiển thị giây (VLC, MPC-HC):

- cột `ket_qua`: `DUNG` (đúng), `SAI` (sai), hoặc `BO_QUA` (không nghe rõ / nhạc nền).
  Viết có dấu cũng được (`ĐÚNG`, `bỏ qua`).
- nếu `SAI`: gõ câu tiếng Nhật đúng vào `text_ja_dung` (để tính CER — tỉ lệ lỗi ký tự).

**Cảnh** — mở `kiem_tra_canh.csv`, xem keyframe trong thư mục `keyframes`:

- cột `ket_qua`: `DUNG` nếu đầu cảnh đúng là một điểm cắt thật, `CAT_NHAM` nếu không phải.
  Cột `nghi_cat_nham = CO` là những chỗ công cụ tự nghi ngờ — xem kỹ.
- cột `sot_canh`: số điểm cắt thật bị bỏ sót bên trong cảnh đó (để trống = 0).

Lưu file (giữ định dạng CSV UTF-8), rồi chạy `4_CHAM_DIEM.bat`. Ngưỡng đánh giá:
thoại đúng ≥ 85% và CER ≤ 15% → PASS; cắt nhầm ≤ 10% và bỏ sót ≤ 10% số cảnh → PASS.
Điền chưa hết → `PARTIAL`, không bao giờ PASS.

---

## 7. Trình tự đề xuất

1. Chạy `1_CAI_DAT.bat` → mọi dòng `PASS` (dòng `WARN` về jsonschema/GPU có thể chấp nhận).
2. Đặt 1 tập vào `anime_trial\video_vao`, chạy `2_CHAY_THU_DOAN_NGAN.bat`.
3. Đọc `trial_report.md`: có `FAIL` không? Phiên âm chạy GPU (`cuda`) hay bị chuyển CPU?
4. Điền 2 file CSV → `4_CHAM_DIEM.bat`.
5. Nếu ổn: `3_CHAY_TOAN_BO.bat`, rồi gửi cho Claude: `trial_report.md`, `review_packet.md`
   và thư mục `keyframes` (không cần gửi video) để làm bước phân tích hình ảnh.

---

## 8. Xử lý lỗi thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| `Không tìm thấy Python 3.11 hoặc 3.12` | Cài Python 3.11 từ python.org, tick "Add python.exe to PATH", mở lại cửa sổ. |
| `không tìm thấy ffmpeg trong PATH` sau khi cài | Đóng mọi cửa sổ cmd rồi chạy lại `1_CAI_DAT.bat` (PATH mới chỉ có hiệu lực ở cửa sổ mới). |
| `cublas64_12.dll` / `cudnn_ops64_9.dll` not found | Chạy lại `1_CAI_DAT.bat`. Công cụ tự nạp DLL từ `.venv-anime\Lib\site-packages\nvidia\*\bin`. Vẫn lỗi → cập nhật driver NVIDIA. Trong lúc đó vẫn chạy được bằng CPU (báo cáo ghi rõ). |
| Hết bộ nhớ GPU (out of memory) | Đóng game/trình duyệt nặng; hoặc `--model-size medium`; hoặc `--device cpu`. |
| Tải model `huggingface.co` lỗi | Kiểm tra mạng / tường lửa; chạy lại `1_CAI_DAT.bat`. |
| `scene_detect_degenerate` (chỉ 1 cảnh cho cả video) | Báo lại cho Claude kèm báo cáo — phát hiện cảnh đã thất bại, đừng dùng kết quả đó. |
| Excel hiện chữ Nhật lỗi font | File đã có BOM UTF-8; nếu vẫn lỗi, dùng Data → From Text/CSV → UTF-8. |
| Thoại toàn câu kiểu 「ご視聴ありがとうございました」 | Đó là ảo giác quen thuộc của Whisper trên đoạn nhạc/im lặng — công cụ đã đánh dấu `known_hallucination`, không dùng làm bằng chứng. |
