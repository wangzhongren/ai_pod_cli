"""PipelineContext — 贯穿整个执行链的共享数据管道。"""


class PipelineContext:
    """在执行链中流转的共享上下文。

    Attributes:
        params: 入口参数（由 AI 从用户指令中推断）。
        data:   组件间共享的数据池，每个组件可以读写。
        steps:  执行轨迹，记录每个组件的输入输出。
    """

    def __init__(self, params: dict | None = None):
        self.params: dict = params or {}
        self.data: dict = {}
        self.steps: list[dict] = []

    def set(self, key: str, value) -> None:
        """向数据池写入一个值。"""
        self.data[key] = value

    def get(self, key: str, default=None):
        """从数据池读取一个值。"""
        return self.data.get(key, default)

    def record_step(self, component_id: str, result, duration_ms: float | None = None) -> None:
        """记录一个执行步骤。"""
        step = {
            "component": component_id,
            "result": result,
        }
        if duration_ms is not None:
            step["duration_ms"] = round(duration_ms, 3)
        self.steps.append(step)

    def summary(self) -> dict:
        """返回执行摘要。"""
        return {
            "params": self.params,
            "data": self.data,
            "steps": [
                {
                    "component": s["component"],
                    "result_preview": str(s["result"])[:200],
                    **({"duration_ms": s["duration_ms"]} if "duration_ms" in s else {}),
                }
                for s in self.steps
            ],
        }

    def __repr__(self):
        return f"PipelineContext(params={self.params}, data_keys={list(self.data.keys())}, steps={len(self.steps)})"
