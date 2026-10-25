# Giai đoạn 3 — Dùng Claude Pro hiểu phim và viết kịch bản (không API, $0)

Claude (tài khoản **Pro** của bạn trên claude.ai, hoặc **Claude Code** đăng nhập bằng tài khoản Pro) là bộ não chính:
hiểu cốt truyện, rồi viết kịch bản review tiếng Nhật 60–90 giây. OpenMontage chỉ làm phần máy:

- xuất **gói bằng chứng** từ video gốc + kết quả Giai đoạn 2;
- kiểm tra câu trả lời của Claude: mã frame có thật không, line_id có thật không, thời gian có khớp không, có kể
  giả thuyết như sự thật không;
- xuất `kich_ban.md` và `cut_list.csv`. **Không render.**

Không cần API key, không phát sinh phí API. Bước xuất gói **không chạy lại Whisper** và **không sửa** file nào của
Giai đoạn 1–2 (`anime_source_index.json`, keyframe, transcript). Mã nguồn và kết quả Qwen3-VL giữ nguyên để so sánh sau.

> Bộ công cụ đã được kiểm thử trên Linux bằng một video tổng hợp 70 giây. Các file `.bat` **chưa chạy trên Windows**.
> **Chưa có tập Shin-chan thật nào** được xuất gói, và Claude chưa phân tích tập nào. Lần đầu bạn chạy trên #253-1
> chính là lần thử thật đầu tiên.

## 1. Gói bằng chứng gồm những gì

Thư mục `anime_trial\ket_qua\<lần chạy #253-1>\claude_pro\<tên gói>\`:

| File | Dùng để |
|---|---|
| `01_khung_hinh.pdf` | **Tải lên Claude.** Khung hình theo đúng thứ tự thời gian. Mỗi ảnh ghi mã frame (`f004321` = frame 4321), thời điểm thật (`02:41.23`), mã cảnh, lý do chọn, câu thoại đang nói. Cuối mỗi trang in đầy đủ lời thoại của khoảng thời gian đó. File lớn hơn 24 MB tự tách thành `01_khung_hinh_phan1.pdf`, `_phan2.pdf`… |
| `02_loi_thoai_va_canh.md` | **Tải lên Claude.** Lời thoại tiếng Nhật (line_id, giây, độ tin cậy, cờ cảnh báo), bảng cảnh, mục lục khung hình, các điểm máy gợi ý xem kỹ |
| `03_PROMPT_HIEU_PHIM.md` | **Dán vào Claude** (bước hiểu phim). Gói tổng quan dùng `03_PROMPT_CHON_TINH_HUONG.md` |
| `04_PROMPT_VIET_KICH_BAN.md` | Dán vào Claude **sau khi bạn đã duyệt** phần hiểu phim |
| `bo_sung_01.pdf`, … | Khung hình dày hơn cho các đoạn Claude yêu cầu xem thêm |
| `hieu_phim.json` / `hieu_phim.md` | Câu trả lời hiểu phim của Claude, đã được máy kiểm tra (đọc file `.md`) |
| `kich_ban.json` / `kich_ban.md` / `anime_recap_plan.json` | Kịch bản Nhật + Việt, thời lượng, mốc nguồn từng câu |
| `cut_list.csv` | Danh sách cắt: frame vào/ra, giây, timecode, thời gian trên timeline, lời kể. Chỉ được tạo khi kiểm tra **OK** |
| `packet.json`, `frames\` | Dữ liệu máy đọc, ảnh gốc của từng frame |

**Gói không chứa tên tập hay tên file video** — Claude phải hiểu từ hình ảnh và lời thoại, không đoán theo tên tập.
Gói cũng không chứa bản đồ cốt truyện của Qwen.

**Không chỉ lấy keyframe thưa.** Gói chi tiết có:

- lưới đều mỗi 0,5 giây (giãn tới khoảng 0,75–1 giây nếu đoạn dài);
- frame đầu và cuối của **mọi cảnh**, mỗi câu thoại 2 frame;
- **4–5 frame/giây** ở các vùng máy gợi ý:
  - chuyển động nhanh (đo bằng ffmpeg);
  - câu nói to hoặc có dấu "！" (cao trào?);
  - câu có え？, 違う, なんで, 本当… (nghi hiểu lầm?);
  - cụm từ được nói lại sau đó (nghi setup → payoff?).

Các vùng này có **viền cam** trên PDF. Đây chỉ là tín hiệu kỹ thuật để xem kỹ hơn, **không phải kết luận về nội dung**.
Cần rõ hơn nữa thì dùng lựa chọn 3 (bổ sung): mặc định 8 frame/giây.

## 2. Cập nhật mã nguồn trên Windows

Chép đè từ nhánh `claude/charming-meitner-s9tzcb` vào thư mục OpenMontage:

- `lib\anime_claude_packet.py` (mới)
- `scripts\anime_trial\9_XUAT_GOI_CHO_CLAUDE.bat`, `scripts\anime_trial\10_NHAP_KET_QUA_CLAUDE.bat` (mới)
- `docs\anime-recap\HUONG_DAN_CLAUDE_PRO.md`, `docs\anime-recap\DECISIONS.md`
- (tùy chọn) `tests\lib\test_anime_claude_packet.py`, `tests\contracts\test_anime_trial_windows_kit.py`

Dùng lại `.venv-anime` của Giai đoạn 2. Lần đầu, `9_XUAT_GOI_CHO_CLAUDE.bat` sẽ tự cài thêm thư viện **Pillow**
(~3 MB, cần mạng một lần) để dựng trang PDF. Không tải model nào. Font chữ lấy sẵn trong Windows: Yu Gothic hoặc
Meiryo cho tiếng Nhật, Segoe UI hoặc Arial cho tiếng Việt.

## 3. Thử ngay trên tập #253-1 (một tình huống 60–90 giây)

**Bước A — chọn tình huống** (bỏ qua nếu bạn đã biết đoạn nào):

1. Bấm đúp `scripts\anime_trial\9_XUAT_GOI_CHO_CLAUDE.bat`, chọn `1`. Có thể kéo thả thư mục kết quả #253-1 lên
   file `.bat`; nếu không, máy dùng lần chạy mới nhất.
2. Mở claude.ai, tạo **cuộc trò chuyện mới**. Tải lên `01_khung_hinh.pdf` và `02_loi_thoai_va_canh.md` của gói
   `tong_quan_…`, rồi dán nội dung `03_PROMPT_CHON_TINH_HUONG.md`.
3. Claude đề xuất 1–3 đoạn, kết thúc bằng dòng `CHON: <bắt đầu>-<kết thúc>`. Bạn xem nhanh đoạn đó trong video để
   chắc đó là một tình huống trọn vẹn.

**Bước B — hiểu phim:**

4. Chạy lại `9_XUAT_GOI_CHO_CLAUDE.bat`, chọn `2`, nhập thời gian bắt đầu và kết thúc (ví dụ `2:10` và `3:30`; đoạn
   chi tiết cho phép 20–150 giây).
5. Tạo **cuộc trò chuyện mới** trên Claude để tránh lẫn với gói tổng quan. Tải lên `01_khung_hinh.pdf` (hoặc các
   phần `_phanN`) và `02_loi_thoai_va_canh.md` của gói `chi_tiet_…`, rồi dán `03_PROMPT_HIEU_PHIM.md`.
6. Nếu Claude ghi `evidence_requests` (cần xem thêm):
   - chạy `9_…bat` → `3`;
   - dán các khoảng thời gian, ví dụ `2:41-2:44, 3:02.5-3:04`;
   - tải `bo_sung_01.pdf` lên **cùng** cuộc trò chuyện.
7. Chạy `10_NHAP_KET_QUA_CLAUDE.bat` → `1`. Notepad mở ra: dán **toàn bộ** câu trả lời của Claude, lưu (Ctrl+S), đóng
   Notepad, quay lại cửa sổ đen bấm Enter. Máy kiểm tra và mở `hieu_phim.md`.
   - **FAIL**: chép mục "❌ Lỗi phải sửa" gửi lại cho Claude, nhận JSON mới, nhập lại.
   - **OK**: chỉ nghĩa là bằng chứng hợp lệ về mặt kỹ thuật. Máy **không biết** Claude hiểu đúng hay sai.
8. **Bạn đối chiếu `hieu_phim.md` với video**: mở video tại từng mốc thời gian.
   - Lời thoại trong 「」 là máy chèn từ transcript gốc, không phải Claude tự viết.
   - Sai chỗ nào thì nói cho Claude sửa, rồi nhập lại (bước 7).

**Bước C — duyệt và viết kịch bản:**

9. `10_NHAP_KET_QUA_CLAUDE.bat` → `2`, gõ `DUYET`. Việc duyệt gắn với nội dung file (SHA-256): `hieu_phim.json` đổi
   thì phải duyệt lại.
10. Trong **cùng** cuộc trò chuyện, dán `04_PROMPT_VIET_KICH_BAN.md`. Prompt này yêu cầu Claude áp dụng 5 skill
    **sw-sitcom-comedy, sw-japanese-screenwriting, sw-scene-craft, sw-character-conflict, sw-story-structure**:
    - nếu tài khoản claude.ai của bạn đã cài các skill này, Claude dùng trực tiếp;
    - nếu chưa, prompt có sẵn tóm tắt cách áp dụng từng skill.
11. `10_NHAP_KET_QUA_CLAUDE.bat` → `3`, dán câu trả lời. Máy tự tính frame, timeline, ghép lời kể và ước tính thời
    lượng đọc, rồi kiểm tra:
    - mỗi câu kể sự kiện (`event`/`quote`) phải dựa trên beat đã duyệt, **không phải giả thuyết**;
    - câu trích dẫn phải đúng nguyên văn lời thoại;
    - mỗi cut nằm trong phạm vi đã phân tích và trong cảnh đã ghi;
    - lời kể đọc kịp trong độ dài cut;
    - tổng 60–90 giây, có đủ hook → development → conflict → climax → resolution.
12. Đọc `kich_ban.md`: mỗi câu có tiếng Nhật, tiếng Việt, thời lượng dự kiến, mốc nguồn. `cut_list.csv` mở được
    bằng Excel. Trạng thái `CHO_DUYET` — **không có bước render nào chạy**.

Chỉ khi một tình huống chạy trọn quy trình và bạn thấy kịch bản **hay và đúng phim** mới mở rộng sang cả tập 7 phút
(làm từng tình huống, mỗi tình huống một gói).

## 4. Dùng Claude Code thay vì claude.ai (cũng không cần API key)

Nếu máy bạn có Claude Code đăng nhập bằng tài khoản Pro:

1. Mở thư mục OpenMontage trong Claude Code.
2. Nói: "Đọc `anime_trial\ket_qua\<…>\claude_pro\<gói>\03_PROMPT_HIEU_PHIM.md` và làm theo."

Claude Code đọc được PDF và ảnh trong `frames\`, tự ghi câu trả lời, tự chạy kiểm tra và sửa đến khi OK, tự xin
thêm khung hình bằng lệnh `more`. Prompt **cấm Claude Code tự duyệt** — bước 9 vẫn do bạn làm.

## 5. Giới hạn cần biết

- **claude.ai**:
  - mỗi file tối đa 30 MB, nên gói tự tách PDF ở mức 24 MB;
  - Claude đọc hình trong PDF tối đa khoảng 100 trang;
  - một gói chi tiết 90 giây thường 25–40 trang. Mình ước tính khoảng 2–3 nghìn token mỗi trang (chưa đo), nên mỗi
    lần phân tích tốn đáng kể hạn mức Pro.
  - Nên làm **một tình huống mỗi cuộc trò chuyện**.
- Claude **không nghe được âm thanh** — chỉ đọc lời thoại Whisper và nhìn khung hình. Câu Whisper nghi sai sẽ được ghi
  trong `dialogue_doubts` để bạn nghe lại.
- Timecode trong `cut_list.csv` là non-drop trên 24 khung/giây danh nghĩa. Cột `in_frame` / `out_frame` và
  `source_in_seconds` mới là mốc chính xác.
- Thời lượng lời kể là **ước tính** (6,5 ký tự/giây), chưa phải giọng đọc thật.

## 6. Xử lý lỗi

| Hiện tượng | Cách xử lý |
|---|---|
| `không tìm thấy video gốc` | Để video ở vị trí cũ của lần chạy Giai đoạn 2, hoặc chạy lệnh trực tiếp với `--video`: `.venv-anime\Scripts\python.exe -m lib.anime_claude_packet export --mode detail --start 2:10 --end 3:30 --video "D:\…\tap.mp4"` |
| `thiếu thư viện Pillow` | Chạy lại `9_…bat` khi có mạng (tự cài) |
| `đoạn chi tiết nên dài 20–150 giây` | Nhập lại thời gian; tình huống dài hơn thì chia làm hai gói |
| `không tìm thấy khối JSON` | Dán nguyên câu trả lời, gồm cả khối ```json …``` |
| `frame … không có trong gói` | Claude bịa hoặc gõ sai mã frame — gửi mục lỗi cho Claude |
| `GIẢ THUYẾT — không được kể như sự thật` | Kịch bản dựa trên điều chưa xác minh. Xin khung hình bổ sung để xác minh, hoặc bỏ câu đó |
| `chưa được bạn duyệt` / `đã thay đổi sau khi duyệt` | Chạy `10_…bat` → `2` |
| PDF chữ Nhật thành ô vuông | Máy không có font Nhật (Yu Gothic, Meiryo, MS Gothic). Lời thoại vẫn đầy đủ trong `02_loi_thoai_va_canh.md` |

Khi chạy xong, gửi cho Claude (phiên phát triển này):

- `hieu_phim.md` và `kich_ban.md`;
- `cut_list.csv` nếu có;
- nhận xét của bạn: chỗ nào Claude hiểu sai, câu nào chưa hay.

Không cần gửi video.
