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

## 7. 情绪引擎的三种模式

| 引擎 | 触发条件 | 行为 |
|---|---|---|
| `auto`（默认） | 有 `HF_TOKEN` | 走 HF 模型；任何失败（限流 / 冷启动 / 网络）**静默降级**到本地词典，并在返回里带 `note` 说明原因 |
| `lexicon` | 显式指定 | 只跑内置中英双语金融词典，零网络、毫秒级，CI 可跑 |
| `hf` | 显式指定 | 只跑 HF 模型；缺 Token 或失败直接抛 `HFError`（kind=auth/http/...），不隐藏问题 |

> **402 不等于 Key 错。** Token 有效但账户没有推理额度时，router 返回
> `402 {"error":"You have no remaining credits. Purchase pre-paid credits ..."}`
> ——此时 `/api/whoami-v2` 仍然 200，`hf_hub` 会把它归为 `kind=payment`，
> `auto` 引擎降级到本地词典，**公开数据集取数完全不受影响**。
> 想确认到底是哪种，跑 `python hf_hub.py --check`：前四项绿、只有模型推理红，就是额度问题。

本地词典（`hf_hub.py` 内）包含约 200 个中英文金融情感词，带三类处理：

- **否定翻转**：「未出现增长」「failed to beat」会把极性翻过来（打 0.8 折）；
- **程度副词**：「大幅 / 显著 / sharply / record」加权 1.5 倍；
- **最长匹配优先**：先匹配「业绩预增」，再匹配「增长」，避免重复计分。

分数落在 `[-1, 1]`（阈值 ±0.18 判正负），聚合后映射成 `-100 ~ +100` 的「偏多 / 中性 / 偏空」。

> ⚠️ 词典判的是**语气**，不是价格方向。`HuangYiYang/...` 的模型卡里那句提醒值得抄在这里：
> *The label describes the tone of the headline, not where a price will go.*

---

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
