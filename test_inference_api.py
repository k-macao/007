#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线单元测试：统一推理 API（inference_api.py）与 App 接入

覆盖：
- Provider 预设 / 环境变量解析 / 本地端点免 Key / Key 脱敏
- 非流式对话、SSE 流式、/models、health
- 429/5xx 自动重试、401 映射、网络失败分类
- OpenAI 兼容网关（含 Token 鉴权）
- 大屏 /api/inference/* 接口（配置、健康、对话）

全部使用本地 Mock 端点，不访问外网，可在 CI 离线执行。
"""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request

import inference_api as ia

ENV_KEYS = list(ia.INFERENCE_ENV_NAMES) + [
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "INFERENCE_GATEWAY_TOKEN",
]


class EnvMixin:
    """每个测试前清空 AI 相关环境变量，结束后精确恢复。"""

    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in ENV_KEYS}
        for name in ENV_KEYS:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def use_mock(base_url: str, key: str = "sk-test-12345678",
                 model: str = "mock-inference-1", **extra: str) -> None:
        os.environ["INFERENCE_BASE_URL"] = base_url
        os.environ["INFERENCE_API_KEY"] = key
        os.environ["INFERENCE_MODEL"] = model
        for name, value in extra.items():
            os.environ[name] = value


# ================================================================ 配置


class ConfigTests(EnvMixin, unittest.TestCase):
    def test_presets_complete(self):
        self.assertGreaterEqual(len(ia.PRESETS), 8)
        for preset in ia.PRESETS.values():
            self.assertTrue(preset.label)
            self.assertTrue(preset.model)
            self.assertIn(preset.name, ia.PRESETS)

    def test_unconfigured(self):
        cfg = ia.resolve_config()
        self.assertFalse(cfg.configured)
        with self.assertRaises(ia.InferenceError) as ctx:
            ia.InferenceClient(cfg).chat("hi")
        self.assertEqual(ctx.exception.kind, "config")

    def test_explicit_env(self):
        self.use_mock("https://example.com/v1", key="sk-abcdefgh12345678",
                      model="demo-model")
        cfg = ia.resolve_config()
        self.assertTrue(cfg.configured)
        self.assertTrue(cfg.explicit)
        self.assertEqual(cfg.endpoint(), "https://example.com/v1/chat/completions")
        payload = json.dumps(cfg.describe(), ensure_ascii=False)
        self.assertNotIn("sk-abcdefgh12345678", payload)
        self.assertIn("…", cfg.describe()["key_masked"])

    def test_legacy_deepseek_key(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-deepseek-test"
        cfg = ia.resolve_config()
        self.assertEqual(cfg.provider, "deepseek")
        self.assertEqual(cfg.base_url, "https://api.deepseek.com/v1")
        self.assertTrue(cfg.configured)

    def test_legacy_openai_key_respects_base_url(self):
        os.environ["OPENAI_API_KEY"] = "sk-openai-test"
        os.environ["OPENAI_BASE_URL"] = "https://proxy.example.com/v1"
        cfg = ia.resolve_config()
        self.assertEqual(cfg.provider, "openai")
        self.assertEqual(cfg.base_url, "https://proxy.example.com/v1")

    def test_local_provider_keyless(self):
        os.environ["INFERENCE_PROVIDER"] = "ollama"
        cfg = ia.resolve_config()
        self.assertTrue(cfg.configured)
        self.assertTrue(cfg.keyless)
        self.assertFalse(cfg.requires_key)

    def test_endpoint_normalization(self):
        cfg = ia.resolve_config(base_url="https://x.test/v1/",
                                api_key="sk-123", model="m")
        self.assertEqual(cfg.endpoint(), "https://x.test/v1/chat/completions")
        cfg2 = ia.resolve_config(base_url="https://x.test/v1/chat/completions",
                                 api_key="sk-123", model="m")
        self.assertEqual(cfg2.endpoint(), "https://x.test/v1/chat/completions")

    def test_unknown_provider_raises(self):
        os.environ["INFERENCE_PROVIDER"] = "not-a-provider"
        with self.assertRaises(ia.InferenceError):
            ia.resolve_config()

    def test_require_explicit(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-deepseek-test"
        self.assertIsNone(ia.explicit_config())
        os.environ["INFERENCE_MODEL"] = "deepseek-chat"
        self.assertIsNotNone(ia.explicit_config())

    def test_mask_key(self):
        self.assertEqual(ia.mask_key(""), "")
        self.assertEqual(ia.mask_key("sk-1"), "****")
        masked = ia.mask_key("abcdefgh12345678")
        self.assertNotIn("abcdefgh", masked)
        self.assertTrue(masked.startswith("abcd"))


# ================================================================ 客户端


class ClientTests(EnvMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ia.MockServerHandle().start()
        cls.env = {
            "INFERENCE_BASE_URL": cls.server.base_url,
            "INFERENCE_API_KEY": "sk-mock-12345678",
            "INFERENCE_MODEL": "mock-inference-1",
            "INFERENCE_TIMEOUT": "10",
            "INFERENCE_MAX_RETRIES": "2",
        }

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def client(self, **overrides):
        for name, value in self.env.items():
            os.environ[name] = value
        for name, value in overrides.items():
            os.environ[name] = value
        return ia.InferenceClient(ia.replace(ia.resolve_config(), retry_backoff=0.01))

    def test_chat_result(self):
        result = self.client().chat_result("做一份摘要")
        self.assertTrue(result["ok"])
        self.assertIn("MOCK", result["content"])
        self.assertTrue(result["usage"].get("total_tokens"))
        self.assertEqual(result["model"], "mock-inference-1")

    def test_chat_stream(self):
        chunks = list(self.client().chat_stream("量化因子解读"))
        self.assertGreaterEqual(len(chunks), 2)
        joined = "".join(chunks)
        self.assertIn("MOCK", joined)
        self.assertGreater(len(joined), 20)

    def test_models_and_health(self):
        client = self.client()
        models = client.list_models()
        self.assertIn("mock-inference-1", models)
        report = client.health()
        self.assertTrue(report["ok"], report.get("error"))
        self.assertEqual(report["mode"], "models")

    def test_retry_on_server_error(self):
        server = ia.MockServerHandle(fail_plan=[500, 503]).start()
        try:
            client = self.client(INFERENCE_BASE_URL=server.base_url)
            result = client.chat_result("重试一下")
            self.assertTrue(result["ok"])
            self.assertIn("MOCK", result["content"])
        finally:
            server.stop()

    def test_auth_error(self):
        client = self.client(INFERENCE_API_KEY="sk-bad-key-1234")
        with self.assertRaises(ia.InferenceError) as ctx:
            client.chat("hello")
        self.assertEqual(ctx.exception.kind, "auth")
        self.assertEqual(ctx.exception.status, 401)

    def test_network_error(self):
        client = self.client(INFERENCE_BASE_URL="http://127.0.0.1:1/v1",
                             INFERENCE_MAX_RETRIES="0")
        with self.assertRaises(ia.InferenceError) as ctx:
            client.chat("hello")
        self.assertIn(ctx.exception.kind, ("network", "timeout"))
        self.assertFalse(client.health()["ok"])

    def test_invalid_messages(self):
        client = self.client()
        with self.assertRaises(ia.InferenceError):
            client.chat([])
        with self.assertRaises(ia.InferenceError):
            client.chat([{"role": "bad", "content": "x"}])

    def test_health_unconfigured(self):
        report = ia.InferenceClient(ia.resolve_config()).health()
        self.assertFalse(report["ok"])
        self.assertEqual(report["kind"], "config")


# ================================================================ 网关


class GatewayTests(EnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.upstream = ia.MockServerHandle().start()
        self.gateway = ia.start_gateway("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.gateway.serve_forever,
                                       name="gateway-test", daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.gateway.server_address[1]}"
        self.use_mock(self.upstream.base_url)

    def tearDown(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.thread.join(timeout=2)
        self.upstream.stop()
        super().tearDown()

    def post(self, payload, token: str = ""):
        data = json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.base}/v1/chat/completions", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_proxy_chat(self):
        status, data = self.post({"messages": [{"role": "user",
                                                "content": "网关摘要"}]})
        self.assertEqual(status, 200)
        content = data["choices"][0]["message"]["content"]
        self.assertIn("MOCK", content)

    def test_gateway_health(self):
        with urllib.request.urlopen(f"{self.base}/healthz", timeout=10) as response:
            data = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertTrue(data["upstream"]["configured"])
        self.assertNotIn("sk-mock-12345678", json.dumps(data))

    def test_token_enforcement(self):
        os.environ["INFERENCE_GATEWAY_TOKEN"] = "gw-secret"
        status, _ = self.post({"messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 401)
        status, _ = self.post({"messages": [{"role": "user", "content": "x"}]},
                              token="gw-secret")
        self.assertEqual(status, 200)


# ================================================================ 既有 AI 链路接入


class PipelineIntegrationTests(EnvMixin, unittest.TestCase):
    """设置 INFERENCE_* 后，研报 / 技能 / 压缩链路应自动改用统一端点。"""

    def setUp(self):
        super().setUp()
        self.server = ia.MockServerHandle().start()
        self.use_mock(self.server.base_url, model="mock-inference-1")

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def test_pushplus_credentials_override(self):
        import pushplus_deepseek as pp
        url, model, key, label, keyless = pp.ai_credentials("deepseek")
        self.assertEqual(url, self.server.base_url + "/chat/completions")
        self.assertEqual(model, "mock-inference-1")
        self.assertTrue(key)
        self.assertIn("推理API", label)
        self.assertFalse(keyless)
        self.assertTrue(pp.ai_ready("deepseek"))
        self.assertEqual(pp.ai_auto_provider(), "deepseek")

    def test_stock_report_auto_provider(self):
        import stock_report as sr
        self.assertEqual(sr.resolve_ai_provider("auto"), "deepseek")
        self.assertEqual(sr.resolve_ai_provider(""), "deepseek")

    def test_report_uses_inference_endpoint(self):
        import stock_report as sr
        result = sr.run_report(
            "600519", channel="console", ai_provider="auto", dry_run=True,
            collect_news=False, no_chart=True, ai_compress=False)
        self.assertEqual(result["provider"], "deepseek")
        self.assertIn("推理API", result.get("gen_note") or "")
        self.assertTrue(result.get("report_md"))


# ================================================================ 大屏 App 接口


class DashboardApiTests(EnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = ia.MockServerHandle().start()
        self.use_mock(self.server.base_url)
        import server_dashboard as sd
        self.sd = sd

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def test_config_masked(self):
        payload = self.sd._api_inference_config()
        self.assertTrue(payload["configured"])
        self.assertTrue(payload["presets"])
        self.assertNotIn("sk-test-12345678", json.dumps(payload, ensure_ascii=False))

    def test_health(self):
        payload = self.sd._api_inference_health()
        self.assertTrue(payload["ok"], payload.get("error"))

    def test_chat(self):
        payload = self.sd._api_inference_chat(
            {"prompt": "把以下爬虫快讯做摘要：港股科技股走强。", "max_tokens": 200})
        self.assertTrue(payload["ok"], payload.get("error"))
        self.assertIn("MOCK", payload["content"])

    def test_chat_validation(self):
        self.assertFalse(self.sd._api_inference_chat({})["ok"])
        self.assertFalse(self.sd._api_inference_chat({"prompt": "x" * 7000})["ok"])

    def test_http_endpoints(self):
        httpd = self.sd.MonitorTCPServer(("127.0.0.1", 0), self.sd.MonitorHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(f"{base}/api/inference/config", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(json.loads(resp.read())["configured"])
            request = urllib.request.Request(
                f"{base}/api/inference/chat",
                data=json.dumps({"prompt": "HTTP 接口摘要测试"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=5) as resp:
                data = json.loads(resp.read())
            self.assertTrue(data["ok"], data.get("error"))
            self.assertIn("MOCK", data["content"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
