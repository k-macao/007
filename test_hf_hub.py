#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线单元测试：Hugging Face Hub 接入层（hf_hub.py）

覆盖：
- 精选目录完整性 / 配置摘要 / Key 脱敏
- 中英双语金融情感词典（含否定、程度副词、空输入）
- 标签归一化（FinBERT / 中文 tone / SST-2 / 中文标签）
- Hub 检索与详情、datasets-server 七大端点（Mock）
- HF 推理引擎（Mock task API）、无 Token 报错、auto 降级
- 用 HF 数据集做基线评估（准确率 / macro-F1 / 分组）
- CLI 主流程（--catalog / --search / --rows / --sentiment）

全部走本地 Mock，不访问外网，可在 CI 离线执行。
"""
import io
import json
import os
import unittest
from contextlib import redirect_stdout

import hf_hub as hh

ENV_KEYS = list(hh.HF_TOKEN_ENVS) + [
    "HF_HUB_BASE", "HF_DATASETS_SERVER", "HF_TASK_API",
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY",
]


class EnvMixin(unittest.TestCase):
    """每个测试前清空 HF 相关环境变量，结束后精确恢复。"""

    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in ENV_KEYS}
        for name in ENV_KEYS:
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def offline(**extra: str) -> dict:
        env = hh._offline_env()
        env.update(extra)
        return env


class MockMixin:
    """提供一个线程级 Mock 服务，并把三个端点指过去。"""

    server: hh.MockServerHandle

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = hh.start_mock_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.close()

    def mock_env(self, **extra: str) -> dict:
        root = self.server.root
        env = {
            "HF_HUB_BASE": root,
            "HF_DATASETS_SERVER": f"{root}/datasets-server",
            "HF_TASK_API": f"{root}/task/models",
        }
        env.update(extra)
        return env


# ================================================================ 目录与配置


class TestCatalog(EnvMixin):
    def test_catalog_integrity(self):
        items = hh.HF_CATALOG
        self.assertGreaterEqual(len(items), 15)
        for item in items:
            self.assertIn(item["kind"], hh.KINDS)
            self.assertIn("/", item["id"])
            self.assertTrue(item["label"])
            self.assertTrue(item["why"])
        self.assertEqual(len({i["id"] for i in items}), len(items))

    def test_catalog_categories_known(self):
        for item in hh.HF_CATALOG:
            self.assertIn(item["cat"], hh.CATALOG_CATEGORIES)

    def test_filter_by_category(self):
        models = hh.catalog("sentiment-model")
        self.assertTrue(models)
        self.assertTrue(all(i["cat"] == "sentiment-model" for i in models))
        self.assertEqual(hh.catalog("不存在的分类"), [])

    def test_describe_config_offline(self):
        cfg = hh.describe_config()
        self.assertFalse(cfg["token_present"])
        self.assertEqual(cfg["token_masked"], "")
        self.assertIn("router.huggingface.co", cfg["task_api"])
        self.assertEqual(hh.DEFAULT_SENTIMENT_MODEL, "ProsusAI/finbert")

    def test_token_pickup_order(self):
        with hh._EnvSandbox(HUGGINGFACE_API_KEY="hf_second_123456"):
            self.assertEqual(hh.hf_token(), "hf_second_123456")
            self.assertTrue(hh.has_token())
            self.assertTrue(hh.describe_config()["token_present"])
        with hh._EnvSandbox(HF_TOKEN="hf_first_123456",
                            HUGGINGFACE_API_KEY="hf_second_123456"):
            self.assertEqual(hh.hf_token(), "hf_first_123456")

    def test_mask_key(self):
        self.assertEqual(hh.mask_key(""), "")
        self.assertEqual(hh.mask_key("short"), "*" * 5)
        self.assertEqual(hh.mask_key("hf_abcdefgh1234"), "hf_a…1234")


# ================================================================ 本地词典


class TestLexicon(EnvMixin):
    def test_chinese_positive(self):
        hit = hh.lexicon_score("公司三季度净利润同比增长 42%，大幅超市场预期")
        self.assertEqual(hit["label"], "positive")
        self.assertGreater(hit["score"], 0)
        self.assertTrue(hit["pos_hits"])

    def test_chinese_negative(self):
        hit = hh.lexicon_score("公司上半年巨亏，商誉减值，控股股东拟减持")
        self.assertEqual(hit["label"], "negative")
        self.assertLess(hit["score"], 0)
        self.assertTrue(hit["neg_hits"])

    def test_chinese_neutral(self):
        self.assertEqual(hh.lexicon_score("公司将于下周四披露三季报")["label"],
                         "neutral")

    def test_english(self):
        self.assertEqual(
            hh.lexicon_score("Revenue surges 38%, beats estimates")["label"],
            "positive")
        self.assertEqual(
            hh.lexicon_score("Results miss estimates; cuts guidance")["label"],
            "negative")
        self.assertEqual(
            hh.lexicon_score("The company will host a meeting")["label"],
            "neutral")

    def test_negation_flips(self):
        plain = hh.lexicon_score("净利润增长")
        self.assertEqual(plain["label"], "positive")
        negated = hh.lexicon_score("净利润未出现增长，低于市场预期")
        self.assertEqual(negated["label"], "negative")

    def test_empty_and_plain(self):
        for text in ("", "   ", "今天天气不错"):
            self.assertEqual(hh.lexicon_score(text)["label"], "neutral")

    def test_longest_term_wins(self):
        # 「业绩预增」应整体命中，而不是被拆成「增长」
        hit = hh.lexicon_score("公司发布业绩预增公告")
        self.assertIn("业绩预增", hit["pos_hits"])

    def test_score_bounds(self):
        hit = hh.lexicon_score("暴涨 " * 300)
        self.assertGreaterEqual(hit["score"], -1.0)
        self.assertLessEqual(hit["score"], 1.0)


class TestLabelCanonicalization(EnvMixin):
    def test_finbert_labels(self):
        for label in ("positive", "negative", "neutral"):
            self.assertEqual(hh.canonical_label(label, "ProsusAI/finbert"), label)

    def test_chinese_tone_model(self):
        model = "yiyanghkust/finbert-tone-chinese"
        self.assertEqual(hh.canonical_label("LABEL_0", model), "neutral")
        self.assertEqual(hh.canonical_label("LABEL_1", model), "positive")
        self.assertEqual(hh.canonical_label("LABEL_2", model), "negative")

    def test_sst2_default(self):
        self.assertEqual(hh.canonical_label("LABEL_0"), "negative")
        self.assertEqual(hh.canonical_label("LABEL_1"), "positive")

    def test_chinese_words(self):
        self.assertEqual(hh.canonical_label("看涨"), "positive")
        self.assertEqual(hh.canonical_label("看跌"), "negative")
        self.assertEqual(hh.canonical_label("中性"), "neutral")

    def test_unknown_falls_back(self):
        self.assertIn(hh.canonical_label("weird-label-42"), hh.POS_LABELS)


# ================================================================ 聚合与渲染


class TestAggregate(EnvMixin):
    TEXTS = [
        "公司三季度净利润同比增长 42%，大幅超市场预期",
        "公司上半年巨亏，商誉减值，控股股东拟减持",
        "公司将于下周四披露三季报",
        "Revenue surges 38%, beats estimates",
        "Results miss estimates; cuts guidance",
    ]

    def test_batch_scoring(self):
        result = hh.score_texts(self.TEXTS, engine="lexicon")
        self.assertEqual(result["count"], 5)
        self.assertEqual(len(result["items"]), 5)
        agg = result["aggregate"]
        self.assertEqual(agg["positive"] + agg["neutral"] + agg["negative"], 5)
        self.assertIn(agg["label"], ("偏多", "中性", "偏空"))
        self.assertTrue(-100 <= agg["score"] <= 100)

    def test_empty_input(self):
        result = hh.score_texts([], engine="lexicon")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["aggregate"]["label"], "中性")

    def test_market_sentiment_dicts(self):
        result = hh.market_sentiment(
            [{"title": "净利润大增超预期", "source": "cls"},
             {"title": "公司被立案调查", "source": "cls"}],
            engine="lexicon")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["aggregate"]["positive"], 1)
        self.assertEqual(result["aggregate"]["negative"], 1)

    def test_market_sentiment_strings(self):
        result = hh.market_sentiment(["业绩暴雷，股价跌停"], engine="lexicon")
        self.assertEqual(result["aggregate"]["label"], "偏空")

    def test_render_markdown(self):
        md = hh.render_sentiment_md(hh.score_texts(self.TEXTS, engine="lexicon"))
        self.assertIn("情绪判定", md)
        self.assertIn("正面", md)
        self.assertIn("最强负面信号", md)

    def test_render_html(self):
        html = hh.render_sentiment_html(hh.score_texts(self.TEXTS, engine="lexicon"))
        self.assertTrue(html.startswith("<div"))
        self.assertIn("</div>", html)


# ================================================================ Hub / datasets-server（Mock）


class TestHubMock(MockMixin, EnvMixin):
    def test_search_models(self):
        with hh._EnvSandbox(**self.mock_env()):
            models = hh.search_models("finbert", limit=5)
        self.assertTrue(models)
        ids = [m["id"] for m in models]
        self.assertIn("ProsusAI/finbert", ids)
        first = next(m for m in models if m["id"] == "ProsusAI/finbert")
        self.assertEqual(first["kind"], "model")
        self.assertEqual(first["author"], "ProsusAI")
        self.assertGreater(first["downloads"], 0)
        self.assertEqual(first["license"], "cc-by-nc-sa-4.0")
        self.assertTrue(first["url"].startswith("https://huggingface.co/models/"))

    def test_search_datasets_and_spaces(self):
        with hh._EnvSandbox(**self.mock_env()):
            self.assertTrue(hh.search_datasets("sentiment"))
            spaces = hh.search_spaces(limit=3)
        self.assertTrue(spaces)
        self.assertEqual(spaces[0]["kind"], "space")
        self.assertEqual(spaces[0]["sdk"], "gradio")

    def test_repo_info(self):
        with hh._EnvSandbox(**self.mock_env()):
            info = hh.repo_info("ProsusAI/finbert", "model")
            self.assertIn("config.json", info["files"])
            self.assertTrue(info["warm"])
            data = hh.repo_info("Kenpache/multilingual-financial-sentiment",
                                "dataset")
        self.assertEqual(data["kind"], "dataset")

    def test_unknown_kind_raises(self):
        with self.assertRaises(hh.HFError) as ctx:
            hh.search("movie", "test")
        self.assertEqual(ctx.exception.kind, "config")

    def test_dataset_is_valid_and_splits(self):
        with hh._EnvSandbox(**self.mock_env()):
            self.assertTrue(hh.dataset_is_valid("Kenpache/x")["viewer"])
            splits = hh.dataset_splits("Kenpache/x")
        self.assertEqual(splits[0]["config"], "default")
        self.assertIn(splits[0]["split"], ("train", "test"))

    def test_dataset_rows_auto_split(self):
        with hh._EnvSandbox(**self.mock_env()):
            payload = hh.dataset_rows("Kenpache/x", length=5)
        self.assertEqual(len(payload["records"]), 5)
        self.assertEqual(payload["config"], "default")
        self.assertEqual(payload["split"], "train")
        self.assertEqual(payload["num_rows_total"], 240)
        self.assertIn("text", payload["features"])

    def test_dataset_rows_explicit_split(self):
        with hh._EnvSandbox(**self.mock_env()):
            payload = hh.dataset_rows("Kenpache/x", config="default", split="test",
                                      offset=3, length=2)
        self.assertEqual(payload["split"], "test")
        self.assertEqual(payload["offset"], 3)

    def test_dataset_search_and_filter(self):
        with hh._EnvSandbox(**self.mock_env()):
            found = hh.dataset_search("Kenpache/x", "净利润", length=3)
            self.assertEqual(len(found["records"]), 3)
            filtered = hh.dataset_filter("Kenpache/x", "label='positive'", length=4)
        self.assertEqual(len(filtered["records"]), 4)

    def test_dataset_size_and_parquet(self):
        with hh._EnvSandbox(**self.mock_env()):
            self.assertEqual(hh.dataset_size("Kenpache/x")["rows"], 240)
            files = hh.dataset_parquet("Kenpache/x")["files"]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["url"].endswith(".parquet"))


# ================================================================ 基线评估与画像


class TestBenchmark(MockMixin, EnvMixin):
    def test_benchmark_report(self):
        with hh._EnvSandbox(**self.mock_env()):
            report = hh.benchmark("Kenpache/multilingual-financial-sentiment",
                                  length=12, group_field="language",
                                  engine="lexicon")
        self.assertEqual(report["samples"], 12)
        self.assertGreaterEqual(report["accuracy"], 0.6)
        self.assertGreaterEqual(report["macro_f1"], 0.6)
        self.assertEqual(set(report["per_class"]), set(hh.POS_LABELS))
        self.assertIn("zh", report["by_group"])
        md = hh.render_benchmark_md(report)
        self.assertIn("macro-F1", md)
        self.assertIn("Kenpache", md)

    def test_benchmark_with_where(self):
        with hh._EnvSandbox(**self.mock_env()):
            report = hh.benchmark("Kenpache/x", where="label='positive'",
                                  length=8, engine="lexicon")
        self.assertGreater(report["samples"], 0)
        self.assertEqual(report["where"], "label='positive'")

    def test_digest(self):
        with hh._EnvSandbox(**self.mock_env()):
            result = hh.digest("NickyNicky/rss-feed", length=12,
                               group_field="language", engine="lexicon")
        self.assertEqual(result["aggregate"]["count"], 12)
        self.assertIn("zh", result["by_group"])
        md = hh.render_digest_md(result)
        self.assertIn("NickyNicky/rss-feed", md)

    def test_pick_field(self):
        features = ["idx", "sentence", "gold_label", "language"]
        self.assertEqual(hh.pick_field(features, hh.TEXT_FIELD_HINTS), "sentence")
        self.assertEqual(hh.pick_field(features, hh.LABEL_FIELD_HINTS),
                         "gold_label")


# ================================================================ HF 推理引擎


class TestHFEngine(MockMixin, EnvMixin):
    def test_requires_token(self):
        with hh._EnvSandbox(**self.mock_env()):
            with self.assertRaises(hh.HFError) as ctx:
                hh.score_texts(["业绩大增"], engine="hf")
        self.assertEqual(ctx.exception.kind, "auth")

    def test_hf_engine_with_mocked_token(self):
        with hh._EnvSandbox(**self.mock_env(HF_TOKEN="hf_test_1234567890")):
            result = hh.score_texts(
                ["净利润大幅增长超预期", "公司将于下月召开董事会"], engine="hf")
            self.assertEqual(result["engine"], "hf")
            self.assertEqual(result["model"], hh.DEFAULT_SENTIMENT_MODEL)
            self.assertEqual(result["aggregate"]["count"], 2)
            positive = next(i for i in result["items"] if "增长" in i["text"])
            self.assertEqual(positive["label"], "positive")
            self.assertGreater(positive["probs"]["positive"], 0.5)

    def test_auto_prefers_model(self):
        with hh._EnvSandbox(**self.mock_env(HF_TOKEN="hf_test_1234567890")):
            self.assertEqual(hh.score_texts(["业绩暴雷"], engine="auto")["engine"],
                             "hf")

    def test_auto_falls_back_without_token(self):
        with hh._EnvSandbox(**self.mock_env()):
            result = hh.score_texts(["净利润增长超预期"], engine="auto")
        self.assertEqual(result["engine"], "lexicon")
        self.assertIn("HF_TOKEN", result["note"])

    def test_auto_falls_back_when_model_down(self):
        env = self.mock_env(HF_TOKEN="hf_test_1234567890",
                            HF_TASK_API="http://127.0.0.1:1/task/models")
        with hh._EnvSandbox(**env):
            result = hh.score_texts(["净利润增长超预期"], engine="auto")
        self.assertEqual(result["engine"], "lexicon")
        self.assertTrue(result["note"])

    def test_hf_classify_rejects_missing_auth_header(self):
        with hh._EnvSandbox(**self.mock_env(HF_TOKEN="hf_test_1234567890")):
            labels = hh.hf_classify(["profit surges"])  # 带 Token → 200
            self.assertEqual(len(labels), 1)
        # 去掉 Token 后 mock 返回 401 → auth
        with hh._EnvSandbox(**self.mock_env()):
            with self.assertRaises(hh.HFError) as ctx:
                hh.hf_classify(["profit surges"])
        self.assertEqual(ctx.exception.kind, "auth")

    def test_hf_classify_empty_input(self):
        self.assertEqual(hh.hf_classify([]), [])


# ================================================================ 错误分类 / Key 安全


class TestErrors(EnvMixin):
    def test_http_status_mapping(self):
        self.assertEqual(hh._error_from_http(401, "", "u").kind, "auth")
        self.assertEqual(hh._error_from_http(403, "", "u").kind, "auth")
        self.assertEqual(hh._error_from_http(404, "", "u").kind, "not_found")
        self.assertEqual(hh._error_from_http(429, "", "u").kind, "rate_limit")
        err = hh._error_from_http(402, '{"error":"You have no remaining credits"}', "u")
        self.assertEqual(err.kind, "payment")
        self.assertIn("额度", str(err))
        self.assertEqual(hh._error_from_http(500, "", "u").kind, "http")
        self.assertEqual(hh._error_from_http(418, "", "u").kind, "http")

    def test_cold_start_hint(self):
        err = hh._error_from_http(503, '{"error":"Model is currently loading"}', "u")
        self.assertIn("冷启动", str(err))

    def test_error_to_dict(self):
        err = hh.HFError("auth", "no key", status=401, url="u")
        payload = err.to_dict()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["kind"], "auth")
        self.assertEqual(payload["status"], 401)

    def test_token_only_to_huggingface(self):
        self.assertTrue(hh._token_allowed("https://huggingface.co/api/models"))
        self.assertTrue(hh._token_allowed("https://router.huggingface.co/v1"))
        self.assertTrue(hh._token_allowed("https://datasets-server.huggingface.co/x"))
        self.assertTrue(hh._token_allowed("http://127.0.0.1:18090/mock"))
        self.assertFalse(hh._token_allowed("https://evil.example.com/api"))
        self.assertFalse(hh._token_allowed("https://huggingface.com.evil.net/api"))

    def test_network_error_classification(self):
        import urllib.error
        err = hh._network_error(urllib.error.URLError(TimeoutError("timeout")), "u")
        self.assertEqual(err.kind, "timeout")
        err = hh._network_error(urllib.error.URLError("refused"), "u")
        self.assertEqual(err.kind, "network")

    def test_is_local_url(self):
        self.assertTrue(hh.is_local_url("http://127.0.0.1:8080/x"))
        self.assertFalse(hh.is_local_url("https://huggingface.co/x"))


# ================================================================ CLI


class TestCLI(MockMixin, EnvMixin):
    def run_cli(self, argv, env_extra: dict | None = None) -> tuple[int, str]:
        env = self.mock_env(**(env_extra or {}))
        buf = io.StringIO()
        with hh._EnvSandbox(**env), redirect_stdout(buf):
            code = hh.main(argv)
        return code, buf.getvalue()

    def test_catalog_json(self):
        code, out = self.run_cli(["--catalog", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload)
        self.assertIn("id", payload[0])

    def test_search_text(self):
        code, out = self.run_cli(["--search", "finbert", "--kind", "model",
                                  "--limit", "2"])
        self.assertEqual(code, 0)
        self.assertIn("ProsusAI/finbert", out)

    def test_rows(self):
        code, out = self.run_cli(["--rows", "Kenpache/x", "--length", "2"])
        self.assertEqual(code, 0)
        self.assertIn("Kenpache/x", out)

    def test_sentiment_readable(self):
        code, out = self.run_cli(
            ["--sentiment", "公司三季度净利润同比增长 42%，大幅超市场预期"])
        self.assertEqual(code, 0)
        self.assertIn("positive", out)
        self.assertIn("引擎：", out)

    def test_benchmark_cli(self):
        code, out = self.run_cli(["--benchmark", "Kenpache/x", "--length", "12",
                                  "--group-field", "language"])
        self.assertEqual(code, 0)
        self.assertIn("macro-F1", out)

    def test_error_exit_code(self):
        code, _ = self.run_cli(["--sentiment", "测试文本", "--engine", "hf"])
        self.assertEqual(code, 2)

    def test_help_when_no_args(self):
        code, out = self.run_cli([])
        self.assertEqual(code, 1)
        self.assertIn("usage", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
