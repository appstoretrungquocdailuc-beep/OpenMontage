# Thử nghiệm Tầng 1 — Qwen3-VL hiểu phim (offline, $0)

Mục tiêu: kiểm chứng xem một model mã nguồn mở chạy **offline** trên RTX 3060 12 GB có thật sự *hiểu* được
diễn biến một tập Shin-chan hay không — nhân vật muốn gì, vấp phải gì, sai ở đâu, phản ứng ra sao, nguyên nhân →
hành động → phản ứng → hậu quả, hiểu lầm, setup → payoff của tình huống hài và **vì sao** nó buồn cười.

- Ngân sách API: **$0**. Không gọi Anthropic hay dịch vụ đám mây nào. Keyframe và transcript **không rời máy**.
- Mạng chỉ dùng **một lần** để tải thư viện (pypi.org, download.pytorch.org) và model (huggingface.co).
  Sau đó chạy hoàn toàn **offline**.
- Không đụng tới Whisper / `.venv-anime`: Qwen3-VL cài trong venv riêng `.venv-qwen`.
- Chưa có kịch bản, TTS hay render ở bước này. Tầng 2 (biên kịch) chỉ làm sau khi Tầng 1 được chứng minh.

> **Lần chạy thật đầu tiên (RTX 3060, #253-1) đã THẤT BẠI** và bộ thử nghiệm đã được sửa theo kết quả đó:
> 1499 s cho 4 lượt gọi (16.87 token/s, VRAM 4662 MB), 3/4 lượt chạm giới hạn 6144 token, JSON bị cắt, 0 kết luận
> có bằng chứng — nhưng bước tổng hợp vẫn tự bịa logline. 4 lượt × 6144 token ÷ 16.87 token/s ≈ 1460 s: gần như
> toàn bộ thời gian là model **sinh token không giới hạn** (nhiều khả năng là phần "suy nghĩ"; con số chính xác sẽ
> có trong bảng từng lượt gọi của lần chạy mới). Bản sửa: các bước JSON ngắn có ngân sách token riêng, chặn tổng
> hợp khi không có bằng chứng, giới hạn số lượt gọi, ghi chẩn đoán từng lượt. Bản sửa mới chỉ kiểm thử bằng model
> giả trên Linux — **chưa chạy lại trên máy bạn**.

## 1. Model và cách chạy

| Thành phần | Dùng gì | Ghi chú |
|---|---|---|
| Model chính | **`Qwen/Qwen3-VL-4B-Thinking`** (trọng số chính thức của QwenLM trên Hugging Face) | Bản "Thinking" suy luận từng bước trước khi trả lời |
| Cách nạp | Hugging Face `transformers` (`Qwen3VLForConditionalGeneration`), lượng tử hóa 4-bit **NF4** bằng `bitsandbytes` | Ước tính ~3–4 GB VRAM cho trọng số; tổng thực tế được đo và ghi trong báo cáo |
| Model so sánh (tùy chọn) | `qwen3-vl:4b` qua Ollama | `--backend ollama` — xem mục 6 |
| Thư viện | `torch`/`torchvision` (CUDA 12.6), `transformers>=4.57`, `accelerate`, `bitsandbytes>=0.45`, `huggingface_hub` | Cài vào `.venv-qwen` |

**Đọc video thế nào:** model không xem "video liền mạch". Mỗi đoạn (20 giây khi thử, ~40 giây khi chạy cả tập) (vài cảnh liên tiếp) được đưa vào
dưới dạng **chuỗi khung hình theo thời gian**, trước mỗi ảnh có nhãn `[t=12.345s sc004]` là **thời điểm thật
trong phim**, kèm lời thoại Whisper (có id, mốc giờ, độ tin cậy). Đây là cách gắn thời gian tường minh; chưa dùng
chế độ video gốc của Qwen3-VL.

## 2. VideoMind — phần nào là code thật, phần nào là ý tưởng

**Không tích hợp mã nguồn hay trọng số VideoMind.** Các checkpoint VideoMind được công bố là LoRA trên
Qwen2-VL, không dùng chung với Qwen3-VL. Bộ thử nghiệm chỉ **áp dụng ý tưởng** chia vai của VideoMind, mỗi vai là
một lời gọi tới **cùng** model Qwen3-VL cục bộ hoặc là code:

| Vai | Thực hiện bằng | Việc |
|---|---|---|
| Planner | code (`lib/anime_story_reasoner.py`) | Chia cảnh thành các đoạn theo thời gian, giữ **trí nhớ câu chuyện** (tóm tắt đến hiện tại, mục tiêu nhân vật, chi tiết đã gài chờ payoff) |
| 1. Trích xuất | Qwen3-VL (xem ảnh) | **Chỉ** 2–4 sự kiện nhìn/nghe thấy trực tiếp, mỗi sự kiện có mốc giờ + bằng chứng. Ngân sách 512 token nghĩ / 768 token trả lời |
| Grounder | code | Khớp mốc thời gian với khung hình thật đã đưa, chỉ giữ id thoại có thật, gán cảnh; sự kiện **không có bằng chứng bị loại** |
| 2. Suy luận | Qwen3-VL (chỉ đọc chữ) | Nhân quả, mục tiêu/trở ngại/sai lầm nhân vật, hiểu lầm, setup → payoff, vì sao buồn cười — **bắt buộc dẫn id sự kiện đã có bằng chứng**; dẫn id lạ hoặc không dẫn gì → bị loại. 768 / 1024 token |
| 3. Verifier | Qwen3-VL | Đưa lại đúng khung hình/thoại của kết luận, hỏi "bằng chứng có ủng hộ không" → bác bỏ thì hạ thành GIẢ THUYẾT. 256 / 256 token |
| 4. Tổng hợp | Qwen3-VL | **Chỉ chạy khi có ít nhất 1 kết luận có bằng chứng**, chỉ trong phạm vi đã phân tích (bản thử ghi rõ THỬ NGHIỆM); mục nào không dẫn claim_id hợp lệ bị xoá, không hiển thị. 768 / 1536 token |

Model "Thinking" khi nghĩ hết ngân sách sẽ bị **buộc dừng nghĩ** (chèn `</think>`) rồi mới trả lời — không còn cảnh
nghĩ 6144 token rồi bị cắt. 0 kết luận có bằng chứng → **FAIL**, không có logline/tóm tắt nào được viết.

Mỗi kết luận được gắn nhãn **QUAN SÁT** (thấy/nghe trực tiếp) · **SUY LUẬN** (suy ra từ bằng chứng) ·
**GIẢ THUYẾT** (chưa xác minh — không bao giờ dùng làm sự thật).

## 3. Các bước chạy

Điều kiện: đã chạy Giai đoạn 2 cho tập #253-1 (có thư mục `anime_trial\ket_qua\<...>` chứa
`anime_source_index.json` và `keyframes\`), máy còn **~15 GB** ổ đĩa, driver NVIDIA mới.

1. **`scripts\anime_trial\6_CAI_DAT_QWEN3_VL.bat`** — tạo `.venv-qwen`, cài thư viện, tải `Qwen/Qwen3-VL-4B-Thinking`
   (~9 GB, lần đầu), rồi **chạy thử thật** 1 khung hình màu đỏ: in ra câu trả lời, thời gian, VRAM đỉnh.
   Dòng `"ok": true` = model chạy được trên GPU của bạn.
2. **`scripts\anime_trial\7_QWEN3_VL_HIEU_PHIM.bat`** — kéo thả thư mục kết quả #253-1 lên file (hoặc bấm đúp để
   dùng lần chạy mới nhất). **Không tải lại model** (model đã có trong bộ nhớ đệm từ bước 1). Chọn:
   - `1` = **1 đoạn ngắn 20 giây**, 8 khung hình, **tối đa 4 lượt gọi** (trích xuất, suy luận, kiểm chứng, tổng hợp).
     Ước tính 3–6 phút (trần ≈ 5900 token ở 16.9 token/s). **Làm bước này trước.**
   - `2` = 2 đoạn 30 giây, tối đa 8 lượt gọi;
   - `3` = **cả tập** — chỉ khi lựa chọn `1` cho ra sự kiện đúng với video.

   Trong cửa sổ PowerShell/CMD bạn thấy trực tiếp: đoạn nào, bao nhiêu frame, bước nào, lượt gọi thứ mấy, số token
   (cập nhật mỗi 5 giây), thời gian, và số **kết luận hợp lệ** sau mỗi đoạn. Chạm giới hạn lượt gọi thì dừng và ghi rõ.
3. Đọc kết quả trong `anime_trial\ket_qua\<...>\reasoner\`.
4. **Đối chiếu với video**: mở `kiem_tra_ban_do_cot_truyen.csv` bằng Excel, mở video tại từng mốc thời gian, ghi
   cột `ket_qua`: `DUNG` / `SAI` / `THIEU_BANG_CHUNG` / `KHONG_CHAC` (ghi chú thêm nếu cần), lưu lại.
5. **`scripts\anime_trial\8_CHAM_BAN_DO_COT_TRUYEN.bat`** — tính độ chính xác (tổng và theo QUAN SÁT/SUY LUẬN/
   GIẢ THUYẾT), cập nhật báo cáo và bảng so sánh.

## 4. Kết quả nằm ở đâu

```
anime_trial\ket_qua\<ten>_full_<ngay-gio>\reasoner\
├── ban_do_cot_truyen.md            ← BẢN ĐỒ CỐT TRUYỆN TIẾNG VIỆT (đọc file này)
├── story_map.json                  (cùng nội dung, dạng máy đọc — dùng cho Tầng 2)
├── kiem_tra_ban_do_cot_truyen.csv  ← bạn chấm từng kết luận khi xem video
├── qwen_trial_report.md            ← model thật đã chạy, GPU, tốc độ, VRAM, BẢNG TỪNG LƯỢT GỌI, chẩn đoán, kết quả chấm
├── qwen_trial_report.json
├── raw\                            (nguyên văn từng câu trả lời, kể cả phần suy nghĩ — chỉ để gỡ lỗi)
└── frames\                         (khung hình đã đưa cho model)
anime_trial\ket_qua\so_sanh_model.csv  ← mỗi lần chạy một dòng: model, tốc độ, VRAM, số kết luận, độ chính xác
```

## 5. Báo cáo đo những gì

- **Model thực tế**: backend, `model_id`, lượng tử hóa, tên GPU.
- **Tốc độ**: tổng thời gian, số lần gọi model, token/giây trung bình; **VRAM đỉnh** (PyTorch).
- **Bảng từng lượt gọi**: bước (trích xuất / suy luận / kiểm chứng / tổng hợp), số ảnh, số giây, ngân sách, **token suy
  nghĩ**, **token trả lời**, có bị buộc dừng nghĩ không, có chạm giới hạn không, lỗi JSON, tên file thô trong `raw\`.
- **Chẩn đoán**: model nghĩ hết ngân sách hay câu trả lời quá dài; số mục suy luận bị loại vì dẫn id không tồn tại.
- **Suy luận**: số kết luận theo QUAN SÁT/SUY LUẬN/GIẢ THUYẾT; verifier ủng hộ/một phần/bác bỏ; số lần xin thêm
  bằng chứng; số đoạn model trả lời không đọc được (JSON lỗi) — đoạn đó bị đánh dấu thất bại, **không tự bịa**.
- **Trạng thái**: `FAIL` nếu không có kết luận nào có bằng chứng (khi đó không có logline/tóm tắt). Ngược lại là
  `PENDING` — **độ chính xác thật** chỉ có sau khi bạn chấm file CSV (bước 4–5).

## 6. So sánh với model khác (tùy chọn, vẫn offline)

Cài Ollama (ollama.com), chạy `ollama pull qwen3-vl:4b` (hoặc tag khác có trong thư viện Ollama), rồi:

```bat
.venv-qwen\Scripts\python.exe -m lib.anime_story_reasoner run --backend ollama --model qwen3-vl:4b --run "anime_trial\ket_qua\<...>"
```

Mỗi lần chạy thêm một dòng vào `so_sanh_model.csv` (có cột `pham_vi`, ví dụ `pilot 1/12`).

**Thử Qwen3-VL-4B-Instruct cho bước trích xuất (tùy chọn).** Bản Instruct không "suy nghĩ" nên trả lời ngắn và
nhanh hơn — có thể hợp với việc chỉ liệt kê sự kiện; bản Thinking vẫn dùng cho suy luận/kiểm chứng/tổng hợp. Cần tải
thêm **~9 GB** (một lần) và cả hai model cùng nằm trong VRAM (ước tính ~6–8 GB với NF4, chưa đo). Chỉ thử sau khi đã
có kết quả lựa chọn `1` để so sánh:

```bat
.venv-qwen\Scripts\python.exe -m lib.anime_story_reasoner run --run "anime_trial\ket_qua\<...>" --max-windows 1 --window-seconds 20 --frames-per-window 8 --verify-limit 1 --max-calls 4 --extract-model Qwen/Qwen3-VL-4B-Instruct
```

Chưa có dữ liệu nào cho thấy Instruct tốt hơn hay kém hơn — chỉ kết luận sau khi chấm cả hai lần chạy. Claude Sonnet/Opus chỉ được thêm vào bảng này **sau**, khi bạn
cho phép dùng API trả phí. **Không model nào được coi là tốt nhất trước khi có điểm chấm thật.**

## 7. Xử lý lỗi

| Hiện tượng | Cách xử lý |
|---|---|
| `PyTorch does not see a CUDA GPU` | Chạy lại `6_CAI_DAT_QWEN3_VL.bat`; kiểm tra driver NVIDIA (`nvidia-smi`). |
| Lỗi `bitsandbytes` trên Windows | Thử `--quant bf16` (không lượng tử hóa, ~9 GB VRAM cho trọng số — có thể thiếu VRAM). |
| Hết VRAM (CUDA out of memory) | Giảm khung hình: `--frames-per-window 8` hoặc đoạn ngắn hơn: `--window-seconds 25`. Đóng game/trình duyệt. |
| Lỗi JSON / chạm giới hạn | Xem bảng từng lượt gọi: cột token suy nghĩ / trả lời cho biết phần nào dài; mở file trong `raw\` để xem model đã viết gì. Đoạn đó bị đánh dấu thất bại, **không tự bịa**, không gọi lại cùng ngân sách. |
| `FAIL` — 0 kết luận có bằng chứng | Model chưa hiểu được đoạn này. Gửi `qwen_trial_report.md` + thư mục `raw\`; **không chạy cả tập**. |
| Chạy quá lâu | Lựa chọn `1` bị chặn ở 4 lượt gọi; tốc độ thật được ghi trong `qwen_trial_report.md`. |

Khi chạy xong, gửi cho Claude: `ban_do_cot_truyen.md`, `qwen_trial_report.md`, `so_sanh_model.csv`, thư mục `raw\`
(nhỏ, chỉ là chữ) và (nếu đã chấm) `kiem_tra_ban_do_cot_truyen.csv`. Không cần gửi video.
