# Thử nghiệm Tầng 1 — Qwen3-VL hiểu phim (offline, $0)

Mục tiêu: kiểm chứng xem một model mã nguồn mở chạy **offline** trên RTX 3060 12 GB có thật sự *hiểu* được
diễn biến một tập Shin-chan hay không — nhân vật muốn gì, vấp phải gì, sai ở đâu, phản ứng ra sao, nguyên nhân →
hành động → phản ứng → hậu quả, hiểu lầm, setup → payoff của tình huống hài và **vì sao** nó buồn cười.

- Ngân sách API: **$0**. Không gọi Anthropic hay dịch vụ đám mây nào. Keyframe và transcript **không rời máy**.
- Mạng chỉ dùng **một lần** để tải thư viện (pypi.org, download.pytorch.org) và model (huggingface.co).
  Sau đó chạy hoàn toàn **offline**.
- Không đụng tới Whisper / `.venv-anime`: Qwen3-VL cài trong venv riêng `.venv-qwen`.
- Chưa có kịch bản, TTS hay render ở bước này. Tầng 2 (biên kịch) chỉ làm sau khi Tầng 1 được chứng minh.

> Bộ thử nghiệm đã được kiểm thử bằng model giả trên Linux. **Chưa có lần chạy model thật nào** — lần đầu bạn
> chạy trên RTX 3060 chính là lần đo thật đầu tiên. Không có con số tốc độ/VRAM nào ở đây là đã đo.

## 1. Model và cách chạy

| Thành phần | Dùng gì | Ghi chú |
|---|---|---|
| Model chính | **`Qwen/Qwen3-VL-4B-Thinking`** (trọng số chính thức của QwenLM trên Hugging Face) | Bản "Thinking" suy luận từng bước trước khi trả lời |
| Cách nạp | Hugging Face `transformers` (`Qwen3VLForConditionalGeneration`), lượng tử hóa 4-bit **NF4** bằng `bitsandbytes` | Ước tính ~3–4 GB VRAM cho trọng số; tổng thực tế được đo và ghi trong báo cáo |
| Model so sánh (tùy chọn) | `qwen3-vl:4b` qua Ollama | `--backend ollama` — xem mục 6 |
| Thư viện | `torch`/`torchvision` (CUDA 12.6), `transformers>=4.57`, `accelerate`, `bitsandbytes>=0.45`, `huggingface_hub` | Cài vào `.venv-qwen` |

**Đọc video thế nào:** model không xem "video liền mạch". Mỗi đoạn ~40 giây (vài cảnh liên tiếp) được đưa vào
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
| Answerer | Qwen3-VL | Đọc khung hình + thoại → sự kiện, nhân quả, hiểu lầm, leo thang/đảo ngược, setup/payoff, vì sao buồn cười; được **xin thêm khung hình** ở đoạn còn mơ hồ |
| Grounder | code | Khớp mốc thời gian với khung hình thật đã đưa, chỉ giữ id thoại có thật, gán cảnh; không có bằng chứng → hạ thành giả thuyết |
| Verifier | Qwen3-VL | Đưa lại đúng các khung hình/thoại của từng SUY LUẬN, hỏi "bằng chứng có ủng hộ không" → bác bỏ thì hạ thành GIẢ THUYẾT |
| Tổng hợp | Qwen3-VL | Nối toàn tập: chuỗi nhân quả, xung đột, bước ngoặt, các gag; id lạ bị loại, không tự bịa |

Mỗi kết luận được gắn nhãn **QUAN SÁT** (thấy/nghe trực tiếp) · **SUY LUẬN** (suy ra từ bằng chứng) ·
**GIẢ THUYẾT** (chưa xác minh — không bao giờ dùng làm sự thật).

## 3. Các bước chạy

Điều kiện: đã chạy Giai đoạn 2 cho tập #253-1 (có thư mục `anime_trial\ket_qua\<...>` chứa
`anime_source_index.json` và `keyframes\`), máy còn **~15 GB** ổ đĩa, driver NVIDIA mới.

1. **`scripts\anime_trial\6_CAI_DAT_QWEN3_VL.bat`** — tạo `.venv-qwen`, cài thư viện, tải `Qwen/Qwen3-VL-4B-Thinking`
   (~9 GB, lần đầu), rồi **chạy thử thật** 1 khung hình màu đỏ: in ra câu trả lời, thời gian, VRAM đỉnh.
   Dòng `"ok": true` = model chạy được trên GPU của bạn.
2. **`scripts\anime_trial\7_QWEN3_VL_HIEU_PHIM.bat`** — kéo thả thư mục kết quả #253-1 lên file (hoặc bấm đúp để
   dùng lần chạy mới nhất). Chọn:
   - `1` = chạy thử **2 đoạn đầu** (khuyên dùng lần đầu, để đo tốc độ và xem chất lượng);
   - `2` = **cả tập**.
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
├── qwen_trial_report.md            ← model thật đã chạy, tốc độ, VRAM, lỗi, kết quả chấm
├── qwen_trial_report.json
└── frames\                         (khung hình đã đưa cho model)
anime_trial\ket_qua\so_sanh_model.csv  ← mỗi lần chạy một dòng: model, tốc độ, VRAM, số kết luận, độ chính xác
```

## 5. Báo cáo đo những gì

- **Model thực tế**: backend, `model_id`, lượng tử hóa, tên GPU.
- **Tốc độ**: tổng thời gian, số lần gọi model, token/giây trung bình; **VRAM đỉnh** (PyTorch); số lần chạm giới
  hạn độ dài trả lời (dấu hiệu model "nghĩ" quá dài).
- **Suy luận**: số kết luận theo QUAN SÁT/SUY LUẬN/GIẢ THUYẾT; verifier ủng hộ/một phần/bác bỏ; số lần xin thêm
  bằng chứng; số đoạn model trả lời không đọc được (JSON lỗi) — đoạn đó bị đánh dấu thất bại, **không tự bịa**.
- **Độ chính xác thật** chỉ có sau khi bạn chấm file CSV (bước 4–5). Trạng thái luôn là `PENDING` cho tới lúc đó.

## 6. So sánh với model khác (tùy chọn, vẫn offline)

Cài Ollama (ollama.com), chạy `ollama pull qwen3-vl:4b` (hoặc tag khác có trong thư viện Ollama), rồi:

```bat
.venv-qwen\Scripts\python.exe -m lib.anime_story_reasoner run --backend ollama --model qwen3-vl:4b --run "anime_trial\ket_qua\<...>"
```

Mỗi lần chạy thêm một dòng vào `so_sanh_model.csv`. Claude Sonnet/Opus chỉ được thêm vào bảng này **sau**, khi bạn
cho phép dùng API trả phí. **Không model nào được coi là tốt nhất trước khi có điểm chấm thật.**

## 7. Xử lý lỗi

| Hiện tượng | Cách xử lý |
|---|---|
| `PyTorch does not see a CUDA GPU` | Chạy lại `6_CAI_DAT_QWEN3_VL.bat`; kiểm tra driver NVIDIA (`nvidia-smi`). |
| Lỗi `bitsandbytes` trên Windows | Thử `--quant bf16` (không lượng tử hóa, ~9 GB VRAM cho trọng số — có thể thiếu VRAM). |
| Hết VRAM (CUDA out of memory) | Giảm khung hình: `--frames-per-window 8` hoặc đoạn ngắn hơn: `--window-seconds 25`. Đóng game/trình duyệt. |
| Nhiều đoạn "không đọc được JSON" / chạm giới hạn độ dài | Model 4B nghĩ quá dài hoặc lạc định dạng — ghi lại trong báo cáo; đây chính là dữ liệu đánh giá model. |
| Chạy quá lâu | Dùng lựa chọn `1` (2 đoạn) để đo trước; tốc độ thật được ghi trong `qwen_trial_report.md`. |

Khi chạy xong, gửi cho Claude: `ban_do_cot_truyen.md`, `qwen_trial_report.md`, `so_sanh_model.csv` và (nếu đã chấm)
`kiem_tra_ban_do_cot_truyen.csv`. Không cần gửi video.
