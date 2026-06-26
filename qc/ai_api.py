"""
ai_api.py —— AI 调用统一接口（标准协议实现）

接入"模型评测平台"标准协议：
    POST http://llm-api.model-eval.woa.com/v1/chat/completions
    Authorization: Bearer APP_ID:APP_KEY
    model: api_anthropic_claude-opus-4-7

凭证（APP_ID / APP_KEY）通过 .env 文件管理，绝不硬编码、绝不入库。
框架内所有 LLM 语义判断都通过 call_ai() 调用。

设计约定：
- call_ai(prompt, *, system=None, schema=None, **kwargs)
- 传 schema(JSON Schema) 时要求返回 JSON 并解析为 dict；否则返回文本。
- 凭证缺失时抛 AINotConfiguredError，上层据此降级（确定性层不受影响）。
- system 块默认带 cache_control 以命中 Claude 提示词缓存（降本提速）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import requests


class AINotConfiguredError(RuntimeError):
    """AI 凭证未配置（APP_ID / APP_KEY 缺失）。"""


# ─────────────────────────────────────────────────────────────────────────────
# .env 加载（零依赖，避免引入 python-dotenv）
# ─────────────────────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    """从 qc_framework/.env 读取键值对到 os.environ（不覆盖已存在的环境变量）。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────

import config

DEFAULT_URL = config.DEFAULT_API_URL
DEFAULT_MODEL = config.DEFAULT_MODEL


def _config() -> tuple[str, str, str, str]:
    """返回 (app_id, app_key, url, model)。凭证缺失抛 AINotConfiguredError。"""
    app_id = os.environ.get("APP_ID", "").strip()
    app_key = os.environ.get("APP_KEY", "").strip()
    if not app_id or not app_key or app_id == "YOUR_APP_ID" or app_key == "YOUR_APP_KEY":
        raise AINotConfiguredError(
            "APP_ID / APP_KEY 未配置。请在 qc_framework/.env 中填写真实凭证"
            "（参考 .env.example）。"
        )
    url = os.environ.get("QC_API_URL", DEFAULT_URL).strip()
    model = os.environ.get("QC_MODEL", DEFAULT_MODEL).strip()
    return app_id, app_key, url, model


# ─────────────────────────────────────────────────────────────────────────────
# 底层调用
# ─────────────────────────────────────────────────────────────────────────────

def _post_chat(messages: list[dict], *, max_tokens: int = 4096,
               timeout: int | None = None, tools: Optional[list[dict]] = None,
               tool_choice: Optional[str] = None) -> dict:
    """标准协议底层 POST，返回原始 choices[0].message（dict）。
    供单次文本调用与多轮 tool use 循环共用。

    超时/连接类瞬时异常自动指数退避重试（config.AI_MAX_RETRIES 次）——
    长上下文单次推理慢易撞读超时，但属可恢复故障，不应让整题判空。
    HTTP 4xx/5xx 业务错误不在此重试（语义性失败，重试无益）。
    """
    if timeout is None:
        timeout = config.AI_REQUEST_TIMEOUT
    app_id, app_key, url, model = _config()
    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {app_id}:{app_key}",
    }
    import time
    last_exc: Exception | None = None
    for attempt in range(config.AI_MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt < config.AI_MAX_RETRIES:
                time.sleep(config.AI_RETRY_BACKOFF ** attempt)
                continue
            raise
        if not resp.ok:
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} — 响应体：{resp.text[:500]}",
                response=resp,
            )
        data = resp.json()
        return data["choices"][0]
    # 理论不可达（循环内要么 return 要么 raise）；兜底重抛最后异常。
    raise last_exc  # type: ignore[misc]


def _build_messages(prompt: str, system: Optional[str]) -> list[dict]:
    messages: list[dict] = []
    if system:
        messages.append({
            "role": "system",
            "content": [{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
        })
    messages.append({"role": "user", "content": prompt})
    return messages


def _call_model(
    prompt: str,
    system: Optional[str],
    *,
    max_tokens: int = 4096,
    timeout: int | None = None,
    **_: Any,
) -> str:
    """标准协议单次调用，返回模型文本输出。"""
    choice = _post_chat(_build_messages(prompt, system),
                        max_tokens=max_tokens, timeout=timeout)
    return choice["message"]["content"]


def call_ai_with_meta(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: int = 4096,
    timeout: int | None = None,
) -> tuple[str, Optional[str]]:
    """单次文本调用，返回 (文本, finish_reason)。

    finish_reason == 'length' 表示输出被 max_tokens 截断（答复不完整）——
    调用方据此区分"模型答完了但有 bug" vs"模型还没写完就被掐断"，给针对性反馈。
    凭证缺失抛 AINotConfiguredError。
    """
    choice = _post_chat(_build_messages(prompt, system),
                        max_tokens=max_tokens, timeout=timeout)
    msg = choice.get("message", {}) or {}
    return msg.get("content", "") or "", choice.get("finish_reason")


# ─────────────────────────────────────────────────────────────────────────────
# 公共入口
# ─────────────────────────────────────────────────────────────────────────────

def call_ai(
    prompt: str,
    *,
    system: Optional[str] = None,
    schema: Optional[dict] = None,
    max_retries: int = 2,
    **kwargs: Any,
) -> Any:
    """
    统一 AI 调用入口。

    Args:
        prompt: 用户消息。
        system: 可选 system prompt（带 cache_control）。
        schema: 若提供（JSON Schema），要求返回 JSON，函数解析为 dict 返回。
        max_retries: schema 解析失败时的重试次数。
        kwargs: 透传 max_tokens / temperature / timeout 等。

    Returns:
        schema is None -> str；schema is dict -> dict。

    Raises:
        AINotConfiguredError: 凭证未配置。
        ValueError: 多次重试后仍无法解析为合法 JSON。
        requests.HTTPError: 接口返回非 2xx。
    """
    if schema is not None:
        schema_hint = (
            "\n\n你必须只输出一个 JSON 对象，严格符合以下 JSON Schema，"
            "不要包含任何额外文字或 markdown 代码块标记：\n"
            + json.dumps(schema, ensure_ascii=False, indent=2)
        )
        full_prompt = prompt + schema_hint
    else:
        full_prompt = prompt

    last_err: Optional[Exception] = None
    for _attempt in range(max_retries + 1):
        text = _call_model(full_prompt, system, **kwargs)
        if schema is None:
            return text
        try:
            return _extract_json(text)
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise ValueError(f"AI 返回无法解析为合法 JSON（已重试 {max_retries} 次）: {last_err}")


def call_ai_agentic(
    prompt: str,
    *,
    system: Optional[str] = None,
    tools: list[dict],
    tool_executor,
    schema: Optional[dict] = None,
    max_rounds: int = 25,
    max_tokens: int = 4096,
    timeout: int | None = None,
    on_step=None,
    finish_gate=None,
    max_gate_nudges: int = 3,
) -> Any:
    """带 tool use 循环的 AI 调用。

    模型可多轮调用工具（如 list_dir / read_file / grep）自主探索文件，
    每轮的工具结果回填进对话历史 —— 历史本身就是"记忆"，模型读一个文件、
    把结论留在上下文里，再决定下一步读什么，直到它给出最终答复。

    Args:
        prompt:        首条用户消息（任务说明 + 探索目标）。
        system:        system prompt（带 cache_control）。
        tools:         OpenAI 风格 tools 定义列表。
        tool_executor: 可调用对象 (name: str, args: dict) -> str，执行工具返回文本结果。
        schema:        若提供，最终答复要求为符合 schema 的 JSON，解析为 dict 返回。
        max_rounds:    工具调用最多轮数（防失控）。
        on_step:       可选回调 (round_idx, tool_name, args) -> None，用于打印进度。
        finish_gate:   可选回调 (tool_history) -> Optional[str]。模型想收尾（不再调工具）时被调用：
                       返回非空字符串=探索不充分，该串作为 user 消息注入把模型拽回继续读；
                       返回 None/空=放行。业务侧用它强制"必读 grade.py / solver_runs 才准下结论"。
                       tool_history 是 [(tool_name, args_dict), ...] 已发生的工具调用记录。
        max_gate_nudges: finish_gate 最多拦截几次（防门槛与模型顶住导致死循环）。

    Returns:
        schema is None -> 最终文本；schema is dict -> 解析后的 dict。

    Raises:
        AINotConfiguredError: 凭证未配置。
        ValueError:           JSON 解析失败。
    """
    _config()  # 触发凭证校验

    if schema is not None:
        prompt = prompt + (
            "\n\n探索完成后，你的最终答复必须只输出一个 JSON 对象，"
            "严格符合以下 JSON Schema，不要包含任何额外文字或 markdown 代码块标记：\n"
            + json.dumps(schema, ensure_ascii=False, indent=2)
        )

    messages = _build_messages(prompt, system)
    tool_history: list[tuple[str, dict]] = []  # 已发生的 (tool_name, args)，供 finish_gate 判断
    gate_nudges = 0

    for round_idx in range(max_rounds):
        choice = _post_chat(messages, max_tokens=max_tokens,
                            timeout=timeout, tools=tools)
        msg = choice["message"]
        tool_calls = msg.get("tool_calls")

        # 没有工具调用 → 模型想给出最终答复
        if not tool_calls:
            # 收尾门槛：业务侧可在此判断"探索是否充分"，不充分则把模型拽回继续读
            if finish_gate and gate_nudges < max_gate_nudges:
                try:
                    nudge = finish_gate(list(tool_history))
                except Exception:  # noqa: BLE001
                    nudge = None
                if nudge:
                    gate_nudges += 1
                    # 保留模型这轮的话，再追加一条 user 提示要求补充探索
                    messages.append({"role": "assistant",
                                     "content": msg.get("content") or ""})
                    messages.append({"role": "user", "content": str(nudge)})
                    continue
            content = msg.get("content") or ""
            if schema is None:
                return content
            try:
                return _extract_json(content)
            except Exception:  # noqa: BLE001
                # 最终 JSON 解析失败：给一次重申机会（要求只输出合法 JSON），再不行才抛。
                if gate_nudges < max_gate_nudges:
                    gate_nudges += 1
                    messages.append({"role": "assistant",
                                     "content": content})
                    messages.append({"role": "user", "content": (
                        "你上面的最终输出不是合法 JSON（解析失败）。请只输出一个严格符合 schema 的"
                        " JSON 对象，不要任何额外文字、解释或 markdown 代码块标记。")})
                    continue
                raise

        # 把 assistant 的 tool_calls 消息原样加入历史
        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": tool_calls,
        })

        # 逐个执行工具，把结果作为 role=tool 消息回填
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            tool_history.append((name, args))
            if on_step:
                try:
                    on_step(round_idx, name, args)
                except Exception:  # noqa: BLE001
                    pass
            try:
                result = tool_executor(name, args)
            except Exception as e:  # noqa: BLE001
                result = f"[工具执行错误] {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": str(result),
            })

    # 轮数耗尽：再逼一次最终答复（不带 tools）
    messages.append({
        "role": "user",
        "content": "已达到探索轮数上限。请基于目前掌握的信息，立即给出最终答复。",
    })
    choice = _post_chat(messages, max_tokens=max_tokens, timeout=timeout)
    content = choice["message"].get("content") or ""
    if schema is None:
        return content
    return _extract_json(content)


def _extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象（容忍 ```json 代码块包裹）。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start : end + 1]
    return json.loads(s)


def is_ai_available() -> bool:
    """探测凭证是否已配置（不发起真实请求）。"""
    try:
        _config()
        return True
    except AINotConfiguredError:
        return False


def ping() -> str:
    """连通性自检：发一条最小请求，返回模型回复。供 CLI 调试用。"""
    return _call_model("Say OK only.", None, max_tokens=16)


if __name__ == "__main__":
    # 命令行自检：python -m qc.ai_api
    if not is_ai_available():
        print("✗ 凭证未配置：请在 qc_framework/.env 填写 APP_ID / APP_KEY")
    else:
        try:
            print("✓ 凭证已加载，正在连通测试 ...")
            print("  模型回复:", ping())
        except Exception as e:  # noqa: BLE001
            print(f"✗ 调用失败：{e}")
