"""Versioned SDK reference injected by role; examples are executed in tests."""

from textwrap import dedent


SDK_EXAMPLES = {
    "model": '''from ai_pod_cli import Model

class ExampleValue(Model):
    value: int
''',
    "provider": '''from injector import inject
from ai_pod_cli.config_store import ConfigStore

class ConfiguredProvider:
    @inject
    def __init__(self, config: ConfigStore):
        self.increment = config.get("example.increment", 2)

    def add(self, value: int) -> int:
        return value + self.increment
''',
    "repository": '''def repository_example(repo, Record):
    # Record is an existing registered table Model with id and value fields.
    saved = repo.save(Record(value=3))
    found = repo.get(Record, saved.id)
    matches = repo.find(Record, value=3)
    repo.delete(saved)
    return found, matches
''',
    "contracts": '''INPUTS = {"value": {"type": "int", "required": False}}
OUTPUTS = {"answer": {"type": "int"}}
''',
    "service": '''from injector import inject
from ai_pod_cli import PipelineContext
from modules.providers.public.example import ConfiguredProvider

class ExampleService:
    @inject
    def __init__(self, provider: ConfiguredProvider):
        self.provider = provider

    def execute(self, ctx: PipelineContext):
        result = {"answer": self.provider.add(ctx.get("value", 1))}
        ctx.set("answer", result["answer"])
        return result
''',
    "pipeline": '''from ai_pod_cli.config import load_beans
from ai_pod_cli.container import Pod, build_container
from modules.services.public.example import ExampleService

def run(ctx):
    S = Pod(build_container(load_beans()))
    return S(ExampleService).execute_all(ctx)
''',
    "runner": '''from ai_pod_cli.runner import PipelineRunner

def run_example():
    runner = PipelineRunner()
    result = runner.run("example", {"value": 3})
    return result
''',
    "adapter": '''from ai_pod_cli.interface import InterfaceAdapter

class ExampleAdapter(InterfaceAdapter):
    def required_routes(self):
        return ["example"]

    def smoke_payloads(self):
        return {"example": {"value": 3}}

    def start(self, context, payload=None):
        # A short-lived entry; HTTP/GUI adapters may instead block in start().
        return context.run_route("example", payload or {})
''',
    "interface_check": '''from ai_pod_cli.interface import load_manifest, load_adapter, create_context

def check_interface(project_root="."):
    manifest_path, manifest = load_manifest("example", project_root)
    adapter = load_adapter(manifest, project_root)
    context = create_context(manifest, project_root)
    report = adapter.smoke(context)
    assert report["status"] == "passed", report
    return manifest_path, adapter, context
''',
}


def _example(name):
    return "\n```python\n" + SDK_EXAMPLES[name] + "```\n"


SECTIONS = {
    "models": dedent('''
        MODEL API
        from ai_pod_cli import Model; from sqlmodel import Field
        Model inherits SQLModel/Pydantic. Define value models without table=True.
        Persistent models use class Record(Model, table=True) and a primary key such as
        id: int | None = Field(default=None, primary_key=True). Import existing Models by
        registered class_path; they are data, not injected dependencies.
        ModelClass.model_validate(value) -> instance; raises Pydantic ValidationError.
        ModelClass.validate(value, path="$" ) -> list[str]; [] means valid, not an instance.
        instance.to_dict() -> dict. ModelClass.sample() -> synthetic required-field values;
        samples do not establish domain validity. Add strictness/range validators as needed.
    ''') + _example("model"),
    "dependencies": dedent('''
        CONFIGURATION, DEPENDENCIES AND STORAGE
        from ai_pod_cli.config_store import ConfigStore
        from ai_pod_cli.repository import ModelRepository
        Python constructors use @inject from injector and actual Provider type annotations.
        The container binds Providers/Services as singletons. Register dependency IDs in
        finish.components; Python injects by type, not a Node-style dependencies dictionary.
        ConfigStore(config_path="config.toml") loads TOML immediately.
        config.get("section.key", default=None) -> value/default; config.reload() reloads it.
        ModelRepository(config_store: ConfigStore) uses AIPOD_DATABASE_URL or database.url.
        repo.save(instance) -> saved Model (generated id refreshed).
        repo.get(ModelClass, id) -> Model | None; repo.list(ModelClass) -> list[Model].
        repo.find(ModelClass, filters=None, **filter_values) -> list[Model].
        repo.delete(instance) -> None; repo.close() releases connections.
        Save/get/list/find initialize tables from modules.models. Reuse the built-in repository
        for persistence; use the Agent's temporary database for checks.
    ''') + _example("repository"),
    "providers": "PROVIDER IMPLEMENTATION\nPlace the implementation in impl/ and re-export it by name from public/.\n" + _example("provider"),
    "context": dedent('''
        PIPELINE CONTEXT
        from ai_pod_cli import PipelineContext
        PipelineContext(params=None, *, data=None, branch_id=None).
        ctx.get(key, default=None) reads data first, then params. ctx.set(key, value) -> None.
        ctx.summary() -> {params, data, steps}; ctx.steps contains execution traces.
        Python PipelineContext has no output() or typed() method.
        Return the complete declared output mapping from Service.execute(), even if those
        values were also written with ctx.set: execute_all() returns the Service's raw result.
    '''),
    "contracts": dedent('''
        REGISTRATION CONTRACTS
        inputs/outputs are mappings of field names to schemas. Use plain types (int, float,
        str, bool, dict, list, any) or structured schemas. required=False permits an absent key;
        it does not permit None for an int/str field. Nullable values can use an anyOf schema
        such as {"anyOf": [{"type": "str"}, {"type": "null"}]}.
        Do not put prose such as "int 1..60, optional" in a type expression; put explanations
        in description and implement domain bounds explicitly. A string type implies required.
        Do not assume contract defaults populate ctx: use ctx.get(name, fallback) for defaults.
        Outputs absent on failure paths must be optional, or returned on every path.
        validate_contract_data(data, fields, prefix="$") -> list[str] in ai_pod_cli.contracts.
        Submit public Provider/Service class_path and matching schemas through finish;
        registrations are written by the controller, not by an SDK call in generated code.
    ''') + _example("contracts"),
    "services": "SERVICE IMPLEMENTATION\nThis example uses the contracts above and an existing public Provider.\n" + _example("service"),
    "pipelines": dedent('''
        COMPOSITION API
        from ai_pod_cli.config import load_beans; from ai_pod_cli.container import Pod, build_container
        load_beans() -> {beans: [...]}; build_container(config) -> injector.Injector.
        S = Pod(container); S(ServiceClass) returns a composition reference. Pod is not a
        context manager. Use classes imported from their registered public paths.
        S(First).execute_all(ctx) -> raw result; (S(First) | S(Second)).execute_all(ctx) ->
        last result, stopping at Failure. For async Services use await execute_all_async(ctx).
        Define run(ctx) in each pipeline file. Before a NEW route is registered, test run(ctx)
        directly. Submit name/file/inputs in finish.pipelines; the controller writes routes.toml.
    ''') + _example("pipeline"),
    "runner": dedent('''
        REGISTERED ROUTE EXECUTION
        from ai_pod_cli.runner import PipelineRunner
        PipelineRunner(routes_path="routes.toml"); runner.route_names() -> list[str].
        runner.run(route_name, params=None) -> pipeline result dict, NOT a tuple or Node Result.
        runner.run_with_context(route_name, params=None) -> (result, PipelineContext).
        await runner.run_async(...) and run_with_context_async(...) support async pipelines.
        Unknown routes raise KeyError; missing pipeline files raise FileNotFoundError;
        Service/pipeline exceptions can propagate. SDK Success/Failure results are serialized
        with status/output or status/error; ordinary dict results keep their application shape.
        Run from the project root (route file paths are relative), or use InterfaceContext.run_route.
        Construct a new runner after changing route registrations. Match route names and inputs
        to the current registry, and inspect the actual application result when checking success.
    ''') + _example("runner"),
    "interfaces": dedent('''
        INTERFACE SDK — Python signatures and return shapes
        from ai_pod_cli.interface import load_manifest, load_adapter, create_context, InterfaceAdapter
        load_manifest(target, project_root=".") -> (Path, manifest_dict). Unpack both values;
        do not call .get() on the tuple. target may be an Interface name or manifest path.
        load_adapter(manifest_dict, project_root=".") -> instantiated InterfaceAdapter.
        create_context(manifest_dict, project_root=".") -> InterfaceContext.
        context.route_names() -> list[str]; context.route_contract(route) -> contract dict.
        context.run_route(route, params=None) -> pipeline result; it activates the project root.
        context.activated() is a context manager; context.emit(event, payload=None) records events.
        InterfaceAdapter is a base class. Override required_routes(), smoke_payloads() and
        start(self, context, payload=None). Base smoke(context) -> {status: passed|failed,
        missing_routes, missing_smoke_payloads, contract_errors, ...}; it checks route declarations
        and sample input contracts, not the actual HTTP/GUI behavior.
        start() is adapter-defined and may block in a server/event loop. Do not call a blocking
        start() and then try to send HTTP requests in the same thread. Test long-lived entries
        in a separate process, send requests, then terminate/wait for that process in finally.
        Base stop(context) does nothing; a generated adapter must implement actual shutdown
        if it advertises that capability. Keep project activation for the entry lifetime or
        run a child with cwd=context.project_root.
        Manifest: interfaces/<name>/interface.json, with name/kind/adapter/artifacts.
        adapter = {path: "interfaces/example/adapter.py", class_name: "ExampleAdapter"}.
        When returning finish.interfaces include all intended lifecycle/verify/support fields:
        the controller writes the submitted manifest. Lifecycle commands are argv lists,
        e.g. ["{python}", "app.py", "--port", "8770"], not one joined shell string.
        The optional legacy helper verify_adapter_candidate(project_root, manifest, sources,
        timeout=30) -> list[str] requires sources={relative_path: complete_source} and copies
        a candidate project. It is not required for checking an already delivered Interface.
    ''') + _example("adapter") + _example("interface_check"),
    "verification": dedent('''
        ENTRY AND ACCEPTANCE CHECKS
        python -m ai_pod_cli interface --project-root /PROJECT smoke example
        python -m ai_pod_cli interface --project-root /PROJECT --payload '{"value":3}' run example
        The SDK smoke CLI prints a status object: inspect status, not just the process exit code.
        Run actual behavior checks for the original requirement, including negative cases.
        Rerun a failed check after the owner repairs it. Keep test commands' failing exit codes;
        do not hide them with | tail or || true (the framework already bounds captured output).
        A successful import, help command or smoke declaration check is not full acceptance.
        Report which checks ran and what remains unverified. Never claim uncovered behavior passed.
    '''),
}

ROLE_SECTIONS = {
    "models": ("models",),
    "providers": ("dependencies", "contracts", "providers"),
    "services": ("context", "dependencies", "contracts", "services"),
    "pipelines": ("context", "contracts", "pipelines", "runner"),
    "interfaces": ("contracts", "runner", "interfaces", "verification"),
    "pod": ("contracts", "runner", "interfaces", "verification"),
}


def sdk_reference(role: str) -> str:
    """Return only the SDK chapters relevant to this Agent's ownership role."""
    if role not in ROLE_SECTIONS:
        raise ValueError(f"Unknown SDK reference role: {role}")
    header = f'''AIPod Python SDK reference bundled with this framework — role: {role}.
Use these signatures and examples directly; they describe SDK calls, not response instructions.
Example names/paths are placeholders: use the actual registered names from project context.
Use the task's Python interpreter (AIPOD_PYTHON) and preserve its supplied PYTHONPATH.
This reference stays in the system prompt on every turn. Consult SDK source only for a specific
missing detail or observed discrepancy; do not repeatedly dump modules to rediscover these APIs.
Other chapters are available from ai_pod_cli.sdk_reference.sdk_reference(role).
'''
    return header + "\n" + "\n".join(SECTIONS[name].strip() for name in ROLE_SECTIONS[role])
