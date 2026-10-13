# AI News Radar

Quét tin AI mỗi ngày — **benchmark, tính năng mới, model miễn phí, giảm giá, tin reset giới hạn dùng của Claude / Codex** —
rồi xuất ra đúng định dạng của [`tool-marketing-studio`](https://github.com/vohoailinh90/tool-marketing-studio)
để studio render ảnh và đăng Facebook (3 ngôn ngữ: Tiếng Việt, 日本語, English).

```
nguồn → thu thập → phát hiện (luật) → xếp hạng → bundle cho studio → studio check → duyệt → đăng
```

Không có model nào trong vòng lặp này: mọi bước đều là script có thể kiểm tra lại. Bài đăng là **bản nháp**
(`status: draft`); duyệt và đăng vẫn là việc của bạn trong studio.

## Chạy

```bash
pip install -r requirements.txt
python radar.py scan                      # lấy tin từ mọi nguồn, lưu data/last-scan.json
python radar.py export                    # tạo out/ai-news-YYYY-MM-DD/ (cây thư mục giống studio)
python radar.py run                       # scan + export (GitHub Action chạy lệnh này mỗi ngày)
python radar.py export --studio ../tool-marketing-studio     # ghi thẳng vào bản clone của studio
python radar.py verify --studio ../tool-marketing-studio --slug ai-news-2026-10-13   # chạy `studio.py check` của chính studio
```

Mã thoát: `0` xong · `2` mọi nguồn đều lỗi · `3` hôm nay không có tin đủ điểm · `4` từ chối ghi đè.

Đưa vào studio: chép `out/ai-news-<ngày>/tools/*` và `out/ai-news-<ngày>/campaigns/*` vào cùng thư mục của studio
(hoặc dùng `--studio`), rồi trong studio: `python studio.py check ai-news-<ngày>` → render → đọc từng ảnh → duyệt → đăng.
Mỗi ngày là một cặp tool + campaign riêng (`ai-news-<ngày>`) vì studio buộc mọi con số trong bài phải nằm trong brief
của đúng tool đó. Xong đợt đăng thì xoá thư mục cũ nếu muốn gọn.

## Nguồn tin — cái nào đã chạy thật, cái nào chưa

| Nguồn | Loại | Tin cậy | Đã chạy thật? |
|---|---|---|---|
| Claude Code changelog | `changelog_md` | chính thức | ✅ 2026-10-13 (dữ liệu thật trong `tests/fixtures`) |
| Anthropic news (bản feed của cộng đồng, link trỏ về anthropic.com) | `rss` | chính thức | ✅ 2026-10-13 |
| Aider polyglot leaderboard | `yaml_leaderboard` | chính thức | ✅ nhưng **nguồn đã cũ**: mục mới nhất 2025-10-03 — run report ghi `STALE` |
| OpenRouter (model mới, miễn phí, giảm giá) | `openrouter` | chính thức | ❌ chưa từng gọi thật |
| Hacker News (Algolia) | `hn` | **cộng đồng** | ❌ chưa từng gọi thật |
| Codex / Gemini CLI releases (Atom), OpenAI news, DeepMind blog, Hugging Face blog | `rss` | chính thức | ❌ chưa từng gọi thật |

Repo này được dựng trong một môi trường có chính sách mạng chặn các host ❌ ở trên, nên parser của chúng được kiểm thử
bằng **fixture tổng hợp** (`tests/fixtures/synthetic_*`, ghi rõ trong file) theo đúng định dạng API công bố — chưa phải
phản hồi thật. **Việc đầu tiên nên làm:** chạy `python radar.py scan` một lần trên máy bạn hoặc bằng nút *Run workflow*
của Action, xem cột `ok/FAIL` ở từng nguồn. Nguồn lỗi chỉ bị ghi vào báo cáo, không làm hỏng cả lượt quét.
Trường `live_checked` trong `config/sources.yaml` ghi ngày mỗi URL được kiểm chứng; `false` = việc cần làm.

Chưa có: Reddit và X (nơi tin reset quota thường xuất hiện đầu tiên) — cần đăng nhập hoặc bị chặn. Tin reset quota
hiện chỉ bắt được khi một nguồn trên đăng tiêu đề kiểu "weekly usage limits have been reset".

## Độ tin cậy của từng tin

- Nguồn `primary` (vendor / API giá / file leaderboard) → `verified: true`.
- Nguồn `community` (Hacker News) → `verified: false`, bài có slide **"Tin cộng đồng, chưa xác nhận"**.
  Khi một nguồn chính thức nói cùng vendor + chủ đề trong 48 giờ thì mới được nâng lên đã xác nhận.
- Luật phát hiện khớp **cụm hành động** (reset, tăng gấp đôi, miễn phí đến, giảm 30%…), không khớp danh từ trơn
  như "limit" hay "free". Các dòng "Fixed…" bị loại. Luật nằm trong `config/rules.yaml` — sửa ở đó, không sửa code.
- "Tính năng nổi bật" trong changelog chọn bằng từ khoá (tối đa 3/bản). Đây là heuristic, bạn vẫn phải đọc lại.
- Mọi chuỗi giống URL/tên miền bị xoá khỏi bài (studio từ chối chúng); link nguồn chỉ nằm ở bình luận đầu tiên qua `{link}`.
  Studio sẽ gắn thêm tham số `utm_*` vào các link này như với mọi link khác.

## Cấu hình

- `config/sources.yaml` — nguồn, độ tin cậy, tham số từng nguồn.
- `config/rules.yaml` — luật phát hiện, trọng số xếp hạng, ngưỡng xuất (`min_score`, `max_stories`, `spotlights`).
- Lần quét đầu tiên là **baseline** cho OpenRouter và leaderboard (chưa có gì để so sánh nên không phát tin);
  changelog chỉ báo bản mới nhất. Trạng thái nằm ở `data/state.json` (Action tự commit). File hỏng sẽ dừng lượt chạy
  thay vì âm thầm reset và báo lại tin cũ.

## Phát triển

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 42 test; test hợp đồng với studio tự bỏ qua nếu không có bản clone studio
STUDIO_PATH=../tool-marketing-studio python -m pytest -q
```

Mẫu thư mục: `radar/` (code), `config/`, `tests/`, `data/` (trạng thái), `out/` (bundle xuất ra).
Giao diện/CLI của repo này bằng tiếng Anh vì nó là công cụ phát triển; chữ trong bài đăng nằm ở `radar/templates.py`
và có đủ vi/ja/en.
