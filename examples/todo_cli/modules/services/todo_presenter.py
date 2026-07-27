from ai_pod_cli.context import PipelineContext


class TodoPresenter:
    def execute(self, ctx: PipelineContext) -> dict:
        todo = ctx.get("todo")
        display = f"[ ] {todo['title'].upper()}"
        ctx.set("display", display)
        return {"status": "presented"}
