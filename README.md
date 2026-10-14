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

Mã thoát: `0` xong · `2` mọi nguồn đều lỗi / tham số sai · `3` hôm nay không có tin đủ điểm · `4` từ chối ghi đè · `5` bài chứa chuỗi giống link (bị chặn, không ghi gì). Lỗi không lường trước thoát bằng mã `1` kèm traceback.

Đưa vào studio: chép `out/ai-news-<ngày>/tools/*` và `out/ai-news-<ngày>/campaigns/*` vào cùng thư mục của studio
(hoặc dùng `--studio`), rồi trong studio: `python studio.py check ai-news-<ngày>` → render → đọc từng ảnh → duyệt → đăng.
Mỗi ngày là một cặp tool + campaign riêng (`ai-news-<ngày>`) vì studio buộc mọi con số trong bài phải nằm trong brief
của đúng tool đó. Xong đợt đăng thì xoá thư mục cũ nếu muốn gọn.

## Nguồn tin — cái nào đã chạy thật, cái nào chưa

| Nguồn | Loại | Tin cậy | Đã chạy thật? |
|---|---|---|---|
| Claude Code changelog | `changelog_md` | chính thức | ✅ 2026-10-13 (dữ liệu thật trong `tests/fixtures`) |
| Anthropic news (đọc qua bản feed của cộng đồng; mỗi tin được xác nhận bằng trang anthropic.com) | `rss` | chính thức **sau khi xác nhận** | ✅ 2026-10-13 |
| Aider polyglot leaderboard | `yaml_leaderboard` | chính thức | ✅ nhưng **nguồn đã cũ**: mục mới nhất 2025-10-03 — run report ghi `STALE` |
| OpenRouter (model mới, miễn phí, giảm giá) | `openrouter` | chính thức | ❌ chưa từng gọi thật |
| Hacker News (Algolia) | `hn` | **cộng đồng** | ❌ chưa từng gọi thật |
| Codex / Gemini CLI releases (Atom; phần mô tả bản phát hành được đọc từng dòng như changelog), OpenAI news, DeepMind blog, Hugging Face blog | `rss` | chính thức | ❌ chưa từng gọi thật |

Repo này được dựng trong một môi trường có chính sách mạng chặn các host ❌ ở trên, nên parser của chúng được kiểm thử
bằng **fixture tổng hợp** (`tests/fixtures/synthetic_*`, ghi rõ trong file) theo đúng định dạng API công bố — chưa phải
phản hồi thật. **Việc đầu tiên nên làm:** chạy `python radar.py scan` một lần trên máy bạn hoặc bằng nút *Run workflow*
của Action, xem cột `ok/FAIL` ở từng nguồn. Nguồn lỗi chỉ bị ghi vào báo cáo, không làm hỏng cả lượt quét.
Trường `live_checked` trong `config/sources.yaml` ghi ngày mỗi URL được kiểm chứng; `false` = việc cần làm.

Chưa có: Reddit và X (nơi tin reset quota thường xuất hiện đầu tiên) — cần đăng nhập hoặc bị chặn. Tin reset quota
hiện chỉ bắt được khi một nguồn trên đăng tiêu đề kiểu "weekly usage limits have been reset".

## Độ tin cậy của từng tin

- Nguồn `primary` (vendor / API giá / file leaderboard) → `verified: true`.
- Nguồn `community` (Hacker News) → `verified: false`: bài có slide **"Tin cộng đồng, chưa xác nhận"** và dòng nói về tin đó trong caption mang nhãn
  `[chưa xác nhận]` / 【未確認】 / `[unconfirmed]`. Chỉ được nâng lên đã xác nhận khi một nguồn chính thức nói **cùng vendor, cùng chủ đề và
  có ít nhất 2 từ khoá chung** (không tính tên vendor) trong 48 giờ; vendor "other" không bao giờ đối chứng.
- Anthropic không có feed chính thức, nên tin được đọc qua một feed mirror của cộng đồng. Vì thế mirror **không** được tin: chỉ nhận mục có link
  trỏ về anthropic.com (`allowed_hosts`), và với mỗi tin radar **tự tải trang đó** rồi so tiêu đề (`og:title` hoặc `<title>`, so bằng, không so tiền tố).
  Khớp thì mới là nguồn chính thức; không khớp, không tải được, hay vượt giới hạn 15 trang mỗi lượt thì tin bị hạ xuống "cộng đồng, chưa xác nhận"
  và run report ghi lý do. Cơ chế này bật bằng `confirm_page: true` trong `config/sources.yaml`. Vì chỉ **tiêu đề** được xác nhận, nguồn
  bật `confirm_page` chỉ được nhận diện trên tiêu đề, không bao giờ trên phần tóm tắt do mirror cung cấp.
- Luật phát hiện khớp **cụm hành động** (reset, tăng gấp đôi, miễn phí đến, giảm 30%…), không khớp danh từ trơn như "limit" hay "free".
  Các chủ đề thương mại (giới hạn, miễn phí, giá) chỉ tính khi văn bản nhắc tên một hãng AI; tên model phải kèm phiên bản/tầng
  ("GPT-6", "Claude Opus 9", không phải "Claude Code"). Dòng "Fixed…", "Show HN" bị loại. Luật nằm trong `config/rules.yaml` — sửa ở đó,
  không sửa code, và thêm một dòng vào `tests/test_headlines.py` (bảng tiêu đề có nhãn, cả dòng "phải không ra tin").
- "Tính năng nổi bật" trong changelog chọn bằng từ khoá (tối đa 3/bản). Đây là heuristic, bạn vẫn phải đọc lại.
- Mọi chuỗi giống URL/tên miền bị xoá khỏi bài (studio từ chối chúng); link nguồn chỉ nằm ở bình luận đầu tiên qua `{link}`.
  Studio sẽ gắn thêm tham số `utm_*` vào các link này như với mọi link khác.

## Cấu hình

- `config/sources.yaml` — nguồn, độ tin cậy, tham số từng nguồn.
- `config/rules.yaml` — luật phát hiện, trọng số xếp hạng, ngưỡng xuất (`min_score`, `max_stories`, `spotlights`).
- Tin đủ điểm nhưng chưa vào số hôm nay (vượt `max_stories`, hoặc lệnh export bị từ chối) được giữ trong `data/last-scan.json`
  tối đa `carry_days` ngày và chấm điểm lại (tin cũ nhạt dần) — không bị mất. Ngày của số báo tính theo giờ Việt Nam (`timezone_offset_hours`).
- Lần quét đầu tiên là **baseline** cho OpenRouter và leaderboard (chưa có gì để so sánh nên không phát tin);
  changelog chỉ báo bản mới nhất. Trạng thái nằm ở `data/state.json` (Action tự commit). File hỏng sẽ dừng lượt chạy
  thay vì âm thầm reset và báo lại tin cũ — kể cả khi file là JSON hợp lệ nhưng sai hình dạng (`[]`, `null`…). Mỗi lượt quét ghi
  `last-scan.json` (nguyên tử) **trước** rồi mới ghi `state.json`, nên bị ngắt giữa chừng thì tin được phát hiện lại thay vì bị đánh dấu "đã thấy" mà chưa lưu ở đâu.

## Phát triển

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 489 test (gồm 300 ca ngẫu nhiên cố định hạt giống cho bộ chọn tin); test hợp đồng với studio tự bỏ qua nếu không có bản clone studio
STUDIO_PATH=../tool-marketing-studio python -m pytest -q
```

Mẫu thư mục: `radar/` (code), `config/`, `tests/`, `data/` (trạng thái), `out/` (bundle xuất ra).
Giao diện/CLI của repo này bằng tiếng Anh vì nó là công cụ phát triển; chữ trong bài đăng nằm ở `radar/templates.py`
và có đủ vi/ja/en.

## Giới hạn đã biết (đừng tin hơn mức này)

- Nhận diện bằng luật là heuristic. Bảng `tests/test_headlines.py` do người viết tay theo các dạng tiêu đề phổ biến, **không** phải một
  kho tiêu đề thật có gán nhãn; độ phủ trên tin thật chưa được đo. Cần chạy vài tuần rồi sửa luật theo kết quả.
- Tên hãng trong bài là tên chung (`vendor_names`): mọi tin của Google thành "Gemini", mọi tin của Anthropic thành "Claude".
- Bài tiếng Việt / Nhật dựng từ luật giữ nguyên **tiêu đề gốc tiếng Anh** (chỉ tin từ OpenRouter và leaderboard có câu dịch riêng).
  Slide spotlight ghi rõ "tiêu đề gốc"; muốn văn viết lại thì dùng bước "Viết bài" bằng AI của studio.
- Đối chứng ("tin cộng đồng được nguồn chính thức xác nhận") chỉ nhìn trong các tin còn nằm trong lượt quét + phần giữ lại, cần cùng vendor,
  cùng chủ đề, ≥ 2 từ khoá chung và cùng chiều ("tăng gấp đôi" không xác nhận "bị cắt"). Nó thiên về **không** xác nhận: một tin thật có thể
  vẫn mang nhãn chưa xác nhận, và đó là mặc định an toàn.
- Brief xuất ra cũng ghi rõ: tin chưa xác nhận có tiền tố `[unconfirmed]` ở cả `features[].fact` lẫn `claims[].text` (và `verified: false`).
- Một tin chính thức và bản tin cộng đồng về **cùng một việc** (`rank.same_story`) được gộp thành một tin: tin của nguồn chính thức là bản chính
  (kể cả khi bản cộng đồng điểm cao hơn), nguồn kia ghi trong brief (`also reported by …`) và không bị đăng lại ở số sau.
- Collector không bao giờ cắt hay giữ lại sự kiện: mọi thay đổi quan sát được (model mới, đổi giá, mục leaderboard mới) được ghi ngay vào
  `last-scan.json` trước khi đánh dấu "đã thấy". Bộ xuất bản mới là nơi trải tin: tối đa `max_per_source_kind` (2) tin cho mỗi nguồn và loại trong một
  số, phần còn lại ở lại làm tin chờ (`carry_days`). Ảnh chụp trạng thái của một nguồn chỉ được ghi khi nguồn đó xử lý xong; nguồn lỗi giữa chừng
  bị hoàn tác, nên lượt sau không lấy dữ liệu dở dang làm mốc.
- Một lần quan sát (id) gắn với **thế hệ** ảnh chụp mà nó được so (bộ đếm tăng dần, không phải đồng hồ), tính tuổi từ lúc quan sát chứ không từ ngày
  tạo model (ngày tạo giữ ở `detail`), và một bản chờ đã hết hạn vẫn chặn bản replay của nó: `carry_days` là cuối cùng.
- Gộp tin bắc cầu: mọi bản tin của cùng một việc (đã chọn, đang chờ ô, hay mới đến) gộp thành một tin, tin chính thức là bản chính; tin bị đẩy
  ra hoặc chưa có chỗ vẫn "hút" các bản tin đến sau, nên không bao giờ có chuyện bản cộng đồng được đăng trước còn bản chính thức chờ số sau.
  `tests/test_select_properties.py` kiểm điều đó trên 300 bộ sự kiện ngẫu nhiên.
- Tin chính thức chỉ gộp với bản tin cộng đồng khi cùng nằm trong cửa sổ 48 giờ (`corroboration_window_hours`); một lần lặp lại
  sau nhiều ngày là tin riêng, chưa xác nhận.
- Mọi chuỗi nhắc lại một tin chưa xác nhận (tiêu đề bìa, caption, slide số liệu, danh sách, dòng "tiêu đề gốc") đều mang nhãn; một test
  duyệt từng chuỗi của bài để chắc điều đó.
- Id sự kiện không phụ thuộc thứ tự hay giờ quét: dòng release dùng hash nội dung; thay đổi giá OpenRouter dùng ảnh chụp trước đó, nên chạy lại sau
  khi lưu trạng thái hỏng không đăng trùng, còn một lần giảm giá lặp lại thật vẫn là tin mới.
- Phần mô tả bản phát hành của Codex / Gemini CLI được tách theo dòng và bỏ các dòng "What's Changed", "New Contributors", "by @user". Đã thử trên HTML
  viết tay theo dạng của GitHub, **chưa** thử trên bản thật; recall của hai nguồn này nhiều khả năng còn thấp (nhiều dự án viết kiểu `feat(scope): …`).
- Chưa có Reddit/X. Chưa có cơ chế tự mở PR vào studio (cần token chéo repo do bạn tạo).
