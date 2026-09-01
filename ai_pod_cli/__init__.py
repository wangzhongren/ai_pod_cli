"""AIPodCli - AI-native IoC container low-code engine CLI."""

__version__ = "0.9.1"

from ai_pod_cli.context import PipelineContext
from ai_pod_cli.model import Model
from ai_pod_cli.repository import ModelRepository
from ai_pod_cli.contracts import (
    ContractField, analyze_parallel_contracts, analyze_pipeline_contracts,
    analyze_stream_contracts, types_compatible,
)
from ai_pod_cli.result import Effect, Failure, Result, Success
from ai_pod_cli.streaming import StreamItem, StreamPipeline, stream

__all__ = [
    "PipelineContext", "Model", "ModelRepository", "ContractField", "Effect", "Failure", "Result", "Success",
    "StreamItem", "StreamPipeline", "stream", "analyze_parallel_contracts",
    "analyze_pipeline_contracts", "analyze_stream_contracts", "types_compatible",
]
