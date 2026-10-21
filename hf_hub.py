#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hf_hub.py — Hugging Face Hub 接入层（数据集 / 模型 / Spaces / 金融情绪引擎）

给 273（章鱼 AI 全景分析）补上「从 Hugging Face 拿数据 + 用 Hugging Face 模型算情绪」
两层能力，全部**纯标准库**实现（urllib + json），不装 datasets / transformers /
huggingface_hub，和仓库既有约定一致（python -m compileall 即可自检）。

四件事：

1. Hub 检索：模型 / 数据集 / Spaces 的搜索与详情（下载量、点赞、更新时间、
   pipeline_tag、license），用来挑「现在最值得接进来的 HF 项目」。
   GET https://huggingface.co/api/{models,datasets,spaces}

2. datasets-server 免下载取数：任意公开数据集走
   https://datasets-server.huggingface.co 的 /is-valid /splits /rows /search
   /filter /parquet /size 直接切片读取，**不用 pip install datasets，也不用
   下载整个数据集**（GB 级数据集也能只读 100 行）。

3. 金融情绪引擎：
   - `lexicon` 引擎：内置中英双语金融情感词典（含否定与程度副词处理），
     零依赖、零网络、毫秒级，CI 里可跑；
   - `hf` 引擎：调用 Hugging Face Inference Providers 的 task API
     （https://router.huggingface.co/hf-inference/models/<model>）跑真正的
     FinBERT / 中文 tone 模型，带 Key 时自动启用；
   - `auto`：有 HF Token 走模型，失败或未配置自动降级到词典。

4. 用 HF 公开数据集给本地词典打分：`--benchmark <dataset_id>` 直接抓
   HF 上的情感标注数据集（如 Kenpache/multilingual-financial-sentiment、
   zeroshot/twitter-financial-news-sentiment）当黄金标签，算出准确率 /
   macro-F1 / 混淆矩阵——离线词典不再「凭感觉」。

Token（只需 Read / Inference Providers 权限）：
    HF_TOKEN → HUGGINGFACE_API_KEY → HUGGINGFACEHUB_API_TOKEN → INFERENCE_API_KEY
    https://huggingface.co/settings/tokens

CLI 速查：
    python hf_hub.py --catalog                             # 精选金融 HF 资源目录
    python hf_hub.py --search "financial sentiment" --kind dataset --limit 8
    python hf_hub.py --info ProsusAI/finbert --kind model
    python hf_hub.py --rows Kenpache/multilingual-financial-sentiment --length 5
    python hf_hub.py --ds-search zeroshot/twitter-financial-news-sentiment -q "Nvidia"
    python hf_hub.py --sentiment "公司三季度净利润同比增长 42%，超市场预期" --json
    python hf_hub.py --benchmark Kenpache/multilingual-financial-sentiment --length 120
    python hf_hub.py --digest NickyNicky/finance-financialmodelingprep-stock-news-sentiments-rss-feed
    python hf_hub.py --check                                # 连通性自检（需网络）
    python hf_hub.py --mock-server 18090                    # 离线 Mock（HF 三协议）
    python hf_hub.py --selftest                             # 纯离线自检（CI 门禁）

离线设计：所有网络端点都可用环境变量指向自建/镜像/Mock：
    HF_HUB_BASE / HF_DATASETS_SERVER / HF_TASK_API
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Sequence

VERSION = "1.0.0-hf-2026-10-22"
USER_AGENT = f"octopus-hf-hub/{VERSION}"

# ——— Hub 三类资源
HUB_BASE = "https://huggingface.co"
# ——— datasets-server：免下载切片读取任意公开数据集
DATASETS_SERVER = "https://datasets-server.huggingface.co"
# ——— Inference Providers 的 task API（text-classification / zero-shot 等）。
#     注意：旧的 https://api-inference.huggingface.co/models/<model> 已下线，
#     统一改用 router.huggingface.co（见 --check 输出）。
HF_TASK_API = "https://router.huggingface.co/hf-inference/models"

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 0.5
MAX_RETRY_AFTER = 10.0
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}

# Token 读取顺序（只需 Read / Inference Providers 权限）
HF_TOKEN_ENVS = ("HF_TOKEN", "HUGGINGFACE_API_KEY",
                 "HUGGINGFACEHUB_API_TOKEN", "INFERENCE_API_KEY")

KINDS = ("model", "dataset", "space")

# 情绪模型默认选择：英文 FinBERT（HF 官方 text-classification 推荐模型）、
# 中文 tone 模型（yiyanghkust，Apache-2.0）。
DEFAULT_SENTIMENT_MODEL = "ProsusAI/finbert"
DEFAULT_SENTIMENT_MODEL_ZH = "yiyanghkust/finbert-tone-chinese"


# ================================================================ 错误类型


class HFError(RuntimeError):
    """Hub / datasets-server / 推理调用失败；kind 供 App 端分支处理。"""

    # payment：HF 推理额度用尽（HTTP 402）。这不是 Token 无效——whoami 仍会 200，
    # 只有 Inference Providers 的任务调用会被拒。auto 引擎会据此降级到本地词典。
    KINDS = ("config", "auth", "payment", "rate_limit", "http", "network",
             "timeout", "format", "not_found")

    def __init__(self, kind: str, message: str, *, status: int = 0,
                 body: str = "", url: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.body = body
        self.url = url

    def to_dict(self) -> dict:
        return {"ok": False, "kind": self.kind, "status": self.status,
                "error": str(self), "url": self.url}


# ================================================================ 基础工具


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def hub_base() -> str:
    """Hub API 根地址（可用 HF_HUB_BASE 指向镜像 / Mock）。"""
    return (_env("HF_HUB_BASE") or HUB_BASE).rstrip("/")


def datasets_server_base() -> str:
    return (_env("HF_DATASETS_SERVER") or DATASETS_SERVER).rstrip("/")


def task_api_base() -> str:
    return (_env("HF_TASK_API") or HF_TASK_API).rstrip("/")


def hf_token() -> str:
    for name in HF_TOKEN_ENVS:
        value = _env(name)
        if value:
            return value
    return ""


def has_token() -> bool:
    return bool(hf_token())


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def is_local_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in LOCAL_HOSTS


def _token_allowed(url: str) -> bool:
    """Key 只发给 *.huggingface.co 或本机（Mock / 自建网关），避免外泄。"""
    if is_local_url(url):
        return True
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "huggingface.co" or host.endswith(".huggingface.co")


def _error_from_http(status: int, body: str, url: str) -> HFError:
    low = (body or "").lower()
    if status in (401, 403):
        kind = "auth"
        msg = "HF Token 无效或权限不足（需 Read / Inference Providers）"
    elif status == 402:
        # 2026-10-22 实测：Token 有效但账户无额度时，router 返回
        # {"error":"You have no remaining credits. Purchase pre-paid credits ... "}
        kind = "payment"
        msg = ("HF 推理额度已用尽（账户无 credits）：需购买 pre-paid credits "
               "或订阅 PRO；Token 本身仍然有效，公开数据集与本地词典不受影响")
    elif status == 404:
        kind = "not_found"
        msg = "资源不存在（仓库名拼写？数据集未公开 / 被 gated？）"
    elif status == 429:
        kind = "rate_limit"
        msg = "HF 限流（稍后重试或降低并发）"
    elif status >= 500:
        kind = "http"
        msg = f"HF 服务端错误 HTTP {status}"
    else:
        kind = "http"
        msg = f"HF 返回 HTTP {status}"
    # 模型冷启动是常态，把 hint 带上
    if "loading" in low or "currently loading" in low:
        msg += "（模型冷启动中，可重试或带 wait_for_model）"
    return HFError(kind, msg, status=status, body=body[:1000], url=url)


def _retry_sleep(attempt: int, backoff: float, headers: Any = None) -> None:
    delay = backoff * (2 ** attempt)
    if headers is not None:
        try:
            retry_after = float(headers.get("Retry-After") or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
        if retry_after > 0:
            delay = max(delay, min(retry_after, MAX_RETRY_AFTER))
    time.sleep(min(delay, MAX_RETRY_AFTER))


def _network_error(exc: BaseException, url: str) -> HFError:
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return _error_from_http(exc.code, body, url)
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return HFError("timeout", f"请求超时：{reason}", url=url)
        return HFError("network", f"网络不可达：{reason}", url=url)
    if isinstance(exc, TimeoutError):
        return HFError("timeout", f"请求超时：{exc}", url=url)
    return HFError("network", f"{exc.__class__.__name__}: {exc}", url=url)


def _request_json(method: str, url: str, *, params: dict | None = None,
                  payload: Any = None, headers: dict | None = None,
                  timeout: float | None = None,
                  retries: int | None = None,
                  backoff: float | None = None) -> Any:
    """发一次 JSON 请求；429/5xx 指数退避重试，错误统一成 HFError。"""
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean)}"
    data = None
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    token = hf_token()
    if token and _token_allowed(url):
        req_headers["Authorization"] = f"Bearer {token}"
    if headers:
        req_headers.update(headers)

    attempts = max(0, int(DEFAULT_MAX_RETRIES if retries is None else retries)) + 1
    sleep_base = DEFAULT_RETRY_BACKOFF if backoff is None else backoff
    last: HFError | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=req_headers,
                                         method=method.upper())
            with urllib.request.urlopen(
                    req, timeout=DEFAULT_TIMEOUT if timeout is None else timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            if not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise HFError("format", f"响应不是合法 JSON：{raw[:200]}",
                              url=url) from exc
        except HFError as exc:
            last = exc
            if exc.kind not in ("http", "rate_limit") or exc.status not in RETRY_STATUS:
                raise
            if attempt < attempts - 1:
                _retry_sleep(attempt, sleep_base)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            err = _network_error(exc, url)
            last = err
            if err.status in RETRY_STATUS and attempt < attempts - 1:
                _retry_sleep(attempt, sleep_base,
                             getattr(exc, "headers", None) if isinstance(
                                 exc, urllib.error.HTTPError) else None)
                continue
            raise err from exc
    raise last or HFError("network", f"请求失败：{url}", url=url)


# ================================================================ Hub 检索


def search(kind: str, query: str = "", *, author: str = "",
           tag: str = "", sort: str = "downloads", direction: int = -1,
           limit: int = 10, full: bool = False) -> list[dict]:
    """检索 Hub：kind ∈ model / dataset / space。"""
    if kind not in KINDS:
        raise HFError("config", f"未知资源类型：{kind}（可选 {'/'.join(KINDS)}）")
    params: dict[str, Any] = {
        "search": query or "",
        "author": author,
        "sort": sort,
        "direction": direction,
        "limit": max(1, min(100, int(limit))),
    }
    if kind == "model":
        if tag:
            params["filter"] = tag
        params["full"] = "true" if full else "false"
        params.setdefault("config", "true")
    elif kind == "dataset" and tag:
        params["filter"] = tag
    raw = _request_json("GET", f"{hub_base()}/api/{kind}s", params=params)
    if not isinstance(raw, list):
        raise HFError("format", f"Hub 返回结构异常：{str(raw)[:160]}")
    return [normalize_repo(kind, item) for item in raw]


def search_models(query: str = "", **kw: Any) -> list[dict]:
    return search("model", query, **kw)


def search_datasets(query: str = "", **kw: Any) -> list[dict]:
    return search("dataset", query, **kw)


def search_spaces(query: str = "", **kw: Any) -> list[dict]:
    kw.pop("tag", None)
    return search("space", query, **kw)


def normalize_repo(kind: str, raw: dict) -> dict:
    """把 Hub 原始条目收敛成一份稳定字段，App / 大屏直接可用。"""
    repo_id = raw.get("id") or raw.get("modelId") or ""
    author, _, name = repo_id.partition("/")
    tags = [str(t) for t in (raw.get("tags") or [])]
    license_id = ""
    for tag in tags:
        if tag.startswith("license:"):
            license_id = tag.split(":", 1)[1]
            break
    item = {
        "kind": kind,
        "id": repo_id,
        "author": author,
        "name": name or repo_id,
        "downloads": int(raw.get("downloads") or 0),
        "likes": int(raw.get("likes") or 0),
        "updated": raw.get("lastModified") or "",
        "tags": tags[:12],
        "license": license_id or (raw.get("cardData") or {}).get("license", "")
        if isinstance(raw.get("cardData"), dict) else license_id,
        "url": f"https://huggingface.co/{'spaces' if kind == 'space' else kind + 's'}/{repo_id}",
    }
    if kind == "model":
        item["pipeline_tag"] = raw.get("pipeline_tag") or ""
        item["library"] = raw.get("library_name") or ""
        item["inference"] = raw.get("inference") or ""
    elif kind == "dataset":
        item["pipeline_tag"] = ""
        item["downloads_all"] = int(raw.get("downloadsAllTime") or 0)
    else:
        item["sdk"] = raw.get("sdk") or ""
        item["runtime"] = "running" if (raw.get("runtime") or {}).get("stage") == "RUNNING" else ""
    return item


def repo_info(repo_id: str, kind: str = "model") -> dict:
    """单个仓库详情（404 会带明确提示）。"""
    repo_id = repo_id.strip().strip("/")
    if kind not in KINDS:
        raise HFError("config", f"未知资源类型：{kind}")
    raw = _request_json("GET", f"{hub_base()}/api/{kind}s/{repo_id}")
    if not isinstance(raw, dict):
        raise HFError("format", f"Hub 返回结构异常：{str(raw)[:160]}")
    item = normalize_repo(kind, raw)
    siblings = raw.get("siblings") or []
    item["files"] = [s.get("rfilename", "") for s in siblings][:50]
    item["summary"] = {
        "gated": bool(raw.get("gated")),
        "private": bool(raw.get("private")),
        "disabled": bool(raw.get("disabled")),
        "created_at": raw.get("createdAt") or "",
        "sha": (raw.get("sha") or "")[:12],
    }
    if kind == "model":
        item["config_arch"] = ((raw.get("config") or {}).get("architectures") or [])
        item["warm"] = raw.get("inference") == "warm"
    return item


# ================================================================ datasets-server


def _ds_url(path: str) -> str:
    return f"{datasets_server_base()}/{path.lstrip('/')}"


def dataset_is_valid(dataset: str) -> dict:
    """数据集是否可被 viewer 读取（preview / viewer / search / filter / statistics）。"""
    raw = _request_json("GET", _ds_url("is-valid"), params={"dataset": dataset})
    return raw if isinstance(raw, dict) else {"raw": raw}


def dataset_splits(dataset: str) -> list[dict]:
    """列出 (config, split) 组合；config/split 留空时用它自动补全。"""
    raw = _request_json("GET", _ds_url("splits"), params={"dataset": dataset})
    splits = (raw or {}).get("splits") or []
    return [{"config": s.get("config", ""), "split": s.get("split", "")}
            for s in splits if isinstance(s, dict)]


def _auto_split(dataset: str, config: str | None, split: str | None) -> tuple[str, str]:
    if config and split:
        return config, split
    splits = dataset_splits(dataset)
    if not splits:
        raise HFError("not_found", f"数据集没有可读 split（可能在转换队列中）：{dataset}")
    first = splits[0]
    if not config:
        config = first["config"]
    if not split:
        for s in splits:
            if s["config"] == config:
                return config, s["split"]
        return config, first["split"]
    return config, split


def dataset_rows(dataset: str, *, config: str | None = None,
                 split: str | None = None, offset: int = 0,
                 length: int = 20, timeout: float | None = None) -> dict:
    """按切片取行（最大 100 行/次），返回 records + 元信息。"""
    config, split = _auto_split(dataset, config, split)
    raw = _request_json(
        "GET", _ds_url("rows"),
        params={"dataset": dataset, "config": config, "split": split,
                "offset": max(0, int(offset)),
                "length": max(1, min(100, int(length)))},
        timeout=timeout)
    rows = raw.get("rows") or []
    records = [r.get("row", {}) for r in rows if isinstance(r, dict)]
    return {
        "dataset": dataset, "config": config, "split": split,
        "offset": max(0, int(offset)),
        "num_rows_total": raw.get("num_rows_total"),
        "features": [(f or {}).get("name", "") for f in (raw.get("features") or [])],
        "records": records,
        "partial": bool(raw.get("partial")),
    }


def dataset_first_rows(dataset: str, *, config: str | None = None,
                       split: str | None = None) -> dict:
    return dataset_rows(dataset, config=config, split=split, offset=0, length=100)


def dataset_search(dataset: str, query: str, *, config: str | None = None,
                   split: str | None = None, offset: int = 0,
                   length: int = 10, timeout: float | None = None) -> dict:
    """服务端全文检索（不用把数据集拉下来再 grep）。"""
    config, split = _auto_split(dataset, config, split)
    raw = _request_json(
        "GET", _ds_url("search"),
        params={"dataset": dataset, "config": config, "split": split,
                "query": query, "offset": max(0, int(offset)),
                "length": max(1, min(100, int(length)))},
        timeout=timeout)
    rows = raw.get("rows") or []
    return {
        "dataset": dataset, "config": config, "split": split, "query": query,
        "num_rows_total": raw.get("num_rows_total"),
        "records": [r.get("row", {}) for r in rows if isinstance(r, dict)],
    }


def dataset_filter(dataset: str, where: str, *, config: str | None = None,
                   split: str | None = None, offset: int = 0,
                   length: int = 10, orderby: str = "",
                   timeout: float | None = None) -> dict:
    """服务端 SQL 过滤，例如 where=\"label = 'positive'\"。"""
    config, split = _auto_split(dataset, config, split)
    raw = _request_json(
        "GET", _ds_url("filter"),
        params={"dataset": dataset, "config": config, "split": split,
                "where": where, "orderby": orderby,
                "offset": max(0, int(offset)),
                "length": max(1, min(100, int(length)))},
        timeout=timeout)
    rows = raw.get("rows") or []
    return {
        "dataset": dataset, "config": config, "split": split, "where": where,
        "num_rows_total": raw.get("num_rows_total"),
        "records": [r.get("row", {}) for r in rows if isinstance(r, dict)],
    }


def dataset_parquet(dataset: str) -> dict:
    raw = _request_json("GET", _ds_url("parquet"), params={"dataset": dataset})
    files = raw.get("parquet_files") or []
    return {"dataset": dataset,
            "files": [{"config": f.get("config"), "split": f.get("split"),
                       "url": f.get("url"), "size": f.get("size")}
                      for f in files if isinstance(f, dict)]}


def dataset_size(dataset: str) -> dict:
    raw = _request_json("GET", _ds_url("size"), params={"dataset": dataset})
    size = (raw or {}).get("size") or {}
    return {
        "dataset": dataset,
        "rows": (size.get("dataset") or {}).get("num_rows"),
        "bytes": (size.get("dataset") or {}).get("num_bytes_original_files"),
        "raw": size,
    }


# ================================================================ 精选金融目录
#
# 全部为**已核对存在**的公开仓库（核对日期 2026-10-22）：
# 模型用 /api/models/<id>、数据集用 datasets-server /is-valid 逐一验证。
# 统计数字（下载量/点赞）不写死——运行时 live_stats() 实时拉取。

HF_CATALOG: list[dict] = [
    # ——— ① 情绪模型（直接当情绪引擎后端）
    {"kind": "model", "cat": "sentiment-model", "id": "ProsusAI/finbert",
     "label": "FinBERT（金融情绪三分类事实标准）",
     "lang": "en", "license": "cc-by-nc-sa-4.0",
     "why": "BERT 在金融语料上继续预训练 + Financial PhraseBank 微调，"
            "输出 positive/negative/neutral；HF 官方 text-classification 任务页首位推荐。",
     "use": "--sentiment \"...\" --engine hf"},
    {"kind": "model", "cat": "sentiment-model", "id": "yiyanghkust/finbert-tone-chinese",
     "label": "FinBERT-tone 中文（分析师报告语调）",
     "lang": "zh", "license": "apache-2.0",
     "why": "bert-base-chinese 在约 8k 分析师报告句子上微调，测试准确率 0.88 / "
            "macro-F1 0.87；标签 0=中性 1=正面 2=负面（已在 LABEL_MAPS 里映射）。",
     "use": "--sentiment \"...\" --engine hf --model yiyanghkust/finbert-tone-chinese"},
    {"kind": "model", "cat": "sentiment-model",
     "id": "HuangYiYang/finbert-financial-news-sentiment",
     "label": "FinBERT + LoRA（新闻标题口径，2026-10）",
     "lang": "en", "license": "cc-by-nc-sa-4.0",
     "why": "5,041 条新闻标题微调 + 训练期后 6 个月的 826 条留出集："
            "准确率 0.870 / macro-F1 0.812，原版 FinBERT 只有 0.501 / 0.436。"
            "强调「标的是标题语气，不是价格方向」。",
     "use": "--engine hf --model HuangYiYang/finbert-financial-news-sentiment"},
    {"kind": "model", "cat": "sentiment-model",
     "id": "ahmedrachid/FinancialBERT-Sentiment-Analysis",
     "label": "FinancialBERT（PhraseBank 上最优）",
     "lang": "en", "license": "未声明",
     "why": "论文口径在 Financial PhraseBank 上 macro-F1 0.98，推理状态 warm，适合做英文对照。",
     "use": "--engine hf --model ahmedrachid/FinancialBERT-Sentiment-Analysis"},
    {"kind": "model", "cat": "sentiment-model",
     "id": "Roeyyyyyy/quantmind-sentiment-dapt-flame",
     "label": "QuantMind 中文财经新闻三分类（DAPT + LoRA）",
     "lang": "zh", "license": "未声明",
     "why": "bert-base-chinese 先做 MLM 域自适应（20 万条中文市场语料），再用 LoRA 在 "
            "14,680 条中文财经新闻上微调；测试 acc 0.829 / macro-F1 0.808，"
            "标签为 看涨/看跌/中性（已在 LABEL_MAPS 里映射）。"
            "训练数据正是目录里的 Kenpache 数据集，两者可配对做中文评测。",
     "use": "--engine hf --model Roeyyyyyy/quantmind-sentiment-dapt-flame"},

    # ——— ② 情绪数据集（当黄金标签做基线 / 训练 / 回测）
    {"kind": "dataset", "cat": "sentiment-dataset",
     "id": "zeroshot/twitter-financial-news-sentiment",
     "label": "推特金融新闻情绪（经典基准）",
     "lang": "en", "license": "mit",
     "why": "11,932 条标注推文（train 9,542 / validation 2,390），"
            "Bullish / Bearish / Neutral；体量小、加载快，适合当回归基线。",
     "use": "--benchmark zeroshot/twitter-financial-news-sentiment"},
    {"kind": "dataset", "cat": "sentiment-dataset",
     "id": "Kenpache/multilingual-financial-sentiment",
     "label": "多语言金融情绪（含中文 7,930 条）",
     "lang": "zh/en/ja/es/de/fr/ar", "license": "mit",
     "why": "39,829 条、7 语种、80+ 新闻源，字段 sentence/label/source/language；"
            "用 where=\"language='zh'\" 可只取中文——本仓库做 A 股/港股舆情的首选基线。",
     "use": "--benchmark Kenpache/multilingual-financial-sentiment --where \"language='zh'\""},
    {"kind": "dataset", "cat": "sentiment-dataset", "id": "NOSIBLE/financial-sentiment",
     "label": "NOSIBLE 10 万条新闻情绪",
     "lang": "en", "license": "未声明",
     "why": "10 万条清洗去重后的短文本，标注「事件对公司的财务影响」正/中/负，"
            "视角是价格影响而非语气，适合做基本面口径的补充。",
     "use": "--benchmark NOSIBLE/financial-sentiment"},
    {"kind": "dataset", "cat": "sentiment-dataset",
     "id": "Pugnola/finance-news-sentiment-35k",
     "label": "4 万条 LLM 双裁判标注新闻标题",
     "lang": "en", "license": "未声明",
     "why": "39,965 条真实新闻快讯，两模型独立标注 + 仲裁；"
            "FinBERT 在其上微调后 test acc 0.847 / macro-F1 0.810，"
            "并给出「标注噪声天花板」的诚实分析。",
     "use": "--benchmark Pugnola/finance-news-sentiment-35k"},
    {"kind": "dataset", "cat": "sentiment-dataset", "id": "yixuantt/FinEntity",
     "label": "FinEntity（实体级金融情绪）",
     "lang": "en", "license": "未声明",
     "why": "新闻中每个金融实体单独标注情绪——一条新闻里「A 公司被罚、B 公司受益」"
            "这种混合情绪，句子级标注会失真，实体级才是正解。",
     "use": "--benchmark yixuantt/FinEntity"},
    {"kind": "dataset", "cat": "sentiment-dataset", "id": "takala/financial_phrasebank",
     "label": "Financial PhraseBank（经典学术基准）",
     "lang": "en", "license": "cc-by-sa-4.0",
     "why": "Malo et al. 2014，4,840 句人工标注；注意：datasets-server 对其 "
            "is-valid=false（viewer 不可用），需直接取 parquet / 原始文件。",
     "use": "--parquet takala/financial_phrasebank（viewer 不可用）"},

    # ——— ③ 研报 / 财报 / 公告语料
    {"kind": "dataset", "cat": "report-dataset", "id": "jinsong8/FinRpt",
     "label": "FinRpt 中英双语个股研报生成基准",
     "lang": "zh/en", "license": "未声明",
     "why": "给定代码 + 日期，输入公司概况/财务指标/公告/新闻/行情，"
            "输出结构化研报；同时保留多智能体中间产物（新闻分析、财务分析、风险评估）。"
            "可直接当本仓库 equity_research 九章栏目的 few-shot 语料。",
     "use": "--rows jinsong8/FinRpt --length 3"},
    {"kind": "dataset", "cat": "report-dataset", "id": "Maciel/FinCUGE-Instruction",
     "label": "FinCUGE 中文金融 NLP 八任务指令集",
     "lang": "zh", "license": "未声明",
     "why": "摘要 FinNA / 事件问答 FinQA / 新闻分类 FinNL / 关系抽取 FinRE / "
            "情绪 FinNE / 负面主体 FinNSP / 因果 FinCQA / 主体抽取 FinESE；"
            "中文金融指令微调与评测的一站式来源。",
     "use": "--rows Maciel/FinCUGE-Instruction --length 3"},
    {"kind": "dataset", "cat": "report-dataset", "id": "Duxiaoman-DI/FinCorpus",
     "label": "FinCorpus 中文金融语料（约 60G）",
     "lang": "zh", "license": "未声明",
     "why": "上市公司公告 20G + 金融资讯 30G + 研报文章 10G + 金融试题 370M；"
            "续预训练 / RAG 底座。体量大，**只能切片读**，别整包下。",
     "use": "--rows Duxiaoman-DI/FinCorpus --length 3"},

    # ——— ④ 行情 / 新闻流（可直接喂给舆情引擎）
    {"kind": "dataset", "cat": "market-dataset",
     "id": "NickyNicky/finance-financialmodelingprep-stock-news-sentiments-rss-feed",
     "label": "FMP 股票新闻 + 情绪 RSS（14.2 万条）",
     "lang": "en", "license": "未声明",
     "why": "带 ticker / 发布时间 / 情绪分的新闻流，"
            "适合跑 `--digest` 做「按 ticker 的情绪画像」。",
     "use": "--digest NickyNicky/finance-financialmodelingprep-stock-news-sentiments-rss-feed"},
    {"kind": "dataset", "cat": "market-dataset", "id": "etfbench/etfbench_submission",
     "label": "ETF-Bench 日频新闻与收益对齐（2024-2025）",
     "lang": "zh/en", "license": "未声明",
     "why": "cn_news.jsonl / us_news.jsonl 覆盖 731 个连续日历日，"
            "「新闻窗口 → 文本信号」按日对齐收益——做新闻因子回测的正经数据。",
     "use": "--rows etfbench/etfbench_submission --length 3"},

    # ——— ⑤ 时序基座模型（价格 / 波动率概率预测）
    {"kind": "model", "cat": "tsfm", "id": "amazon/chronos-2",
     "label": "Chronos-2（HF 下载量最高的时序基座）",
     "lang": "-", "license": "apache-2.0",
     "why": "1.2 亿参数，上下文 8,192、预测步长上限 1,024，原生支持协变量与多变量，"
            "输出分位数（可做概率区间）；T5 骨架，单 GPU 300+ 次/秒。",
     "use": "需 transformers 环境；本仓库零依赖策略下仅登记，供后续接入"},
    {"kind": "model", "cat": "tsfm", "id": "google/timesfm-2.5-200m-pytorch",
     "label": "TimesFM 2.5（200M，Apache-2.0）",
     "lang": "-", "license": "apache-2.0",
     "why": "Google 时序基座，2 亿参数、上下文 16k，可选 30M 分位数头输出 P10–P90。"
            "注意：TimesFM 3.0 权重为非商用许可，**生产要用 2.5**。",
     "use": "需 transformers 环境；本仓库零依赖策略下仅登记，供后续接入"},
    {"kind": "model", "cat": "tsfm", "id": "Salesforce/moirai-2.0-R-small",
     "label": "Moirai 2.0（any-variate，11M）",
     "lang": "-", "license": "cc-by-nc-4.0",
     "why": "解码器-only + 多 token 预测，原生处理多变量/多频率；"
            "11M 版本很轻，适合多标的相关性建模。",
     "use": "需 transformers 环境；本仓库零依赖策略下仅登记，供后续接入"},

    # ——— ⑥ 参考实现（Space）
    {"kind": "space", "cat": "reference-space",
     "id": "towardsinnovationlab/stock_market_analysis_and_forecast",
     "label": "Chronos 股票分析与预测 Space",
     "lang": "en", "license": "-",
     "why": "Chronos-2 官方卡上列出的股票分析与预测参考实现，"
            "可对照本仓库大屏的「预测区间」交互设计。",
     "use": "浏览器打开参考"},
    {"kind": "space", "cat": "reference-space", "id": "Vishwas1/Moirai_Stock_Price_Predictor",
     "label": "Moirai 股价预测 Space",
     "lang": "en", "license": "-",
     "why": "Moirai 2.0 的股价预测演示，看别人怎么把分位数区间画到 K 线上。",
     "use": "浏览器打开参考"},
]

CATALOG_CATEGORIES = {
    "sentiment-model": "🤖 情绪模型（可直接当情绪引擎后端）",
    "sentiment-dataset": "🏷 情绪数据集（黄金标签 / 基线）",
    "report-dataset": "📑 研报 · 公告 · 中文金融语料",
    "market-dataset": "📈 行情与新闻流（日频对齐）",
    "tsfm": "🔮 时序基座模型（概率预测）",
    "reference-space": "🚀 参考实现（Spaces）",
}


def catalog(category: str = "", *, with_stats: bool = False,
            timeout: float | None = None) -> list[dict]:
    """返回精选目录；with_stats=True 时实时拉取下载量/点赞（失败留空，不抛错）。"""
    items = [dict(x) for x in HF_CATALOG]
    if category:
        items = [x for x in items if x["cat"] == category]
    if with_stats:
        for item in items:
            try:
                info = repo_info(item["id"], item["kind"])
                item["downloads"] = info.get("downloads", 0)
                item["likes"] = info.get("likes", 0)
                item["updated"] = info.get("updated", "")
                item["live"] = True
            except Exception as exc:  # noqa: BLE001 —— 目录展示不能因为单点失败而崩
                item["live"] = False
                item["live_error"] = f"{exc.__class__.__name__}"
    return items


# ================================================================ 情绪引擎（词典）

# 中文金融情感词（权重：绝对值越大越强）
CN_POSITIVE = {
    "涨停": 3.0, "一字板": 2.5, "暴涨": 4.0, "飙升": 3.5, "大涨": 3.0, "拉升": 2.5,
    "放量上涨": 2.5, "走强": 2.0, "反弹": 1.5, "创新高": 3.0, "新高": 2.0,
    "突破": 1.5, "超预期": 3.0, "超市场预期": 3.0, "好于预期": 2.5, "胜预期": 2.5,
    "业绩预增": 3.0, "预增": 2.0, "扭亏": 2.5, "由亏转盈": 3.0, "净利增长": 2.5,
    "净利润增长": 2.5, "利润增长": 2.0, "营收增长": 2.0, "增长": 1.5, "同比增长": 1.0,
    "盈利": 1.5, "盈利改善": 2.5, "毛利率提升": 2.0, "降本增效": 2.0, "减亏": 1.5,
    "中标": 2.0, "签约": 1.0, "订单增长": 2.0, "新增订单": 2.0, "满产": 1.5,
    "供不应求": 2.5, "产能扩张": 1.5, "扩产": 1.5, "提价": 1.5, "涨价预期": 1.5,
    "回购": 1.5, "增持": 2.0, "分红": 1.0, "派息": 1.0, "特别股息": 2.0,
    "利好": 2.5, "上调目标价": 2.0, "上调评级": 2.0, "买入评级": 2.0, "增持评级": 2.0,
    "跑赢大盘": 1.5, "回暖": 1.5, "复苏": 1.5, "复苏迹象": 1.5, "企稳": 1.2,
    "获批": 1.5, "获批上市": 2.0, "政策支持": 2.0, "减税": 2.0, "降准": 2.0,
    "降息": 2.0, "补贴": 1.2, "刺激": 1.5, "松绑": 1.5, "看涨": 2.5, "做多": 2.0,
    "资金流入": 1.8, "北向净买入": 2.0, "业绩超预期": 3.0,
}

CN_NEGATIVE = {
    "跌停": -3.0, "一字跌停": -3.5, "暴跌": -4.0, "崩盘": -5.0, "跳水": -3.0,
    "大跌": -3.0, "重挫": -3.0, "下挫": -2.0, "下跌": -2.0, "走弱": -2.0,
    "回落": -1.5, "承压": -1.5, "疲软": -2.0, "创新低": -3.0, "新低": -2.0,
    "低于预期": -3.0, "不及预期": -3.0, "逊于预期": -2.5, "业绩预减": -3.0,
    "预亏": -3.0, "预减": -2.5, "亏损": -2.5, "由盈转亏": -3.5, "巨亏": -4.0,
    "下滑": -1.5, "下降": -1.0, "萎缩": -1.5, "负增长": -2.5, "净利下滑": -2.5,
    "营收下滑": -2.5, "毛利下滑": -2.5, "商誉减值": -3.0, "减值": -2.0, "坏账": -2.5,
    "减持": -2.0, "套现": -2.0, "质押": -1.5, "平仓": -2.5, "爆仓": -3.5,
    "违约": -3.0, "爆雷": -4.0, "退市": -4.0, "立案": -3.0, "调查": -2.0,
    "处罚": -2.5, "罚款": -2.0, "诉讼": -1.5, "仲裁": -1.5, "反垄断": -2.0,
    "利空": -2.5, "下调评级": -2.0, "下调目标价": -2.0, "降级": -2.0, "卖出评级": -2.0,
    "裁员": -2.0, "停产": -2.5, "停工": -2.0, "召回": -2.0, "断供": -2.0,
    "库存高企": -2.0, "去库存": -1.0, "产能过剩": -2.0, "需求疲弱": -2.5,
    "看空": -2.5, "做空": -2.0, "资金流出": -1.8, "加息": -2.0, "收紧": -1.5,
    "停牌": -1.0, "警示": -1.5, "风险提示": -1.5, "业绩暴雷": -4.0,
}

EN_POSITIVE = {
    "beat estimates": 3.0, "beats estimates": 3.0, "topped estimates": 3.0,
    "beat": 2.5, "beats": 2.5, "raises guidance": 3.0, "raised guidance": 3.0,
    "upgrade": 2.0, "upgraded": 2.0, "buy rating": 2.0, "outperform": 2.0,
    "record high": 3.0, "record profit": 3.0, "all-time high": 3.0,
    "surge": 3.0, "surges": 3.0, "soar": 3.0, "soars": 3.0, "jump": 2.0,
    "rally": 2.5, "rallies": 2.5, "bullish": 2.5, "upside": 2.0,
    "profit": 1.5, "growth": 1.5, "expansion": 1.5, "recovery": 1.5,
    "strong demand": 2.5, "beat expectations": 3.0, "wins contract": 2.0,
    "dividend": 1.0, "buyback": 1.5, "approval": 1.5, "approved": 1.5,
    "optimistic": 2.0, "momentum": 1.0, "raises outlook": 3.0, "upbeat": 2.0,
}

EN_NEGATIVE = {
    "miss estimates": -3.0, "misses estimates": -3.0, "missed estimates": -3.0,
    "miss": -2.5, "misses": -2.5, "missed": -2.5, "cuts guidance": -3.0,
    "cut guidance": -3.0, "lowers outlook": -3.0, "downgrade": -2.0,
    "downgraded": -2.0, "sell rating": -2.0, "underperform": -2.0,
    "plunge": -3.0, "plunges": -3.0, "slump": -2.5, "tumble": -2.5,
    "crash": -4.0, "selloff": -3.0, "sell-off": -3.0, "bearish": -2.5,
    "loss": -2.0, "losses": -2.0, "decline": -1.5, "declines": -1.5,
    "drop": -1.5, "drops": -1.5, "falls": -1.5, "weak demand": -2.5,
    "layoffs": -2.5, "bankruptcy": -4.0, "fraud": -4.0, "probe": -2.0,
    "lawsuit": -2.0, "recall": -2.0, "profit warning": -3.0, "warning": -2.0,
    "downside": -2.0, "default": -3.0, "delisting": -4.0, "investigation": -2.0,
    "halts": -1.5, "halted": -1.5, "writedown": -2.5, "write-down": -2.5,
}

# 否定词（命中后极性翻转）
CN_NEGATIONS = ("不", "未", "无", "没有", "并非", "尚未", "未能", "不会",
                "难以", "取消", "终止", "暂停")
EN_NEGATIONS = ("not ", "no ", "never ", "without ", "failed to ", "isn't ",
                "doesn't ", "didn't ", "won't ", "cannot ")

# 程度副词（加权）
CN_INTENSIFIERS = ("大幅", "显著", "强劲", "急剧", "严重", "巨幅", "超", "暴",
                   "极", "大幅", "明显", "远超")
EN_INTENSIFIERS = ("sharply", "significantly", "substantially", "record",
                   "steeply", "heavily", "deeply", "massively", "far")

POS_LABELS = ("positive", "neutral", "negative")

# LABEL_x 的默认约定：SST-2 系（0=负面 1=正面）；中文 tone 模型在下面单独覆盖
DEFAULT_LABEL_MAP = {"LABEL_0": "negative", "LABEL_1": "positive",
                     "LABEL_2": "neutral"}
# 各模型自己的 id2label（以模型卡为准）
LABEL_MAPS: dict[str, dict[str, str]] = {
    "yiyanghkust/finbert-tone-chinese": {
        "LABEL_0": "neutral", "LABEL_1": "positive", "LABEL_2": "negative",
        "0": "neutral", "1": "positive", "2": "negative",
        "中性": "neutral", "正面": "positive", "负面": "negative",
    },
    "ProsusAI/finbert": {"positive": "positive", "negative": "negative",
                         "neutral": "neutral"},
    "HuangYiYang/finbert-financial-news-sentiment": {
        "positive": "positive", "negative": "negative", "neutral": "neutral"},
    "ahmedrachid/FinancialBERT-Sentiment-Analysis": {
        "positive": "positive", "negative": "negative", "neutral": "neutral"},
    "Roeyyyyyy/quantmind-sentiment-dapt-flame": {
        "看涨": "positive", "看跌": "negative", "中性": "neutral",
        "positive": "positive", "negative": "negative", "neutral": "neutral"},
}

# 按长度排一次，保证「业绩预增」优先于「增长」
_TERMS: list[tuple[str, float, str]] = sorted(
    [(w, v, "zh") for w, v in CN_POSITIVE.items()]
    + [(w, v, "zh") for w, v in CN_NEGATIVE.items()]
    + [(w.lower(), v, "en") for w, v in EN_POSITIVE.items()]
    + [(w.lower(), v, "en") for w, v in EN_NEGATIVE.items()],
    key=lambda x: len(x[0]), reverse=True)


def canonical_label(label: Any, model: str = "") -> str:
    """把任意模型的标签收敛到 positive / neutral / negative。"""
    text = str(label).strip()
    table = LABEL_MAPS.get(model) or {}
    if text in table:
        return table[text]
    low = text.lower()
    if low in table:
        return table[low]
    if low in ("positive", "pos", "bullish", "上涨", "看涨", "正面", "利好",
               "label_2" if model == "yiyanghkust/finbert-tone-chinese" else ""):
        return "positive"
    if low in ("negative", "neg", "bearish", "下跌", "看跌", "负面", "利空"):
        return "negative"
    if low in ("neutral", "neu", "中性", "持平"):
        return "neutral"
    if "涨" in low or "bull" in low or "pos" in low:
        return "positive"
    if "跌" in low or "bear" in low or "neg" in low:
        return "negative"
    if "中" in low or "neu" in low:
        return "neutral"
    return DEFAULT_LABEL_MAP.get(text, DEFAULT_LABEL_MAP.get(low, "neutral"))


def lexicon_score(text: str) -> dict:
    """中英双语金融情感词典打分（零依赖、零网络）。

    返回：label / score(-1~1) / confidence / pos_hits / neg_hits / engine
    """
    raw = str(text or "")
    low = raw.lower()
    pos_hits: list[str] = []
    neg_hits: list[str] = []
    total = 0.0

    for term, weight, lang in _TERMS:
        start = 0
        hay = low if lang == "en" else raw
        needle = term if lang == "en" else term
        while True:
            idx = hay.find(needle, start)
            if idx < 0:
                break
            start = idx + len(needle)
            # 否定检测：向前看一小段
            window = hay[max(0, idx - (24 if lang == "en" else 6)):idx]
            value = weight
            negated = False
            for neg in (EN_NEGATIONS if lang == "en" else CN_NEGATIONS):
                if window.endswith(neg) or neg in window:
                    negated = True
                    break
            if negated:
                value = -value * 0.8
            # 程度副词
            for inten in (EN_INTENSIFIERS if lang == "en" else CN_INTENSIFIERS):
                if inten in window:
                    value *= 1.5
                    break
            total += value
            (pos_hits if value > 0 else neg_hits).append(
                f"{term}{'（否定）' if negated else ''}")
            if len(pos_hits) + len(neg_hits) >= 24:  # 防止刷词攻击
                break
        if len(pos_hits) + len(neg_hits) >= 24:
            break

    score = total / (abs(total) + 4.0) if total else 0.0
    score = max(-1.0, min(1.0, round(score, 4)))
    if score >= 0.18:
        label = "positive"
    elif score <= -0.18:
        label = "negative"
    else:
        label = "neutral"
    return {
        "label": label,
        "score": score,
        "confidence": round(min(1.0, abs(total) / 6.0), 3),
        "pos_hits": pos_hits[:8],
        "neg_hits": neg_hits[:8],
        "engine": "lexicon",
    }


# ================================================================ 情绪引擎（HF 模型）


def hf_classify(texts: Sequence[str], *, model: str = DEFAULT_SENTIMENT_MODEL,
                token: str | None = None, timeout: float | None = None,
                wait_for_model: bool = True) -> list[dict]:
    """调用 HF Inference Providers 的 task API 做文本分类（真 FinBERT）。

    端点：POST https://router.huggingface.co/hf-inference/models/<model>
    载荷：{"inputs": [...], "options": {"wait_for_model": true}}
    返回：[[{"label": "positive", "score": 0.97}, ...], ...]
    """
    items = [str(t) for t in texts if str(t).strip()]
    if not items:
        return []
    if not (token or hf_token()):
        raise HFError("auth", "未配置 HF Token（HF_TOKEN / HUGGINGFACE_API_KEY），"
                              "无法调用 Hugging Face 模型推理")
    url = f"{task_api_base()}/{model.lstrip('/')}"
    payload = {"inputs": items,
               "options": {"wait_for_model": bool(wait_for_model),
                           "use_cache": True}}
    raw = _request_json("POST", url, payload=payload,
                        headers={"X-Wait-For-Model": "true" if wait_for_model else "false"},
                        timeout=timeout or 60.0)
    if isinstance(raw, dict) and raw.get("error"):
        err = raw["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise HFError("http", f"HF 推理返回错误：{message}", url=url,
                      body=json.dumps(raw, ensure_ascii=False)[:500])

    # 批处理返回 list[list[dict]]；单条返回 list[dict]
    batches: list[Any]
    if isinstance(raw, list) and raw and isinstance(raw[0], list):
        batches = list(raw)
    else:
        batches = [raw]

    results: list[dict] = []
    for idx, batch in enumerate(batches):
        probs = {"positive": 0.0, "neutral": 0.0, "negative": 0.0}
        top_label, top_score = "neutral", 0.0
        if isinstance(batch, list):
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                label = canonical_label(entry.get("label", ""), model)
                try:
                    value = float(entry.get("score") or 0.0)
                except (TypeError, ValueError):
                    value = 0.0
                probs[label] = probs.get(label, 0.0) + value
                if value >= top_score:
                    top_label, top_score = label, value
        score = round(probs["positive"] - probs["negative"], 4)
        confidence = round(max(top_score, abs(score)), 3)
        results.append({
            "label": top_label,
            "score": score,
            "confidence": confidence,
            "probs": {k: round(v, 4) for k, v in probs.items()},
            "engine": f"hf:{model}",
            "text": items[idx] if idx < len(items) else "",
        })
    return results


def score_texts(texts: Sequence[str] | str, *, engine: str = "auto",
                model: str = DEFAULT_SENTIMENT_MODEL,
                timeout: float | None = None) -> dict:
    """给一批文本打情绪分，并给出聚合画像。

    engine:
      auto    —— 有 HF Token 走模型，任何失败静默降级到词典
      lexicon —— 只用本地词典（零网络，CI 可跑）
      hf      —— 只用 HF 模型（缺 Token / 报错直接抛 HFError）
    """
    if isinstance(texts, str):
        texts = [texts]
    items_src = [str(t).strip() for t in texts if str(t).strip()]
    if not items_src:
        return _empty_result(engine, model)

    engine = (engine or "auto").lower()
    used_model = model
    items: list[dict] = []
    used = "lexicon"
    fallback_note = ""

    if engine in ("auto", "hf"):
        if engine == "hf" or has_token():
            try:
                classified = hf_classify(items_src, model=model, timeout=timeout)
                if len(classified) == len(items_src):
                    for text, res in zip(items_src, classified):
                        items.append({
                            "text": text, "label": res["label"],
                            "score": res["score"],
                            "confidence": res["confidence"],
                            "engine": res["engine"],
                            "probs": res.get("probs", {}),
                            "pos_hits": [], "neg_hits": [],
                        })
                    used = "hf"
            except HFError as exc:
                if engine == "hf":
                    raise
                fallback_note = f"{exc.kind}: {exc}"
        elif engine == "hf":
            raise HFError("auth", "engine=hf 需要 HF_TOKEN（或 HUGGINGFACE_API_KEY）")

    if used == "lexicon":
        for text in items_src:
            res = lexicon_score(text)
            res["text"] = text
            res["probs"] = {}
            items.append(res)
        if engine == "auto" and not has_token():
            fallback_note = "未配置 HF_TOKEN，自动使用本地词典"

    return {
        "ok": True,
        "engine": used,
        "requested_engine": engine,
        "model": used_model if used == "hf" else "",
        "count": len(items),
        "items": items,
        "aggregate": _aggregate(items, engine=used, model=used_model),
        "note": fallback_note,
    }


def _empty_result(engine: str, model: str) -> dict:
    return {"ok": True, "engine": "lexicon", "requested_engine": engine,
            "model": "", "count": 0, "items": [],
            "aggregate": {"count": 0, "score": 0, "label": "中性",
                          "positive": 0, "neutral": 0, "negative": 0,
                          "top_positive": [], "top_negative": []},
            "note": "没有可分析的文本"}


def _aggregate(items: list[dict], *, engine: str = "lexicon",
               model: str = "") -> dict:
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    for it in items:
        counts[it.get("label", "neutral")] = counts.get(it.get("label", "neutral"), 0) + 1
    n = len(items) or 1
    # 加权得分：正 -1、负 +1 的净值，映射到 -100 ~ 100
    net = (counts["positive"] - counts["negative"]) / n
    score = int(round(net * 100))
    if score >= 15:
        label = "偏多"
    elif score <= -15:
        label = "偏空"
    else:
        label = "中性"
    ranked = sorted(items, key=lambda x: x.get("score", 0.0), reverse=True)
    return {
        "count": len(items),
        "score": score,
        "label": label,
        "positive": counts["positive"],
        "neutral": counts["neutral"],
        "negative": counts["negative"],
        "avg_score": round(sum(i.get("score", 0.0) for i in items) / n, 4),
        "engine": engine,
        "model": model,
        "top_positive": [{"text": i.get("text", "")[:80], "score": i.get("score", 0)}
                         for i in ranked[:3] if i.get("score", 0) > 0],
        "top_negative": [{"text": i.get("text", "")[:80], "score": i.get("score", 0)}
                         for i in reversed(ranked[-3:]) if i.get("score", 0) < 0],
    }


def market_sentiment(items: Iterable[Any], *, text_keys: Sequence[str] = (),
                     engine: str = "auto",
                     model: str = DEFAULT_SENTIMENT_MODEL) -> dict:
    """把任意结构的新闻/快讯条目（dict 或 str）转成情绪画像。"""
    keys = tuple(text_keys) or ("title", "text", "content", "summary",
                                "description", "news", "headline")
    texts: list[str] = []
    for item in items:
        if isinstance(item, str):
            texts.append(item)
            continue
        if isinstance(item, dict):
            picked = ""
            for key in keys:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    picked = value.strip()
                    break
            if picked:
                texts.append(picked)
    return score_texts(texts, engine=engine, model=model)


def render_sentiment_md(result: dict, title: str = "🤗 Hugging Face 情绪引擎") -> str:
    """渲染成仓库既有推送风格（无表格，纯列表，PushPlus/企微可直接发）。"""
    agg = result.get("aggregate") or {}
    lines = [
        f"## {title}",
        "",
        f"**情绪判定：{agg.get('label', '中性')} · {agg.get('score', 0):+d}**"
        f"（样本 {agg.get('count', 0)} 条 · 引擎 "
        f"{'Hugging Face 模型 ' + result.get('model', '') if result.get('engine') == 'hf' else '本地金融词典'}）",
        "",
        f"- 🟢 正面：{agg.get('positive', 0)} 条",
        f"- ⚪ 中性：{agg.get('neutral', 0)} 条",
        f"- 🔴 负面：{agg.get('negative', 0)} 条",
        f"- 📊 平均强度：{agg.get('avg_score', 0):+.2f}",
    ]
    if result.get("note"):
        lines.append(f"- ⚠️ 备注：{result['note']}")
    top_pos = agg.get("top_positive") or []
    top_neg = agg.get("top_negative") or []
    if top_pos:
        lines += ["", "**最强正面信号**"]
        lines += [f"- {t['text']}（{t['score']:+.2f}）" for t in top_pos]
    if top_neg:
        lines += ["", "**最强负面信号**"]
        lines += [f"- {t['text']}（{t['score']:+.2f}）" for t in top_neg]
    return "\n".join(lines)


def render_sentiment_html(result: dict) -> str:
    """给大屏用的轻量 HTML 片段（不引外部资源）。"""
    agg = result.get("aggregate") or {}
    score = int(agg.get("score", 0))
    color = "#5cff5c" if score > 0 else ("#ff5c5c" if score < 0 else "#9aa0b5")
    pos = agg.get("positive", 0)
    neu = agg.get("neutral", 0)
    neg = agg.get("negative", 0)
    total = max(1, pos + neu + neg)
    engine_txt = ("Hugging Face 模型 · " + str(result.get("model") or "")
                  if result.get("engine") == "hf" else "本地金融词典（离线）")

    def bar(value: int, color_: str) -> str:
        pct = value * 100.0 / total
        return (f'<div style="flex:{max(1, value)};background:{color_};'
                f'height:10px;" title="{value} 条 ({pct:.0f}%)"></div>')

    rows = "".join(
        f'<div style="display:flex;gap:8px;align-items:baseline;'
        f'padding:4px 0;border-bottom:1px dashed rgba(255,255,255,.08);'
        f'font-size:13px;color:#d7dbe8;">'
        f'<span style="color:{"#5cff5c" if it.get("score", 0) > 0 else ("#ff5c5c" if it.get("score", 0) < 0 else "#9aa0b5")};">'
        f'{it.get("score", 0):+.2f}</span>'
        f'<span style="flex:1;">{str(it.get("text", ""))[:120]}</span></div>'
        for it in (result.get("items") or [])[:8])
    return (
        f'<div style="font-family:inherit;">'
        f'<div style="display:flex;align-items:baseline;gap:10px;">'
        f'<span style="font-size:26px;font-weight:700;color:{color};">'
        f'{score:+d}</span>'
        f'<span style="font-size:14px;color:{color};">{agg.get("label", "中性")}</span>'
        f'<span style="font-size:12px;color:#8b93a7;">{agg.get("count", 0)} 条 · {engine_txt}</span>'
        f'</div>'
        f'<div style="display:flex;gap:2px;margin:8px 0 10px;">'
        f'{bar(pos, "#5cff5c")}{bar(neu, "#6b7280")}{bar(neg, "#ff5c5c")}</div>'
        f'{rows}</div>')


# ================================================================ 用 HF 数据集给词典打分

TEXT_FIELD_HINTS = ("text", "sentence", "sentences", "title", "headline",
                    "news", "content", "body", "message", "tweet", "abstract")
LABEL_FIELD_HINTS = ("label", "labels", "sentiment", "sentiment_label",
                     "gold", "target", "tone", "class", "category")
GROUP_FIELD_HINTS = ("language", "lang", "source", "ticker", "symbol", "market")


def pick_field(features: Sequence[str], hints: Sequence[str]) -> str:
    lowered = [str(f).lower() for f in features]
    for hint in hints:
        if hint in lowered:
            return str(features[lowered.index(hint)])
    for idx, name in enumerate(lowered):
        for hint in hints:
            if hint in name:
                return str(features[idx])
    return ""


def benchmark(dataset: str, *, text_field: str = "", label_field: str = "",
              group_field: str = "", config: str | None = None,
              split: str | None = None, where: str = "", length: int = 100,
              engine: str = "lexicon", model: str = DEFAULT_SENTIMENT_MODEL,
              timeout: float | None = None) -> dict:
    """用 HF 上带标注的数据集当黄金标签，给情绪引擎打分（准确率 / macro-F1）。

    默认 length=100（datasets-server 单次上限），配 --where 可只测某个子集，
    例如 --where "language='zh'"。
    """
    length = max(1, min(100, int(length)))
    if where:
        payload = dataset_filter(dataset, where, config=config, split=split,
                                 length=length, timeout=timeout)
    else:
        payload = dataset_rows(dataset, config=config, split=split,
                               length=length, timeout=timeout)
    records = payload.get("records") or []
    if not records:
        raise HFError("not_found", f"数据集没有取到行：{dataset}"
                                   f"（where={where or '—'}）")
    features = payload.get("features") or list(records[0].keys())
    text_field = text_field or pick_field(features, TEXT_FIELD_HINTS)
    label_field = label_field or pick_field(features, LABEL_FIELD_HINTS)
    if not text_field or not label_field:
        raise HFError("format",
                      f"无法定位文本/标签字段（可用字段：{', '.join(map(str, features))}）；"
                      f"用 --text-field / --label-field 指定")

    gold_rows: list[tuple[str, str, str]] = []
    for rec in records:
        text = rec.get(text_field)
        label = rec.get(label_field)
        if not isinstance(text, str) or not text.strip() or label is None:
            continue
        gold = canonical_label(label, model if engine == "hf" else "")
        if gold not in POS_LABELS:
            continue
        group = str(rec.get(group_field) or "") if group_field else ""
        gold_rows.append((text.strip(), gold, group))

    if not gold_rows:
        raise HFError("format", "没有可用样本（标签无法识别为 positive/neutral/negative）")

    result = score_texts([t for t, _, _ in gold_rows], engine=engine, model=model)
    preds = [it["label"] for it in result["items"]]
    golds = [g for _, g, _ in gold_rows]
    return _classification_report(golds, preds, dataset=dataset,
                                  config=payload.get("config", ""),
                                  split=payload.get("split", ""),
                                  where=where, text_field=text_field,
                                  label_field=label_field,
                                  group_field=group_field,
                                  groups=[g for _, _, g in gold_rows],
                                  engine=result.get("engine", "lexicon"),
                                  model=result.get("model", ""),
                                  note=result.get("note", ""),
                                  total_rows=payload.get("num_rows_total"))


def _classification_report(golds: list[str], preds: list[str], **meta: Any) -> dict:
    labels = POS_LABELS
    confusion = {g: {p: 0 for p in labels} for g in labels}
    for gold, pred in zip(golds, preds):
        confusion[gold][pred] = confusion[gold].get(pred, 0) + 1
    per_class: dict[str, dict] = {}
    f1s: list[float] = []
    for label in labels:
        tp = confusion[label][label]
        support = sum(confusion[label].values())
        pred_count = sum(confusion[g][label] for g in labels)
        precision = tp / pred_count if pred_count else 0.0
        recall = tp / support if support else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if (precision + recall) else 0.0)
        per_class[label] = {"precision": round(precision, 4),
                            "recall": round(recall, 4),
                            "f1": round(f1, 4),
                            "support": support}
        if support:
            f1s.append(f1)
    correct = sum(confusion[l][l] for l in labels)
    by_group: dict[str, dict] = {}
    groups = meta.get("groups") or []
    if groups and any(groups):
        buckets: dict[str, list[int]] = {}
        for idx, group in enumerate(groups):
            key = group or "—"
            bucket = buckets.setdefault(key, [0, 0])
            bucket[1] += 1
            if preds[idx] == golds[idx]:
                bucket[0] += 1
        for key, (ok, total) in sorted(buckets.items(), key=lambda kv: -kv[1][1]):
            by_group[key] = {"accuracy": round(ok / total, 4), "count": total}
    return {
        "ok": True,
        "dataset": meta.get("dataset", ""),
        "config": meta.get("config", ""),
        "split": meta.get("split", ""),
        "where": meta.get("where", ""),
        "text_field": meta.get("text_field", ""),
        "label_field": meta.get("label_field", ""),
        "group_field": meta.get("group_field", ""),
        "engine": meta.get("engine", "lexicon"),
        "model": meta.get("model", ""),
        "note": meta.get("note", ""),
        "samples": len(golds),
        "total_rows": meta.get("total_rows"),
        "accuracy": round(correct / len(golds), 4) if golds else 0.0,
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "per_class": per_class,
        "confusion": confusion,
        "by_group": by_group,
    }


def render_benchmark_md(report: dict) -> str:
    lines = [
        f"## 🧪 情绪引擎基线 · {report.get('dataset', '')}",
        "",
        f"- 引擎：{report.get('engine', '')}"
        f"{' (' + report['model'] + ')' if report.get('model') else ''}",
        f"- 切片：config={report.get('config') or '—'} split={report.get('split') or '—'}"
        f"{' where=' + report['where'] if report.get('where') else ''}",
        f"- 字段：text={report.get('text_field')} label={report.get('label_field')}"
        f"{' group=' + report['group_field'] if report.get('group_field') else ''}",
        f"- 样本：{report.get('samples')} 条"
        f"（数据集总行数 {report.get('total_rows') or '—'}）",
        f"- **准确率 {report.get('accuracy', 0):.3f} · macro-F1 {report.get('macro_f1', 0):.3f}**",
    ]
    if report.get("note"):
        lines.append(f"- 备注：{report['note']}")
    lines += ["", "**分档表现**"]
    for label in POS_LABELS:
        stat = (report.get("per_class") or {}).get(label) or {}
        lines.append(
            f"- {label}：P {stat.get('precision', 0):.3f} / "
            f"R {stat.get('recall', 0):.3f} / F1 {stat.get('f1', 0):.3f}"
            f"（支持数 {stat.get('support', 0)}）")
    if report.get("by_group"):
        lines += ["", f"**分组准确率（按 {report.get('group_field')}）**"]
        for key, stat in list(report["by_group"].items())[:8]:
            lines.append(f"- {key}：{stat['accuracy']:.3f}（{stat['count']} 条）")
    return "\n".join(lines)


# ================================================================ 数据集 → 情绪画像


def digest(dataset: str, *, text_field: str = "", group_field: str = "",
           config: str | None = None, split: str | None = None,
           where: str = "", length: int = 100, engine: str = "auto",
           model: str = DEFAULT_SENTIMENT_MODEL) -> dict:
    """抓一个 HF 新闻/快讯数据集 → 直接出情绪画像（可 render_sentiment_md）。"""
    length = max(1, min(100, int(length)))
    if where:
        payload = dataset_filter(dataset, where, config=config, split=split, length=length)
    else:
        payload = dataset_rows(dataset, config=config, split=split, length=length)
    records = payload.get("records") or []
    if not records:
        raise HFError("not_found", f"数据集没有取到行：{dataset}")
    features = payload.get("features") or list(records[0].keys())
    text_field = text_field or pick_field(features, TEXT_FIELD_HINTS)
    group_field = group_field or pick_field(features, GROUP_FIELD_HINTS)
    if not text_field:
        raise HFError("format", f"无法定位文本字段（可用：{', '.join(map(str, features))}）")
    texts, groups = [], []
    for rec in records:
        value = rec.get(text_field)
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
            groups.append(str(rec.get(group_field) or "") if group_field else "")
    result = market_sentiment(texts, engine=engine, model=model)
    result["dataset"] = dataset
    result["config"] = payload.get("config", "")
    result["split"] = payload.get("split", "")
    result["text_field"] = text_field
    result["group_field"] = group_field
    result["total_rows"] = payload.get("num_rows_total")
    if group_field and any(groups):
        buckets: dict[str, list[dict]] = {}
        for item, group in zip(result.get("items", []), groups):
            buckets.setdefault(group or "—", []).append(item)
        result["by_group"] = {
            key: _aggregate(vals, engine=result.get("engine", "lexicon"),
                            model=result.get("model", ""))
            for key, vals in sorted(buckets.items(), key=lambda kv: -len(kv[1]))}
    return result


def render_digest_md(result: dict) -> str:
    head = render_sentiment_md(result, title="🤗 Hugging Face 数据集情绪画像")
    lines = [head, "",
             f"- 数据集：`{result.get('dataset', '')}`"
             f"（config={result.get('config') or '—'} split={result.get('split') or '—'}）",
             f"- 文本字段：{result.get('text_field') or '—'}"
             f" · 数据集总行数 {result.get('total_rows') or '—'}"]
    groups = result.get("by_group") or {}
    if groups:
        lines += ["", f"**按 {result.get('group_field')} 分组**"]
        for key, agg in list(groups.items())[:10]:
            lines.append(
                f"- {key}：{agg.get('label')} {agg.get('score', 0):+d}"
                f"（{agg.get('count', 0)} 条 · 正 {agg.get('positive', 0)} / "
                f"中 {agg.get('neutral', 0)} / 负 {agg.get('negative', 0)}）")
    return "\n".join(lines)


# ================================================================ 配置摘要（给 App）

def describe_config() -> dict:
    token = hf_token()
    return {
        "ok": True,
        "module": "hf_hub",
        "version": VERSION,
        "token_present": bool(token),
        "token_masked": mask_key(token),
        "hub_base": hub_base(),
        "datasets_server": datasets_server_base(),
        "task_api": task_api_base(),
        "default_model": DEFAULT_SENTIMENT_MODEL,
        "default_model_zh": DEFAULT_SENTIMENT_MODEL_ZH,
        "catalog_count": len(HF_CATALOG),
        "engines": ["auto", "lexicon", "hf"],
    }


# ================================================================ 离线 Mock（HF 三协议）

MOCK_MODELS = [
    {"_id": "1", "id": "ProsusAI/finbert", "author": "ProsusAI",
     "pipeline_tag": "text-classification", "library_name": "transformers",
     "downloads": 4144813, "likes": 1281, "lastModified": "2026-10-12T09:04:17.000Z",
     "tags": ["finance", "text-classification", "license:cc-by-nc-sa-4.0"],
     "inference": "warm", "siblings": [{"rfilename": "config.json"}]},
    {"_id": "2", "id": "yiyanghkust/finbert-tone-chinese", "author": "yiyanghkust",
     "pipeline_tag": "text-classification", "library_name": "transformers",
     "downloads": 34555, "likes": 64, "lastModified": "2024-02-06T11:46:52.000Z",
     "tags": ["zh", "license:apache-2.0"], "inference": "warm",
     "siblings": [{"rfilename": "config.json"}]},
    {"_id": "3", "id": "amazon/chronos-2", "author": "amazon",
     "pipeline_tag": "time-series-forecasting", "library_name": "chronos-forecasting",
     "downloads": 15276730, "likes": 546, "lastModified": "2026-06-05T11:55:42.000Z",
     "tags": ["license:apache-2.0"], "siblings": [{"rfilename": "config.json"}]},
]

MOCK_DATASETS = [
    {"_id": "1", "id": "Kenpache/multilingual-financial-sentiment",
     "author": "Kenpache", "downloads": 12345, "likes": 88,
     "lastModified": "2026-05-01T00:00:00.000Z", "tags": ["finance", "multilingual"],
     "siblings": [{"rfilename": "data.csv"}]},
    {"_id": "2", "id": "zeroshot/twitter-financial-news-sentiment",
     "author": "zeroshot", "downloads": 98765, "likes": 432,
     "lastModified": "2026-03-03T00:00:00.000Z", "tags": ["finance"],
     "siblings": [{"rfilename": "train.csv"}]},
]

MOCK_SPACES = [
    {"_id": "1", "id": "towardsinnovationlab/stock_market_analysis_and_forecast",
     "author": "towardsinnovationlab", "likes": 12, "sdk": "gradio",
     "lastModified": "2026-04-04T00:00:00.000Z", "runtime": {"stage": "RUNNING"},
     "tags": ["finance"]},
]

_MOCK_CORPUS: list[tuple[str, str, str]] = [
    ("公司三季度净利润同比增长 42%，大幅超市场预期，毛利率显著提升", "positive", "zh"),
    ("公司发布业绩预告，预计上半年巨亏，商誉减值风险高企", "negative", "zh"),
    ("公司将于下周四披露三季度业绩，具体时间另行通知", "neutral", "zh"),
    ("券商上调目标价至 120 港元，维持买入评级，看好云业务复苏", "positive", "zh"),
    ("控股股东拟减持不超过 2% 股份，套现约 3 亿元", "negative", "zh"),
    ("央行宣布降准 0.5 个百分点，释放长期资金约 1 万亿元", "positive", "zh"),
    ("公司因涉嫌信息披露违法被证监会立案调查", "negative", "zh"),
    ("Board approves new share buyback program and raises dividend", "positive", "en"),
    ("Quarterly results miss estimates; company cuts full-year guidance", "negative", "en"),
    ("The company will host its annual general meeting next month", "neutral", "en"),
    ("Revenue surges 38% year over year on strong demand, beats estimates", "positive", "en"),
    ("Supplier halts shipments after factory fire, output to decline", "negative", "en"),
]


def _mock_records(offset: int, length: int, *, query: str = "",
                  where: str = "") -> list[dict]:
    corpus = list(_MOCK_CORPUS)
    if query:
        key = query.lower()
        corpus = [c for c in corpus if key in c[0].lower()] or list(_MOCK_CORPUS)
    if where and "positive" in where:
        corpus = [c for c in corpus if c[1] == "positive"] or corpus
    if where and "negative" in where:
        corpus = [c for c in corpus if c[1] == "negative"] or corpus
    total = 240
    out: list[dict] = []
    for i in range(offset, min(offset + length, total)):
        text, label, lang = corpus[i % len(corpus)]
        out.append({"row_idx": i, "row": {"text": text, "label": label,
                                          "language": lang,
                                          "source": "mock-wire"}})
    return out


def _mock_features() -> list[dict]:
    return [{"name": "text", "type": {"dtype": "string", "_type": "Value"}},
            {"name": "label", "type": {"dtype": "string", "_type": "Value"}},
            {"name": "language", "type": {"dtype": "string", "_type": "Value"}},
            {"name": "source", "type": {"dtype": "string", "_type": "Value"}}]


class _MockHandler(BaseHTTPRequestHandler):
    """一次 Mock 住 Hub API + datasets-server + Inference task API。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        return

    # —— 工具
    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    # —— 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = self._query()

        if path == "/api/models":
            hits = [m for m in MOCK_MODELS
                    if not qs.get("search")
                    or qs["search"].lower() in m["id"].lower()]
            return self._send(200, hits[:int(qs.get("limit") or 10)])
        if path == "/api/datasets":
            hits = [d for d in MOCK_DATASETS
                    if not qs.get("search")
                    or qs["search"].lower() in d["id"].lower()]
            return self._send(200, hits[:int(qs.get("limit") or 10)])
        if path == "/api/spaces":
            return self._send(200, MOCK_SPACES[:int(qs.get("limit") or 10)])
        if path.startswith("/api/models/") or path.startswith("/api/datasets/"):
            repo_id = path.split("/", 3)[-1]
            table = MOCK_MODELS if path.startswith("/api/models/") else MOCK_DATASETS
            for item in table:
                if item["id"].lower() == repo_id.lower():
                    return self._send(200, item)
            if "missing" in repo_id.lower():
                return self._send(404, {"error": "not found"})
            return self._send(200, {"id": repo_id, "author": repo_id.split("/")[0],
                                    "downloads": 100, "likes": 1,
                                    "lastModified": "2026-01-01T00:00:00.000Z",
                                    "tags": ["license:mit"],
                                    "siblings": [{"rfilename": "README.md"}]})
        if path.startswith("/api/spaces/"):
            return self._send(200, dict(MOCK_SPACES[0],
                                        id=path.split("/", 3)[-1]))

        # —— datasets-server
        if path == "/datasets-server/is-valid":
            return self._send(200, {"preview": True, "viewer": True,
                                    "search": True, "filter": True,
                                    "statistics": True})
        if path == "/datasets-server/splits":
            return self._send(200, {
                "splits": [{"config": "default", "split": "train"},
                           {"config": "default", "split": "test"}],
                "pending": [], "failed": []})
        if path == "/datasets-server/rows" or path == "/datasets-server/first-rows":
            offset = int(qs.get("offset") or 0)
            length = int(qs.get("length") or 20)
            return self._send(200, {
                "features": _mock_features(),
                "rows": _mock_records(offset, length),
                "num_rows_total": 240, "partial": False})
        if path == "/datasets-server/search":
            return self._send(200, {
                "features": _mock_features(),
                "rows": _mock_records(int(qs.get("offset") or 0),
                                      int(qs.get("length") or 10),
                                      query=qs.get("query") or ""),
                "num_rows_total": 42, "partial": False})
        if path == "/datasets-server/filter":
            return self._send(200, {
                "features": _mock_features(),
                "rows": _mock_records(int(qs.get("offset") or 0),
                                      int(qs.get("length") or 10),
                                      where=qs.get("where") or ""),
                "num_rows_total": 80, "partial": False})
        if path == "/datasets-server/parquet":
            return self._send(200, {"parquet_files": [
                {"dataset": qs.get("dataset") or "", "config": "default",
                 "split": "train", "filename": "0000.parquet",
                 "url": "https://huggingface.co/mock/0000.parquet",
                 "size": 123456}], "pending": [], "failed": []})
        if path == "/datasets-server/size":
            return self._send(200, {"size": {
                "dataset": {"num_rows": 240, "num_bytes_original_files": 654321},
                "configs": [], "splits": []}})
        return self._send(404, {"error": f"mock: no route {path}"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if parsed.path.startswith("/task/models/"):
            if not (self.headers.get("Authorization") or "").startswith("Bearer "):
                return self._send(401, {"error": "Authorization header required"})
            inputs = payload.get("inputs") or []
            if isinstance(inputs, str):
                inputs = [inputs]

            def classify(text: str) -> list[dict]:
                low = text.lower()
                if any(k in low for k in ("超预期", "增长", "上调", "回购", "降准",
                                          "surges", "beats", "buyback", "raises")):
                    top = "positive"
                elif any(k in low for k in ("巨亏", "立案", "减持", "miss", "cuts",
                                            "halts", "亏损", "暴跌")):
                    top = "negative"
                else:
                    top = "neutral"
                base = {"positive": 0.08, "neutral": 0.12, "negative": 0.08}
                base[top] = 0.82
                return [{"label": k, "score": v} for k, v in base.items()]

            out = [classify(str(t)) for t in inputs]
            return self._send(200, out if len(out) != 1 or not isinstance(
                (payload.get("inputs") or ""), str) else out[0])
        return self._send(404, {"error": f"mock: no route {parsed.path}"})


class MockServerHandle:
    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        try:
            self._server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass


def start_mock_server(host: str = "127.0.0.1", port: int = 0) -> MockServerHandle:
    server = ThreadingHTTPServer((host, port), _MockHandler)
    thread = threading.Thread(target=server.serve_forever,
                              name="hf-mock-server", daemon=True)
    thread.start()
    return MockServerHandle(server, thread)


# ================================================================ 自检 / CLI


class _EnvSandbox:
    """进入时覆盖环境变量，退出时精确还原（自检 / 测试用）。"""

    def __init__(self, **env: str | None) -> None:
        self._env = env
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "_EnvSandbox":
        for name, value in self._env.items():
            self._saved[name] = os.environ.get(name)
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return self

    def __exit__(self, *exc: Any) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _clear_hf_env() -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in
            (*HF_TOKEN_ENVS, "HF_HUB_BASE", "HF_DATASETS_SERVER", "HF_TASK_API")}


def _offline_env() -> dict[str, str | None]:
    env = _clear_hf_env()
    for name in env:
        env[name] = None
    return env


def selftest(verbose: bool = True) -> int:
    """纯离线自检：词典 / 目录 / 解析 / Mock 三协议 / 降级。返回失败项数。"""
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: Any = "") -> None:
        checks.append((name, bool(ok), str(detail)[:160]))

    print(f"🧪 hf_hub 离线自检（{VERSION}）")
    print("=" * 64)

    print("① 精选目录与配置")
    with _EnvSandbox(**_offline_env()):
        cats = {item["cat"] for item in HF_CATALOG}
        check("目录覆盖 6 个分类", len(cats) == 6, f"{len(cats)} 类")
        check("每条都有 id/kind/why", all(
            i.get("id") and i.get("kind") in KINDS and i.get("why")
            for i in HF_CATALOG))
        check("id 无重复", len({i["id"] for i in HF_CATALOG}) == len(HF_CATALOG))
        check("分类筛选可用",
              all(i["cat"] == "sentiment-model"
                  for i in catalog("sentiment-model")))
        cfg = describe_config()
        check("默认端点走 router.huggingface.co",
              "router.huggingface.co" in cfg["task_api"], cfg["task_api"])
        check("无 Token 时 token_present=False", cfg["token_present"] is False)

    print("② 本地词典（零网络）")
    cases = [
        ("公司三季度净利润同比增长 42%，大幅超市场预期", "positive"),
        ("公司发布业绩预告，预计上半年巨亏，商誉减值", "negative"),
        ("公司将于下周四披露三季报", "neutral"),
        ("Revenue surges 38% year over year, beats estimates", "positive"),
        ("Quarterly results miss estimates; cuts full-year guidance", "negative"),
        ("The company will host its annual meeting next month", "neutral"),
    ]
    for text, expect in cases:
        got = lexicon_score(text)["label"]
        check(f"词典判定 {expect}：{text[:18]}…", got == expect, got)
    negated = lexicon_score("公司三季度净利润未出现增长，低于市场预期")
    check("否定与利空叠加判负", negated["label"] == "negative", negated["score"])
    check("空文本判中性", lexicon_score("")["label"] == "neutral")
    check("中文 tone 模型 LABEL_1=正面",
          canonical_label("LABEL_1", "yiyanghkust/finbert-tone-chinese") == "positive")
    check("SST-2 默认 LABEL_1=正面", canonical_label("LABEL_1") == "positive")
    check("中文标签 看涨→positive", canonical_label("看涨") == "positive")

    print("③ 聚合与渲染")
    result = score_texts([t for t, _, _ in _MOCK_CORPUS], engine="lexicon")
    check("批量打分条数一致", result["count"] == len(_MOCK_CORPUS), result["count"])
    check("聚合含三档计数",
          sum(result["aggregate"][k] for k in POS_LABELS) == result["count"])
    md = render_sentiment_md(result)
    check("Markdown 含情绪判定", "情绪判定" in md and "最强正面信号" in md)
    html = render_sentiment_html(result)
    check("HTML 片段非空", "<div" in html and "</div>" in html)
    check("market_sentiment 吃 dict 列表",
          market_sentiment([{"title": "净利润大增超预期"}])["aggregate"]["label"] == "偏多")

    print("④ Hub / datasets-server / Task API（Mock 三协议）")
    server = start_mock_server()
    try:
        root = server.root
        env = dict(_offline_env(), HF_HUB_BASE=root,
                   HF_DATASETS_SERVER=f"{root}/datasets-server",
                   HF_TASK_API=f"{root}/task/models")
        with _EnvSandbox(**env):
            models = search_models("finbert", limit=5)
            check("搜索模型返回结果", any(m["id"] == "ProsusAI/finbert" for m in models))
            check("模型归一化字段", models and models[0]["downloads"] > 0
                  and models[0]["url"].startswith("https://huggingface.co"),
                  models[0]["url"] if models else "")
            datasets = search_datasets("sentiment", limit=5)
            check("搜索数据集返回结果", len(datasets) >= 1)
            spaces = search_spaces(limit=3)
            check("搜索 Spaces 返回结果", len(spaces) >= 1)
            info = repo_info("ProsusAI/finbert", "model")
            check("模型详情含 files", "config.json" in (info.get("files") or []))
            check("实时统计可拉取", catalog("sentiment-model", with_stats=True)[0]
                  .get("downloads", 0) > 0)

            valid = dataset_is_valid("Kenpache/multilingual-financial-sentiment")
            check("is-valid 返回 viewer=True", valid.get("viewer") is True)
            splits = dataset_splits("Kenpache/multilingual-financial-sentiment")
            check("splits 解析出 config/split",
                  splits and splits[0]["config"] == "default", splits[:1])
            rows = dataset_rows("Kenpache/multilingual-financial-sentiment", length=5)
            check("取行返回 5 条", len(rows["records"]) == 5, len(rows["records"]))
            check("自动补全 config/split",
                  rows["config"] == "default" and rows["split"] == "train",
                  f"{rows['config']}/{rows['split']}")
            found = dataset_search("Kenpache/multilingual-financial-sentiment",
                                   "净利润", length=3)
            check("服务端检索有结果", len(found["records"]) == 3)
            filtered = dataset_filter("Kenpache/multilingual-financial-sentiment",
                                      "label='positive'", length=4)
            check("服务端过滤有结果", len(filtered["records"]) == 4)
            check("size 端点可用",
                  dataset_size("Kenpache/multilingual-financial-sentiment")["rows"] == 240)
            check("parquet 端点可用",
                  len(dataset_parquet("Kenpache/multilingual-financial-sentiment")
                      ["files"]) == 1)

            report = benchmark("Kenpache/multilingual-financial-sentiment",
                               length=12, engine="lexicon",
                               group_field="language")
            check("基线样本数 > 0", report["samples"] == 12, report["samples"])
            check("基线准确率 ≥ 0.6", report["accuracy"] >= 0.6,
                  f"{report['accuracy']:.3f}")
            check("基线含分组", "by_group" in report and report["by_group"])
            check("基线渲染含 macro-F1", "macro-F1" in render_benchmark_md(report))

            dig = digest("NickyNicky/finance-financialmodelingprep-stock-news-sentiments-rss-feed",
                         length=12, engine="lexicon", group_field="language")
            check("digest 出画像", dig["aggregate"]["count"] == 12)
            check("digest 渲染含数据集名",
                  "NickyNicky" in render_digest_md(dig))

            # 无 Token → hf 引擎必须报 auth
            try:
                score_texts(["业绩大增"], engine="hf")
                check("无 Token 时 hf 引擎报 auth", False, "没有抛错")
            except HFError as exc:
                check("无 Token 时 hf 引擎报 auth", exc.kind == "auth", exc.kind)

        # 有 Token（Mock）→ 走真模型链路，并验证中文 tone 标签映射
        with _EnvSandbox(**dict(env, HF_TOKEN="hf_selftest_1234567890")):
            got = score_texts(["净利润大幅增长超预期", "公司将于下月召开董事会"],
                              engine="hf")
            check("HF 引擎生效", got["engine"] == "hf", got["engine"])
            check("HF 引擎分数 ±1 内",
                  all(-1 <= i["score"] <= 1 for i in got["items"]))
            check("auto 引擎优先走 HF",
                  score_texts(["业绩暴雷"], engine="auto")["engine"] == "hf")

        # Token 断掉 → auto 静默降级到词典
        with _EnvSandbox(**dict(env, HF_TOKEN="hf_selftest_1234567890",
                                HF_TASK_API="http://127.0.0.1:1/task/models")):
            fell = score_texts(["净利润增长超预期"], engine="auto")
            check("模型不可用时降级到词典", fell["engine"] == "lexicon", fell["engine"])
            check("降级带说明", bool(fell.get("note")), fell.get("note"))

    finally:
        server.close()

    print("⑤ 错误分类与 Key 安全")
    with _EnvSandbox(**dict(_offline_env(), HF_TOKEN="hf_secret_token_123456")):
        check("401 映射为 auth",
              _error_from_http(401, "", "https://huggingface.co/x").kind == "auth")
        check("404 映射为 not_found",
              _error_from_http(404, "", "https://huggingface.co/x").kind == "not_found")
        check("429 映射为 rate_limit",
              _error_from_http(429, "", "https://huggingface.co/x").kind == "rate_limit")
        check("402 映射为 payment（额度用尽，不是 Token 无效）",
              _error_from_http(402, '{"error":"You have no remaining credits"}',
                               "https://router.huggingface.co/x").kind == "payment")
        check("冷启动提示", "冷启动" in _error_from_http(
            503, '{"error":"Model is currently loading"}', "").args[0])
        check("Key 不发往非 HF 域名", _token_allowed("https://evil.example.com") is False)
        check("Key 发往 huggingface.co", _token_allowed("https://huggingface.co/api") is True)
        check("Key 可发往本机 Mock", _token_allowed("http://127.0.0.1:18090/x") is True)
        check("mask_key 脱敏", mask_key("hf_abcdefgh1234") == "hf_a…1234")

    failed = [name for name, ok, _ in checks if not ok]
    if verbose:
        print("-" * 64)
        for name, ok, detail in checks:
            print(f"{'✅' if ok else '❌'} {name}" + (f"  ← {detail}" if not ok and detail else ""))
        print("-" * 64)
    print(f"{'✅ 全部通过' if not failed else '❌ 有失败项'}："
          f"{len(checks) - len(failed)}/{len(checks)}")
    return len(failed)


def _check_online() -> int:
    """联网自检：Hub / datasets-server /（可选）推理端点。"""
    print(f"🔌 hf_hub 连通性自检（{VERSION}）")
    print(f"   Hub            {hub_base()}")
    print(f"   datasets-server{datasets_server_base()}")
    print(f"   task API       {task_api_base()}")
    print(f"   Token          {mask_key(hf_token()) or '未配置'}")
    print("-" * 64)
    failed = 0

    def probe(name: str, fn) -> None:
        nonlocal failed
        started = time.time()
        try:
            value = fn()
            print(f"✅ {name} · {int((time.time() - started) * 1000)}ms · {value}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            kind = getattr(exc, "kind", exc.__class__.__name__)
            print(f"❌ {name} · {kind} · {exc}")

    probe("Hub 检索（models）",
          lambda: f"{len(search_models('financial sentiment', limit=3))} 条")
    probe("Hub 详情（ProsusAI/finbert）",
          lambda: f"下载 {repo_info('ProsusAI/finbert', 'model')['downloads']}")
    probe("datasets-server 校验",
          lambda: f"viewer={dataset_is_valid('Kenpache/multilingual-financial-sentiment').get('viewer')}")
    probe("datasets-server 取行",
          lambda: f"{len(dataset_rows('Kenpache/multilingual-financial-sentiment', length=3)['records'])} 行")
    if has_token():
        probe("HF 模型推理（FinBERT）",
              lambda: str(hf_classify(["profit surges, beats estimates"])[0]["label"]))
    else:
        print("⏭ HF 模型推理：未配置 Token，跳过（本地词典可离线用）")
    print("-" * 64)
    print("✅ 全部连通" if not failed else f"❌ {failed} 项失败")
    return failed


def _print_repos(items: list[dict]) -> None:
    if not items:
        print("（没有结果）")
        return
    for item in items:
        extra = ""
        if item["kind"] == "model":
            extra = f" · {item.get('pipeline_tag') or '-'}"
        elif item["kind"] == "space":
            extra = f" · {item.get('sdk') or '-'}"
        print(f"- [{item['kind']}] {item['id']}{extra}")
        print(f"    ⬇ {item.get('downloads', 0):,}  ♥ {item.get('likes', 0)}"
              f"  · 更新 {item.get('updated', '')[:10] or '-'}"
              f"  · {item.get('license') or 'license 未声明'}")
        if item.get("why"):
            print(f"    💡 {item['why']}")


def _print_catalog(category: str, live: bool) -> None:
    groups: dict[str, list[dict]] = {}
    for item in catalog(category, with_stats=live):
        groups.setdefault(item["cat"], []).append(item)
    for cat, items in groups.items():
        print(f"\n{CATALOG_CATEGORIES.get(cat, cat)}  （{len(items)} 项）")
        print("-" * 64)
        for item in items:
            stats = ""
            if item.get("live"):
                stats = f"  [⬇ {item.get('downloads', 0):,} ♥ {item.get('likes', 0)}]"
            elif live:
                stats = "  [实时统计不可用]"
            print(f"- {item['id']}  [{item['lang']}]{stats}")
            print(f"    {item['label']}")
            print(f"    {item['why']}")
            if item.get("use"):
                print(f"    ▶ python hf_hub.py {item['use']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hf_hub.py",
        description="Hugging Face Hub 接入层：检索 / 免下载取数 / 金融情绪引擎 / 基线评估",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python hf_hub.py --catalog
  python hf_hub.py --search "financial sentiment" --kind dataset --limit 8
  python hf_hub.py --rows Kenpache/multilingual-financial-sentiment --length 5
  python hf_hub.py --sentiment "净利润同比增长 42%，超市场预期" --json
  python hf_hub.py --benchmark Kenpache/multilingual-financial-sentiment --group-field language
  python hf_hub.py --selftest
""")
    p.add_argument("--catalog", action="store_true", help="打印精选金融 HF 资源目录")
    p.add_argument("--category", default="", help="只打印某个分类（配合 --catalog）")
    p.add_argument("--live", action="store_true",
                   help="目录额外实时拉取下载量/点赞（需联网）")
    p.add_argument("--search", metavar="Q", help="搜索 Hub（配合 --kind）")
    p.add_argument("--kind", default="model", choices=list(KINDS),
                   help="搜索类型：model / dataset / space（默认 model）")
    p.add_argument("--author", default="", help="按作者/组织过滤")
    p.add_argument("--tag", default="", help="按标签过滤（模型为 pipeline_tag）")
    p.add_argument("--sort", default="downloads",
                   choices=["downloads", "likes", "lastModified", "trendingScore"],
                   help="排序字段（默认 downloads）")
    p.add_argument("--limit", type=int, default=10, help="返回条数（默认 10）")
    p.add_argument("--info", metavar="REPO", help="查看仓库详情，如 ProsusAI/finbert")
    p.add_argument("--rows", metavar="DATASET",
                   help="取数据集切片行（datasets-server /rows）")
    p.add_argument("--splits", metavar="DATASET", help="列出数据集的 config/split")
    p.add_argument("--size", metavar="DATASET", help="数据集行数与体积")
    p.add_argument("--parquet", metavar="DATASET", help="列出 parquet 直链")
    p.add_argument("--ds-search", metavar="DATASET",
                   help="数据集内全文检索（配合 -q）")
    p.add_argument("--ds-filter", metavar="DATASET",
                   help="数据集内 SQL 过滤（配合 --where）")
    p.add_argument("-q", "--query", default="", help="检索关键词（--ds-search）")
    p.add_argument("--where", default="", help="过滤条件（--ds-filter / --benchmark）")
    p.add_argument("--config", dest="ds_config", default="", help="数据集 config 名")
    p.add_argument("--split", default="", help="数据集 split 名")
    p.add_argument("--offset", type=int, default=0, help="起始偏移")
    p.add_argument("--length", type=int, default=20, help="取行数（上限 100）")
    p.add_argument("--sentiment", metavar="TEXT", help="给一段文本打情绪分")
    p.add_argument("--sentiment-file", metavar="PATH",
                   help="给文件里每一行打情绪分")
    p.add_argument("--engine", default="auto", choices=["auto", "lexicon", "hf"],
                   help="情绪引擎：auto（有 Token 走模型）/ lexicon（离线）/ hf（强制模型）")
    p.add_argument("--model", default=DEFAULT_SENTIMENT_MODEL,
                   help=f"HF 分类模型（默认 {DEFAULT_SENTIMENT_MODEL}）")
    p.add_argument("--benchmark", metavar="DATASET",
                   help="用该数据集当黄金标签评估情绪引擎")
    p.add_argument("--text-field", default="", help="文本字段名（自动探测失败时用）")
    p.add_argument("--label-field", default="", help="标签字段名")
    p.add_argument("--group-field", default="", help="分组字段名（如 language / ticker）")
    p.add_argument("--digest", metavar="DATASET",
                   help="抓数据集 → 出情绪画像 Markdown")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.add_argument("--check", action="store_true", help="联网连通性自检")
    p.add_argument("--mock-server", metavar="PORT", type=int,
                   help="启动离线 Mock（Hub + datasets-server + task API）")
    p.add_argument("--selftest", action="store_true", help="纯离线自检后退出")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.selftest:
        return selftest()

    if args.mock_server:
        server = start_mock_server("0.0.0.0" if args.mock_server != 0 else "127.0.0.1",
                                   args.mock_server)
        print(f"🧪 HF Mock 服务已启动：{server.root}")
        print(f"   export HF_HUB_BASE={server.root}")
        print(f"   export HF_DATASETS_SERVER={server.root}/datasets-server")
        print(f"   export HF_TASK_API={server.root}/task/models")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.close()
        return 0

    if args.check:
        return _check_online()

    try:
        if args.catalog:
            if args.json:
                print(json.dumps(catalog(args.category, with_stats=args.live),
                                 ensure_ascii=False, indent=2))
            else:
                _print_catalog(args.category, args.live)
            return 0

        if args.search:
            items = search(args.kind, args.search, author=args.author, tag=args.tag,
                           sort=args.sort, limit=args.limit)
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=2))
            else:
                _print_repos(items)
            return 0

        if args.info:
            kind = args.kind if args.kind in KINDS else "model"
            info = repo_info(args.info, kind)
            print(json.dumps(info, ensure_ascii=False, indent=2)
                  if args.json else _format_info(info))
            return 0

        if args.splits:
            splits = dataset_splits(args.splits)
            print(json.dumps(splits, ensure_ascii=False, indent=2) if args.json
                  else "\n".join(f"- {s['config']} / {s['split']}" for s in splits))
            return 0

        if args.rows:
            payload = dataset_rows(args.rows, config=args.ds_config or None,
                                   split=args.split or None, offset=args.offset,
                                   length=args.length)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                _print_records(payload)
            return 0

        if args.ds_search:
            payload = dataset_search(args.ds_search, args.query,
                                     config=args.ds_config or None,
                                     split=args.split or None,
                                     offset=args.offset, length=args.length)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                _print_records(payload)
            return 0

        if args.ds_filter:
            payload = dataset_filter(args.ds_filter, args.where,
                                     config=args.ds_config or None,
                                     split=args.split or None,
                                     offset=args.offset, length=args.length)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                _print_records(payload)
            return 0

        if args.size:
            print(json.dumps(dataset_size(args.size), ensure_ascii=False, indent=2))
            return 0

        if args.parquet:
            print(json.dumps(dataset_parquet(args.parquet), ensure_ascii=False,
                             indent=2))
            return 0

        if args.benchmark:
            report = benchmark(args.benchmark, text_field=args.text_field,
                               label_field=args.label_field,
                               group_field=args.group_field,
                               config=args.ds_config or None,
                               split=args.split or None, where=args.where,
                               length=args.length, engine=args.engine,
                               model=args.model)
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json
                  else render_benchmark_md(report))
            return 0

        if args.digest:
            result = digest(args.digest, text_field=args.text_field,
                            group_field=args.group_field,
                            config=args.ds_config or None, split=args.split or None,
                            where=args.where, length=args.length,
                            engine=args.engine, model=args.model)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
                  else render_digest_md(result))
            return 0

        if args.sentiment_file:
            with open(args.sentiment_file, "r", encoding="utf-8") as fh:
                texts = [line.strip() for line in fh if line.strip()]
            result = score_texts(texts, engine=args.engine, model=args.model)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
                  else render_sentiment_md(result))
            return 0

        if args.sentiment:
            result = score_texts([args.sentiment], engine=args.engine,
                                 model=args.model)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                item = (result["items"] or [{}])[0]
                print(f"文本：{args.sentiment}")
                print(f"判定：{item.get('label')} "
                      f"（{item.get('score', 0):+.2f}，置信度 {item.get('confidence', 0):.2f}）")
                print(f"引擎：{result['engine']}"
                      f"{' · ' + result['model'] if result.get('model') else ''}")
                if item.get("pos_hits"):
                    print(f"正面词：{'、'.join(item['pos_hits'])}")
                if item.get("neg_hits"):
                    print(f"负面词：{'、'.join(item['neg_hits'])}")
                if result.get("note"):
                    print(f"备注：{result['note']}")
            return 0

    except HFError as exc:
        print(f"❌ [{exc.kind}] {exc}", file=sys.stderr)
        if exc.url:
            print(f"   {exc.url}", file=sys.stderr)
        return 2

    build_parser().print_help()
    return 1


def _format_info(info: dict) -> str:
    lines = [f"{info['id']}  [{info['kind']}]",
             f"  标签：{info.get('pipeline_tag') or info.get('sdk') or '-'}",
             f"  下载：{info.get('downloads', 0):,}  点赞：{info.get('likes', 0)}",
             f"  更新：{info.get('updated', '')[:19] or '-'}"
             f"  许可：{info.get('license') or '未声明'}",
             f"  链接：{info.get('url', '')}"]
    if info.get("warm"):
        lines.append("  推理：warm（可直接走 Inference Providers）")
    if info.get("files"):
        lines.append(f"  文件：{', '.join(info['files'][:12])}")
    return "\n".join(lines)


def _print_records(payload: dict) -> None:
    records = payload.get("records") or []
    print(f"数据集：{payload.get('dataset')} "
          f"(config={payload.get('config')} split={payload.get('split')})"
          f"  共 {payload.get('num_rows_total') or len(records)} 行")
    for idx, rec in enumerate(records, 1):
        print(f"\n[{idx}] " + " | ".join(
            f"{k}: {str(v)[:200]}" for k, v in list(rec.items())[:6]))


if __name__ == "__main__":
    sys.exit(main())
