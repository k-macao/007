# 04 - 文件合并工具 · 章鱼 AI 全景分析

一个强大的 Python 文件合并工具集，并集成港股/A 股实时行情、多通道推送与**机构级个股投研独立栏目**。

## 🏛 机构级个股投研独立栏目（新增）

基于 [equity-research-skill](https://github.com/k-macao/equity-research-skill)（上游 [rollingSirius/equity-research-skill](https://github.com/rollingSirius/equity-research-skill)）落地的独立研究栏目：

| 能力 | 说明 |
|---|---|
| 九章完整深度研究 | 一页速览 / 业务 / 竞争护城河 / 治理 / 财务与质量 / 估值 / 分析师 / 催化剂 / 结论与反方论证 |
| 九章财报模式 | 预期差质量、分部 KPI、GAAP/Non-GAAP、现金流、电话会、估值变动桥 |
| 预期差主线 | 反向 DCF + PVGO、Gap 表、独立观点检验 |
| 可复算估值 | 本地 `equity_research/scripts/dcf.py`：三情景 DCF / EPV / EVA / 蒙特卡洛 / 仓位 |
| 质量检查 | `check_research_output.py` 一致性核查 |
| 20 类行业附录 | `equity_research/industries/*.md` |

### 快速使用

```bash
# 离线自检（skill + dcf + 九章骨架）
python equity_research_column.py --selftest

# rule 模式生成九章栏目（不耗 API，估值由 dcf.py 计算）
python equity_research_column.py 09988 --mode full --ai-provider rule

# 财报深度模式
python equity_research_column.py 600519 --mode earnings --ai-provider rule

# 经 stock_report 统一入口（template=equity）
python stock_report.py 09988 --template equity --mode full --ai-provider rule

# 大屏独立栏：启动后打开 / ，使用「🏛 机构级个股投研」区块
python server_dashboard.py 8080
# GET  /api/equity/teaser?code=09988
# POST /api/equity   {"code":"09988","mode":"full","channel":"console"}
```

Skill 资源目录：`equity_research/`（含 `SKILL.md`、`references/`、`industries/`、`scripts/`、`Example/`）。

---

## 🧩 Skills Hub（2026-08-13 已更新）

补齐了 **Agent Skills 标准安装**，并接入 Anthropic 官方金融技能：

| 来源 | Skills | 入口 |
|---|---|---|
| rollingSirius v3.0.0 | 九章个股投研 + `dcf.py` | `equity` / `skills/equity-research/` |
| Anthropic financial-services（Apache-2.0） | 首次覆盖 / 财报前瞻 / 季报更新 / 模型修订 / 晨会纪要 / 催化剂日历 / 论点记分卡 / 行业格局 / 选股扫描 | `initiate` `earnings_preview` `earnings_update` `model_update` `morning_note` `catalysts` `thesis` `sector` `ideas` |

```bash
python skills_hub.py --list
python skills_hub.py --selftest
python skills_hub.py 09988 --skill morning_note --ai-provider rule
python stock_report.py 09988 --template initiate --ai-provider rule
python server_dashboard.py 8080   # GET /api/skills · POST /api/skills
```

目录：`skills/`（catalog + NOTICE）· `.claude/skills/`（Agent 可直接读）。

---

## 📦 功能特性（文件合并）

### 1. 文件合并 (`merge.py`)

- **文本文件合并** - 支持去重、添加标题、排序、自定义分隔符
- **JSON 文件合并** - 支持深合并、列表拼接/去重策略
- **CSV 文件合并** - 自动对齐不同表头、支持去重
- **二进制文件合并** - 适用于图片、音频等
- **自动检测合并** - 根据扩展名自动选择策略
- **文件夹合并** - 合并多个文件夹，同名文件智能合并

### 2. 归并算法 (`merge_algorithms.py`)

- **合并两个有序数组** - O(n+m)
- **合并 K 个有序数组** - 最小堆实现 O(N log K)
- **归并排序** - 稳定排序 O(n log n)
- **合并区间** - 经典算法题

## 🚀 快速开始

### 安装

```bash
# 无需外部依赖，纯 Python 实现
git clone https://github.com/k-macao/04.git
cd 04
```

### 基本使用

#### Python API

```python
from merge import merge_text_files, merge_json_files, merge_csv_files, merge_files

# 文本合并
merge_text_files(["a.txt", "b.txt"], "merged.txt", deduplicate=True, add_filename_header=True)

# JSON 深合并
merge_json_files(["data1.json", "data2.json"], "merged.json", deep_merge=True)

# CSV 合并，自动对齐表头
merge_csv_files(["a.csv", "b.csv"], "merged.csv", deduplicate=True)

# 自动检测
merge_files(["a.txt", "b.txt"], "out.txt", strategy="auto")

# 文件夹合并
from merge import merge_folders
merge_folders(["folder1", "folder2"], "merged_folder", conflict_strategy="merge")
```

#### 合并算法

```python
from merge_algorithms import merge_two_sorted, merge_k_sorted, merge_sort, merge_intervals

merge_two_sorted([1,3,5], [2,4,6])  # [1,2,3,4,5,6]
merge_k_sorted([[1,4,7],[2,5,8],[3,6,9]])  # [1,2,3,4,5,6,7,8,9]
merge_sort([5,2,8,1,9])
merge_intervals([[1,3],[2,6],[8,10]])
```

### CLI 命令行

```bash
# 文本合并
python merge.py text file1.txt file2.txt -o merged.txt --deduplicate --header --sort

# JSON 合并
python merge.py json data1.json data2.json -o merged.json --deep-merge --list-strategy unique

# CSV 合并
python merge.py csv a.csv b.csv -o merged.csv --deduplicate

# 自动检测
python merge.py auto file1 file2 file3 -o output --strategy auto

# 文件夹合并
python merge.py folder dir1 dir2 -o merged_dir --conflict merge
```

## 📁 项目结构

```
04/
├── merge.py                  # 核心文件合并工具
├── merge_algorithms.py       # 归并算法实现
├── test_merge.py             # 合并测试用例
├── pushplus_deepseek.py      # 微信推送主流程（DeepSeek/OpenAI/rule + 多通道）
├── stock_report.py           # 股票研报统一入口（港股/A股 + 九章投研 + Skills Hub）
├── equity_research_column.py # 机构级个股投研独立栏目
├── skills_hub.py             # Skills Hub 调度（equity + Anthropic 9 技能）
├── hk_quote.py               # 免费港股/A股实时行情（三源核验）
├── us_quote.py               # 境外（美股）行情 · 四源交叉验证（腾讯美股/东财美股/Yahoo/Stooq）
├── stock_news_scan.py        # 量价舆情动量 · 十七平台股票扫描
├── newsnow_sources.py        # NewsNow 社媒热榜 10 源采集
├── source_check_db.py        # 数据源检查验证数据库（SQLite 落库）
├── DATA_SOURCES.md           # 数据源配置说明（17 源 / Cookie / 校验库）
├── extract_feeds.py          # 全市场快讯提取
├── server_dashboard.py       # 大屏监控（行情 / 研报 / 投研栏目）
├── server_monitor.html       # 大屏前端
├── .github/workflows/
│   ├── alibaba-push.yml      # 工作流：Manual Run - Alibaba PushPlus+DeepSeek
│   └── stock-report.yml      # 工作流：Manual Run - Stock Report (HK/A-share)
├── equity_research/          # 机构级投研 skill 资源（SKILL/references/industries/scripts）
├── skills/                   # Skills Hub（catalog + NOTICE + skill 安装）
├── .claude/skills/           # Claude Agent 可直接读取的 skill
├── examples/                 # 示例文件 + 主题预览 HTML
└── README.md
```

## 🧪 运行测试

```bash
python test_merge.py
python merge_algorithms.py
```

## 🔧 高级特性

### 深合并示例

```python
from merge import deep_merge_dicts

base = {"a": 1, "b": {"x": 10}, "c": [1,2]}
incoming = {"b": {"y": 20}, "c": [3,4], "d": 2}
result = deep_merge_dicts(base, incoming)
# {"a": 1, "b": {"x": 10, "y": 20}, "c": [1,2,3,4], "d": 2}
```

### CSV 表头对齐

输入:
- a.csv: id,name,age
- b.csv: id,name,city

输出自动合并为: id,name,age,city，并补全缺失字段

---

## 📲 GitHub Actions 工作流（定时全市场简报 + 手动个股研报）

仓库内置三个工作流（`.github/workflows/`）：

| Actions 名称 | 文件 | 入口脚本 | 用途 |
|---|---|---|---|
| **Scheduled Market Brief (DOS Monitor)** | `market-brief.yml` | `pushplus_deepseek.py` | 每日澳门/香港 09:00 / 17:00 推送 24h 全市场快讯与板块情报（含香港券商、国外投行两栏目） |
| **Manual Run - Stock Report (HK/A-share/US)** | `stock-report.yml` | `stock_report.py` | 手动输入代码生成单标的研报；默认仅预览 |
| **Manual Run - Broker Columns** | `broker-columns.yml` | `pushplus_deepseek.py` | 手动单独运行香港券商（`hk_brokers`）或国外投行研报（`foreign_weekly`）栏目；默认仅预览 |

**定时推送只发全市场简报，不发个股页**：使用 `feedscan` 聚合 12 源快讯（金十数据单独取最新≤50 条，其余每源≤10 条），不传股票代码、不调用 `stock_report.py` / `equity` 研报，也关闭个股走势图；AI 提示词仅允许市场与板块层面概览，并明确禁止个股评级、目标价与买卖点。简报正文后自动并入 **🏦 香港券商新闻与数据（48h）** 与 **🏛 国外投行研报标题（72h）** 两节（环境变量 `BRIEF_COLUMNS=0` 可关闭）。定时任务固定使用 DOS CRT 复古终端主题。

- 时间按**澳门/香港时间（UTC+8）**配置：GitHub cron 用 UTC，故写为 `0 1 * * *`（本地 09:00 早报）与 `0 9 * * *`（本地 17:00 晚报）；GitHub 定时任务偶尔会有启动延迟。（2026-10 前为 UTC 09/17 点，实际对应澳门 17:00 与凌晨 01:00，已修正。）
- 自动推送仅在工作流进入仓库**默认分支**后生效。`workflow_dispatch` 手动试跑默认为 dry-run；定时触发始终真实推送。
- **简报过长会自动分篇推送**：微信对单条 HTML 很敏感（实测超过约 48KB 常出现「接口 code=200 但微信端不投递」），因此单篇目标压在 **32KB**（约失效临界点的 2/3），超出即按块切成多篇依次推送，标题带「（1/3）」并在 DOS 主题标题栏标出 `PART n/m`。**「📰 快讯原始明细」附录受保护**：超长裁尾时优先裁 AI 正文分节，明细永远不会被裁掉，也不会因前面某篇推送失败而丢失。
- 手动个股研报工作流没有定时器，且 `dry_run=true` 为默认；只有手动将其改成 `false` 才会投递单标的报告。

### 前置条件：配置 Secrets

仓库 **Settings → Secrets and variables → Actions** 中添加：

| Secret 名称 | 何时必需 | 获取方式 |
|---|---|---|
| `PUSHPLUS_TOKEN` | 通道为 `pushplus`/`all` | [pushplus.plus](https://www.pushplus.plus) 登录后个人中心复制 token |
| `DEEPSEEK_API_KEY` | AI 为 `deepseek`/`auto`（无 Key 时 auto 自动降级 rule） | [platform.deepseek.com](https://platform.deepseek.com) 创建 API Key |
| `WECOM_KEY` | 通道为 `wecom`/`all` | 企业微信群机器人 webhook 地址中 `key=` 后的部分 |
| `SERVERCHAN_SENDKEY` | 通道为 `serverchan`/`all` | Server酱 Turbo 的 SendKey |
| `OPENAI_API_KEY` | AI 为 `openai` | OpenAI 控制台（可选变量 `OPENAI_BASE_URL`） |

> 无 Key 容错：行情源免 Key；`stock_report.py` 的 `auto` AI 无 `DEEPSEEK_API_KEY` 会降级为 `rule`。真实推送时若所选微信通道缺少 `PUSHPLUS_TOKEN` / `WECOM_KEY` / `SERVERCHAN_SENDKEY`，脚本会完整生成并打印正文，然后把该通道标记为“跳过”，不再让 Actions 因缺少推送 Key 失败。

### 运行方式

- **自动推送**：`market-brief.yml` 每日澳门时间 09:00 / 17:00 运行全市场 `feedscan`（含香港券商、国外投行研报两节），只发市场与板块信息；定时运行必须配置 `PUSHPLUS_TOKEN`。工作流进入默认分支后才会触发。
- **手动个股研报**：`stock-report.yml` 的 `dry_run` 默认 `true`（仅预览）；只有主动选 `false` 才会把指定股票报告真实推送。
- **手动栏目**：`broker-columns.yml` 可单独运行香港券商栏目或国外投行研报栏目（窗口默认分别 48h / 168h），同样默认仅预览、选 `false` 才真推。
- **AI 降级**：市场简报默认尝试 DeepSeek；未配置 `DEEPSEEK_API_KEY` 时自动使用本地规则汇总，不影响 PushPlus 投递。
- **通道**：手动个股工作流可选 pushplus / wecom / serverchan / console / all；定时简报固定为 pushplus。
- **模板**：定时简报固定 `feedscan`（12 源全市场快讯 + 双栏目）。手动研报可选经典分析、`equity` 九章个股投研、NewsNow 与 Skills Hub 模板。
- `theme`：HTML 推送主题。Actions 工作流、CLI 与大屏推送页默认 **`dos`**：黑底磷光绿字、琥珀色光标、DOS 命令行状态栏、CRT 扫描线与等宽字体；内联样式适配 PushPlus/微信详情页。仍可选 `guizang`（电子杂志长页）、`monitor`（服务器大屏）、`game`（8-bit 像素游戏）、`noc`（零表格监视）、`klein`（复古纸面）、`pixel`（暗色监控）、`default`（普通 Markdown）。`pushplus_deepseek.py`、`stock_report.py`、`equity_research_column.py`、`skills_hub.py`、`server_dashboard.py` 接受同一组主题名。
- **长报告不再丢样式**：单篇主题 HTML 超过微信软上限（≈48KB）时，PushPlus 通道自动按章节/表格行**切分为多篇（最多 8 篇，标题带 1/N 序号，表格续篇自动补表头）**依次推送，每篇都是完整主题 HTML（guizang 主题第 2 篇起使用紧凑续篇壳，省下巨幅 Hero 让每篇装更多正文）；超过 8 篇时优先裁掉尾部最可弃的附录块（带可见提示、品牌尾注保留），仅当单块实在无法切分时才退回纯 Markdown（旧行为是一超限就退回，推送页完全没有风格）
- `hours`：量价舆情动量/十七平台扫描/全市场快讯的数据窗口，支持 24/48/72/**156** 小时（156h≈6.5 天，覆盖一个完整交易周）
- **荧光文字自带黑色底**：微信/PushPlus 详情页可能剥离外层容器背景，荧光青/荧光绿/荧光黄等发光色文字落到白色页面上几乎不可读。因此所有主题里荧光色文字（标题、涨跌数字、概率数值、■★⌜▚▞ 等图标符号）一律在**文字元素自身**内联纯黑背景（`FLU_BLACK_BG`），不依赖父容器背景存活；荧光色集合 `FLUORESCENT_TEXT_COLORS` 由主题字典派生，改主题色后判定自动跟进。
- **AI 分节压缩（简化 + 压缩正文）**：拿到 AI 生成的正文后，按 Markdown 章节（`#`/`##` 标题）切分成若干「部分」，**每一部分分别调用一次 AI** 做智能简化与压缩：保留标题结构、关键结论与数字（方向判断/概率%/价格/目标位）、因子表逐行保留，删除铺垫与冗余，目标压到原字数 40%~60%。压缩在内容指纹/分篇/推送**之前**完成（压缩结果即最终推送内容）；单节失败或输出异常自动回退该节原文，无 Key 时整体跳过，绝不因压缩丢内容。默认开启，工作流输入 `ai_compress`、CLI `--ai-compress true|false` / `--no-ai-compress`、环境变量 `AI_COMPRESS=0` 均可关闭；短节（<160 字）不单独调用，单次最多压缩 12 节（`COMPRESS_MAX_PARTS`）。

主题预览：`examples/dos_theme_preview.html`（DOS CRT 终端的全市场简报预览）、
`examples/guizang_theme_preview.html`（Guizang 竖版长页单独预览）、
`examples/theme_preview.html`（dos/guizang/monitor/game/klein/pixel 六主题对比）与
`examples/game_theme_preview.html`（旧 game 单独预览），可用 `examples/gen_theme_previews.py` 重新生成；
服务器大屏静态页 `server_monitor.html`（含 `examples/server_monitor_preview.html`）由
`server_dashboard.py::export_static_files()` 生成。

### 📊 推送自带字符模拟图（纯字符，无图片，微信直接可见）

只要给了 `hk_code` 或股票代码（手动 Stock Report 工作流），个股研报会在正文顶部附一段**字符模拟走势图**。定时全市场 `feedscan` 不传代码并显式关闭字符图，因此不会附带个股走势图：

1. **取数**：个股研报使用 Yahoo Finance 日级 OHLC 为主源，东方财富日级数据兜底（均免 Key，取最近 60 个交易日）
2. **渲染**：纯字符等宽模拟图（无图片依赖）——涨 `█` 跌 `▓` 影线 `│`，
   叠加 MA5 `·` / MA10 `×` / MA20 `+` 点位、成交量字符条 `▁▂▃▄▅▆▇█`、近 20 日 S1/R1 支撑压力位虚线 `─`
3. **嵌入**：直接以 Markdown 代码块 ````text```` / HTML `<pre>` 嵌入推送正文（PushPlus HTML 主题、企微、Server酱、console 均可显示，无需 CDN 与图床）
4. **示例**：

```text
09988.HK 字符模拟走势（近 52 日 · YAHOO）
 17.20 ┤      █
 16.80 ┤  █ █ █ │ · ·
       └──────────────────────┘
   VOL │▁▂▃▄▅▆▇█▂▃▄
S1 15.90 ── 支撑  ·  R1 17.40 ── 压力
```

- 任何一步失败（网络/渲染）都会**自动降级**：本次推送不含字符图，绝不影响发送
- 本地 `--dry-run`：控制台直接打印字符图预览
- `--no-chart`（兼容 `--no-kline`）可关闭
- **无需额外权限**：纯字符无需提交图片回仓库，`permissions: contents: read` 即可；已移除图片上传与 jsDelivr CDN 流程

### 分析框架与新增因子

选择 `analysis` 时会逐行输出以下 7 个因子，新增的**量价舆情动量（48h）**不会再被合并到普通消息/情绪面：

1. 基本面（业绩/订单/毛利率）
2. 行业与政策面（平台经济监管/云计算/AI 等真实政策动态）
3. 技术面（趋势/量价/关键价位）
4. 资金面（主力/北向/两融动向）
5. 消息面与情绪面（公告/舆情/行业事件）
6. 估值面（PE/PB 与历史分位）
7. **量价舆情动量（48h）**：基于窗口内价格/成交量、新闻和社媒样本，先本地预聚合，再交给 AI 分析
8. **十七平台股票扫描（窗口跟随 `--hours`，支持 156h）**：输入股票代码后，在最近 156 小时内
   检索十七个平台（**财经 7 源**：Google新闻/财联社电报/华尔街见闻/格隆汇/金十数据/MKTNews/雪球；
   **社媒 10 源**：知乎/微博/抖音/虎扑/AI hot/联合早报/香港01/今日头条/百度/B站，
   2026-08-13 由 7 源扩充并新增检查验证数据库 `source_check_db.py`），找出该股票的**相关新闻**
   （按代码/名称/别名直接命中）与**有关板块**（档案预设板块 + 新闻动态提取「XX板块」），
   逐条本地情绪打标后随附录输出，并注入 AI 上下文。

展示样式：`analysis` 输出的「因子 | 方向 | 多头概率 | 依据」在 HTML 推送中以
**卡片式内容展示**（不再是表格）——每个因子一张卡片：卡头为因子名 + 方向徽章
（▲偏多绿 / ▼偏空红 / ●中性灰），卡身为大号多头概率 + 概率条（game 主题为像素
血条 █░，其余主题为细框进度条）和「依据」标签正文；无【方向】列时按概率 50%
上下自动推导徽章。AI/rule 的 Markdown 输出契约不变，卡片化在渲染层完成。

当 `analysis` 搭配 `hk_code` 运行时，脚本会自动采集该新增因子所需的数据；采集失败会明确标注数据缺口，不会伪造概率。

### 统一品牌头与声明

所有模板、所有通道（含 dry-run/console）的最终结果都会自动加入统一品牌信息：

- **标题**：章鱼 AI 全景分析（推送标题不附带渠道名或时间）
- **副标题**：全网 AI 调研境内境外数据，由多个大模型混合部署。
- **尾部声明**：仅供参考，分析研究。
- **最后一行作者信息**：`作者：章鱼 ai` 及平台定位说明；不再放在标题下方。

作者和声明由实际推送入口 `pushplus_deepseek.py` 统一组装。企业微信等有长度上限的通道会
按 UTF-8 字节截断正文，但会保留尾部声明和最后一行作者信息。

工作流会先运行 **Check required secrets** 步骤：缺少所需 Secret 时立即变红并指出缺哪一个。

### 数据新鲜度看板（每次推送可验证"有没有更新"）

每次推送正文顶部固定输出 **🧭 数据新鲜度 · 本次运行指纹**：

- **行情对比**：三源共识价 + 在线源数 + 行情时间，并标注与上次推送的涨跌差/持平；
- **🆕 新增样本**：与上次运行相比，数据窗口内新增的条数与来源分布，
  附录中新增条目标 🆕 角标；
- **本地计算**：综合多头概率锚点、量价动量、十七平台命中数；
- **🔢 内容指纹**：由正文+行情+锚点+样本集合计算（不含时间戳）。
  与上次完全一致时醒目标注「⚠️ 与上次推送内容一致（窗口内无新增信号）」，
  否则标注「✅ 与上次推送相比已更新」；首次运行建立基线。

指纹与上次一致且使用 AI 时，会自动追加差异化要求换表述重试一次，避免推文逐字复读。
跨运行状态存于 `output/push_state.json`（已 gitignore），工作流用
`actions/cache` 持久化；本地调试可用环境变量 `PUSH_STATE_PATH` 指定路径，
或 `--no-state` 关闭对比。

命令行本地调试：

```bash
python pushplus_deepseek.py --check-only          # 只检查 Secret 配置
python pushplus_deepseek.py --dry-run             # 生成但不推送
python pushplus_deepseek.py --template analysis --hk-code 09988 --hours 48 --dry-run
python pushplus_deepseek.py --template feedscan --topic "全球市场早报" --theme dos --hours 24 --no-chart --dry-run
python pushplus_deepseek.py --template sentiment --hk-code 09988 --topic 阿里巴巴 --hours 156 --dry-run
python pushplus_deepseek.py --channel all         # 三个通道全部推送
```

### 量价舆情动量 · 十七平台股票扫描（`stock_news_scan.py`）

单独使用（不依赖推送通道，纯标准库）：

```bash
# 输入股票代码，检索最近 156 小时内相关新闻与有关板块
python stock_news_scan.py --code 09988 --name 阿里巴巴 --hours 156

# 只给代码也能跑（内置档案自动补全名称/别名/板块）
python stock_news_scan.py --code 9988.HK

# 追加自定义板块关键词 / JSON 输出 / 离线自检
python stock_news_scan.py --code 00700 --name 腾讯 --sectors 游戏,AI --json
python stock_news_scan.py --selftest
```

输出分三组：**直接相关新闻**（代码/名称/别名命中）、**板块相关快讯**（板块关键词命中）、
**有关板块**（档案预设 + 从命中标题动态提取「XX板块」，子串伪影按频次归属吸收）。
带可靠时间戳的条目严格按 156h 过滤；雪球/社媒热榜为实时快照、标记「实时」不参与过滤。
任何单源失败只进「数据缺口」，不拉高命中数、不伪造数据。

### 🩺 数据源检查验证数据库（`source_check_db.py`，新增）

社媒热榜已扩至 **10 源**（新增今日头条 / 百度 / B 站，配 `newsnow_sources.py`），
并提供 SQLite 校验库：每次检查生成运行台账 + 逐源明细（状态/条目数/延迟/违例/错误），
可追踪单源报错率与延迟趋势。

```bash
python source_check_db.py               # 离线校验 10 源（样本→解析器→字段规则），不落网
python source_check_db.py --live        # 真抓取：社媒 10 源 + 财经 7 源
python source_check_db.py --report      # 最近一次运行报告
python source_check_db.py --history toutiao   # 单源历史记录
python source_check_db.py --selftest    # 内存库自检（不落盘）
```

状态口径：`ok` 通过 / `warn` 结果为空或字段违例 / `fail` 抓取解析异常。
每个源的端点、协议、Cookie 策略、降级链与新增数据源 SOP，详见 **[DATA_SOURCES.md](DATA_SOURCES.md)**。

### 🩺 故障排查：Service Unavailable / Failed to resolve action download info

> **TL;DR** 这不是代码问题，是 GitHub Actions Marketplace CDN 瞬时 503。等 2-3 分钟点 **Re-run failed jobs** 即可恢复。

**现象**：Job 停在 **Set up job**，一个 Step 都没执行，日志出现：

```
Error: Service Unavailable
Error: Failed to resolve action download info.
```

**根因**（2026-08-06 已诊断）：

- **不是仓库代码错误**：本地 `--selftest` 全部通过、`--dry-run` 正常生成内容、workflow YAML 语法合法、所用 action tag 均存在。失败点全部在 `Set up job`（下载 action 阶段），而非 Python 代码。
- **是 GitHub 基础设施瞬时故障**：当日最近 20 次运行前 17 次全部 success，仅故障窗口内的 3 次连续 failure；`marketplace.actions.githubusercontent.com` / runner 集群瞬时不可用。社区相同案例：[discussions/65974](https://github.com/orgs/community/discussions/65974)（runner 自动退避重试 29s + 11s 仍 503）、[discussions/166225](https://github.com/orgs/community/discussions/166225)。
- `gh run rerun` 报 `cannot be rerun; its workflow file may be broken` 是 GitHub 对下载阶段失败 Run 的 API 限制，不等同于 YAML 真的 broken——改用网页 **Re-run failed jobs** 即可。
- 为什么新 tag 更容易命中：`v6` 刚发布时 CDN 缓存不如 `v4/v5` 广，故障期未命中缓存概率更高。

**已内置的加固**（两个 workflow 文件均已应用）：

- 固定为最广泛缓存的 `actions/checkout@v4` + `actions/setup-python@v5`，并 **pin 到 commit SHA**（防 tag 解析抖动），保留 tag 便于可读；
- **不要加 `cache: 'pip'`**：本仓库无 requirements.txt/pyproject.toml，会导致 Setup Python 步骤失败；
- 最小权限（`contents: read`）、`timeout-minutes: 15`、`concurrency` 防并发覆盖。

**立即恢复（不等加固）**：

1. **Actions → 失败的 Run → Re-run failed jobs**（网页按钮，非 `gh run rerun` API）。
2. 若仍 503，等 3-5 分钟重试；可查看 [githubstatus.com](https://www.githubstatus.com) 是否有 Actions incident。

**验证清单**：

- [ ] Re-run 一次失败的 Run，确认不再 503
- [ ] Actions 用 `dry_run=true, channel=console, ai_provider=rule` 触发一次 dry-run，日志出现 `✅ 自检全部通过` / dry-run 完成
- [ ] 切回 `dry_run=false` 真实推送一次

---

## 📈 免费港股实时行情（`hk_quote.py` + 大屏 view 接入）

大屏监视界面 `server_dashboard.py` 已接入免费港股实时行情，无任何 API Key：

- **数据源对比（2026-08-07 实测）**：① 腾讯财经 `qt.gtimg.cn`（字段最全：现价/开高低/昨收/量额/涨跌/PE/振幅/市值/52周高低/币种）✅ 稳定；② 东方财富 `push2.eastmoney.com`（JSON 最干净，HK 无 PE，偶发 502 自动换 host 重试）✅；③ Yahoo Finance chart API（无 PE/成交额，境内访问不稳）✅ 参考源；新浪 `hq.sinajs.cn`（需 Referer）与 Stooq CSV 实测 ❌。
- **默认链路**：腾讯财经(主) → 东方财富(备) → 静态演示兜底。视图横幅实时显示「🟢 实时行情 (LIVE) · 数据源 · 行情时间」或「⚠️ 演示数据 (STATIC DEMO)」。
- **📊 字符模拟图 · 智能行情交互视图**：大屏已集成 **字符模拟图交互视图 (Char Simulation Chart Studio)**，支持双模式切换——① **字符点阵模拟图**（纯字符点阵渲染、60 周期历史与移动均线 MA5/10/20、实时成交量字符条、支撑压力位标注 S1/R1，涨 `█` 跌 `▓` 影线 `│`，100% 兼容静态文件与离线环境不白屏）；② **TradingView 官方高级图表控件**（支持一键切换加载官方 `s3.tradingview.com` 实时专业插件）。支持 `09988 阿里巴巴`、`00700 腾讯控股`、`03690 美团`、`BABA 阿里美股` 等多标的，以及分时(1D)/5日(5D)/日级(Daily)/周级/月级 多周期切换。
- **后端接口支持**：前端每 30 秒自动轮询 `/api/quote` 更新价格卡片，`/api/chart`（兼容 `/api/kline`）返回标准化 60 根多空均线字符模拟图数据与指标 JSON，`/api/stock` 返回叠加实时行情的完整视图 JSON。

```bash
python hk_quote.py 00700            # 单只股票标准化行情（3 位小数 HKD）
python hk_quote.py --selftest       # 三源真实网络对比测试（哪个好）
python hk_quote.py --fixture-test   # 离线解析自检（内置真实抓包样本）
python test_hk_quote.py             # 单元测试（8 项）
python server_dashboard.py          # 启动大屏（8080，自动接入实时行情）
```

环境变量：`HK_QUOTE_CHAIN=tencent,eastmoney,yahoo`（链路）、`HK_QUOTE_TIMEOUT=3.5`（秒）、`HK_QUOTE_NO_LIVE=1`（强制静态演示）。

### A 股实时行情（沪深，免 Key）

`hk_quote.py` 现已同时支持 **港股（5 位）与 A 股（6 位）**，自动识别市场：

| 代码写法 | 识别结果 |
|---|---|
| `09988` / `9988.HK` / `hk00700` | 港股 |
| `600519` / `600519.SH` / `sh600519` | 沪A（上交所，6 位且首位 6/9） |
| `000001` / `000001.SZ` / `sz000001` | 深A（深交所，6 位且首位 0/1/2/3） |

- **A 股数据源**：腾讯财经 `qt.gtimg.cn/q=sh600519` / `sz000001`（字段最全，含 PE/PB/换手/振幅/总市值）；东方财富 `push2.eastmoney.com`（`secid=1.600519` / `0.000001`，价格 ×100，**带 PE/PB**，区别于港股接口无 PE）。
- **币种自动标注**：港股 `HKD`，A 股 `CNY`；CLI 打印自动切换「港元/元」。

### 🌐 境外（美股）实时行情 + 四源交叉验证（`us_quote.py`，2026-08-14 新增）

美股代码（`NVDA` / `aapl.us` / `NASDAQ:NVDA`）经 `hk_quote.detect_market` 识别为
`market="us"`，全链路（`stock_report.py` / 九章投研 / 大屏 / 字符模拟图）自动走境外链路：

- **四源采集**：腾讯美股（主）→ 东财美股（备，交易所未知自动试 105/106/107）→
  Yahoo（核验，双 host）→ Stooq CSV（延迟 ≥15min，仅核验不用作主源）。
- **交叉验证**：四源同采取**中位共识价**，逐源给偏离度——≤0.8% ✅ 一致 /
  ≤2% 🟡 基本一致 / >2% ❌ 分歧并点名离群源；研报「实时行情」块下方附
  「🔁 境外行情四源交叉验证」表格（价格/涨跌幅/偏离/行情时间/延迟标注）。
- **字段熔断**：币种非 USD、涨跌幅超 ±25%、有昨收缺涨跌幅 → 计入违例
  （`source_check_db.py` 离线/在线均覆盖境外 4 源）。

```bash
python us_quote.py NVDA            # 首个成功源行情（单行打印 / --json）
python us_quote.py NVDA --verify   # 四源交叉验证 Markdown 表
python us_quote.py --selftest      # 离线样本自检（不触网）
python stock_report.py NVDA --template analysis --ai-provider rule   # 研报直出
```

```bash
python hk_quote.py 600519            # 沪A 贵州茅台实时行情（CNY）
python hk_quote.py 000001.SZ --json  # 深A 平安银行 JSON
python hk_quote.py --fixture-test    # 离线解析自检（含 A 股真实抓包样本）
python test_hk_quote.py              # 单元测试（含 A 股解析与市场识别）
```

## 🧠 股票研报生成与推送（`stock_report.py` + 大屏输入框）

新增**「填入港股 / A 股代码 → 实时查询行情 → AI 分析出研报 → 推送」**的一站式能力，
复用仓库内已有的 `hk_quote`（实时行情）与 `pushplus_deepseek`（DeepSeek/OpenAI/rule 分析 + 多通道推送）。

### 命令行

```bash
python stock_report.py 600519                     # 沪A 贵州茅台：打印研报（预览，不推送）
python stock_report.py sz000001 --template analysis
python stock_report.py 09988 --ai-provider deepseek --channel pushplus --push
python stock_report.py 600519.SH --channel all --push   # 三通道真实推送
python stock_report.py --selftest                 # 离线自检（市场识别 + 研报组装）
python stock_report.py --check-only               # 只检查 Secrets
```

- **AI 提供方**：`--ai-provider auto`（默认，Actions 下拉同名）或留空时自动判断——配了 `DEEPSEEK_API_KEY` 走 DeepSeek（模块内模型），否则降级 `rule` 规则模板（不耗 API、可离线演示）。也可显式指定 `deepseek` / `openai` / `rule`。
- **通道**：console（预览）/ pushplus / wecom / serverchan / all；默认 `--dry-run` 只打印，加 `--push` 才真实推送。
- **主题**：默认 `--theme dos`（DOS CRT 磷光绿复古终端）；也可选 `guizang`（电子杂志长页）/`monitor`（服务器大屏）/`game`（8-bit 复古游戏风）/`klein`/`pixel`/`noc`；超微信软上限自动分篇并保留样式。
- **最新功能一律附带**（不再只在旧的 `pushplus_deepseek.py` 主流程里）：🧭 数据新鲜度看板与内容指纹、📊 港股/A股字符模拟走势图、🛰 十七平台扫描 + 量价舆情动量（`--hours 24/48/72/156`）。手动个股工作流默认 `equity` 但 `dry_run=true`，不自动发送个股报告；每日自动任务另走全市场 `feedscan`。

### 大屏输入框（`server_dashboard.py`）

大屏顶部新增 **🧠 AI 研报输入栏**：填入任意港股/A 股代码（如 `09988` / `600519` / `000001.SZ`），
选择推送通道后点「⚡ 生成研报并推送」，前端调用后端 **`POST /api/report`**：

1. 后端实时取行情（`hk_quote`，港股+A 股，失败自动标注数据缺口、绝不伪造）；
2. 用模块内模型（DeepSeek，未配 Key 自动降级 rule）按 `analysis` 模板生成多空因子研报；
3. 组装品牌头尾 + 实时行情核验块，按所选通道推送（PushPlus 超微信软上限自动分篇，每篇均带主题样式）；
4. 前端在大屏内嵌面板直接渲染完整推送页，风格由研报栏下拉决定（默认 `dos`，可切 `guizang`/`monitor`/`game`/`noc`/`klein`/`pixel`）。

```bash
python server_dashboard.py            # 启动大屏（8080），打开后在顶部输入框填代码即可
curl -X POST http://localhost:8080/api/report \
     -H 'Content-Type: application/json' \
     -d '{"code":"600519","channel":"console","dry_run":true}'
```

> 未内置演示档案的标的（A 股 / 任意港股代码）在大屏会生成**中性占位档案**：价格/估值来自实时行情，
> 七大因子与快讯标注为演示占位，正式分析以「🧠 AI 研报」输出为准。

## 📝 Git 合并演示

本项目最初是一个文件合并工具，主分支为 `main`；新功能开发在 `arena/*` 分支完成，可随时通过 PR 合并：

```bash
git checkout main
git pull
# 通过 GitHub PR 将 arena 分支合并回 main（建议走 PR 评审流程）
```

## License

MIT
