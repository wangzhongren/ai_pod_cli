"""AIPodCli - AI-native IoC container low-code engine CLI."""

__version__ = "0.6.7"

from ai_pod_cli.context import PipelineContext
from ai_pod_cli.contracts import ContractField, analyze_pipeline_contracts, types_compatible

__all__ = ["PipelineContext", "ContractField", "analyze_pipeline_contracts", "types_compatible"]
