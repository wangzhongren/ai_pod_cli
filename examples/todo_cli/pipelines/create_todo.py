from ai_pod_cli.config import load_beans
from ai_pod_cli.container import Pod, build_container
from ai_pod_cli.context import PipelineContext
from modules.services.todo_creator import TodoCreator
from modules.services.todo_presenter import TodoPresenter


def run(ctx: PipelineContext) -> dict:
    container = build_container(load_beans())
    S = Pod(container)
    (S(TodoCreator) | S(TodoPresenter)).execute_all(ctx)
    return ctx.summary()
