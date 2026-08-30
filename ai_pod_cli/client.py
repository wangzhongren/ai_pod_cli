"""OpenAI client initialization with lazy loading and retry logic."""

import json
import os
import time
from collections.abc import Callable

from openai import OpenAI


_client: OpenAI | None = None
_model: str | None = None

# 默认重试配置
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 2  # 秒
DEFAULT_TIMEOUT_SECONDS = 120.0


def _parse_json_content(raw_content: str) -> dict:
    """Parse strict JSON, with a small compatibility fallback for proxy wrappers."""
    content = (raw_content or "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as original_error:
        if content.startswith("```"):
            content = content.removeprefix("```json").removeprefix("```")
            content = content.removesuffix("```").strip()
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(content[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise original_error


def get_client() -> OpenAI:
    """Get or create the OpenAI client instance (lazy singleton).

    Reads configuration from environment variables:
      - OPENAI_API_KEY: API key (required)
      - OPENAI_BASE_URL: API base URL (defaults to https://api.openai.com/v1)
    """
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
        )
    return _client


def get_model() -> str:
    """Get the model name from environment (defaults to deepseek-chat)."""
    global _model
    if _model is None:
        _model = os.environ.get("OPENAI_MODEL", "deepseek-chat")
    return _model


def call_llm(
    system_prompt: str,
    user_content: str,
    *,
    json_mode: bool = False,
    temperature: float = 0.1,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    max_tokens: int = 32768,
    progress_callback: Callable[[dict], None] | None = None,
    progress_label: str = "Model response",
) -> dict | str:
    """Call the LLM with retry on network errors or invalid JSON.

    Args:
        system_prompt: System prompt for the model.
        user_content: User message content.
        json_mode: If True, forces JSON output and parses the result into a dict.
        temperature: Sampling temperature.
        max_retries: Maximum number of retry attempts (default 3).
        retry_delay: Base delay in seconds between retries (doubles each attempt).

    Returns:
        Parsed dict if json_mode=True, otherwise raw string content.

    Raises:
        RuntimeError: If all retries are exhausted.
    """
    client = get_client()
    model = get_model()

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    last_error: Exception | None = None
    delay = retry_delay
    force_non_stream = False

    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"   [retry] 第 {attempt}/{max_retries} 次重试...")

            finish_reason = None
            usage = None
            used_stream = progress_callback is not None and not force_non_stream
            if not used_stream:
                response = client.chat.completions.create(**kwargs)
                raw_content = response.choices[0].message.content
                finish_reason = getattr(response.choices[0], "finish_reason", None)
                usage = getattr(response, "usage", None)
            else:
                progress_callback({"type": "llm_started", "label": progress_label, "characters": 0})
                stream = client.chat.completions.create(**kwargs, stream=True)
                parts: list[str] = []
                character_count = 0
                last_reported_at = 0.0
                last_reported_count = 0
                for chunk in stream:
                    choices = getattr(chunk, "choices", None) or []
                    delta = getattr(choices[0], "delta", None) if choices else None
                    chunk_finish = getattr(choices[0], "finish_reason", None) if choices else None
                    if chunk_finish is not None:
                        finish_reason = chunk_finish
                    content = getattr(delta, "content", None) if delta is not None else None
                    if content:
                        parts.append(content)
                        character_count += len(content)
                    now = time.monotonic()
                    if character_count > last_reported_count and (
                        character_count - last_reported_count >= 256
                        or now - last_reported_at >= 0.4
                    ):
                        progress_callback({"type": "llm_delta", "label": progress_label, "characters": character_count})
                        last_reported_at = now
                        last_reported_count = character_count
                raw_content = "".join(parts)
                completed_event = {
                    "type": "llm_completed", "label": progress_label,
                    "characters": character_count,
                }
                if finish_reason is not None:
                    completed_event["finish_reason"] = finish_reason
                progress_callback(completed_event)

            if finish_reason == "length":
                previous_limit = int(kwargs["max_tokens"])
                next_limit = min(previous_limit * 2, 32768)
                last_error = ValueError(
                    f"模型输出达到 token 上限：finish_reason=length, "
                    f"characters={len(raw_content or '')}, max_tokens={previous_limit}"
                )
                print(
                    f"   [retry] 第 {attempt} 次: 输出达到上限 "
                    f"({previous_limit} tokens, {len(raw_content or '')} chars)"
                )
                if next_limit > previous_limit:
                    kwargs["max_tokens"] = next_limit
                    print(f"   [retry] 下一次提高输出上限到 {next_limit} tokens")
                if attempt < max_retries:
                    time.sleep(delay)
                    delay *= 2
                continue

            if json_mode:
                # 尝试解析 JSON
                try:
                    result = _parse_json_content(raw_content)
                except (json.JSONDecodeError, TypeError) as e:
                    last_error = ValueError(
                        f"AI 返回的内容不是合法 JSON: {e}; finish_reason={finish_reason or 'missing'}; "
                        f"characters={len(raw_content or '')}\n原始内容: {(raw_content or '')[:300]}"
                    )
                    print(
                        f"   [retry] 第 {attempt} 次: JSON 解析失败 "
                        f"(finish_reason={finish_reason or 'missing'}, {len(raw_content or '')} chars)"
                    )
                    if used_stream and finish_reason is None:
                        force_non_stream = True
                        print("   [retry] 流没有正常结束，下一次切换为非流式完整响应")
                    if attempt < max_retries:
                        time.sleep(delay)
                        delay *= 2
                    continue

                # 检查 code 字段是否为空（仅 create/start 场景）
                if "code" in result and not result.get("code", "").strip():
                    last_error = ValueError("AI 返回的 code 字段为空")
                    print(f"   [retry] 第 {attempt} 次: code 字段为空")
                    if attempt < max_retries:
                        time.sleep(delay)
                        delay *= 2
                    continue

                return result

            return raw_content

        except Exception as e:
            last_error = e
            print(f"   [retry] 第 {attempt} 次: API 调用失败 ({type(e).__name__})")
            if attempt < max_retries:
                time.sleep(delay)
                delay *= 2

    raise RuntimeError(f"LLM 调用在 {max_retries} 次重试后仍然失败: {last_error}")
