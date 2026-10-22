# 🤗 Hugging Face 金融资源精选与接入说明

> 配套模块：`hf_hub.py`（纯标准库）+ `test_hf_hub.py`（离线测试）+
> `.github/workflows/hf-sentiment.yml`（周更基线）。

这份文档回答三个问题：

1. **Hugging Face 上哪些项目对本仓库（273 · 章鱼 AI 全景分析）真的有用？**
2. **它们分别解决什么问题、代价是什么（许可 / 体量 / 是否可商用）？**
3. **怎么在不装 `datasets` / `transformers` 的前提下把它们用起来？**

清单里每个仓库都做过存在性核对（模型走 `/api/models/<id>`、数据集走
`datasets-server /is-valid`），核对日期 **2026-10-22**。
下载量/点赞数不写死在文档里——用 `python hf_hub.py --catalog --live` 实时拉。

---

## 0. 一页速览

| 分类 | 首选 | 备选 | 怎么用 |
|---|---|---|---|
| 英文情绪模型 | `ProsusAI/finbert` | `HuangYiYang/finbert-financial-news-sentiment`（新闻标题口径，acc 0.870） | `--engine hf` |
| 中文情绪模型 | `yiyanghkust/finbert-tone-chinese`（acc 0.88 / F1 0.87） | `Roeyyyyyy/quantmind-sentiment-dapt-flame`（中文财经新闻三分类） | `--engine hf --model <id>` |
| 中文情绪基线数据 | `Kenpache/multilingual-financial-sentiment`（中文 7,930 条） | `Maciel/FinCUGE-Instruction`（FinNE 情绪任务） | `--benchmark ... --where "language='zh'"` |
| 英文情绪基线数据 | `zeroshot/twitter-financial-news-sentiment` | `NOSIBLE/financial-sentiment`（10 万条，价格影响口径） | `--benchmark <id>` |
| 研报 / 生成语料 | `jinsong8/FinRpt`（中英研报基准） | `Duxiaoman-DI/FinCorpus`（60G 中文语料，只能切片读） | `--rows <id> --length 3` |
| 新闻流（日频对齐） | `etfbench/etfbench_submission`（731 天 × 中美） | `NickyNicky/...rss-feed`（14.2 万条带 ticker） | `--digest <id>` |
| 时序基座（概率预测） | `amazon/chronos-2`（Apache-2.0） | `google/timesfm-2.5-200m-pytorch`、`Salesforce/moirai-2.0-R-small` | 需 transformers 环境，本仓库目前只登记 |
| **没有 HF 推理额度时** | `--engine llm`（DeepSeek 等大模型端点，走 `inference_api.py`） | `--engine lexicon`（纯离线） | 见 §7 |

---

## 1. 情绪模型（可直接当情绪引擎后端）

### `ProsusAI/finbert` — 英文金融情绪事实标准
- **是什么**：BERT 在金融语料上继续预训练，再用 Financial PhraseBank 微调的三分类器
  （positive / negative / neutral）。
- **为什么选**：HF 官方 `text-classification` 任务页首位推荐；推理状态 `warm`，
  冷启动概率低；体量 110M 参数，serverless CPU 也能跑。
- **代价**：许可 `cc-by-nc-sa-4.0`（**非商用**）。做内部研究与信号生成可以，
  嵌进对外商业产品前要换模型。
- **用法**：`--engine hf`（默认模型就是它）。

### `yiyanghkust/finbert-tone-chinese` — 中文分析师报告语调
- **是什么**：`bert-base-chinese` 在约 8k 条分析师报告句子上微调。
- **指标**：测试准确率 0.88 / macro-F1 0.87。
- **注意**：标签顺序是 **0=中性、1=正面、2=负面**（和常见的 SST-2 约定不同）。
  仓库已在 `hf_hub.LABEL_MAPS` 里写死这个映射，直接传模型名即可，不用自己换算。
- **许可**：Apache-2.0。

### `HuangYiYang/finbert-financial-news-sentiment` — 新闻标题口径（2026-10）
- **是什么**：FinBERT + LoRA（rank 16，已合并回权重）在 5,041 条新闻标题上微调，
  覆盖比特币/黄金/美股三类资产。
- **指标**：训练期**之后 6 个月**的 826 条留出集上 acc **0.870** / macro-F1 **0.812**；
  同口径原版 FinBERT 只有 0.501 / 0.436（McNemar 检验 p=7.2e-67）。
- **为什么值得单独列**：它明确区分了「标题语气」和「价格方向」——
  这是舆情因子最容易踩的坑，本仓库的口径也按这个来（词典只判语气，不预测涨跌）。

### `ahmedrachid/FinancialBERT-Sentiment-Analysis`
- 论文口径在 Financial PhraseBank 上 macro-F1 0.98，推理 `warm`；适合当英文对照模型。

---

## 2. 情绪数据集（当黄金标签用）

用公开标注数据给情绪引擎打基线，是比「看着像对的」靠谱得多的做法。
本仓库的做法：抓 100 行 → 用引擎打标 → 和标准答案比对 → 出准确率 / macro-F1 / 分组表。

```bash
python hf_hub.py --benchmark Kenpache/multilingual-financial-sentiment \
                 --group-field language --length 100
```

| 数据集 | 规模 / 语言 | 特点与坑 |
|---|---|---|
| `Kenpache/multilingual-financial-sentiment` | 39,829 条 · 7 语种（中文 7,930） | 字段 `sentence/label/source/language`；用 `--where "language='zh'"` 只测中文。**做 A 股/港股舆情的首选基线** |
| `zeroshot/twitter-financial-news-sentiment` | 11,932 条 · 英文 | 体量小、加载快，适合当回归基线；标签是 Bullish/Bearish/Neutral |
| `NOSIBLE/financial-sentiment` | 10 万条 · 英文 | 标注视角是「事件对公司的**财务影响**」，不是语气，和 FinBERT 口径不同，别混用 |
| `Pugnola/finance-news-sentiment-35k` | 39,965 条 · 英文 | 双 LLM 裁判 + 仲裁；作者诚实给出了「标注噪声天花板」（双裁判一致处 97% 正确，需仲裁处 65%） |
| `yixuantt/FinEntity` | 实体级 | 一条新闻里「A 被罚、B 受益」时，句子级标注会失真——需要实体级就用它 |
| `takala/financial_phrasebank` | 4,840 句 · 英文 | 经典学术基准，但 **datasets-server 对其 `is-valid=false`**（viewer 不可用），只能走 parquet / 原始文件 |

---

## 3. 研报 / 公告 / 中文金融语料

| 数据集 | 内容 | 用途 |
|---|---|---|
| `jinsong8/FinRpt` | 中英双语个股研报生成基准：给定代码+日期，输入公司概况/财务指标/公告/新闻/行情，输出结构化研报；还保留多智能体中间产物（新闻分析、财务分析、风险评估） | 给本仓库 `equity_research` 九章栏目做 few-shot 语料 / 评测集 |
| `Maciel/FinCUGE-Instruction` | 中文金融 NLP 八任务指令集：摘要 FinNA、事件问答 FinQA、新闻分类 FinNL、关系抽取 FinRE、情绪 FinNE、负面主体判定 FinNSP、因果抽取 FinCQA、主体抽取 FinESE | 中文金融指令微调与评测的一站式来源 |
| `Duxiaoman-DI/FinCorpus` | 中文金融语料约 60G（公告 20G / 资讯 30G / 研报文章 10G / 试题 370M） | 续预训练 / RAG 底座。**只能切片读**（`--rows ... --length 3`），别整包下 |

---

## 4. 行情与新闻流（可直接喂给情绪引擎）

| 数据集 | 特点 |
|---|---|
| `etfbench/etfbench_submission` | `cn_news.jsonl` / `us_news.jsonl` 覆盖 2024-01-01 至 2025-12-31 共 731 个连续日历日，「新闻窗口 → 文本信号」按日对齐——**做新闻因子回测的正经数据** |
| `NickyNicky/finance-financialmodelingprep-stock-news-sentiments-rss-feed` | 14.2 万条新闻，带 ticker / 发布时间 / 情绪分，适合 `--digest` 出「按 ticker 的情绪画像」 |

---

## 5. 时序基座模型（价格与波动率的概率预测）

这三个都是「零样本就能出分位数区间」的时序基座模型。当前本仓库坚持**零第三方依赖**，
所以只登记、不内置；要真正落地需要另起一个带 `transformers` + `torch` 的环境，
把预测结果回灌进本仓库的大屏或推送链路（本仓库负责取数、渲染与推送那一段）。

| 模型 | 参数 | 许可 | 适用场景 |
|---|---|---|---|
| `amazon/chronos-2` | 120M | Apache-2.0 | 上下文 8,192 / 预测步长上限 1,024，原生支持协变量与多变量，输出 P1–P99 分位数；HF 上下载量最高的时序基座 |
| `google/timesfm-2.5-200m-pytorch` | 200M | Apache-2.0 | 上下文 16k，可选 30M 分位数头。**注意 TimesFM 3.0 权重改为非商用许可，生产要用 2.5** |
| `Salesforce/moirai-2.0-R-small` | 11.4M | CC-BY-NC-4.0 | any-variate 原生多变量 / 多频率，11M 很轻，适合多标的相关性建模；非商用许可 |

参考实现（Spaces，可直接开浏览器看别人怎么画分位数区间）：
`towardsinnovationlab/stock_market_analysis_and_forecast`（Chronos）、
`Vishwas1/Moirai_Stock_Price_Predictor`（Moirai）。

---

## 6. 免下载取数：datasets-server 怎么用

传统做法 `pip install datasets` + `load_dataset(...)` 会把整个数据集拉到本地，
GB 级数据集非常痛。datasets-server 提供 HTTP 切片读取，纯标准库就能用：

| 端点 | 作用 |
|---|---|
| `/is-valid` | 这个数据集 viewer 能不能读（有些老数据集 viewer=false） |
| `/splits` | 列出所有 (config, split) 组合 |
| `/rows` | 取切片（**单次上限 100 行**）；返回 `features` + `rows` + `num_rows_total` |
| `/first-rows` | 前 100 行 |
| `/search` | 服务端全文检索（不用拉下来再 grep） |
| `/filter` | 服务端 SQL 过滤，例如 `where="language='zh'"` |
| `/parquet` | 列出 parquet 直链（要整包下时用这个） |
| `/size` | 行数与体积 |

```bash
python hf_hub.py --splits jinsong8/FinRpt
python hf_hub.py --rows jinsong8/FinRpt --length 3
python hf_hub.py --size Duxiaoman-DI/FinCorpus
```

---

## 7. 情绪引擎的四种模式：lexicon / hf / llm / auto

```bash
python hf_hub.py --sentiment "公司三季度净利润同比增长 42%，大幅超市场预期" --engine auto
python hf_hub.py --sentiment "控股股东拟减持不超过 2% 股份" --engine llm      # 走 DeepSeek 等
python hf_hub.py --sentiment-file news.txt --engine lexicon --json
```

| 引擎 | 谁来打标签 | 需要 Key | 速度 | 准确度 | 适用场景 |
|---|---|---|---|---|---|
| `lexicon` | 内置中英金融词典 + 否定/程度副词规则 | 不需要 | 毫秒/条 | 中 | 离线、CI、大批量兜底 |
| `hf` | FinBERT / finbert-tone-chinese（`router.huggingface.co` 分类端点） | HF Token **且要有推理额度** | ~0.5–3s/批 | 高 | 英文新闻、中文研报语调 |
| `llm` | **大模型端点**（走 `inference_api.py`，DeepSeek / OpenAI / 硅基流动 / Ollama…） | 对应厂商 Key | ~1–3s/批（20 条） | 高 | **HF 没额度时的首选**；中文口语、反讽、公告长句 |
| `auto`（默认） | `hf` → `llm` → `lexicon` 依次降级 | 有哪个用哪个 | — | — | 生产默认 |

### 7.1 为什么多了一个 `llm`

2026-10-22 实测：本仓库配置的 HF Token **有效但没有推理额度**，所有
`router.huggingface.co/hf-inference` 调用返回 **HTTP 402**（`You have no remaining credits`）。
402 不是 Key 错——`whoami-v2` 返回 200，检索/下载/datasets-server 全都正常。
所以与其买预付费额度，不如让情绪引擎复用仓库里已有的 `inference_api.py`：

- 13 个推理预设共用一个入口（DeepSeek / OpenAI / 硅基流动 / Moonshot / 智谱 / 阿里百炼 / Ollama / vLLM / LM Studio …）；
- 纯标准库，不新增依赖；
- 换厂商只改环境变量，不动代码。

### 7.2 怎么开（以 DeepSeek 为例）

```bash
export DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx        # 或通用写法，二选一
export INFERENCE_PROVIDER=deepseek             # 指定预设
export INFERENCE_MODEL=deepseek-chat           # 可选，留空用预设默认模型

# 通用写法（任何 OpenAI 兼容端点）
export INFERENCE_BASE_URL=https://api.deepseek.com/v1
export INFERENCE_API_KEY=sk-xxxxxxxxxxxx
export INFERENCE_MODEL=deepseek-chat

python hf_hub.py --check                       # 看「大模型端点」那一行是否 ✅
python hf_hub.py --sentiment "业绩大超预期，上调全年指引" --engine llm
```

预设与环境变量对照：`python inference_api.py --list`。本地 Ollama 那类
`keyless` 端点不需要 Key，`llm_available()` 照样返回 `True`。

### 7.3 `llm` 引擎怎么保证口径一致

提示词只问**语气**，不问涨跌（避免把模型变成"预测股价"，那是另一回事）：

1. 系统提示：三选一 `positive / neutral / negative`，只输出编号与标签；
2. 用户提示：每行 `序号. 正文`，正文截断到 180 字，一批最多 20 条；
3. `temperature=0.0`，减少随机漂移；
4. 解析容错：`1: positive 0.9`、`2）负面`、`**3** neutral`、`{"1":"positive"}`、
   `["positive","negative"]` 都能解析；整批解析不出 → 该批降级本地词典；
   显式 `--engine llm` 且一条都解析不出 → 报 `format` 错误（不静默糊弄）；
5. 输出结构与 `hf` / `lexicon` **完全一致**：`label / score / confidence / probs`，
   下游 `render_sentiment_md`、`market_sentiment`、`benchmark`、大屏面板无需改动。

### 7.4 离线也能跑

`hf_hub.start_llm_mock_server()` 起一个 OpenAI 兼容的"分类器 Mock"：它按编号回
`序号: 标签 0.9`（标签口径就是本地词典），所以**没有 Key 也能验证整条链路**
（提示词 → 调用 → 解析 → 聚合）。自检第 ⑥ 段和 `test_hf_hub.py` 的
`TestLLMEngine` 都用它，CI 全程离线。

```python
import os, hf_hub as hh
server = hh.start_llm_mock_server()
os.environ.update(INFERENCE_BASE_URL=f"{server.root}/v1",
                  INFERENCE_API_KEY="mock-key", INFERENCE_MODEL="mock-classifier")
print(hh.score_texts(["净利润增长超预期"], engine="llm")["aggregate"])
server.close()
```

## 8. 端点与 Key 安全

| 用途 | 端点 |
|---|---|
| Hub 检索 / 详情 | `https://huggingface.co/api/{models,datasets,spaces}` |
| 数据集免下载取数 | `https://datasets-server.huggingface.co/...` |
| Task 推理（文本分类等） | `https://router.huggingface.co/hf-inference/models/<model>` |

- **旧端点已下线**：`https://api-inference.huggingface.co/models/<model>` 已停止服务，
  serverless 推理统一迁到 `router.huggingface.co/hf-inference`。本仓库的
  `inference_api.py`（`huggingface-serverless` 预设）与 `hf_hub.py` 都已更新。
- **Key 只发往 `*.huggingface.co` 与 localhost**，其它域名一律不带 `Authorization` 头。
- 三个地址都可以用 `HF_HUB_BASE` / `HF_DATASETS_SERVER` / `HF_TASK_API` 指向镜像或本机 Mock：

```bash
python hf_hub.py --mock-server 18090
export HF_HUB_BASE=http://127.0.0.1:18090
export HF_DATASETS_SERVER=http://127.0.0.1:18090/datasets-server
export HF_TASK_API=http://127.0.0.1:18090/task/models
python hf_hub.py --search finbert --kind model     # 全程离线
```

Token 只需 **Read** 权限（Inference Providers 调用需要带 inference 权限的 fine-grained token）：
[huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)。

**推理端点（DeepSeek / OpenAI / 硅基流动 / Ollama …）是另一套 Key**，与 HF Token 互不通用：

- 只发往 `INFERENCE_BASE_URL` 指向的主机（或预设厂商域名），Host 白名单由
  `inference_api.py` 的 `allow_outbound_hosts` 控制，默认只放行预设域名与 localhost；
- 日志与 `--check` 只打印脱敏形态（长度 + `前4…后4`），从不打印明文；
- 无 Key 的本地端点（`keyless`，如 Ollama）不会带 `Authorization` 头；
- **HF 402（额度用尽）≠ Key 无效**：`--check` 会区分，`auto` 会自动改走 `llm`。

---

## 9. 扩展清单（`--catalog` 之外还想加什么）

`hf_hub.py` 的 `--search` 走的是 Hub 官方检索，随时可以找新东西：

```bash
python hf_hub.py --search "financial sentiment" --kind dataset --limit 8
python hf_hub.py --search "time series forecasting" --kind model --sort trendingScore --limit 8
python hf_hub.py --info <repo/id> --kind model
```

挑选新资源时建议按这四条筛：

1. **许可能不能用**：`cc-by-nc*` 只能内部研究，`apache-2.0` / `mit` 才谈得上落地；
2. **推理状态**：模型卡上 `inference: warm` 的冷启动概率低，serverless 体验好很多；
3. **口径对不对**：语气 vs 价格影响、句子级 vs 实体级，混用会让指标失去意义；
4. **留出集是不是在训练期之后**：`HuangYiYang/...` 那种「训练后 6 个月的留出集」
   才是有说服力的数字，随机切分往往虚高。

### 推送页金融情绪复核

`analysis` / `sentiment` 推送及 `stock_report.py` 共用的情绪证据附录，
现在自动附带「🧠 金融情绪复核」：综合分、正中负数量、平均强度、
实际引擎、模型与自动降级备注。新闻和扫描标题去重后最多分析 200 条，
同一采集包缓存结果，重复渲染不重复调用推理。

默认 `auto`（HF → 大模型 → 离线词典）；可用环境变量
`SENTIMENT_ENGINE=lexicon|hf|llm|auto` 指定模式。
无样本或复核失败会显示提示，不阻断主报告推送。
四种模式并非四路并行，也不代表股价预测。

日常 09:00 / 17:00 微信全市场简报（`market-brief.yml` 的 `feedscan`）
也在推送正文附带金融情绪复核，保留券商/投行栏目，不恢复原始快讯明细。
更新只对部署后新生成的推送生效，既有微信详情页不会自动更新。
