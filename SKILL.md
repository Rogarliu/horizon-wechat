---
name: horizon-wechat
description: 生成「科技辉说AI日报」并推送到微信公众号草稿箱。把 Horizon 采集的多源科技资讯按资讯向口径打分筛选、英文翻中文、渲染成微信图文、建草稿。用于每日资讯下发、科技日报运营、公众号内容自动化。触发词：今日日报、科技辉说、生成今天的资讯、发公众号、推草稿箱、每日资讯。
---

# 科技辉说AI日报 → 公众号草稿

把 Horizon 当作内容大脑，本 skill 负责编辑口径、翻译、排版、配图、建草稿。

读者是「对科技感兴趣但不是从业者」的人，所以选题偏资讯、可读性优先，
不是技术深度优先。这条判断贯穿评分口径、信息源选择和区块写法。

## 什么时候用

- 用户说「生成今天的日报」「今天有什么值得看的」
- 用户要「把资讯发到公众号」「推草稿箱」
- 每天 07:00 的定时任务

## 前置条件

| 项 | 说明 |
|---|---|
| 公众号 | **已认证**（订阅号或服务号）。未认证账号没有 draft/add 权限 |
| 环境变量 | `WX_APPID` / `WX_SECRET` |
| Horizon | 已 clone 并 `uv sync` 过，`config.json` 里配好信息源与 AI key |
| 依赖 | `httpx`、`pillow` |

## 工作流

### 0. 首次安装

```bash
git clone --depth 1 https://github.com/Thysrael/Horizon.git ~/Horizon
cd ~/Horizon && uv sync
cp data/config.example.json data/config.json   # 然后按需改
```

`uv` 如果不在 PATH 上，`cli.py` 会自动去
`~/.workbuddy-ai/binaries/python/envs/default/bin/uv` 等常见位置找；
都找不到时回退到 `~/Horizon/.venv/bin/python` 直接执行。

### 1. 首次使用先自检

```bash
python scripts/doctor.py
```

验证 stable_token、封面上传、draft/add 三项权限。**不要跳过这一步**——
官方文档对认证订阅号的草稿箱权限表述含糊，先花 10 分钟确认，
比写完代码才发现没权限划算。

**已实测（2026-10-17，认证订阅号）**：五项全通，
`draft/add` 可用。所以这个号不需要降级到「手动粘贴 HTML」。

若报 48001，说明无草稿箱权限，改用 `cli.py preview` 生成 HTML 手动粘贴。

### 2. 配置

```bash
cp config.example.json config.json
```

关键字段：

- `horizon.repo_path`：Horizon 仓库路径
- `digest.title_prefix`：标题前缀，最终标题 ≤ 32 字（当前「科技辉说AI日报」）
- `digest.brand` / `tagline`：页脚署名，当前「科技辉说 · AI 资讯精选」
- `digest.headline_count` / `worth_count` / `scan_count`：三段式各段条数
- `digest.max_per_event`：同一事件最多几条，**默认 0（不限制）**，见关键约束第 16 条
- `translate.*`：英文标题与摘要翻中文，见下
- `state.seen_window_days`：多少天内出现过的不再上榜

### 2.1 编辑口径放在 skill 里，不放在 Horizon 仓库里

`~/Horizon/data/config.json` 的 `processing.profiles_dir` 指向本 skill 的
`profiles/` 目录（绝对路径），而不是 Horizon 自带的 `profiles/`。

这样做的原因：Profile 是「编辑方针」，属于这个号，不属于 Horizon 这个工具。
放在 skill 里既能随 skill 一起版本化，又不会让 Horizon 仓库变脏——
改它的 `profiles/` 会让以后 `git pull` 冲突。

当前只有一个 profile：`tech-news`，口径是**资讯优先、可读性优先**。
想调整选题倾向，只改 `profiles/tech-news/analysis.md` 的评分标准即可，
不用动代码。改完必须清空当天状态重跑，否则看到的是旧结果：

```bash
python scripts/cli.py run --dry-run --force
```

`--force` 会同时清掉 `daily_run` 和当天的「已见 URL」索引。后者必须清：
不清的话，今天刚抓过的 URL 全被判成「7 天内出现过」滤掉，
草稿删了却再也重建不出内容。

### 3. 先看排版，再碰微信

```bash
python scripts/cli.py preview --demo      # 样例数据
python scripts/cli.py run --dry-run       # 真实采集，只渲染
python scripts/cli.py preview --translate # 已有 items.json，只补翻译再渲染
```

产出 `out/preview-YYYY-MM-DD.html` 和 `out/cover-YYYY-MM-DD.jpg`。
`--demo` 走单独的 `preview-demo.html` / `cover-demo.jpg`，不会覆盖当天真实预览。
用浏览器打开预览，确认排版后再走下一步。**复核排版时用 800px 宽截图**，
420px 会因卡片比视口宽而把文字裁掉，看起来像布局坏了。

无头 Chrome 截图（比手工开浏览器快）：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
  --window-size=800,9500 --screenshot=/tmp/p.png \
  "file://$HOME/.workbuddy-ai/skills/horizon-wechat/out/preview-2026-10-24.html"
```

### 4. 建草稿

```bash
python scripts/cli.py run          # 采集 + 渲染 + 建草稿，一步到位
python scripts/cli.py draft        # 用已有 out/items.json 建草稿，不重新采集
```

完整链路：采集 → 打分 → 筛选 → 排重 → **翻译** → 渲染 → 配图 → 上传 → draft/add。
实测 133 条抓取到 15 条入选，全程 **2 分 58 秒**。耗时结构见下面第 13 条——
**不是打分慢，是富化慢**，这条最初判断错过一次。

**`run` 会先验 token 再采集**。白名单没配好时它 3 秒就失败，不会先烧掉三分钟和
一整轮 API 费用才卡在上传那一步。

**`draft` 是「审过的就是发出去的」那个命令**：白名单 / 权限出问题时先用
`run --dry-run` 生成预览给人看，修好后跑 `draft` 用同一份 `items.json` 建草稿。
如果改跑 `run`，它会重新采集、重新打分，选出来的条目很可能和刚审过的不是同一批。
`draft` 会拒绝超过 12 小时的 `items.json`（日期错位会让标题和内容对不上），
确要用就加 `--force`。

结束后向用户报告入选条数、草稿 media_id、预览路径，并提示：
**打开「订阅号助手」App → 草稿箱 → 确认后群发**。

### 5. 反读草稿核对（别只信 media_id）

```bash
python scripts/cli.py verify              # 核对当天的草稿
python scripts/cli.py verify --media-id X # 核对指定草稿
```

`draft/add` 返回 media_id 只代表请求被接受，不代表内容存对了。
`verify` 会把微信侧的 content 拉回来做合规审计（style / class / h1 / 外链图 / rem），
同时打印标题、作者、摘要的实际字数。**这是「接口说成功」和「真的成功」之间唯一的证据。**

### 6. 日常运维

```bash
python scripts/cli.py ready         # 一屏看清所有前置条件
python scripts/cli.py status        # 最近运行记录
python scripts/cli.py sources       # 配置了哪些源 + 上一期实际的出处分布
python scripts/cli.py ip --copy     # 查出口 IP 并复制到剪贴板
python scripts/cli.py clean --days 30   # 清理过期草稿与索引
```

排查问题时**先跑 `ready`**，缺什么会直接列出来。

**「配置了 16 个源」不等于「版面就均衡」**。`sources` 把两件事分开列：
配置里开了哪些（含中文名 / 语言 / 是否境外），以及上一期 15 条实际来自哪里。
用户抱怨「怎么全是国内媒体」时，先看这张表——是源没配够，还是配了但抓不到
（见「已知的信息源限制」），还是抓到了但被高产媒体挤掉了（见约束第 17 条）。

## 定时

用 WorkBuddy automation 每天 07:00 触发，prompt 写：

> 运行 `python ~/.workbuddy-ai/skills/horizon-wechat/scripts/cli.py run`，
> 然后报告入选条数和草稿状态。

脚本自带幂等：当天已 `drafted` 会直接跳过。可以配 07:00 和 09:00 两次触发，
第二次在电脑刚开机时能补跑。

## 部署到服务器（解决 IP 漂移的正解）

本机跑的最大问题不是性能，是**出口 IP 天天变**：10-17 `113.88.61.x`（电信）、
10-18 `120.229.207.x`（移动）、10-24 `116.24.222.x`（电信），
每换一次白名单就失效一次，而且电脑一睡眠当天直接没有。换到服务器上，
出口 IP 固定，白名单加一次就完事，再加 `Persistent=true` 补跑。

```bash
# 本机（Mac）→ 服务器，一条命令推完并安装
HOST=root@1.2.3.4 bash ~/.workbuddy-ai/skills/horizon-wechat/deploy/deploy.sh

# 只推文件，不安装
HOST=root@1.2.3.4 SKIP_BOOTSTRAP=1 bash .../deploy/deploy.sh
```

服务器上的布局（`bootstrap.sh` 幂等，重复跑只补缺的）：

```
~/horizon-wechat/      这个 skill（.env / data/state.db / .venv）
~/Horizon/             Horizon 仓库（.venv 现场建，不传本机的；config.json 只在首次部署种下）
/etc/systemd/system/horizon-wechat.{service,timer}
```

**七个坑，bootstrap.sh / deploy.sh 里都处理了，改脚本时别改掉：**
1. **不传 `.venv`**。本机 147MB 的 venv 里是 macOS 的二进制，
   搬过去必坏。只传代码，服务器上用 `uv sync` 现场建（uv 会自己下 Python 3.12，
   服务器系统 python 常年是 3.9，Horizon 要 ≥3.11）。
2. **装中文字体**。`cover.py` 用 PIL 画封面，Linux 上没装 `fonts-noto-cjk`
   会退化成默认字体——图能生成、能上传，但中文全是方块，最难发现的那种坏。
   bootstrap 会真的加载一次字体并打印路径，不是只看文件在不在。
3. **定时用 UTC 写死**。`OnCalendar=*-*-* 23:00:00 UTC` = 北京时间 07:00。
   写本地时间的话，美国服务器一年会漂两次夏令时。
4. **服务里强制 `TZ=Asia/Shanghai`**。否则 `Date.today()` 在 UTC 服务器上
   会算成前一天，标题日期、`stats-<day>.json`、`state` 的 day 全部错位。
5. **`Persistent=true`**。服务器在 07:00 处于关机/重启时，开机后补跑一次。
   这是换服务器最实际的收益——本机睡眠就彻底漏掉了。
6. **源开关要按实测决定，不能跟着配置文件走**。`deploy/configure-sources.py`
   逐个探活（200 + 内容像 feed），能抓到的开、抓不到的关。
   **只动候选清单里的源**：清单外的个人博客（vLLM、阮一峰、Latent Space）
   是编辑上故意关的，抓得到不代表想每天收——第一版没做这个限制，
   一跑就把六七个博客全打开了。
7. **重部署绝不能覆盖运行时状态**。`deploy.sh` 的 rsync 排除了
   `.env`、`data/state.db`、`data/config.json` 三样，并且对前两者 +
   config.json 用 `--ignore-existing` 做「远端没有才种一份」。
   这三样都是**谁在用谁说了算**：
   - `state.db` 是去重索引。本机那份是旧的，覆盖上去等于把服务器积累的
     历史冲掉，第二天可能把昨天发过的内容再发一遍。
   - `.env` 只在服务器上改过密钥时最明显：一推就盖回本机的旧 AppSecret。
   - `config.json` 里的源开关是**按机器 IP 实测**出来的。本机（深圳家宽）
     把 The Verge / BBC / Guardian / Hugging Face 全关了（502），推到服务器
     就等于把「20 个源」砍回「16 个」——**这个坑真的踩过**：一次
     `SKIP_BOOTSTRAP=1` 的部署把服务器清单从 20 冲回 16，`cli.py sources`
     才发现境外源全没了。修复是重跑 `configure-sources.py`（实测后恢复 20 个）。

   配套的：`deploy/fix-paths.py` 把 `profiles_dir` 里的 `/Users/...` 改写成
   服务器路径，`deploy.sh` 和 `bootstrap.sh` **都调它**。只放在 bootstrap 里
   是不够的——跳过 bootstrap 的快速部署就会把 `/Users/...` 留在服务器上，
   第二天 07:00 直接 `ValueError: No profiles found`。

### 实测：换到美国服务器后，被挡的源全活了

同一份配置，本机（深圳家宽）vs RackNerd VPS（Ubuntu 24.04 / 2C2.4G / UTC）：

| 源 | 本机 | 服务器 |
|---|---|---|
| The Verge | 502 / 超时 | ✅ 200，0.06s |
| BBC Tech | 502 / 超时 | ✅ 200，0.34s |
| The Guardian Tech | 502 / 超时 | ✅ 200，0.20s |
| NYT Tech | 502 | ✅ 200，0.07s |
| Hugging Face Blog | 502 | ✅ 200 |
| Rest of World | 502 / 超时 | ❌ 403（挡机房 IP） |
| VentureBeat | 200 | ❌ 429（限流，重试仍 429） |
| IT之家 / Solidot / InfoQ | 200 | ✅ 200（2.9s / 1.0s / 1.2s，慢但在） |

最终 **20 个 RSS 启用（7 中文 + 13 境外）**，比本机多 5 个。
国内源从美国抓会慢一些（IT之家 2.9s），但都在可接受范围。

部署完先 `cli.py ip` 拿服务器出口 IP 加白名单（**只需一次**），
再 `cli.py run` 建一期草稿验证。确认服务器稳定后，
**把本机的 07:00 automation 停掉**，否则两边各发一期。

## 关键约束（改代码前必读）

1. **所有样式必须内联**。微信会剥离 `<style>` 标签和 `class`，只保留 `style` 属性。
2. **不要用 `<h1>`–`<h6>`**，微信会重写这些标签的样式。用 `<section>` + 内联字号。
3. **图片 `src` 必须是 `mmbiz.qpic.cn`**。外链会被过滤，必须先过 `media/uploadimg`。
4. **`thumb_media_id` 必须是永久素材**（`material/add_material`），临时素材不行。
5. **中文 JSON 必须 `ensure_ascii=False`**。微信文档明确警告不要 Unicode 转义。
6. **标题 ≤ 32 字、摘要 ≤ 120 字、正文 < 2 万字符**。`render.validate()` 会提前拦截。
7. **正文 `<a>` 不可点击**，链接只能作为纯文本给读者复制。
8. **正文有内容预算**：`render_digest` 默认按 18000 字符装配（微信硬上限 20000）。
   实测 13 条真实内容能到 19185，只剩 4% 余量，所以超预算时会自动降级，
   顺序是「从最不重要的那条开始丢区块」，丢到刚好装下就停：
   - 头条永远保留完整区块（整期最值钱的部分）
   - 「值得一读」从尾部逐条丢，只剩摘要；先丢 2 条就够，就绝不丢 3 条
   - 区块全丢了还超，才从尾部减条目（最少保留 3 条）
   实测 13 条超 1185 字符时，7 条里只丢了最后 2 条的区块，正文 17540。
   想调就改 `config.json` 的 `digest.content_budget`。
9. **英文必须翻译成中文**。Horizon 的 `ai.languages` 只管富化区块，
   `title` 和 `summary` 永远是原文。`translate.py` 在渲染前补这一步，
   批量翻（8 条一请求），翻不动的逐条补一遍。
   验收标准是「中文变多了」而不是「中文占比达标」——后者会误杀
   「Show HN：Gemini 4 Argon 玩《星际争霸》」这种保留了大量专名的正确翻译。
10. **合规红线优先于新闻价值**。`analysis.md` 第 7 条明确要求
   涉及未成年人受侵害、性犯罪、血腥暴力、政治敏感的题材压到 3 分以下。
    实测曾选入「副检察长用 AI 制作儿童性虐待材料」这类条目，
    公众号审核会拦——宁可少一条，不要冒封号风险。
11. **合规过滤是两道闸，不是一道**。模型打分是概率性的，同一条内容
    换个批次可能从 7 分变 2 分。所以 `config.json` 的 `filter.block_keywords`
    再设一道确定性的关键词过滤，命中就丢，不问分数。
    关键词刻意避开「未成年」这种宽词（会误杀「未成年人模式上线」），
    只收「儿童性虐待」「性侵」「恐怖袭击」这类无歧义的词。要加词改配置即可。
12. **时效性要按「事件发生时间」判，不是「文章发布时间」**。
    聚合页 / 话题页（The Verge 的 `All the news about X`、各家早报、盘点）
    的 RSS 时间戳会随每次更新刷新，看起来是几小时前的新内容，
    实际讲的是几个月前的事。`published_at` 过滤永远拦不住它们。
    两道防线：
    - `bridge.py::drop_roundups()` 在**打分之前**按标题/URL 特征剔除，
      外加结构特征兜底（一条标题里 ≥2 个分号 = 多事件合集）。
      **必须在打分前**：这类页面分数通常很高，Horizon 的话题去重会
      「留下聚合页、把真正的新消息判为重复丢掉」，等选完再删就找不回来了。
    - `analysis.md` 第 8 条要求模型按事件时间打分，旧事压到 3 分以下。
    注意 `cli.py::_drop_by_patterns()` 必须跑在**翻译之前**——
    特征（`all the news about`、`-news-announcements`）只在英文原题和 URL 里。
13. **耗时几乎全在富化阶段，不是打分**。
    实测拆解（同一批 135 条）：

    | 阶段 | 耗时 | 说明 |
    |---|---|---|
    | 抓取 | ~90–120s | 16 个 RSS + HN + GitHub，网络决定 |
    | 打分 + 筛选 | ~60s | 135 条在 `analysis_concurrency: 4` 下只需 1 分钟 |
    | **富化** | **~15 分钟（旧）→ 2 分钟（现）** | 15 条 × 2 区块 × 多轮「LLM→调工具→LLM」 |

    罪魁是 `web_search`：ddgs 一次要 18 秒（它在背后轮询多个搜索引擎后端，
    前面的不通就逐个等超时），而富化是逐条等工具的。
    **现在已经把 `web_search` 从 `profiles/tech-news/profile.json` 的两个区块里去掉**
    （只留本地 SQLite 的 `history_search`），并把 `enrichment.md` 里
    让它「需要外部事实就搜索」的指令改成「只用文章内容，缺就写缺」。
    实测富化区块质量没有下降——搜索本来就没成功过，模型一直是靠正文写的。

    结果：`bridge.py` 全链路 20 分 22 秒 → **2 分 54 秒**，CLI 全流程约 4 分钟
    （16 个源、候选池 30 条时实测 3 分 56 秒——放宽候选池只多打分 20 条，
    富化仍然只做最终 15 条，所以不涨）。
    `bridge.py::patch_web_search()` 保留作保险（万一以后又给区块加回搜索工具），
    看到 `⏱ web_search 超时` 说明有人在用 ddgs，且它在这个网络下不可用。

    `cli.py` 改成实时转发 stderr（原来 `capture_output` 要等进程结束才吐日志，
    长任务全程静默，出问题没法定位）。
14. **每条都要带出处**。`bridge.py::source_name()` 把出处拍成 `source_name` 字段：
    RSS 用 `metadata.feed_name`（The Verge / IT之家 / 量子位 这些真名），
    其余按平台名（Hacker News / GitHub · 仓库名 / Reddit · r/xxx）。
    **不能直接用 `source`**——那是 `rss` / `hackernews` 这类内部枚举，
    印在日报上对读者毫无意义。
    `render.py::_source_name_of()` 还有一层域名兜底：老版本的 `items.json`
    没有这个字段，靠 URL 域名推，才不会渲染成空白。
    版面上出处排在 meta 行第一位、用主色加粗——标签、时间、评分都是辅助信息，
    只有「哪家媒体说的」是读者判断可信度的依据。速览行也带出处（灰色小字）。
    速览行的出处用全角括号而不是 `margin` 分隔：微信对行内 margin 支持不稳，
    实测会塌成零间距，标题和出处粘成「…超 GPT-6IT之家」。

    **聚合站要写原发媒体，不能只写聚合站**。Hacker News / Techmeme 只是转发，
    只写「Techmeme」等于把二传手当信源，读者没法判断可信度：
    - HN：出处是「Hacker News · theverge.com」，域名从 `url` 取。
    - Techmeme：条目 URL 永远是 techmeme.com 的 permalink，域名没用；
      真正的媒体在**标题末尾的括号**里（`… (Liv McMahon/BBC)`、`… (Bloomberg)`）。
      这个括号在翻译后会被丢掉，所以 `_techmeme_outlet()` 必须在 bridge 里、
      翻译之前取。链接落在 `AGGREGATOR_DOMAINS` 上时不再补域名
      （否则会出现「Techmeme · techmeme.com」这种把二传手写两遍的废话）。
15. **评分不上版面**。meta 行只放「出处 ｜ 标签 ｜ 时间」。
    加上出处后一行放不下，实测「评分 9.0」会被折成两行（「评分」在行尾、
    「9.0」掉到下一行）。评分是内部排序口径，条目本来就按它降序排，
    顺序已经表达了重要性。`items.json` 里仍保留 `score`。
16. **同一事件默认不限制条数**（`digest.max_per_event = 0`）。
    实测 10-24 那期 15 条里有 4 条是 iPhone Duo 开售（拆机说明书 / 屏幕问题 /
    首日越狱 / 门店办 eSIM），近三成版面被一件事占掉。Horizon 的 `topic_dedup`
    只合并近似重复，角度不同就都留下了。
    `cli.py::_merge_same_event()` 提供可选约束：按标题字符二元组 Jaccard +
    共有英文专名判同事件，超出 N 条就丢分数低的。
    阈值在 15 条真实数据上标定：同事件三条互相 0.22–0.34，其余任意两条 < 0.15。
    实测 `max_per_event=2` 时 15 条变 14 条，`=1` 时变 13 条。
    **默认关闭是有意的**——「拆机说明书 / 屏幕问题 / 越狱」确实是三条不同的新闻，
    砍哪条都可能可惜，这是编辑决策不是技术决策。
    注意它抓不到措辞完全不同的同事件条目（门店 eSIM 那条重合度 < 0.15），
    所以开了也只能压到 3 条。
17. **单家媒体在一期里最多 3 条**（`digest.max_per_source = 3`）。
    这是「均衡」问题，不是「质量」问题：打分只看内容好坏，不看产量，
    而 IT之家一天出 60 条、Solidot 18 条、The Verge 只有 10 条
    （10-24 实测：182 条里 IT之家 60 条，占 33%）。不设限额的话，
    高产媒体靠数量自然垄断版面——那一期 15 条里 9 条来自国内源。
    实现分两步，都在 `bridge.py`：
    - `run()` 先把 `config.digest.max_items` 临时放宽到目标的两倍当候选池。
      **必须放宽**：池子里只有 15 条时，切掉一条就少一条，补不回来。
    - `cap_per_source()` 按 `_origin_key()` 计数，每家取够 3 条就跳过。
      键取「·」右边——聚合站的「Hacker News · theverge.com」按整串分组
      会让 HN 的每条转发各自成组，等于没有上限。
    **限额是软约束**：切完不够 15 条时按原顺序把落选的补回来，
    宁可两条同源也不交一期 9 条的日报。
    富化只对最终的 15 条做，放宽候选池不会增加 API 成本。

    10-24 实测效果（182 条 → 51 条达标 → 池子 30 条 → 限额后 15 条）：
    Solidot 5→3、IT之家 3、InfoQ 2，中文源 9→8 条、英文源 6→7 条。
    想更偏英文源就把 `max_per_source` 调到 2（中文三家最多 6 条）。
    **别调到 1**：15 条要 15 家媒体才填得满，池子里没那么多家。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `40164` | 出口 IP 不在白名单 | 跑 `cli.py ip --copy` 取当前 IP，去后台「设置与开发 → 基本配置 → 开发密钥 → API IP白名单 → 设置名单」加入。**新版后台白名单为空 = 全部拒绝，不能靠清空放行**（页面原话：「设置 IP 白名单后，可调用 access_token」）。命中时会在 `out/whitelist-needed.txt` 留一条记录，`cli.py status` 每次都会念一遍——**别只靠通知**：10-19 到 10-24 六天都发了通知，没人看见，六期内容全停在 `previewed` |
| `40013` | AppID 不合法 | 检查 `.env` 里是否有空格或大小写错误 |
| `40125` | AppSecret 无效 | 后台重置 AppSecret 后更新 `.env`。**迁移到服务器后要改两处**：本机和服务器的 `.env` 是各自独立的副本，只改一边会出现「本机能跑、服务器 40125」或者反过来。改的时候走标准输入（`printf '%s' "$NEW" \| ssh ... python3 -c ...`），别写成 `VAR=x ssh host 'echo $VAR'`——**ssh 不会把本地环境变量带过去**，远端拿到的是空串，会静默写成 `WX_SECRET=` |
| `40001` | token 被顶 | 脚本自动 force_refresh 重试一次 |
| `48001` | 无草稿箱权限 | 改走 `preview` 手动粘贴 |
| 采集 0 条 | 时间窗内无新内容 | 正常情况，不建草稿 |
| 内容全重复 | seen 窗口太宽 | 调小 `state.seen_window_days` |
| 排版在手机上崩 | 用了 class 或 h1 | 检查是否绕过了 `render.py` |
| 某个 RSS 返回 `403` | 站点按 UA 拦截 | `bridge.py` 的 `patch_default_ua()` 已统一补浏览器 UA，正常不该再出现；若新站仍 403，多半还看 Referer |
| `Error fetching RSS feed X: `（**冒号后面什么都没有**） | 超时。`httpx.ReadTimeout` 转字符串是空串，所以看着像配置错误，其实是网慢 | `bridge.py::patch_default_ua()` 已把 Horizon 写死的 `timeout=30.0` 放宽到 90 秒。实测 IT之家（272KB）正常 1.5s，但会瞬时限速到 3.8KB/s，同一份要 73 秒——30 秒必挂。补丁只改「显式传了 ≤30 秒数字」的客户端，LLM SDK 传的是 `httpx.Timeout` 对象，不受影响 |
| 某个 RSS 返回 `502` | 代理层拦了整站 | 先在 `trust_env=False` 下重试一次：也超时 = 整站不可达，把该源 `enabled` 设为 `false` 换源；只有代理 502 而直连 200 = 代理节点的问题，别删源 |
| 版面被某家媒体占满 / 「怎么全是国内媒体」 | 高产媒体靠数量挤掉了低产媒体 | 先跑 `cli.py sources` 看是没配源、抓不到、还是被挤掉；被挤掉就调 `digest.max_per_source`（默认 3，调 2 更均衡） |
| 跑得特别慢（20 分钟以上） | 富化区块里还挂着 `web_search`，逐后端等超时 | 见约束第 13 条：把区块的 `tools` 里的 `web_search` 去掉。日志里出现大量 `⏱ web_search 超时` 就是这个原因 |
| 想再快一点 | 富化并发是 2 | 可调 `ai.enrichment_concurrency`；`analysis_concurrency` 已设 4，**不建议超过 4**——单条分析失败会被 analyzer 吞掉（`score=None` 后静默落选），并发越高越难察觉丢了哪条 |
| 草稿里出现「；」连起来的合集标题 | 早报类内容没被拦住 | 已在 `drop_roundups()` 用「≥2 个分号」兜底；若仍漏，把该站标题特征加进 `filter.block_patterns` |
| `--force` 重跑后内容变很少 | 已见索引没清 | 已修：`reset_run()` 现在同时清 `seen_url` |
| 07:00 没生成草稿 | 多半是 40164（IP 变了），或电脑在 07:00 处于关机/睡眠 | 跑 `cli.py status`：状态是 `previewed` 而不是 `drafted`、且 media_id 为空，就是内容采到了但没建成草稿——看 `out/whitelist-needed.txt` 有没有记录，有就补白名单后跑 `draft`。**整行都没有**才是没跑起来（关机/睡眠），这种情况没有任何机制能补跑，只能手动执行 |

## 已知的信息源限制

`bridge.py` 里有个 `patch_default_ua()`：Horizon 建 httpx 客户端时没设 User-Agent，
默认的 `python-httpx/x.y` 会被部分站点直接 403（量子位就是）。**没有改 Horizon 源码**，
而是在 bridge 里给 `httpx.AsyncClient.__init__` 加了一道 setdefault，
这样以后 `git pull` 不会冲突。用 setdefault 是为了不影响自带 UA 的客户端（如 OpenAI SDK）。

实测不可用的源：

**先说结论：国内媒体占版面，主因不是配置偏心，是这台机器的网络出口。**
2026-10-24 用 `httpx` 双模式对照（`trust_env=True` 走本地代理 / `False` 直连）：

| 源 | 走代理 | 直连 | 结论 |
|---|---|---|---|
| The Verge / BBC Tech / The Guardian Tech / Rest of World | 502 | 连接超时 | 整站被网络挡住，已 `enabled: false` |
| NYT Tech / Semafor / FT Tech / Bloomberg Tech / Google News | 502 | — | 同上，不要浪费时间试 |
| IEEE Spectrum | 握手超时 | — | 同上 |
| The Register | 200，但内容是「Are we human?」PoW 页 | — | 反爬，feed 拿不到条目 |
| Engadget | 202 空响应 | — | 不可用 |
| Reuters | 404 | — | feed 地址已废弃 |
| Hugging Face Blog | 502 | — | 代理层拦截整站，已 `enabled: false` |
| 量子位 | 403 | — | **已修复**（UA 补丁），实测能抓到条目 |
| OSS Insight | — | — | 数据源 2026-03 起停供，上游死了，不要开 |
| GDELT | 429 | — | 限流 1 次/5 秒，不适合日报 |
| Reddit | 502 | — | 本机不可达，已 `enabled: false` |

**「走代理 502」+「直连超时」两个同时成立，才是「整站不可达」的证据**：
只 502 可能是站点拦代理（换 UA/换出口能救），只超时可能是 DNS 抖动。
两个都中就换源，别再调参。

可用且已验证的境外源（16 个 RSS 里的一半）：
Techmeme、Ars Technica、TechCrunch、MIT Technology Review、CNBC Tech、
VentureBeat、OpenAI News、Cloudflare Blog、Simon Willison。

被挡住的那几家里，The Verge / BBC 的新闻仍会经 Hacker News / Techmeme 进来
（10-24 那期 HN 就贡献了 4 条，域名分别是 theverge.com / kimi.ai /
music.apple.com / block.xyz），所以不是完全没有覆盖——只是不如独立 feed 稳定。

### X / Twitter 开不了

用户明确问过「为什么没有 X 之类」。结论：**这台机器上开不了**，不是漏配：
- `sources.twitter.enabled = false`，`users` 和 `keywords` 都是空的——不知道该跟谁。
- 抓取要 `APIFY_TOKEN`（Horizon 用 Apify 的 twitter scraper），`~/Horizon/.env` 里没有。
- 备选路径是 Playwright + 登录 cookie，本机没有登录态。

要开的三步：把 `APIFY_TOKEN` 写进 `~/Horizon/.env`；`data/config.json` 里
`sources.twitter.enabled = true` 并填 `users`（如 `sama`、`karpathy`）和 `keywords`；
`fetch_limit` 别超过 10。注意 Apify 按次计费，日报每天跑一次要算清成本。

## 凭证

凭证放在 skill 根目录的 `.env`（权限 600，不要提交到任何仓库）：

```
WX_APPID=wx...
WX_SECRET=...
```

`wx.py` 在导入时自动加载，不覆盖已有环境变量。

**关于 IP 白名单（这是本 skill 最高频的故障，不是偶发）**：
家用宽带的公网 IP 会变——光猫重拨、路由器重启、甚至换了接入网段都会变。
一旦变了，所有接口调用都返回 `40164`，当天日报就发不出去。

实测记录：

| 日期 | 出口 IP | 结果 |
|---|---|---|
| 2026-10-17 | `113.88.61.x` | 加白名单后正常 |
| 2026-10-18 | `120.229.207.x` | **隔天就变了**（网段从 113.88 跳到 120.229），07:00 那次失败 |

应对顺序：

1. `cli.py ip --ranges` 看当前 IP、上次成功的 IP，以及可填的网段
2. 去后台「设置与开发 → 基本配置 → 开发密钥 → API IP白名单 → 设置名单」加入新 IP
3. 跑 `cli.py draft` 用已生成的内容建草稿（**不要**重跑 `run`，见 §4）

**白名单支持 CIDR，不用只填单个 IP**（官方文档原文：
「支持具体 IP 以及 IP 段，如 `172.0.0.1`、`172.0.0.1/24`（不支持 `172.0.0.*` 这种格式）」，
「可支持配置多个 IP」，「配置修改后需等待数分钟生效」）。
`cli.py ip --ranges` 会算出覆盖当前 IP 的 `/24` 和 `/16`：

| 填法 | 能扛住 | 代价 |
|---|---|---|
| `120.229.207.x` | 什么都不扛，IP 一变就废 | 最安全 |
| `120.229.207.0/24` | 同网段内换 IP（光猫重拨最常见） | 256 个地址 |
| `120.229.0.0/16` | 换子网（换路由器/换接入点） | 6.5 万个地址 |

建议填 `/24`：既解决了最常见的重拨漂移，又不至于把白名单摊得太开。
真正的凭据始终是 AppSecret，白名单只是第二道锁——但别一上来就填 `/8`。

**但 `/24` 只解决同网段漂移。** 实测这个号一周内换了三个地址、跨了两家运营商：

| 日期 | 出口 IP | 运营商 |
|---|---|---|
| 10-17 | `113.88.61.x` | 电信（广东） |
| 10-18 | `120.229.207.x` | 移动（深圳） |
| 10-24 | `116.24.222.x` | 电信（深圳） |

10-19 到 10-24 连续 6 天，每天 07:00 的 run 都因为 40164 退化成
`--dry-run`（`status` 里全是 `previewed`），一条草稿都没建成。
这种情况下填单个 IP 或 `/24` 都追不上，要么把三个网段一起加进去，
要么把 skill 挪到固定 IP 的服务器上——后者才是根治。

`cli.py run` 现在会**先验 token 再采集**，所以白名单不对时 3 秒失败，
不会白烧一轮 API 费用。`doctor.py` 也会打印当前 IP。

**注意 `113.88.x` 和 `120.229.x` 不是同一家运营商**（前者电信、后者移动）。
如果是在家里宽带和手机热点之间切换，网段会整段跳，`/24` 救不了，
只能填两个网段，或者上固定 IP 的服务器。

## 文件说明

| 文件 | 职责 |
|---|---|
| `scripts/cli.py` | 统一入口与编排 |
| `scripts/doctor.py` | 权限自检（第一步跑这个） |
| `scripts/bridge.py` | 在 Horizon 环境里跑流水线，输出 JSON + 漏斗统计 |
| `scripts/translate.py` | 英文标题与摘要翻中文（DeepSeek 批量） |
| `scripts/env.py` | 极简 .env 加载，wx/translate 共用 |
| `scripts/render.py` | ContentItem → 微信内联 HTML |
| `scripts/cover.py` | 生成 900×383 封面 |
| `profiles/tech-news/` | 编辑方针：match.md 收什么、analysis.md 怎么打分 |
| `scripts/wx.py` | 微信 API 封装（含 `draft/get` 反读） |
| `scripts/state.py` | 幂等状态与已见索引 |
| `scripts/notify.py` | 本地通知 |

`out/` 下每份产物：`items.json`（入选原始数据，含 `source_name` 出处字段）、
`stats-<日期>.json`（漏斗各阶段条数，导语用它写「从 71 条里选出 15 条」）、
`preview-<日期>.html`、`cover-<日期>.jpg`，以及样例用的 `preview-demo.html`。
