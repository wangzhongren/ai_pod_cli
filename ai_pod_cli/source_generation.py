"""Separate structured component metadata from XML-like source generation."""

import json
from collections.abc import Callable
from pathlib import PurePosixPath, PureWindowsPath

from ai_pod_cli.client import DEFAULT_SOURCE_MAX_TOKENS, DEFAULT_TIMEOUT_SECONDS
from ai_pod_cli.source_codec import decode_source_artifact, encode_source_artifact


def generate_source(
    llm: Callable, system: str, user: str, path: str | Callable[[dict], str], *,
    content_key: str = "code", source_max_tokens: int = DEFAULT_SOURCE_MAX_TOKENS,
    source_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    frozen_metadata: dict | None = None, **options,
) -> dict:
    """Freeze JSON metadata, then request one source file through text mode.

    Metadata retains the caller's transport options. Source has its own output budget
    and timeout. XML failures retry without replanning metadata; candidates still
    require caller validation.
    """
    if source_max_tokens < 1 or source_timeout_seconds <= 0:
        raise ValueError("Source token budget and timeout must be positive")
    metadata = frozen_metadata if frozen_metadata is not None else llm(
        system + "\n本轮只返回 JSON 元数据，不生成源码，不要返回 code 或 content 字段。",
        user, json_mode=True, **options,
    )
    if not isinstance(metadata, dict):
        raise ValueError("Component metadata must be a JSON object")
    # Some endpoints still include unrequested source alongside valid metadata.
    # Discard only these reserved root fields; source must come from the later
    # XML request, never from this JSON response or a fallback to it.
    metadata = {key: value for key, value in metadata.items() if key not in {"code", "content"}}
    path = path(metadata) if callable(path) else path
    if (not isinstance(path, str) or path in ("", ".") or "\\" in path
            or PureWindowsPath(path).drive
            or PurePosixPath(path).is_absolute() or ".." in path.split("/")):
        raise ValueError("Artifact path must be a project-relative path")
    if "path" in metadata and metadata["path"] != path:
        raise ValueError(f"Metadata path must equal planned path '{path}'")
    # Source may not replace the frozen metadata or choose another output path.
    source_system = (
        "Generate exactly one file using the frozen metadata and requirements below. "
        "The requirements describe the completed metadata stage; their JSON output "
        "instructions do not apply to this source stage.\n"
        + system
        + "\nSOURCE OUTPUT PROTOCOL: Return exactly one XML-like <create> action with "
        "one <path> and one <content>. Put all source inside CDATA. No prose, Markdown, "
        "attributes, extra operands or additional actions. The path must be exactly "
        + json.dumps(path)
        + ". Split literal ]]> in source as ]]]]><![CDATA[>. Example:\n"
        + encode_source_artifact(path, "complete file text")
    )
    source_user = user + "\nFrozen metadata:\n" + json.dumps(metadata, ensure_ascii=False)
    evidence = ""
    source_options = {
        **options, "max_tokens": source_max_tokens,
        "timeout_seconds": source_timeout_seconds,
    }
    for attempt in range(3):
        raw = llm(source_system, source_user + evidence, json_mode=False, **source_options)
        try:
            artifact = decode_source_artifact(raw, path)
            if not artifact["content"].strip():
                raise ValueError("Source artifact is empty")
            return {**metadata, "path": path, content_key: artifact["content"]}
        except ValueError as error:
            if attempt == 2:
                raise
            evidence = "\nPrevious XML validation error: " + str(error)
    raise AssertionError("unreachable")
