"""AIPodCli - AI-native IoC container low-code engine CLI."""

__version__ = "0.7.2"

from ai_pod_cli.context import PipelineContext
from ai_pod_cli.contracts import ContractField, analyze_pipeline_contracts, types_compatible
from ai_pod_cli.result import Effect, Failure, Result, Success

__all__ = [
    "PipelineContext", "ContractField", "Effect", "Failure", "Result", "Success",
    "analyze_pipeline_contracts", "types_compatible",
]
