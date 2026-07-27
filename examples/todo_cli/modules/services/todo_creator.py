from ai_pod_cli.context import PipelineContext


class TodoCreator:
    def execute(self, ctx: PipelineContext) -> dict:
        todo = {"id": 1, "title": ctx.params["title"], "completed": False}
        ctx.set("todo", todo)
        return {"status": "created", "id": todo["id"]}
