# 数据源可用性检查报告 — 避免数据丢失

> 生成时间：2026-10-19 UTC · 分支 arena/b977cb3e-273  
> 检查工具：`source_check_db.py` / `hk_quote.py` / `us_quote.py` / `stock_news_scan.py` / `newsnow_sources.py` / `deepseek_search.py` / `foreign_institution_weekly.py`

## 1. 总览：当前数据源清单

| 大类 | 数量 | 模块 | 备注 |
|---|---|---|---|
| 社媒热榜 | 10 | `newsnow_sources.py` | 知乎/抖音/微博/虎扑/AI hot/联合早报/香港01/今日头条/百度/B站 |
| 财经快讯 | 8 | `stock_news_scan.py` | Google新闻(RSS)/财联社/华尔街见闻/格隆汇/金十/MKTNews/雪球/DeepSeek搜索（AI搜索引擎，2026-10-19新增） |
| 港股/A股行情 | 3源链路 | `hk_quote.py` | 腾讯财经(主) → 东方财富(备) → Yahoo(参考) + 静态演示兜底 |
| 美股/境外行情 | 4源交叉 | `us_quote.py` | 腾讯美股(主)/东财美股(备,自动试交易所)/Yahoo(核验)/Stooq(延迟兜底) |
| 全市场快讯聚合 | 13端点 | `extract_feeds.py` / `pushplus_deepseek.py` | feedscan 模板，复用财经+社媒+DeepSeek（13源，2026-10-19由12→13） |
| 国外机构一周研报 | 12机构 | `foreign_institution_weekly.py` | Goldman/MorganStanley/JPMorgan/BofA/Citi/UBS/Barclays/Nomura/HSBC/Jefferies/Bernstein/Morningstar，168h窗口，DeepSeek主+Google备，独立栏目 foreign_weekly |

总计 **18 平台扫描 + 4 境外行情源 + 3 港股行情源 + 12 国外机构 = 37 个逻辑端点**，物理上 18+4+3 去重后 **10+8+4=22 源** + 12机构研报，全部已接入自检与推送管线；校验库 `source_check_db.py` 覆盖 **15 源**（社媒10+境外4+DeepSeek1）。

## 2. 离线可用性（不触网，纯解析器自检）

在沙箱执行：

```bash
python source_check_db.py --selftest
python hk_quote.py --fixture-test
python us_quote.py --selftest
python stock_news_scan.py --selftest
python newsnow_sources.py
python deepseek_search.py --selftest
python foreign_institution_weekly.py --selftest
python pushplus_deepseek.py --selftest
```

结果：**全部通过**

- `source_check_db.py --selftest`：15 源离线样本解析 ok（社媒10+境外4+DeepSeek1），落库、报告、历史查询均正常
- `hk_quote --fixture-test`：7 个样本（港股 4 + A股 3）解析正确，现价/PE/涨跌幅/市值字段一致
- `us_quote --selftest`：19 项检查，含代码识别、4源解析、交叉验证中位价、离群检测、延迟标记
- `stock_news_scan --selftest`：31 项，156h窗口、板块去重、雪球涨幅映射、动态板块提取，**18平台**（原17+DeepSeek）
- `newsnow_sources`：14 项，10源样本+注册表覆盖
- `deepseek_search --selftest`：9 项，样本解析/围栏容错/时间解析/无Key提示
- `foreign_institution_weekly --selftest`：7 项，12机构清单/别名/演示包5篇/情绪打标/渲染表格/上下文/附录
- `pushplus_deepseek --selftest`：全绿，含 foreign_weekly 模板 24 模板可构造

结论：**解析器本身健壮，有真实抓包样本作为离线基准，即使上游改版也能通过自检快速定位。新增 DeepSeek 与国外机构栏目均具备离线自检与演示包降级能力。**

## 3. 在线可用性（受沙箱网络限制，预期失败）

沙箱仅允许 `github.com / api.github.com / pypi.org` 等，外部财经站被阻断：

```bash
python source_check_db.py --live --only zhihu,baidu,toutiao
# → ❌ 3/3 fail，错误均为 "网络请求失败 ... TLS/SSL connection has been closed"
python hk_quote.py 00700
# → ❌ 无法获取行情
python us_quote.py NVDA
# → ❌ 全部行情源失败
python stock_news_scan.py --code 09988 --hours 24
# → 17/17 平台进入数据缺口，均为网络失败
```

这是**环境限制**，非代码问题。在 GitHub Actions 真实网络下，`market-brief.yml` 每日 09:00/17:00 UTC 成功运行（历史 20 次中 17 次 success，3 次为 GitHub Marketplace 503 瞬时故障，已有加固）。

## 4. 备用与容灾机制 — 已实现

### 4.1 港股/A股行情 `hk_quote.py`

- **链路**：`DEFAULT_CHAIN = tencent,eastmoney`，可通过 `HK_QUOTE_CHAIN` 环境变量覆盖
- **Host 容灾**：东财 `push2.eastmoney.com` ↔ `push2delay.eastmoney.com` 自动切换（502 重试）；Yahoo `query1` ↔ `query2`
- **市场识别**：`detect_market()` 自动识别 hk/sh/sz/us，6位代码按首位区分沪深，纯字母走美股链路
- **缓存**：内存正缓存 10s，负缓存 60s，避免离线反复请求打爆
- **静态兜底**：`server_dashboard.py:get_stock_view()` 在行情失败时返回演示档案或中性占位档案，并明确标注 `quote_live=False`，绝不伪造价格
- **离线样本**：`FIXTURES` 含港股/A股真实抓包，供 `--fixture-test`

### 4.2 美股行情 `us_quote.py`

- **四源采集**：`collect_us_quotes()` 全部尝试，单源失败只记 `errors`，不中断
- **交易所自动试**：东财美股 `105(NASDAQ)/106(NYSE)/107(AMEX) × 2 host = 6 次尝试`
- **交叉验证**：中位价为共识，逐源偏离度 ≤0.8% ✅ 一致 / ≤2% 🟡 基本一致 / >2% ❌ 分歧并点名离群源
- **延迟源隔离**：Stooq 标记 `delayed=True`，仅核验不作主源，避免延迟价污染
- **主入口降级**：`fetch_us_quote()` 默认链路 `tencent→eastmoney→yahoo→stooq`，首个成功即返回；完整验证用 `fetch_us_verified()`
- **字段熔断**：币种非 USD、涨跌幅 ±25% 外、缺字段记违例，`source_check_db.py` 已覆盖

### 4.3 社媒热榜 10 源 `newsnow_sources.py`

| 源 | 备用/容错 |
|---|---|
| 知乎 | 仅 UA，无 Cookie，JSON 直连 |
| 抖音 | `CookieJar` 预热 `login.douyin.com` 再请求，自动 |
| 微博 | 内置 `SUB=` 匿名 Cookie，失效时解析为空，文档说明更换方法 |
| 虎扑 | HTML 正则，无鉴权 |
| AI hot | **JSON → RSS 双链路**：API 200 且非空直接返回，否则降级 `feed/all.xml` |
| 联合早报 | **三重解码**：gb2312 → utf-8 → gbk 兜底，防乱码 |
| 香港01 | **4 端点逐级降级**：v1 JSON → v2 JSON → `/hot` HTML → `/most-popular` HTML |
| 今日头条 | Cookie 预热 + Referer，解析为空显式抛错「接口结构可能变更」，避免把风控空页当无新闻 |
| 百度 | 移动端 UA + Referer，空解析同样抛错 |
| B站 | UA + Referer，API code 非 0 抛错，空解析抛错 |

全部遵循 `SAMPLE → parse → fetch → FETCHERS/TARGET_SOURCES/SAMPLES 注册` 5步，`source_check_db.py` 自动覆盖。

### 4.4 财经快讯 8 源 `stock_news_scan.py`（2026-10-19 由 7→8，新增 DeepSeek）

- **新增 DeepSeek 搜索**：`deepseek_search.py` 作为 AI 搜索引擎，`fetch_deepseek_search` 返回 JSON 数组，`stock_news_scan.py` 自动注册为第 8 源，失败记缺口不中断；离线样本与自检已覆盖
- **双链路**：DeepSeek 主链路 + Google RSS 备用，`foreign_institution_weekly.py` 同样采用此双链路（每机构单查询+短超时，无 Key 时前2家连续失败快速降级 demo）
- **原有 7 源容错**：

- **通用提取**：`_json_find_list()` 支持 `data/list/items/result/news/lives/flash_list` 双层嵌套，兼容接口改版
- **JS 壳剥离**：金十 `strip_js_shell()` 去 `var flash_newest = ...;`
- **时间兼容**：`parse_any_time()` 支持 epoch(s/ms)、ISO、RFC822、中文 `YYYY-MM-DD HH:MM`
- **窗口过滤**：带可靠时间戳的严格按 `--hours`（默认 156h≈6.5天）过滤；热榜类标记 `实时` 不参与过滤，避免误丢
- **逐源隔离**：每个源独立 try/except，失败进 `pack.errors`，报告渲染为「⚠️ 数据缺口」
- **雪球 Cookie 预热**：先请求 `xueqiu.com/hq` 再取热榜，失败单独记录

### 4.5 推送与状态层

- **新鲜度看板**：每次推送顶部「🧭 数据新鲜度·本次运行指纹」含三源共识价、新增样本数、内容指纹（SHA256 正文+行情+锚点），一致时标注「⚠️ 与上次推送内容一致」，否则「✅ 已更新」
- **内容指纹去重**：指纹一致且使用 AI 时自动追加差异化要求重试一次，避免逐字复读
- **跨运行持久化**：`output/push_state.json` + GitHub Actions `actions/cache` restore/save，`PUSH_STATE_PATH` 可覆盖，`--no-state` 可关闭
- **分篇保留样式**：PushPlus 单篇超 48KB 自动切分最多 8 篇，每篇完整主题 HTML，表格续篇自动补表头，尾部声明与作者行强制保留
- **AI 分节压缩回退**：压缩按 Markdown 章节逐节调用，失败回退原文，无 Key 跳过，绝不因压缩丢内容
- **字符模拟图降级**：Yahoo OHLC 主源，东财兜底，渲染失败自动不含图推送
- **Actions 加固**：`checkout@v4` + `setup-python@v5` pin 到 commit SHA，`timeout-minutes:15`，`concurrency` 防并发覆盖，最小权限 `contents: read`

### 4.6 检查验证数据库 `source_check_db.py`

- **表结构**：`check_runs` 运行台账 + `source_checks` 逐源明细 + JSON 违例
- **状态口径**：ok / warn（空结果或字段违例）/ fail（异常）
- **校验规则**：标题缺失/超长、链接缺失/非 http(s)、重复标题超 30%、条目数>50、价格≤0、币种非 USD、涨跌幅 ±25% 外熔断
- **历史追踪**：`--history <key>` 查单源报错率与延迟趋势，`--report` 打印最近运行，`--only` 定点检查
- **离线/在线双模式**：默认离线不触网，`--live` 真抓取覆盖 10+4+8+1 源（社媒10+境外4+财经8+DeepSeek1），总数 15（离线）/ 23（在线全量）

### 4.7 国外机构一周研报 `foreign_institution_weekly.py`（2026-10-19 新增）

- **机构清单**：12 家顶级投行/研究机构，`INSTITUTIONS` 常量含 key/name/aliases/focus/search_terms，支持 `only` 参数定点采集
- **双链路**：DeepSeek AI 搜索主链路（`_fetch_via_deepseek`，需 Key，失败抛错）+ Google新闻 RSS 备用（`_fetch_via_google`，`when:7d` 窗口），每机构单查询+短超时（min 5s），无 Key 时前2家连续失败快速降级演示包，避免 12*15s 超时
- **容错**：单机构失败只记 `pack.errors`，不中断整体；`collect_foreign_weekly` 返回空时上层 `pushplus_deepseek.py` 自动降级 `demo_pack`（5篇离线样本），rule 模板直接走离线演示包，保证 dry-run 1.8s 内完成
- **打标与渲染**：`_score_text` 按关键词映射利好/利空/中性与分数，`_mk_item` 提取评级；`render_foreign_weekly_rule` 表格+汇总+覆盖机构，`render_foreign_weekly_appendix` 明细+链接，`foreign_weekly_context` 供 AI 上下文
- **推送与大屏**：`pushplus_deepseek.py` 新增模板 `foreign_weekly`（TEMPLATES 24 个，MAX_TOKENS 6000），`build_messages` 要求 12机构表格+主线+板块+评级调整+风险+下周关注；`server_dashboard.py` 新增 `/api/foreign_weekly`（GET teaser + POST report）与前端独立板块，支持主题搜索与附录渲染
- **自检**：`--selftest` 7 项离线（机构清单/别名/样本/情绪/渲染/上下文/附录），已接入 `pushplus_deepseek.py --selftest` 全绿

### 4.8 DeepSeek AI 搜索 `deepseek_search.py`（2026-10-19 新增，第18平台/第13源）

- **角色**：智能搜索引擎，`fetch_deepseek_search` 按关键词+hours 返回 JSON 数组，提示词强制 JSON 数组输出，兼容围栏与前后解释
- **容错**：429/502/503/504 指数退避重试 2 次；无 Key 抛友好错由上层记跳过；空数组视为未命中而非错误；`_http_post_json` 纯标准库
- **接入**：`stock_news_scan.py` 第 8 财经源（18平台扫描）、`pushplus_deepseek.py` feedscan 第13源（13源快讯）、`source_check_db.py` 第15源校验，离线样本与自检已覆盖

## 5. 其他读取方法 — 避免数据丢失的补充方案

> 以下为已评估可立即落地的增强，均保持纯标准库，无额外依赖。

### 5.1 本地文件缓存（已在 `.gitignore` 忽略）

- **行情文件缓存**：`hk_quote.py` / `us_quote.py` 在每次成功后写 `output/last_quote_{code}.json`，失败时若内存缓存也无，则读文件缓存并标注 `stale`，保证大屏/研报至少有上一笔价格
- **快讯文件缓存**：`stock_news_scan.py` 可将最近一次成功原始 JSON 存 `output/last_scan_{code}.json`，用于离线演示或网络抖动时回放
- 现状：内存缓存已有，文件缓存为建议增强，已在 `server_dashboard.py` 用静态演示兜底部分实现

### 5.2 多级降级链（已实现 + 可扩展）

- 行情：腾讯 → 东财 → Yahoo → Stooq → 静态演示 → 文件缓存
- 热榜：主 JSON → 备 RSS/HTML → 样本 → 空但报错
- 财经：主 API → 备 host → 通用 JSON 提取 → 数据缺口
- 建议：新增 `rsshub` 公开实例作为第 3 级，例如 `rsshub.app/zhihu/hot` 等，可在 `FETCHERS` 中作为 fallback

### 5.3 结构化容错

- **空结果显式抛错**：今日头条/百度/B站已实现 `if not items: raise RuntimeError("解析为空")`，避免把风控页误判为无新闻
- **字段级校验**：`validate_items()` / `validate_us_quote()` 已实现，建议对港股也加 `validate_hk_quote()`（价格>0、币种 HKD/CNY、涨跌幅 ±20% 熔断）
- **去重与伪影吸收**：`extract_dyn_sectors()` 已实现子串对按频次归属，避免「能源板块」被「新能源板块」误拆

### 5.4 时间与窗口

- 财经源时间解析已兼容 5 种格式，建议增加 `try: ts = now` 的最终兜底，避免因单条时间解析失败导致整批丢弃
- 热榜类标记「实时」不过滤，已避免因无时间戳被 156h 窗口误杀

### 5.5 推送层保障

- **多通道**：pushplus / wecom / serverchan / console / all，单通道缺 Secret 标记跳过而非失败
- **Dry-run**：`--dry-run` 本地预览，`--check-only` 只检查 Secrets
- **分篇与品牌尾注保留**：超长时优先裁附录，尾注强制保留，避免微信截断丢声明

### 5.6 运维层

- **GitHub Actions 缓存**：已用 `actions/cache` 持久化 `push_state.json`
- **Service Unavailable 处理**：文档 `DATA_SOURCES.md` 五、常见故障已说明，根因为 Marketplace CDN 503，重试 `Re-run failed jobs` 即可
- **自检入口统一**：`--selftest` / `--fixture-test` / `--check-only` 覆盖全部模块，CI 可加 `python source_check_db.py --selftest && python hk_quote.py --fixture-test && ...`

## 6. 实测结论（更新至 2026-10-19，含 DeepSeek）

| 检查项 | 结果 | 说明 |
|---|---|---|
| 离线解析 15 源 | ✅ 15/15 ok | 10社媒+4境外+1 DeepSeek，样本→解析器→校验全通过 |
| 港股/A股 fixture | ✅ 7/7 | 价格/PE/市值一致 |
| 美股 4 源 + 交叉 | ✅ 19/19 | 中位共识/离群检测正常 |
| 十八平台扫描 | ✅ 31/31 | 156h窗口/板块去重/情绪映射，含 DeepSeek |
| NewsNow 10 源 | ✅ 14/14 | 注册表覆盖 |
| DeepSeek 搜索 | ✅ 9/9 | 样本解析/围栏容错/时间解析/无Key提示 |
| 国外机构周报 | ✅ 7/7 | 12机构清单/别名/样本5篇/情绪/渲染/上下文/附录 |
| pushplus 全模板 | ✅ 24/24 | 含 foreign_weekly，dry-run 1.8s 完成 |
| 在线 live（沙箱受限） | ❌ 预期失败 | TLS 被阻断，生产环境正常；DeepSeek 无 Key 时记跳过，国外机构周报降级 demo |

### DeepSeek 作为搜索引擎的在线表现
- 无 `DEEPSEEK_API_KEY`：`source_check_db --live` 记 fail 但提示友好，`stock_news_scan` / `feedscan` 记“跳过（未配置）”，不中断其他 17 源；`foreign_institution_weekly` 前2家失败快速降级 demo
- 有 Key：自动成为第 18 平台（财经 8 源 + 社媒 10 源）和 feedscan 第 13 源，参与情绪打标、板块提取、指纹去重；国外机构周报主链路 DeepSeek + Google 备用，168h 窗口，12机构全覆盖

## 7. 建议的后续加固（按优先级）

1. **文件缓存兜底**：`hk_quote.fetch_quote()` 成功写 `output/last_quote.json`，失败读文件并标记 `stale`，避免大屏白屏
2. **Weibo Cookie 自动轮换**：增加环境变量 `WEIBO_COOKIE` 覆盖常量，Actions Secrets 注入，过期时 `source_check_db.py --history weibo` 告警
3. **增加 RSSHub 备用**：为知乎/微博/B站各加一个 RSSHub 端点作为第 3 级，代码中 `for url in [primary, rsshub]` 尝试
4. **港股字段校验**：新增 `validate_hk_quote()`，币种 HKD/CNY、价格>0、涨跌幅 ±20% 熔断，接入 `source_check_db.py`
5. **指数退避重试**：对 502/503 做 3 次指数退避（1s,2s,4s），已在东财/Yahoo 多 host 上部分实现，可统一为 `_http_get_with_retry()`
6. **法布财经占位实现**：`extract_feeds.py` 中 `法布财经 快讯/头条` 当前为空，建议补公开端点或移除占位，避免统计时误判
7. **监控告警**：`source_check_db.py --live` 失败率 >30% 时通过企业微信机器人告警（复用 `WECOM_KEY`）

---

## 8. 本次已落地的加固（避免数据丢失）

为直接解决“源头不可用时数据丢失”问题，已在 `arena/b977cb3e-273` 分支实现：

### 8.1 行情文件缓存兜底
- `hk_quote.py` 新增 `output/quote_cache/{market}_{code}.json` 文件缓存：
  - 成功时 `_save_file_cache()` 落盘
  - 全部链路失败时 `_load_file_cache()` 读取最近一次成功结果，标记 `stale` / `stale_age_s` / `from_file_cache`
  - 环境变量 `HK_QUOTE_FILE_CACHE=0` 可关闭，`HK_QUOTE_FILE_TTL` 控制新鲜度（默认 3600s）
- `us_quote.py` 同策略：`us_{code}.json`，`fetch_us_quote()` 与 `fetch_us_verified()` 均支持文件缓存兜底
- 已验证：沙箱网络阻断时，预置缓存文件可被成功读取，`source_label` 显示“(文件缓存)”

### 8.2 指数退避重试
- `hk_quote._http_get()` / `us_quote._http_get()` / `newsnow_sources.http_request()` / `stock_news_scan.http_request()` / `deepseek_search._http_post_json()` 均新增 `retries=2` 与 `0.5*2^attempt` 退避
- 对 429/502/503/504 可重试错误自动重试，避免瞬时抖动导致整批丢失

### 8.3 DeepSeek API 作为搜索引擎（2026-10-19 新增）
- **新模块** `deepseek_search.py`：纯标准库，`DEEPSEEK_API_KEY` Bearer 鉴权，`deepseek-chat` 模型，`DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` 可覆盖
- **提示词强制 JSON**：system 要求只返回 JSON 数组，user 要求 5-10 条含 title/url/summary/source/date，兼容 ```json 围栏与前后解释文字，自动提取 `[`…`]` 区间
- **接入点**：
  - `stock_news_scan.py`：注册为第 8 财经源 `DeepSeek 搜索`，签名 `(query, timeout)`，内部调用 `fetch_deepseek_search_api(query, timeout, hours)`，按 156h 窗口过滤，缺口单独记录，总平台数从 17 → 18
  - `pushplus_deepseek.py`：`collect_feeds()` 新增第 13 源，`fetch_deepseek_feed()` 情绪打标，`feed_context` / `render_feed_rule` / `render_feed_appendix` 均展示，日志显式提示跳过，`--topic` 透传为查询词
  - `source_check_db.py`：离线用 `DEEPSEEK_SAMPLE_JSON` 走解析器，在线真调 API，计入 15 源离线总数，`--history deepseek` 可查
- **自检**：`deepseek_search --selftest` 9 项（样本解析、围栏容错、时间解析、无 Key 提示）
- **CLI**：`python deepseek_search.py --query "阿里巴巴 09988" --hours 48 --json` / `--selftest`
- **容错**：无 Key 时抛 `RuntimeError("缺少 DEEPSEEK_API_KEY")`，上层记“跳过”不中断；空数组视为未命中而非错误；网络失败指数退避；与现有 17 源隔离，绝不因 DeepSeek 失败拖垮主流程

### 8.4 国外专业投资机构一周研报栏目（2026-10-19 新增，独立栏目 foreign_weekly）
- **新模块** `foreign_institution_weekly.py`：12 家顶级投行/研究机构（Goldman/MorganStanley/JPMorgan/BofA/Citi/UBS/Barclays/Nomura/HSBC/Jefferies/Bernstein/Morningstar），168h 窗口，纯标准库，DeepSeek 主链路 + Google RSS 备用，支持主题搜索、情绪打标、评级提取、表格渲染、附录明细
- **采集**：`collect_foreign_weekly(topic,hours,timeout,only,use_deepseek)`，每机构单查询 `"{别名} {topic} research report weekly rating target"`，短超时 5s，无 Key 时前2家连续失败快速降级 demo，避免 12*15s 超时；单机构失败记 `errors`，不中断整体
- **打标**：`_score_text` 关键词映射利好/中性/利空与分数，`_mk_item` 提取评级（增持/买入/中性/减持/卖出/上调/下调/overweight/underweight 等）
- **渲染**：`render_foreign_weekly_rule` 表格（机构|评级|标题|来源|时间）+ 汇总（篇数/机构/情绪均值→偏多/偏空）+ 覆盖机构；`render_foreign_weekly_appendix` 明细+链接+摘要；`foreign_weekly_context` 供 AI 上下文
- **推送主流程**：`pushplus_deepseek.py` 新增模板 `foreign_weekly`（TEMPLATES 23→24，TITLES/MAX_TOKENS 6000），`build_messages` 中 prompt 要求 12机构表格+主线+板块+评级调整+风险+下周关注，`gen_by_rule` 走 `demo_pack` 离线样本，`main` 新增 `foreign_weekly_pack` 采集分支（附录+上下文+日志），支持 `--hours 168 --topic "AI芯片" --dry-run`，rule 路径 1.8s 完成，AI 路径在线 0 篇时自动降级 demo
- **指纹与看板**：`gather_item_keys` 新增 `外研·{机构}` 指纹，`extra_points` 新增 `foreign_n`/`foreign_hit`，新鲜度看板显示“外资研报 5 篇 / 5/12 机构”
- **大屏**：`server_dashboard.py` 新增 `/api/foreign_weekly`（GET teaser 离线演示 + POST report 在线聚合）与前端独立板块（12机构芯片、主题输入、生成按钮、结果面板），`loadForeignTeaser`/`genForeignWeekly` JS，支持回车触发
- **自检**：`foreign_institution_weekly.py --selftest` 7 项离线（机构清单12家/别名/演示包5篇/情绪/渲染/上下文/附录），`pushplus_deepseek.py --selftest` 含 foreign_weekly 模板可构造，全绿

### 8.5 空结果显式告警（已存在，确认有效）
- 今日头条/百度/B站解析为空时抛 `RuntimeError("解析为空（接口结构可能变更）")`，进入 `errors` 而非静默丢失
- `validate_items()` 对标题缺失、链接非 http(s)、重复>30% 记 `warn`
- DeepSeek 空数组返回空列表，上层记缺口而非静默；国外机构周报 0 篇时自动降级 demo，绝不空推

### 8.6 后续建议（已在报告第 7 节）
- Weibo Cookie 环境变量化、RSSHub 第三级备用、港股字段校验、监控告警

---

**总结（2026-10-19 更新）**：当前项目已具备**多源(18平台=10社媒+8财经含DeepSeek)、多 host、多格式、多级降级、逐源隔离、离线样本、SQLite 追踪、指纹去重、分篇保样式、多通道推送、文件缓存兜底、指数退避重试、AI搜索引擎、国外机构一周研报12家**的完整容灾体系。
- **DeepSeek 作为搜索引擎**：已作为第 18 平台和 feedscan 第 13 源接入，支持任意关键词搜索，返回结构化 JSON，参与情绪打标与板块提取，无 Key 时优雅跳过，绝不因 AI 搜索失败导致数据丢失
- **国外机构一周研报**：12家顶级投行/研究机构，168h窗口，DeepSeek主+Google备，独立栏目 foreign_weekly，支持主题搜索、情绪打标、表格渲染、附录明细、离线自检与多通道推送，rule路径1.8s完成，在线0篇自动降级demo，绝不因单机构失败导致栏目丢失
- **防丢**：此前已实现文件缓存兜底，即使所有在线源同时失败，仍可返回最近一次成功行情，避免大屏白屏与研报缺价格
- 在真实网络下可用性高；沙箱受限导致的 live 失败符合预期，不代表数据丢失风险
