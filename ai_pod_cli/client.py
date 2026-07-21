"""OpenAI client initialization with lazy loading and retry logic."""

import json
import os
import time

from openai import OpenAI


_client: OpenAI | None = None
_model: str | None = None

# 默认重试配置
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 2  # 秒


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
        "max_tokens": 8192,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    last_error: Exception | None = None
    delay = retry_delay

    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"   [retry] 第 {attempt}/{max_retries} 次重试...")

            response = client.chat.completions.create(**kwargs)
            raw_content = response.choices[0].message.content

            if json_mode:
                # 尝试解析 JSON
                try:
                    result = json.loads(raw_content)
                except (json.JSONDecodeError, TypeError) as e:
                    last_error = ValueError(f"AI 返回的内容不是合法 JSON: {e}\n原始内容: {raw_content[:300]}")
                    print(f"   [retry] 第 {attempt} 次: JSON 解析失败")
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
