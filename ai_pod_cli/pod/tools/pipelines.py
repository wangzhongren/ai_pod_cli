"""Pipeline composition tool for frozen Services."""

from ai_pod_cli.commands.compose import handle_compose


def generate_pipelines(
    *, pipelines: list[dict], generated: list[str], reused: list[str], args,
    progress_callback=None, load_routes, replace_existing: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    _load_routes_map = load_routes
    # 生成 pipelines
    generated_pipelines = []
    failed_pipelines = []
    reused_pipelines = []
    if pipelines and (generated or reused):
        print(f"\n🔗 [生成 Pipeline] {len(pipelines)} 条")
        existing_routes = _load_routes_map()

        for i, pipe in enumerate(pipelines, 1):
            pipe_name = pipe.get("name", f"pipeline_{i}")
            instruction = pipe.get("instruction", "")
            execution = pipe.get("execution") or {"mode": "sequential"}
            if execution.get("mode", "sequential") != "sequential":
                instruction += (
                    "\nExecution policy (must use AIPod Runtime APIs exactly): "
                    + str(execution)
                )
            print(f"\n   [{i}/{len(pipelines)}] {pipe_name}: {instruction}")
            if pipe_name in existing_routes:
                if not replace_existing:
                    reused_pipelines.append(pipe_name)
                    print(f"   ♻️  [Pipeline 复用] {pipe_name}")
                    continue
                print(f"   🔧 [Pipeline 重建] {pipe_name}")

            # 构造 compose 的 args
            class ComposeArgs:
                pass
            compose_args = ComposeArgs()
            compose_args.cmd = instruction
            compose_args.name = pipe_name
            compose_args.list = False
            compose_args.progress_callback = progress_callback
            compose_args.auto_repair = bool(getattr(args, "auto_repair", False) or args.yes)
            compose_args.json = getattr(args, "json", False)

            try:
                compose_succeeded = handle_compose(compose_args)
                route_was_registered = pipe_name in _load_routes_map()
                if compose_succeeded is True and route_was_registered:
                    generated_pipelines.append(pipe_name)
                else:
                    print(
                        "   ❌ Pipeline 工具未返回成功回执或未注册路由；"
                        "本条不会被标记为完成。"
                    )
                    failed_pipelines.append(pipe_name)
            except Exception as e:
                print(f"   ❌ Pipeline 生成失败: {e}")
                failed_pipelines.append(pipe_name)

        print(f"\n   🔗 Pipeline 成功: {len(generated_pipelines)} 条 — {', '.join(generated_pipelines) if generated_pipelines else '(无)'}")
        if failed_pipelines:
            print(f"   ❌ Pipeline 失败: {len(failed_pipelines)} 条 — {', '.join(failed_pipelines)}")

    return generated_pipelines, failed_pipelines, reused_pipelines
