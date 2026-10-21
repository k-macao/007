#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inference_api.py — 统一推理 API 接入层（OpenAI 兼容）

一个文件完成「对接推理 API → 自检 → 网关 → 集成到 App」四件事：

1. 统一配置：任何 OpenAI 兼容端点（DeepSeek / OpenAI / 硅基流动 /
   Moonshot / 智谱 GLM / 阿里云百炼 DashScope / Ollama / vLLM /
   LM Studio / 自建网关）都只需要三个环境变量：

       INFERENCE_BASE_URL=https://api.siliconflow.cn/v1
       INFERENCE_API_KEY=sk-xxxx
       INFERENCE_MODEL=Qwen/Qwen2.5-7B-Instruct

   也可以只写 INFERENCE_PROVIDER=deepseek，用内置预设（见 --list-providers）。
   为兼容旧配置，未显式设置 INFERENCE_* 时会自动沿用 DEEPSEEK_API_KEY /
   OPENAI_API_KEY / OPENAI_BASE_URL。

2. 推理客户端：chat / chat_stream（SSE 流式）/ list_models / health，
   带超时、指数退避重试、标准错误分类（config/auth/rate_limit/http/
   network/timeout/format），绝不打印明文 Key。

3. 网关：`python inference_api.py --serve 8090` 启动 OpenAI 兼容网关，
   App / 手机 / 前端只连网关，Key 留在服务端；可用
   INFERENCE_GATEWAY_TOKEN 给网关加一道 Bearer 鉴权。

4. 离线联调：`python inference_api.py --mock-server 18080` 启动本地
   Mock 推理端点（支持流式与故障注入），无 Key 也能验证整条链路。

CLI 速查：
    python inference_api.py --list-providers
    python inference_api.py --check                 # 连通性自检（真实调用）
    python inference_api.py -p "用一句话解释港股通"
    python inference_api.py -p "写一句开盘提示" --stream
    python inference_api.py --serve 8090            # 给 App 用的网关
    python inference_api.py --mock-server 18080     # 离线 Mock 端点
    python inference_api.py --selftest              # 纯离线自检（CI 门禁）
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Iterator, Sequence

VERSION = "1.0.0"
USER_AGENT = f"octopus-inference/{VERSION}"

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 0.4
MAX_RETRY_AFTER = 10.0

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}

INFERENCE_ENV_NAMES = (
    "INFERENCE_PROVIDER",
    "INFERENCE_BASE_URL",
    "INFERENCE_API_KEY",
    "INFERENCE_MODEL",
    "INFERENCE_TIMEOUT",
    "INFERENCE_MAX_RETRIES",
)

# 这些提供方的专用 Key 只要出现，就视为「显式接入统一推理 API」。
# 老配置 DEEPSEEK_API_KEY / OPENAI_API_KEY 不在此列：保持原有兼容行为。
EXTRA_PROVIDER_KEYS = (
    "HF_TOKEN",
    "HUGGINGFACE_API_KEY",
    "HUGGINGFACEHUB_API_TOKEN",
    "SILICONFLOW_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "DASHSCOPE_API_KEY",
)


# ================================================================ 错误类型


class InferenceError(RuntimeError):
    """推理调用失败；kind 用于 App 端做分支处理。"""

    KINDS = ("config", "auth", "rate_limit", "http", "network", "timeout", "format")

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


# ================================================================ Provider 预设


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    base_url: str
    model: str
    key_env: str = ""
    requires_key: bool = True
    docs: str = ""
    # OpenAI 兼容（默认）或 Hugging Face 经典 task 协议（inputs / generated_text）
    api_style: str = "openai"
    # 同一提供方的备选 Key 环境变量（按顺序取第一个非空值）
    key_envs: tuple[str, ...] = ()


PRESETS: dict[str, Preset] = {
    "deepseek": Preset(
        "deepseek", "DeepSeek 深度求索", "https://api.deepseek.com/v1",
        "deepseek-chat", "DEEPSEEK_API_KEY",
        docs="https://platform.deepseek.com"),
    "openai": Preset(
        "openai", "OpenAI", "https://api.openai.com/v1",
        "gpt-4o-mini", "OPENAI_API_KEY",
        docs="https://platform.openai.com"),
    "siliconflow": Preset(
        "siliconflow", "硅基流动 SiliconFlow", "https://api.siliconflow.cn/v1",
        "Qwen/Qwen2.5-7B-Instruct", "SILICONFLOW_API_KEY",
        docs="https://siliconflow.cn"),
    "moonshot": Preset(
        "moonshot", "Moonshot Kimi", "https://api.moonshot.cn/v1",
        "moonshot-v1-8k", "MOONSHOT_API_KEY",
        docs="https://platform.moonshot.cn"),
    "zhipu": Preset(
        "zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4",
        "glm-4-flash", "ZHIPU_API_KEY",
        docs="https://open.bigmodel.cn"),
    "dashscope": Preset(
        "dashscope", "阿里云百炼 DashScope", "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus", "DASHSCOPE_API_KEY",
        docs="https://bailian.console.aliyun.com"),
    "ollama": Preset(
        "ollama", "本地 Ollama", "http://localhost:11434/v1",
        "qwen2.5:7b", "", False,
        docs="https://ollama.com"),
    "vllm": Preset(
        "vllm", "本地 vLLM / SGLang", "http://localhost:8000/v1",
        "local-model", "", False,
        docs="https://docs.vllm.ai"),
    "lmstudio": Preset(
        "lmstudio", "本地 LM Studio", "http://localhost:1234/v1",
        "local-model", "", False,
        docs="https://lmstudio.ai"),
    "huggingface": Preset(
        "huggingface", "Hugging Face Inference Providers",
        "https://router.huggingface.co/v1",
        "Qwen/Qwen2.5-7B-Instruct", "HF_TOKEN", True,
        docs="https://huggingface.co/docs/inference-providers",
        key_envs=("HUGGINGFACE_API_KEY", "HUGGINGFACEHUB_API_TOKEN")),
    "huggingface-endpoint": Preset(
        "huggingface-endpoint", "Hugging Face 专用 Inference Endpoint",
        "", "tgi", "HF_TOKEN", True,
        docs="https://huggingface.co/docs/inference-endpoints",
        key_envs=("HUGGINGFACE_API_KEY", "HUGGINGFACEHUB_API_TOKEN")),
    "huggingface-serverless": Preset(
        "huggingface-serverless", "Hugging Face Serverless (Task API)",
        "https://api-inference.huggingface.co",
        "Qwen/Qwen2.5-7B-Instruct", "HF_TOKEN", True,
        docs="https://huggingface.co/docs/api-inference",
        api_style="hf-inference",
        key_envs=("HUGGINGFACE_API_KEY", "HUGGINGFACEHUB_API_TOKEN")),
    "custom": Preset(
        "custom", "自定义 OpenAI 兼容端点", "", "default",
        "INFERENCE_API_KEY", True,
        docs="任意兼容 /v1/chat/completions 的服务"),
}

# 常用别名（CLI / 环境变量均可使用）
PROVIDER_ALIASES: dict[str, str] = {
    "hf": "huggingface",
    "hugging-face": "huggingface",
    "huggingface_hub": "huggingface",
    "hf-router": "huggingface",
    "hf-inference": "huggingface-serverless",
    "huggingface-inference": "huggingface-serverless",
    "hf-serverless": "huggingface-serverless",
    "hf-endpoint": "huggingface-endpoint",
}


def _preset_key(preset: Preset) -> str:
    """按 key_env + key_envs 顺序取第一个已配置的 Key。"""
    names = ([preset.key_env] if preset.key_env else []) + list(preset.key_envs)
    for name in names:
        value = _env(name)
        if value:
            return value
    return ""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _env_float(name: str, default: float) -> float:
    try:
        value = float(_env(name))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(_env(name))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def inference_env_present() -> bool:
    """是否显式配置了 INFERENCE_*（决定是否覆盖旧的 DeepSeek/OpenAI 配置）。"""
    return any(_env(name) for name in INFERENCE_ENV_NAMES)


def is_local_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return (host in LOCAL_HOSTS or host.startswith("192.168.")
            or host.startswith("10.") or host.endswith(".local"))


def mask_key(key: str) -> str:
    """只显示前后各 4 位，任何日志/接口都不得出现明文 Key。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}（{len(key)} 位）"


# ================================================================ 配置解析


@dataclass
class InferenceConfig:
    provider: str = "custom"
    label: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff: float = DEFAULT_RETRY_BACKOFF
    temperature: float = 0.7
    requires_key: bool = True
    explicit: bool = False
    api_style: str = "openai"
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        if not self.base_url or not self.model:
            return False
        return bool(self.api_key) or not self.requires_key

    @property
    def keyless(self) -> bool:
        return not self.api_key

    def endpoint(self, path: str = "chat/completions") -> str:
        base = (self.base_url or "").rstrip("/")
        if not base:
            return ""
        if self.api_style == "hf-inference":
            if "/models/" in base:
                return base
            return f"{base}/models/{self.model.lstrip('/')}"
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/{path.lstrip('/')}"

    def describe(self) -> dict:
        """可安全返回给前端/日志的配置摘要（绝不含明文 Key）。"""
        return {
            "provider": self.provider,
            "label": self.label,
            "model": self.model,
            "base_url": self.base_url,
            "endpoint": self.endpoint() if self.base_url else "",
            "key_masked": mask_key(self.api_key),
            "keyless": self.keyless,
            "requires_key": self.requires_key,
            "configured": self.configured,
            "explicit": self.explicit,
            "api_style": self.api_style,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }

    def public(self) -> dict:
        return self.describe()


def resolve_config(provider: str | None = None, *, base_url: str | None = None,
                   api_key: str | None = None, model: str | None = None,
                   timeout: float | None = None,
                   max_retries: int | None = None,
                   temperature: float | None = None,
                   require_explicit: bool = False) -> InferenceConfig:
    """按「显式参数 > INFERENCE_* > Provider 预设 / 旧 Key」解析配置。

    require_explicit=True 时，只有在显式配置了 INFERENCE_* 或传参时才返回
    配置，否则返回未配置对象（用于「是否覆盖旧 DeepSeek/OpenAI 配置」判断）。
    """
    env_provider = _env("INFERENCE_PROVIDER").lower()
    env_base = _env("INFERENCE_BASE_URL")
    env_key = _env("INFERENCE_API_KEY")
    env_model = _env("INFERENCE_MODEL")

    explicit = bool(provider or base_url or api_key or model
                    or env_provider or env_base or env_key or env_model
                    or _env("INFERENCE_TIMEOUT") or _env("INFERENCE_MAX_RETRIES")
                    or any(_env(name) for name in EXTRA_PROVIDER_KEYS))
    if require_explicit and not explicit:
        return InferenceConfig(provider="unset", label="未配置", base_url="",
                               model="", requires_key=False, explicit=False)

    name = (provider or env_provider or "auto").strip().lower()
    name = PROVIDER_ALIASES.get(name, name)
    if name in ("", "auto"):
        if env_base:
            name = "custom"
        else:
            name = "custom"
            for pname, preset in PRESETS.items():
                if preset.requires_key and _preset_key(preset):
                    name = pname
                    break

    if name not in PRESETS and not (base_url or env_base):
        raise InferenceError(
            "config",
            f"未知推理提供方 {name!r}；可用预设：{', '.join(PRESETS)}，"
            "或设置 INFERENCE_BASE_URL 指向任意 OpenAI 兼容端点。",
        )

    preset = PRESETS.get(name) or Preset(
        name=name, label=name, base_url="", model="", requires_key=False,
        key_env="INFERENCE_API_KEY", docs="")

    preset_base = preset.base_url
    if name == "openai" and _env("OPENAI_BASE_URL"):
        preset_base = _env("OPENAI_BASE_URL")
    if name == "huggingface-endpoint" and _env("HF_ENDPOINT_URL"):
        preset_base = _env("HF_ENDPOINT_URL")

    alias_model = _env("HF_MODEL") if name.startswith("huggingface") else ""
    final_base = (base_url if base_url is not None else
                  (env_base or preset_base)).strip()
    final_model = (model if model is not None else
                   (env_model or alias_model or preset.model)).strip()
    final_key = (api_key if api_key is not None else
                 (env_key or _preset_key(preset))).strip()

    requires_key = bool(preset.requires_key) and not is_local_url(final_base)
    if name == "custom" and is_local_url(final_base):
        requires_key = False

    final_timeout = float(timeout if timeout is not None
                          else _env_float("INFERENCE_TIMEOUT", DEFAULT_TIMEOUT))
    final_retries = int(max_retries if max_retries is not None
                        else _env_int("INFERENCE_MAX_RETRIES", DEFAULT_MAX_RETRIES))

    return InferenceConfig(
        provider=name,
        label=f"{preset.label} · {final_model}" if final_model else preset.label,
        base_url=final_base,
        model=final_model,
        api_key=final_key,
        timeout=max(1.0, final_timeout),
        max_retries=max(0, final_retries),
        temperature=0.7 if temperature is None else float(temperature),
        requires_key=requires_key,
        explicit=explicit,
        api_style=preset.api_style,
    )


def explicit_config() -> InferenceConfig | None:
    """仅当用户显式配置了 INFERENCE_* 时返回配置，否则 None。"""
    cfg = resolve_config(require_explicit=True)
    return cfg if cfg.explicit else None


def is_configured(provider: str | None = None) -> bool:
    try:
        return resolve_config(provider).configured
    except InferenceError:
        return False


def describe_config(provider: str | None = None) -> dict:
    """给大屏 / App 用的安全配置摘要 + 预设列表。"""
    try:
        cfg = resolve_config(provider)
        payload = {"ok": True, "configured": cfg.configured, **cfg.describe()}
    except InferenceError as exc:
        payload = {"ok": False, "configured": False, "error": str(exc)}
    payload["presets"] = [
        {"name": p.name, "label": p.label, "base_url": p.base_url,
         "model": p.model, "key_env": p.key_env,
         "key_envs": list(p.key_envs), "api_style": p.api_style,
         "requires_key": p.requires_key, "docs": p.docs}
        for p in PRESETS.values()
    ]
    payload["env"] = list(INFERENCE_ENV_NAMES)
    return payload


# ================================================================ HTTP 薄封装


def _error_from_http(status: int, body: str, url: str) -> InferenceError:
    message = ""
    try:
        data = json.loads(body)
        err = data.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("msg") or "")
        elif isinstance(err, str):
            message = err
        if not message:
            message = str(data.get("message") or data.get("detail") or "")
    except (json.JSONDecodeError, AttributeError, TypeError):
        message = body.strip()

    message = (message or body or "").strip()[:400]
    if status in (401, 403):
        kind = "auth"
    elif status == 429:
        kind = "rate_limit"
    else:
        kind = "http"
    return InferenceError(kind, f"推理 API 返回 HTTP {status}：{message}",
                          status=status, body=body[:1000], url=url)


def _retry_sleep(attempt: int, backoff: float, headers: Any = None) -> None:
    delay = min(MAX_RETRY_AFTER, backoff * (2 ** attempt))
    if headers is not None:
        retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
        try:
            delay = min(MAX_RETRY_AFTER, max(delay, float(retry_after)))
        except (TypeError, ValueError):
            pass
    if delay > 0:
        time.sleep(delay)


def _network_error(exc: BaseException, url: str) -> InferenceError:
    reason = getattr(exc, "reason", exc)
    text = str(reason)
    kind = "timeout" if ("timed out" in text.lower() or "timeout" in text.lower()) else "network"
    return InferenceError(kind, f"网络请求失败 {urllib.parse.urlparse(url).netloc}：{reason}",
                          url=url)


# ================================================================ 推理客户端


def normalize_messages(messages: str | Sequence[dict]) -> list[dict]:
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if not isinstance(messages, (list, tuple)) or not messages:
        raise InferenceError("config", "messages 必须是非空字符串或消息列表")
    out: list[dict] = []
    for item in messages:
        if not isinstance(item, dict) or "role" not in item or "content" not in item:
            raise InferenceError("config", f"非法消息项：{item!r}")
        if item["role"] not in ("system", "user", "assistant", "tool"):
            raise InferenceError("config", f"非法 role：{item['role']!r}")
        out.append({"role": item["role"], "content": item["content"]})
    return out


def _content_text(content: Any) -> str:
    """兼容 content 为字符串或 [{type:text,text:...}] 的多模态返回。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return "" if content is None else str(content)


def _messages_to_prompt(messages: Sequence[dict]) -> str:
    """把 OpenAI 消息格式压平成 Hugging Face task API 需要的单段 prompt。"""
    lines: list[str] = []
    for item in messages:
        role = item.get("role") or "user"
        text = _content_text(item.get("content")).strip()
        if not text:
            continue
        prefix = {"system": "System", "assistant": "Assistant",
                  "tool": "Tool", "user": "User"}.get(role, role.title())
        lines.append(f"{prefix}: {text}")
    lines.append("Assistant:")
    return "\n\n".join(lines)


def _hf_extract_text(data: Any) -> str:
    """解析 Hugging Face 经典推理 API 的返回（generated_text / choices）。"""
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        for item in data:
            text = _hf_extract_text(item)
            if text:
                return text
        return ""
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("generated_text"), str):
        return data["generated_text"]
    if isinstance(data.get("summary_text"), str):
        return data["summary_text"]
    if isinstance(data.get("text"), str):
        return data["text"]
    outputs = data.get("outputs")
    if isinstance(outputs, list):
        return _hf_extract_text(outputs)
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        return _content_text(message.get("content"))
    return ""


class InferenceClient:
    """OpenAI 兼容推理客户端；线程安全（无可变共享状态）。"""

    def __init__(self, config: InferenceConfig | None = None, **overrides: Any) -> None:
        self.config = config or resolve_config(**overrides)

    # —— 内部 HTTP ——

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": accept,
            "User-Agent": USER_AGENT,
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.extra_headers)
        return headers

    def _ensure(self) -> None:
        if not self.config.configured:
            raise InferenceError(
                "config",
                "推理 API 未配置：请设置 INFERENCE_BASE_URL / INFERENCE_API_KEY / "
                "INFERENCE_MODEL，或 INFERENCE_PROVIDER=deepseek 等预设"
                "（参考 --list-providers）。",
            )

    def _post(self, path: str, payload: dict,
              timeout: float | None = None) -> tuple[int, str]:
        self._ensure()
        url = self.config.endpoint(path)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        attempts = max(0, self.config.max_retries) + 1
        last_error: InferenceError | None = None

        for attempt in range(attempts):
            request = urllib.request.Request(
                url, data=body, headers=self._headers(), method="POST")
            try:
                with urllib.request.urlopen(
                        request, timeout=timeout or self.config.timeout) as response:
                    return response.status, response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                last_error = _error_from_http(exc.code, text, url)
                if exc.code in RETRY_STATUS and attempt < attempts - 1:
                    _retry_sleep(attempt, self.config.retry_backoff, exc.headers)
                    continue
                raise last_error from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                last_error = _network_error(exc, url)
                if attempt < attempts - 1:
                    _retry_sleep(attempt, self.config.retry_backoff)
                    continue
                raise last_error from exc

        raise last_error or InferenceError("network", "推理 API 请求失败", url=url)

    def _get_json(self, path: str, timeout: float | None = None) -> dict:
        self._ensure()
        url = self.config.endpoint(path)
        attempts = max(0, self.config.max_retries) + 1
        last_error: InferenceError | None = None

        for attempt in range(attempts):
            request = urllib.request.Request(
                url, headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(
                        request, timeout=timeout or self.config.timeout) as response:
                    body = response.read().decode("utf-8", "replace")
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise InferenceError(
                            "format", f"推理 API 返回非 JSON：{body[:200]}", url=url) from exc
                    return data if isinstance(data, dict) else {"data": data}
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                last_error = _error_from_http(exc.code, text, url)
                if exc.code in RETRY_STATUS and attempt < attempts - 1:
                    _retry_sleep(attempt, self.config.retry_backoff, exc.headers)
                    continue
                raise last_error from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                last_error = _network_error(exc, url)
                if attempt < attempts - 1:
                    _retry_sleep(attempt, self.config.retry_backoff)
                    continue
                raise last_error from exc

        raise last_error or InferenceError("network", "推理 API 请求失败", url=url)

    # —— 公开能力 ——

    def chat(self, messages: str | Sequence[dict], *, model: str | None = None,
             temperature: float | None = None, max_tokens: int | None = None,
             timeout: float | None = None,
             extra: dict | None = None) -> str:
        """一次对话，返回纯文本（App 端最常用）。"""
        return self.chat_result(
            messages, model=model, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, extra=extra)["content"]

    def chat_result(self, messages: str | Sequence[dict], *,
                    model: str | None = None,
                    temperature: float | None = None,
                    max_tokens: int | None = None,
                    timeout: float | None = None,
                    extra: dict | None = None) -> dict:
        """一次对话，返回结构化结果（content / usage / model / latency）。"""
        if self.config.api_style == "hf-inference":
            return self._hf_chat_result(
                messages, model=model, temperature=temperature,
                max_tokens=max_tokens, timeout=timeout)
        payload: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": normalize_messages(messages),
            "temperature": (self.config.temperature if temperature is None
                            else float(temperature)),
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if extra:
            payload.update(extra)

        started = time.time()
        status, body = self._post("chat/completions", payload, timeout)
        latency_ms = int((time.time() - started) * 1000)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise InferenceError(
                "format", f"推理 API 返回非 JSON（HTTP {status}）：{body[:200]}",
                status=status, body=body[:1000]) from exc

        if not isinstance(data, dict):
            raise InferenceError(
                "format", f"推理 API 响应不是 JSON 对象：{body[:200]}",
                status=status, body=body[:1000])
        if isinstance(data.get("error"), (dict, str)):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise InferenceError(
                "http", f"推理 API 返回错误：{message}", status=status,
                body=body[:1000])
        choices = data.get("choices") or []
        if not choices:
            raise InferenceError(
                "format", f"推理 API 响应缺少 choices：{body[:200]}",
                status=status, body=body[:1000])
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = _content_text(message.get("content"))
        if not content and message.get("tool_calls"):
            content = ""  # 工具调用场景：文本可为空，调用方自行处理 raw
        return {
            "ok": True,
            "content": content,
            "model": data.get("model") or payload["model"],
            "provider": self.config.provider,
            "usage": data.get("usage") or {},
            "finish_reason": choice.get("finish_reason") or "",
            "latency_ms": latency_ms,
            "raw": data,
        }

    def chat_stream(self, messages: str | Sequence[dict], *,
                    model: str | None = None,
                    temperature: float | None = None,
                    max_tokens: int | None = None,
                    timeout: float | None = None,
                    extra: dict | None = None) -> Iterator[str]:
        """SSE 流式对话，逐段 yield 文本增量（App 可边收边渲染）。"""
        if self.config.api_style == "hf-inference":
            yield from self._hf_chat_stream(
                messages, model=model, temperature=temperature,
                max_tokens=max_tokens, timeout=timeout)
            return
        self._ensure()
        url = self.config.endpoint("chat/completions")
        payload: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": normalize_messages(messages),
            "temperature": (self.config.temperature if temperature is None
                            else float(temperature)),
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if extra:
            payload.update(extra)

        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(accept="text/event-stream"), method="POST")
        try:
            response = urllib.request.urlopen(
                request, timeout=timeout or self.config.timeout)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise _error_from_http(exc.code, text, url) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise _network_error(exc, url) from exc

        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_text = line[5:].strip()
                elif line.startswith("{"):
                    data_text = line          # 少数网关不带 data: 前缀
                else:
                    continue
                if data_text == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if isinstance(chunk.get("error"), (dict, str)):
                    err = chunk["error"]
                    message = err.get("message") if isinstance(err, dict) else str(err)
                    raise InferenceError("http", f"流式推理返回错误：{message}",
                                         status=0, body=data_text[:500], url=url)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = _content_text(delta.get("content"))
                    if text:
                        yield text

    # —— Hugging Face 经典 task 协议（inputs → generated_text）——

    def _hf_payload(self, messages: str | Sequence[dict], *,
                    max_tokens: int | None = None,
                    temperature: float | None = None,
                    stream: bool = False) -> dict:
        prompt = _messages_to_prompt(normalize_messages(messages))
        parameters: dict[str, Any] = {"return_full_text": False}
        if max_tokens:
            parameters["max_new_tokens"] = int(max_tokens)
        if temperature is not None:
            parameters["temperature"] = float(temperature)
        payload: dict[str, Any] = {"inputs": prompt, "parameters": parameters}
        if stream:
            payload["stream"] = True
        return payload

    def _hf_chat_result(self, messages: str | Sequence[dict], *,
                        model: str | None = None,
                        temperature: float | None = None,
                        max_tokens: int | None = None,
                        timeout: float | None = None) -> dict:
        self._ensure()
        payload = self._hf_payload(
            messages, max_tokens=max_tokens, temperature=temperature)
        url = self.config.endpoint()
        started = time.time()
        status, body = self._post("chat/completions", payload, timeout)
        latency_ms = int((time.time() - started) * 1000)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise InferenceError(
                "format", f"Hugging Face 返回非 JSON（HTTP {status}）：{body[:200]}",
                status=status, body=body[:1000], url=url) from exc
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise InferenceError(
                "http", f"Hugging Face 返回错误：{message}", status=status,
                body=body[:1000], url=url)
        content = _hf_extract_text(data).strip()
        if not content:
            raise InferenceError(
                "format", f"Hugging Face 响应缺少 generated_text：{body[:200]}",
                status=status, body=body[:1000], url=url)
        prompt_tokens = max(1, len(payload["inputs"]) // 4)
        completion_tokens = max(1, len(content) // 4)
        return {
            "ok": True,
            "content": content,
            "model": model or self.config.model,
            "provider": self.config.provider,
            "usage": {"prompt_tokens": prompt_tokens,
                      "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens},
            "finish_reason": "stop",
            "latency_ms": latency_ms,
            "raw": data,
        }

    def _hf_chat_stream(self, messages: str | Sequence[dict], *,
                        model: str | None = None,
                        temperature: float | None = None,
                        max_tokens: int | None = None,
                        timeout: float | None = None) -> Iterator[str]:
        self._ensure()
        url = self.config.endpoint()
        payload = self._hf_payload(
            messages, max_tokens=max_tokens, temperature=temperature, stream=True)
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(accept="text/event-stream"), method="POST")
        try:
            response = urllib.request.urlopen(
                request, timeout=timeout or self.config.timeout)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise _error_from_http(exc.code, text, url) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise _network_error(exc, url) from exc

        accumulated = ""
        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_text = line[5:].strip()
                elif line.startswith("{"):
                    data_text = line
                else:
                    continue
                if data_text == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    err = chunk["error"]
                    message = err.get("message") if isinstance(err, dict) else str(err)
                    raise InferenceError(
                        "http", f"Hugging Face 流式返回错误：{message}",
                        status=0, body=data_text[:500], url=url)
                token = chunk.get("token")
                if isinstance(token, dict) and token.get("text"):
                    text = _content_text(token.get("text"))
                    accumulated += text
                    yield text
                    continue
                full = chunk.get("generated_text")
                if isinstance(full, str):
                    delta = (full[len(accumulated):]
                             if full.startswith(accumulated) else full)
                    accumulated = full
                    if delta:
                        yield delta
                    continue
                choices = chunk.get("choices")
                if isinstance(choices, list) and choices:
                    delta_obj = (choices[0] or {}).get("delta") or {}
                    text = _content_text(delta_obj.get("content"))
                    if text:
                        accumulated += text
                        yield text

    def list_models(self, timeout: float | None = None) -> list[str]:
        """GET /models；不支持该端点的服务会抛 404，交给 health() 降级。"""
        if self.config.api_style != "openai":
            raise InferenceError(
                "http", "Hugging Face task 协议不支持 /models，请用 health() 或直接对话",
                status=405, url=self.config.endpoint())
        data = self._get_json("models", timeout=timeout)
        items = data.get("data") or data.get("models") or []
        models: list[str] = []
        for item in items:
            if isinstance(item, dict):
                name = item.get("id") or item.get("name") or item.get("model")
                if name:
                    models.append(str(name))
            elif isinstance(item, str):
                models.append(item)
        return models

    def health(self, *, timeout: float | None = None, deep: bool = False) -> dict:
        """连通性自检：优先 /models，端点不支持时降级为 1 token 对话。"""
        started = time.time()
        info = {
            "ok": False,
            "provider": self.config.provider,
            "label": self.config.label,
            "model": self.config.model,
            "base_url": self.config.base_url,
            "key_masked": mask_key(self.config.api_key),
            "configured": self.config.configured,
        }
        if not self.config.configured:
            info.update({"kind": "config",
                         "error": "推理 API 未配置（缺少 base_url / model / key）",
                         "latency_ms": int((time.time() - started) * 1000)})
            return info

        probe_timeout = min(timeout or self.config.timeout, 15.0)
        if self.config.api_style != "openai":
            try:
                result = self.chat_result(
                    "ping", max_tokens=8, temperature=0, timeout=probe_timeout)
                info.update({"ok": True, "mode": "chat-ping",
                             "echo": (result.get("content") or "")[:40],
                             "latency_ms": int((time.time() - started) * 1000)})
            except InferenceError as exc:
                info.update({"kind": exc.kind, "status": exc.status,
                             "error": str(exc),
                             "latency_ms": int((time.time() - started) * 1000)})
            return info
        try:
            models = self.list_models(timeout=probe_timeout)
            info.update({"ok": True, "mode": "models", "models": models[:30],
                         "models_count": len(models),
                         "latency_ms": int((time.time() - started) * 1000)})
            return info
        except InferenceError as exc:
            first_error = exc

        if deep or first_error.status in (400, 404, 405) or first_error.kind == "format":
            try:
                result = self.chat_result(
                    "ping", max_tokens=1, temperature=0, timeout=probe_timeout)
                info.update({"ok": True, "mode": "chat-ping",
                             "echo": (result.get("content") or "")[:40],
                             "latency_ms": int((time.time() - started) * 1000)})
                return info
            except InferenceError as exc:
                first_error = exc

        info.update({"kind": first_error.kind, "status": first_error.status,
                     "error": str(first_error),
                     "latency_ms": int((time.time() - started) * 1000)})
        return info


# ================================================================ 模块级便捷函数


def chat(prompt: str | Sequence[dict], *, system: str | None = None,
         **kwargs: Any) -> str:
    """最简调用：`chat("你好")`，自动读取环境变量配置。"""
    client = InferenceClient(resolve_config())
    messages: Any = prompt
    if system and isinstance(prompt, str):
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": prompt}]
    return client.chat(messages, **kwargs)


def chat_stream(prompt: str | Sequence[dict], *, system: str | None = None,
                **kwargs: Any) -> Iterator[str]:
    client = InferenceClient(resolve_config())
    messages: Any = prompt
    if system and isinstance(prompt, str):
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": prompt}]
    return client.chat_stream(messages, **kwargs)


def health(**kwargs: Any) -> dict:
    return InferenceClient(resolve_config()).health(**kwargs)


def quick_prompt(messages: str | Sequence[dict], **kwargs: Any) -> dict:
    """给 HTTP 接口用：返回 JSON 可序列化的完整结果。"""
    return InferenceClient(resolve_config()).chat_result(messages, **kwargs)


def render_presets_table() -> str:
    lines = ["可用推理提供方（INFERENCE_PROVIDER）：", ""]
    for preset in PRESETS.values():
        key = f"key_env={preset.key_env}" if preset.key_env else "无需 Key"
        style = "" if preset.api_style == "openai" else f" style={preset.api_style}"
        lines.append(f"  {preset.name:<22} {preset.label:<34} "
                     f"{preset.base_url or '（需自填 BASE_URL）':<48} "
                     f"model={preset.model:<26} {key}{style}")
    lines.append("")
    lines.append("任意 OpenAI 兼容端点都可用 INFERENCE_BASE_URL 直接接入。")
    return "\n".join(lines)


# ================================================================ Mock 推理端点


def mock_reply(prompt: str, model: str = "mock-inference-1") -> str:
    """确定性的模拟回复：让无 Key 环境也能演示「摘要 / 量化解读」链路。"""
    text = (prompt or "").strip()
    head = " ".join(text.split())[:120]
    if any(word in text for word in ("摘要", "总结", "新闻", "快讯", "舆情")):
        return (
            f"【MOCK 摘要】输入 {len(text)} 字。\n"
            f"1) 已接收爬虫/快讯文本，模拟抽取首句：{head}…\n"
            "2) 情绪整体中性偏多，暂未见单一事件主导；\n"
            "3) 关注板块：互联网平台、半导体、新能源；\n"
            "4) 以上为 Mock 输出，仅用于验证「爬虫 → 推理 API」链路。"
        )
    if any(word in text for word in ("因子", "动量", "量化", "多空", "波动", "仓位")):
        return (
            "【MOCK 量化解读】\n"
            "- 动量因子：近 48h 量价同向，分数 0.62（偏多）；\n"
            "- 波动因子：ATR 抬升 18%，建议仓位上限 60%；\n"
            "- 估值因子：PE 处历史 35% 分位，中性偏低；\n"
            "- 风险提示：数据为模拟样本，仅用于验证「量化 → 推理 API」链路。"
        )
    return (f"[MOCK:{model}] 已收到 {len(text)} 字请求。\n"
            f"回显：{head}\n"
            "（本地 Mock 端点，无需 Key；配置真实 INFERENCE_BASE_URL 即可切换模型。）")


class _MockServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _MockHandler)
        self.fail_plan: list[int] = []
        self.fail_lock = threading.Lock()
        self.started_at = time.time()

    def pop_failure(self) -> int | None:
        with self.fail_lock:
            if self.fail_plan:
                return self.fail_plan.pop(0)
        return None


class _MockHandler(BaseHTTPRequestHandler):
    server_version = f"MockInference/{VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默
        return

    # —— 公共响应 ——

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _path(self) -> str:
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    # —— GET ——

    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        if path in ("/v1/models", "/models"):
            self._json(200, {
                "object": "list",
                "data": [{"id": name, "object": "model", "owned_by": "mock"}
                         for name in ("mock-inference-1", "mock-inference-2")],
            })
            return
        if path in ("/healthz", "/health", "/v1/health"):
            self._json(200, {"ok": True, "mode": "mock",
                             "uptime_s": round(time.time() - self.server.started_at, 1)})
            return
        self._json(404, {"error": {"message": f"mock 未实现 GET {path}"}})

    # —— POST ——

    def do_POST(self) -> None:  # noqa: N802
        path = self._path()
        hf_style = path.startswith("/models/")
        if not hf_style and path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": f"mock 未实现 POST {path}"}})
            return

        failure = self.server.pop_failure()
        if failure:
            self._json(failure, {"error": {"message": f"mock 注入故障 HTTP {failure}",
                                           "type": "mock_error"}})
            return

        auth = (self.headers.get("Authorization") or "").strip()
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if token in ("bad-key", "invalid") or token.startswith("sk-bad"):
            self._json(401, {"error": {"message": "mock: invalid api key",
                                       "type": "authentication_error"}})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "mock: invalid JSON"}})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": {"message": "mock: payload must be object"}})
            return

        if hf_style:
            model = path.split("/models/", 1)[1] or "mock-inference-1"
            user_text = str(payload.get("inputs") or "")
        else:
            model = str(payload.get("model") or "mock-inference-1")
            messages = payload.get("messages") or []
            user_text = ""
            for item in messages:
                if isinstance(item, dict) and item.get("role") == "user":
                    user_text = _content_text(item.get("content"))
        reply = mock_reply(user_text, model)

        parameters = payload.get("parameters") or {}
        stream = bool(payload.get("stream")
                      or (isinstance(parameters, dict) and parameters.get("stream")))
        if stream:
            if hf_style:
                self._stream_reply_hf(reply)
            else:
                self._stream_reply(reply, model)
            return

        if hf_style:
            self._json(200, [{"generated_text": reply}])
            return

        prompt_tokens = max(1, len(user_text) // 4)
        completion_tokens = max(1, len(reply) // 4)
        self._json(200, {
            "id": f"chatcmpl-mock-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": prompt_tokens,
                      "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens},
        })

    def _stream_reply(self, reply: str, model: str) -> None:
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send(chunk: dict) -> None:
            self.wfile.write(
                ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        base = {"id": "chatcmpl-mock-stream", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model}
        send({**base, "choices": [{"index": 0, "delta": {"role": "assistant"},
                                   "finish_reason": None}]})
        for i in range(0, len(reply), 8):
            send({**base, "choices": [{"index": 0,
                                       "delta": {"content": reply[i:i + 8]},
                                       "finish_reason": None}]})
            time.sleep(0.003)
        send({**base, "choices": [{"index": 0, "delta": {},
                                   "finish_reason": "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _stream_reply_hf(self, reply: str) -> None:
        """Hugging Face task API 的 SSE：data: {"token": {"text": ...}}。"""
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        for i in range(0, len(reply), 8):
            chunk = {"token": {"text": reply[i:i + 8], "special": False},
                     "generated_text": None, "details": None}
            self.wfile.write(
                ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.003)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class MockServerHandle:
    """本地 Mock 推理端点句柄（供测试 / 演示 / examples 使用）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 fail_plan: Sequence[int] | None = None) -> None:
        self.httpd = _MockServer((host, port))
        self.httpd.fail_plan = list(fail_plan or [])
        self.host, self.port = self.httpd.server_address[:2]
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def start(self) -> "MockServerHandle":
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        name="mock-inference", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3)

    def __enter__(self) -> "MockServerHandle":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


# ================================================================ 推理网关（给 App 用）


class _GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _GatewayHandler(BaseHTTPRequestHandler):
    """把任意 OpenAI 兼容上游包装成稳定网关：Key 留服务端 + 可选 Token。"""

    server_version = f"InferenceGateway/{VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[gateway] {args[0]}\n")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = _env("INFERENCE_GATEWAY_TOKEN")
        if not token:
            return True
        auth = self.headers.get("Authorization") or ""
        supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return bool(supplied) and hmac.compare_digest(supplied, token)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(401, {"error": {"message": "gateway: unauthorized"}})
        return False

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/healthz", "/health", "/api/inference/health"):
            try:
                cfg = resolve_config()
            except InferenceError as exc:
                self._json(500, exc.to_dict())
                return
            self._json(200, {"ok": cfg.configured, "gateway": VERSION,
                             "upstream": cfg.describe()})
            return
        if path in ("/v1/models", "/models"):
            try:
                cfg = resolve_config()
            except InferenceError as exc:
                self._json(500, exc.to_dict())
                return
            if cfg.api_style != "openai":
                self._json(200, {"object": "list",
                                 "data": [{"id": cfg.model, "object": "model",
                                           "owned_by": cfg.provider}]})
                return
            self._proxy("GET", "models", b"", "")
            return
        self._json(404, {"error": {"message": f"gateway 未实现 GET {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": f"gateway 未实现 POST {path}"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        self._proxy("POST", "chat/completions", body,
                    self.headers.get("Accept") or "application/json")

    def _proxy_hf(self, cfg: InferenceConfig, body: bytes) -> None:
        """把 HF task 协议适配成 OpenAI 协议，App 端无感。"""
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        messages = payload.get("messages") or []
        model = payload.get("model") or cfg.model
        max_tokens = payload.get("max_tokens")
        temperature = payload.get("temperature")
        client = InferenceClient(cfg)
        created = int(time.time())
        base = {"id": f"chatcmpl-hf-{created}", "object": "chat.completion.chunk",
                "created": created, "model": model}

        if payload.get("stream"):
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for text in client.chat_stream(
                        messages, model=model, max_tokens=max_tokens,
                        temperature=temperature):
                    chunk = {**base, "choices": [{
                        "index": 0, "delta": {"content": text},
                        "finish_reason": None}]}
                    self.wfile.write(
                        ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
                    self.wfile.flush()
                done = {**base, "choices": [{
                    "index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(
                    ("data: " + json.dumps(done, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            except InferenceError as exc:
                err = {"error": {"message": str(exc), "type": exc.kind}}
                try:
                    self.wfile.write(
                        ("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        try:
            result = client.chat_result(
                messages, model=model, max_tokens=max_tokens,
                temperature=temperature)
        except InferenceError as exc:
            status = 401 if exc.kind == "auth" else 429 if exc.kind == "rate_limit" else 502
            self._json(status, exc.to_dict())
            return
        self._json(200, {
            "id": f"chatcmpl-hf-{created}",
            "object": "chat.completion",
            "created": created,
            "model": result.get("model") or model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": result.get("content") or ""},
                         "finish_reason": result.get("finish_reason") or "stop"}],
            "usage": result.get("usage") or {},
        })

    def _proxy(self, method: str, path: str, body: bytes, accept: str) -> None:
        try:
            cfg = resolve_config()
        except InferenceError as exc:
            self._json(500, exc.to_dict())
            return
        if not cfg.configured:
            self._json(503, {"ok": False, "kind": "config",
                             "error": "上游推理 API 未配置"})
            return
        if cfg.api_style != "openai" and method == "POST":
            self._proxy_hf(cfg, body)
            return

        headers = {"Content-Type": "application/json",
                   "Accept": accept or "application/json",
                   "User-Agent": USER_AGENT}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"

        request = urllib.request.Request(
            cfg.endpoint(path), data=body if method == "POST" else None,
            headers=headers, method=method)
        try:
            upstream = urllib.request.urlopen(request, timeout=cfg.timeout)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            self._json(exc.code, {"ok": False, "kind": _error_from_http(
                exc.code, text, cfg.endpoint(path)).kind, "error": text[:1000]})
            return
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            err = _network_error(exc, cfg.endpoint(path))
            status = 504 if err.kind == "timeout" else 502
            self._json(status, err.to_dict())
            return

        with upstream:
            content_type = upstream.headers.get("Content-Type") or "application/json"
            self.send_response(upstream.status)
            self._cors()
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            if "text/event-stream" in content_type:
                try:
                    for line in upstream:
                        self.wfile.write(line)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self.wfile.write(upstream.read())


def start_gateway(host: str = "0.0.0.0", port: int = 8090) -> ThreadingHTTPServer:
    """启动推理网关（阻塞调用方需自行 serve_forever）。"""
    httpd = _GatewayServer((host, port), _GatewayHandler)
    return httpd


# ================================================================ 自检 / CLI


class _EnvSandbox:
    """临时改写环境变量，退出时精确恢复（自检/测试用）。"""

    def __init__(self, **updates: str | None) -> None:
        self.updates = updates
        self.saved: dict[str, str | None] = {}

    def __enter__(self) -> "_EnvSandbox":
        for name, value in self.updates.items():
            self.saved[name] = os.environ.get(name)
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return self

    def __exit__(self, *exc: Any) -> None:
        for name, old in self.saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def _clear_ai_env() -> dict[str, str | None]:
    names = list(INFERENCE_ENV_NAMES) + [
        "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "INFERENCE_GATEWAY_TOKEN",
    ]
    return {name: None for name in names}


def selftest(verbose: bool = True) -> int:
    """纯离线自检：Mock 端点上跑通配置 / 对话 / 流式 / 重试 / 鉴权 / 网关。"""
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append((name, bool(cond), detail))
        if verbose:
            print(f"  {'✅' if cond else '❌'} {name}"
                  + (f"  [{detail}]" if detail and not cond else ""))

    saved = {name: os.environ.get(name)
             for name in list(_clear_ai_env()) + ["NO_PROXY", "no_proxy"]}
    for name in _clear_ai_env():
        os.environ.pop(name, None)

    server: MockServerHandle | None = None
    gateway: ThreadingHTTPServer | None = None
    gateway_thread: threading.Thread | None = None
    try:
        print("① 预设与配置解析")
        check("至少 8 个 Provider 预设", len(PRESETS) >= 8, str(len(PRESETS)))
        for preset in PRESETS.values():
            check(f"预设 {preset.name} 字段完整",
                  bool(preset.label and preset.model))

        cfg_none = resolve_config()
        check("无任何配置时 configured=False", cfg_none.configured is False)

        with _EnvSandbox(**dict(_clear_ai_env(),
                                INFERENCE_BASE_URL="https://example.com/v1",
                                INFERENCE_API_KEY="sk-selftest-1234567890",
                                INFERENCE_MODEL="demo-model")):
            cfg = resolve_config()
            check("显式 INFERENCE_* 被识别", cfg.explicit and cfg.configured)
            check("自定义端点拼接正确",
                  cfg.endpoint() == "https://example.com/v1/chat/completions",
                  cfg.endpoint())
            desc = cfg.describe()
            check("配置摘要不含明文 Key",
                  "sk-selftest-1234567890" not in json.dumps(desc, ensure_ascii=False))
            check("Key 掩码保留首尾", desc["key_masked"].startswith("sk-s")
                  and "…" in desc["key_masked"])
            check("远端自定义端点要求 Key", cfg.requires_key is True)

        with _EnvSandbox(**dict(_clear_ai_env(), DEEPSEEK_API_KEY="sk-deepseek-test")):
            cfg = resolve_config()
            check("自动选用 DEEPSEEK_API_KEY",
                  cfg.provider == "deepseek" and cfg.configured)
            check("DeepSeek 预设 base_url",
                  cfg.base_url == "https://api.deepseek.com/v1", cfg.base_url)

        with _EnvSandbox(**dict(_clear_ai_env(), OPENAI_API_KEY="sk-openai-test")):
            cfg = resolve_config()
            check("自动选用 OPENAI_API_KEY",
                  cfg.provider == "openai" and cfg.model == "gpt-4o-mini")

        with _EnvSandbox(**dict(_clear_ai_env(), INFERENCE_PROVIDER="ollama")):
            cfg = resolve_config()
            check("本地 Ollama 无需 Key 也可用",
                  cfg.configured and cfg.keyless and cfg.requires_key is False)

        with _EnvSandbox(**dict(_clear_ai_env(),
                                INFERENCE_BASE_URL="http://127.0.0.1:9/v1",
                                INFERENCE_MODEL="x")):
            cfg = resolve_config()
            check("本地自定义端点按无需 Key 处理",
                  cfg.configured and cfg.requires_key is False)

        print("② Mock 推理端点")
        server = MockServerHandle(fail_plan=[500, 503]).start()
        base = server.base_url
        env = dict(_clear_ai_env(), INFERENCE_BASE_URL=base,
                   INFERENCE_API_KEY="sk-mock-1234567890",
                   INFERENCE_MODEL="mock-inference-1",
                   INFERENCE_TIMEOUT="10", INFERENCE_MAX_RETRIES="2")
        with _EnvSandbox(**env):
            cfg = replace(resolve_config(), retry_backoff=0.01)
            client = InferenceClient(cfg)
            models = client.list_models()
            check("GET /models 返回模型", models and "mock-inference-1" in models,
                  str(models))
            result = client.chat_result("你好，做个摘要")
            check("非流式对话返回 content",
                  result["ok"] and "MOCK" in result["content"], result["content"][:60])
            check("usage 字段透传", bool(result.get("usage")))
            chunks = list(client.chat_stream("量化因子解读"))
            streamed = "".join(chunks)
            check("SSE 流式可逐段接收", len(chunks) >= 2, f"{len(chunks)} chunks")
            check("流式内容完整", "MOCK" in streamed and len(streamed) > 10)
            check("500/503 自动重试后成功",
                  client.chat_result("重试测试")["ok"] is True)

            health = client.health()
            check("health() 返回 ok", health["ok"] is True, str(health.get("error")))

        with _EnvSandbox(**dict(env, INFERENCE_API_KEY="sk-bad-key-000000")):
            cfg = replace(resolve_config(), retry_backoff=0.01)
            try:
                InferenceClient(cfg).chat("触发鉴权失败")
                check("401 映射为 auth 错误", False, "未抛异常")
            except InferenceError as exc:
                check("401 映射为 auth 错误", exc.kind == "auth" and exc.status == 401,
                      f"{exc.kind}/{exc.status}")

        with _EnvSandbox(**dict(env, INFERENCE_BASE_URL="http://127.0.0.1:1/v1",
                                INFERENCE_MAX_RETRIES="0")):
            cfg = resolve_config()
            try:
                InferenceClient(cfg).chat("网络失败")
                check("连接失败映射为 network 错误", False, "未抛异常")
            except InferenceError as exc:
                check("连接失败映射为 network 错误", exc.kind in ("network", "timeout"),
                      exc.kind)
            check("health() 失败时 ok=False",
                  InferenceClient(cfg).health()["ok"] is False)

        print("②b Hugging Face 双协议（Mock）")
        server_root = base.rsplit("/v1", 1)[0]
        hf_env = dict(env)
        hf_env.pop("INFERENCE_API_KEY", None)
        with _EnvSandbox(**dict(hf_env, INFERENCE_PROVIDER="huggingface",
                                HF_TOKEN="hf_selftest_123456")):
            client = InferenceClient(replace(resolve_config(), retry_backoff=0.01))
            result = client.chat_result("HF Router 摘要")
            check("HF Router 对话（OpenAI 协议）",
                  result["ok"] and "MOCK" in result["content"])

        with _EnvSandbox(**dict(hf_env,
                                INFERENCE_PROVIDER="huggingface-serverless",
                                INFERENCE_BASE_URL=server_root,
                                HF_TOKEN="hf_selftest_123456")):
            client = InferenceClient(replace(resolve_config(), retry_backoff=0.01))
            result = client.chat_result("HF Serverless 量化解读")
            check("HF Serverless 对话（inputs → generated_text）",
                  result["ok"] and "MOCK" in result["content"], result["content"][:40])
            chunks = list(client.chat_stream("HF 流式"))
            check("HF Serverless 流式 SSE 可逐段接收",
                  len(chunks) >= 2 and "MOCK" in "".join(chunks),
                  f"{len(chunks)} chunks")
            check("HF Serverless health 走 chat ping",
                  client.health().get("mode") == "chat-ping")

        print("③ OpenAI 兼容网关")
        gateway = start_gateway("127.0.0.1", 0)
        gateway_thread = threading.Thread(target=gateway.serve_forever,
                                          name="gateway-selftest", daemon=True)
        gateway_thread.start()
        gw_port = gateway.server_address[1]
        gw_url = f"http://127.0.0.1:{gw_port}"

        with _EnvSandbox(**env):
            status, body = _http_json("GET", f"{gw_url}/healthz")
            check("网关 /healthz 可用", status == 200 and json.loads(body)["gateway"])
            status, body = _http_json("POST", f"{gw_url}/v1/chat/completions",
                                      {"model": "mock-inference-1",
                                       "messages": [{"role": "user",
                                                     "content": "网关摘要测试"}]})
            data = json.loads(body) if body else {}
            check("网关非流式转发成功",
                  status == 200 and "MOCK" in
                  str(data.get("choices", [{}])[0].get("message", {}).get("content", "")))

        with _EnvSandbox(**dict(env, INFERENCE_GATEWAY_TOKEN="gw-secret")):
            status, _ = _http_json("POST", f"{gw_url}/v1/chat/completions",
                                   {"messages": [{"role": "user", "content": "x"}]})
            check("网关 Token 未带 → 401", status == 401, str(status))
            status, body = _http_json(
                "POST", f"{gw_url}/v1/chat/completions",
                {"messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": "Bearer gw-secret"})
            check("网关 Token 正确 → 200", status == 200, str(status))

        print("④ Key 掩码边界")
        check("空 Key 掩码为空", mask_key("") == "")
        check("短 Key 全掩码", mask_key("sk-1") == "****")
        check("长 Key 不泄露全文", "abcdefgh" not in mask_key("abcdefgh12345678"))
    except Exception as exc:  # noqa: BLE001
        check(f"自检异常：{exc.__class__.__name__}", False, str(exc))
    finally:
        if gateway is not None:
            gateway.shutdown()
            gateway.server_close()
        if gateway_thread is not None:
            gateway_thread.join(timeout=2)
        if server is not None:
            server.stop()
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = len(checks) - passed
    if verbose:
        print(f"\n{'=' * 60}")
        print(f"推理 API 自检：{passed}/{len(checks)} 通过"
              + (f"，{failed} 项失败 ❌" if failed else " ✅"))
    return failed


def _http_json(method: str, url: str, payload: dict | None = None,
               headers: dict | None = None, timeout: float = 15) -> tuple[int, str]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=request_headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inference_api.py",
        description="统一推理 API 接入层（OpenAI 兼容）：自检 / 对话 / 网关 / Mock 端点",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  INFERENCE_PROVIDER=deepseek DEEPSEEK_API_KEY=sk-xxx python inference_api.py --check\n"
            "  INFERENCE_BASE_URL=https://api.siliconflow.cn/v1 \\\n"
            "    INFERENCE_API_KEY=sk-xxx INFERENCE_MODEL=Qwen/Qwen2.5-7B-Instruct \\\n"
            "    python inference_api.py -p '写一句港股开盘提示'\n"
            "  python inference_api.py --serve 8090        # App 连这个网关即可\n"
            "  python inference_api.py --mock-server 18080 # 无 Key 离线联调\n"
        ),
    )
    parser.add_argument("--provider",
                        choices=["auto", *PRESETS, *PROVIDER_ALIASES],
                        default=None,
                        help="推理提供方（默认读 INFERENCE_PROVIDER，否则自动探测）")
    parser.add_argument("--base-url", default=None, help="OpenAI 兼容端点，如 https://api.x.com/v1")
    parser.add_argument("--api-key", default=None, help="API Key（更推荐用环境变量）")
    parser.add_argument("--model", default=None, help="模型名")
    parser.add_argument("--timeout", type=float, default=None, help="单次请求超时秒数")
    parser.add_argument("--max-retries", type=int, default=None, help="失败重试次数")
    parser.add_argument("--temperature", type=float, default=None, help="采样温度")

    parser.add_argument("--list-providers", action="store_true", help="列出所有内置提供方")
    parser.add_argument("--show-config", action="store_true", help="打印脱敏后的当前配置")
    parser.add_argument("--check", action="store_true", help="连通性自检（真实请求 /models 或 ping）")
    parser.add_argument("--models", action="store_true", help="列出上游模型")
    parser.add_argument("-p", "--prompt", default=None, help="发送一条对话")
    parser.add_argument("--system", default=None, help="可选 system 提示词")
    parser.add_argument("--stream", action="store_true", help="流式输出")
    parser.add_argument("--max-tokens", type=int, default=None, help="最大生成 token")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    parser.add_argument("--serve", type=int, metavar="PORT", default=None,
                        help="启动 OpenAI 兼容推理网关（给 App 用）")
    parser.add_argument("--host", default="0.0.0.0", help="网关监听地址（默认 0.0.0.0）")
    parser.add_argument("--mock-server", type=int, metavar="PORT", default=None,
                        help="启动本地 Mock 推理端点（离线联调）")
    parser.add_argument("--selftest", action="store_true", help="纯离线自检（CI 门禁）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.selftest:
        print(f"🧪 inference_api 自检 v{VERSION}")
        return 1 if selftest() else 0

    if args.list_providers:
        print(render_presets_table())
        return 0

    if args.mock_server is not None:
        server = MockServerHandle(port=args.mock_server)
        print(f"🧪 Mock 推理端点已启动：{server.base_url}")
        print("   POST /v1/chat/completions（支持 stream）· GET /v1/models · GET /healthz")
        print("   按 Ctrl+C 停止")
        try:
            server.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.httpd.server_close()
        return 0

    if args.serve is not None:
        gateway = start_gateway(args.host, args.serve)
        print(f"🔌 推理网关已启动：http://{args.host}:{args.serve}")
        print("   POST /v1/chat/completions · GET /v1/models · GET /healthz")
        if _env("INFERENCE_GATEWAY_TOKEN"):
            print("   🔐 已启用 INFERENCE_GATEWAY_TOKEN（请求需带 Bearer Token）")
        try:
            gateway.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            gateway.server_close()
        return 0

    try:
        config = resolve_config(
            args.provider, base_url=args.base_url, api_key=args.api_key,
            model=args.model, timeout=args.timeout,
            max_retries=args.max_retries, temperature=args.temperature)
    except InferenceError as exc:
        print(f"❌ 配置错误：{exc}", file=sys.stderr)
        return 2

    client = InferenceClient(config)

    if args.show_config or not any((args.check, args.models, args.prompt)):
        payload = {"version": VERSION, **config.describe()}
        print(json.dumps(payload, ensure_ascii=False, indent=2)
              if args.json else json.dumps(payload, ensure_ascii=False, indent=2))
        if not any((args.check, args.models, args.prompt)):
            if not config.configured:
                print("\n提示：设置 INFERENCE_BASE_URL / INFERENCE_API_KEY / "
                      "INFERENCE_MODEL 即可接入；或 --mock-server 先离线联调。")
            return 0

    if args.check:
        report = client.health(deep=True)
        print(json.dumps(report, ensure_ascii=False, indent=2)
              if args.json else
              (f"{'✅' if report.get('ok') else '❌'} 推理 API "
               f"{'连通' if report.get('ok') else '不可用'}\n"
               + json.dumps(report, ensure_ascii=False, indent=2)))
        return 0 if report.get("ok") else 1

    if args.models:
        try:
            models = client.list_models()
        except InferenceError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "models": models}, ensure_ascii=False, indent=2)
              if args.json else "\n".join(models))
        return 0

    messages: Any = args.prompt
    if args.system:
        messages = [{"role": "system", "content": args.system},
                    {"role": "user", "content": args.prompt}]

    try:
        if args.stream:
            started = time.time()
            pieces: list[str] = []
            for piece in client.chat_stream(
                    messages, max_tokens=args.max_tokens,
                    temperature=args.temperature):
                pieces.append(piece)
                print(piece, end="", flush=True)
            if not args.json:
                print()
            if args.json:
                print(json.dumps({"ok": True, "content": "".join(pieces),
                                  "provider": config.provider, "model": config.model,
                                  "latency_ms": int((time.time() - started) * 1000)},
                                 ensure_ascii=False, indent=2))
            return 0

        result = client.chat_result(
            messages, max_tokens=args.max_tokens, temperature=args.temperature)
        if args.json:
            result.pop("raw", None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result["content"])
            usage = result.get("usage") or {}
            print(f"\n— {result['model']} · {result['latency_ms']}ms"
                  + (f" · tokens {usage.get('total_tokens')}" if usage.get("total_tokens") else ""),
                  file=sys.stderr)
        return 0
    except InferenceError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
