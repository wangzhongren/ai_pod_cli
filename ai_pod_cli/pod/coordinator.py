"""Pod dispatches file-owning Agents and authorizes upstream changes."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import uuid

import tomlkit

from ai_pod_cli.config import extract_model_fields
from ai_pod_cli.contracts import canonical_contract
from ai_pod_cli.validation import validate_component_contract, validate_pipeline_contract
from ai_pod_cli.workspace import LAYERS, WorkspaceTools, path_owner, owned_paths
from ai_pod_cli.workspace_agent import WorkspaceAgent


class PodCoordinator:
    def __init__(self, root, state: dict, llm, *, save, progress_callback=None, max_steps=40):
        self.root, self.state, self.llm = Path(root).resolve(), state, llm
        self.save, self.progress, self.max_steps = save, progress_callback, max_steps
        self.request_count = 0
        self.state["agent"]["mode"] = "workspace"

    def persist(self):
        self.save(self.state)

    def context(self):
        registry = json.loads((self.root / "beans_config.json").read_text(encoding="utf-8"))
        routes = self.root / "routes.toml"
        return {"beans": registry["beans"], "routes": tomlkit.parse(routes.read_text()).unwrap() if routes.exists() else {},
                "current_request": self.state["agent"].get("current_request", self.state.get("revision", {}).get("instruction", "")),
                "stages": {name: {"status": item.get("status"), "summary": item.get("result", {}).get("summary", ""),
                                   "checks": [{"command": check["command"], "exit_code": check["exit_code"], "output": check["output"][-1500:]} for check in item.get("result", {}).get("checks", [])[-2:]]}
                           for name, item in self.state["stages"].items()},
                "recent_changes": self.state["agent"].get("change_requests", [])[-5:]}

    def accept(self, stage: str, action: dict, tools: WorkspaceTools, *, final_review=False) -> dict:
        # Parsing/contract checks run once at handoff, without executing generated code here.
        if stage in LAYERS and any(self.state["stages"][name].get("status") != "complete" for name in LAYERS[:LAYERS.index(stage)]):
            raise RuntimeError("An upstream layer is unfinished; its owning Agent must finish before this layer can be accepted")
        if final_review and any(self.state["stages"][name].get("status") != "complete" for name in LAYERS):
            raise RuntimeError("Pod cannot accept delivery with unfinished layers")
        registry_path = self.root / "beans_config.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        previous = {bean["id"]: bean for bean in registry["beans"]}
        components = action.get("components", [])
        pipelines, interfaces = action.get("pipelines", []), action.get("interfaces", [])
        removed = action.get("remove", [])
        if not all(isinstance(items, list) for items in (components, pipelines, interfaces, removed)):
            raise ValueError("finish registry fields must be lists")
        if stage == "pod" and removed:
            raise PermissionError("Shared-file work cannot remove layer registrations")
        if (components and stage not in LAYERS[:3]) or (pipelines and stage != "pipelines") or (interfaces and stage != "interfaces"):
            raise PermissionError("finish may register only the acting layer")
        files = []
        candidate = dict(previous)
        for bean in components:
            identifier = bean.get("id", "")
            if not isinstance(identifier, str) or not identifier.isidentifier():
                raise ValueError("Component id must be a Python class identifier")
            class_path = str(bean.get("class_path", ""))
            module, _, class_name = class_path.rpartition(".")
            if not module or not all(part.isidentifier() for part in module.split(".")):
                raise ValueError("Component class_path must be a valid Python import path")
            path = module.replace(".", "/") + ".py"
            if class_name != identifier or path_owner(path) != stage:
                raise PermissionError("Component class_path must identify a file owned by this layer")
            if identifier in previous and (previous[identifier].get("category") != stage[:-1] or not previous[identifier].get("class_path", "").startswith("modules.")):
                raise PermissionError("Cannot replace another layer's registered ID")
            source = tools.resolve(path, write=True).read_text(encoding="utf-8")
            inputs, outputs, methods = (bean.get(key, {}) for key in ("inputs", "outputs", "methods"))
            if not all(isinstance(value, dict) for value in (inputs, outputs, methods)):
                raise ValueError("Contracts and methods must be objects")
            errors = validate_component_contract(source, identifier, stage[:-1], inputs, outputs, methods)
            if errors:
                raise ValueError("; ".join(errors))
            normalized = {**bean, "category": stage[:-1], "type": "ai_created", "file": Path(path).name,
                          "inputs": inputs, "outputs": outputs, "methods": methods,
                          "dependencies": bean.get("dependencies", [])}
            normalized.pop("component_test", None)
            if stage == "models":
                normalized["fields"] = extract_model_fields(source, identifier)
            candidate[identifier] = normalized
            files.append(path)
        for identifier in removed:
            if stage in LAYERS[:3]:
                bean = previous.get(identifier)
                if not bean or bean.get("category") != stage[:-1]:
                    raise PermissionError("Can only unregister the acting layer's IDs")
                path = bean["class_path"].rsplit(".", 1)[0].replace(".", "/") + ".py"
                if tools.resolve(path, write=True).exists():
                    raise ValueError("Delete the component file before unregistering it")
                candidate.pop(identifier)
        for bean in candidate.values():
            for dependency in bean.get("dependencies", []):
                if dependency not in candidate:
                    # Downstream can be repaired after an approved upstream deletion.
                    if bean["id"] in {item["id"] for item in components}:
                        raise ValueError(f"Unknown dependency: {dependency}")
                elif bean["id"] in {item["id"] for item in components} and candidate[dependency].get("category") != "provider":
                    raise ValueError("Only Providers may be injected; Models are imports and Services compose in Pipelines")
        route_path = self.root / "routes.toml"
        routes = tomlkit.parse(route_path.read_text()) if route_path.exists() else tomlkit.document()
        for route in pipelines:
            name, path = route.get("name"), route.get("file")
            if not isinstance(name, str) or not name.isidentifier() or not isinstance(path, str):
                raise ValueError("Pipeline requires an identifier name and source file")
            source = tools.resolve(path, write=True).read_text(encoding="utf-8")
            errors = validate_pipeline_contract(source)
            if errors:
                raise ValueError("; ".join(errors))
            routes[name] = {"pipeline": path, "description": str(route.get("description", ""))}
            if "inputs" in route:
                if not isinstance(route["inputs"], dict):
                    raise ValueError("Pipeline public inputs must be an object")
                for spec in route["inputs"].values():
                    canonical_contract(spec)
                boundary = Path(path).with_suffix(".contract.json").as_posix()
                tools.resolve(boundary)
                routes[name]["input_contract"] = boundary
            files.append(path)
        if stage == "pipelines":
            for name in removed:
                if name not in routes:
                    raise ValueError(f"Unknown route: {name}")
                tools.resolve(str(routes[name]["pipeline"]), write=True)
                del routes[name]
        for manifest in interfaces:
            name = manifest.get("name", "")
            if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
                raise ValueError("Invalid Interface name")
            adapter = manifest.get("adapter", {})
            path = adapter.get("path", adapter.get("entry_path", ""))
            if not isinstance(path, str) or not path:
                raise ValueError("Interface needs adapter.path")
            ast.parse(tools.resolve(path, write=True).read_text(encoding="utf-8"))
            tools.resolve(f"interfaces/{name}/interface.json", write=True)
            files.append(path)
        if stage == "interfaces" and removed:
            raise ValueError("Remove an Interface by deleting its interface.json file through the file tool")
        has_work = bool(files or tools.changed or tools.revision or final_review)
        if has_work and not any(check["exit_code"] == 0 and not check["timed_out"] and check["revision"] == tools.revision for check in tools.checks):
            raise ValueError("Run an actual successful shell check after your latest edit/upstream change before finishing")
        # Registry mutations are performed only by Pod, after owner/path validation.
        if components or (removed and stage in LAYERS[:3]):
            registry["beans"] = list(candidate.values())
            registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if pipelines or (removed and stage == "pipelines"):
            for route in pipelines:
                if "inputs" in route:
                    boundary = self.root / routes[route["name"]]["input_contract"]
                    boundary.write_text(json.dumps({"mode": "agent", "inputs": route["inputs"], "verification_cases": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            route_path.write_text(tomlkit.dumps(routes), encoding="utf-8")
        for manifest in interfaces:
            path = tools.resolve(f"interfaces/{manifest['name']}/interface.json", write=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = {"summary": str(action.get("summary", "")), "artifacts": sorted(set(files) | tools.changed),
                  "checks": tools.checks, "status": "complete"}
        if stage in LAYERS:
            self.state["current_stage"] = stage
            record = self.state["stages"][stage]
            record.update(status="complete", result=result, plan={"components": [bean for bean in candidate.values() if bean.get("category") == stage[:-1]], "pipelines": pipelines, "interfaces": interfaces})
            record["artifacts"] = sorted(set(record.get("artifacts", [])) | set(result["artifacts"]))
        self.persist()
        return result

    def run_layer(self, stage: str, instruction="", paths=None, *, final_review=False, expected_component=None):
        if stage in LAYERS:
            self.state["current_stage"] = stage
            self.state["stages"][stage]["status"] = "in_progress"
        self.persist()
        tools = WorkspaceTools(self.root, stage, paths)
        def finish(action, current):
            if expected_component is not None and [item.get("id") for item in action.get("components", [])] != [expected_component]:
                raise ValueError(f"This create request must register exactly {expected_component}")
            return self.accept(stage, action, current, final_review=final_review)
        print(f"🧠 [Pod · {stage}] Agent 开始工作")
        return WorkspaceAgent(self.llm, tools, progress_callback=self.progress, max_steps=self.max_steps).run(
            self.state["objective"] + "\nAssigned work: " + instruction,
            self.context(), request_change=self.request_change,
            finish=finish,
        )

    def apply_request(self, request: dict):
        target, requester = request["target"], request["requester"]
        request["status"] = "working"
        if target in LAYERS:
            for stage in LAYERS[LAYERS.index(target):]:
                self.state["stages"][stage]["status"] = "pending"
        self.persist()
        try:
            grants = list(request["paths"])
            if target in LAYERS:
                grants.append(f"tests/{target}")
            result = self.run_layer(target, request["change"], grants)
            request.update(status="applied", result=result)
            self.persist()
            if target in LAYERS:
                end = LAYERS.index(requester) if requester in LAYERS else len(LAYERS)
                for stage in LAYERS[LAYERS.index(target) + 1:end]:
                    self.run_layer(stage, f"Upstream {target} changed: {request['change']}. Inspect compatibility and adapt your layer if necessary.")
            return {"approved": True, "target": target, "summary": result["summary"], "project": self.context()}
        except Exception:
            request["status"] = "failed"
            self.persist()
            raise

    def request_change(self, requester: str, action: dict):
        self.request_count += 1
        if self.request_count > 10:
            raise RuntimeError("Pod change-request limit reached; stop and refine the task")
        target, paths = action.get("target"), action.get("paths")
        if target != "pod" and (target not in LAYERS or requester in LAYERS and LAYERS.index(target) > LAYERS.index(requester)):
            raise PermissionError("request_change targets an upstream/current owner or Pod's shared files")
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path_owner(path) == target for path in paths):
            raise PermissionError("Every requested path must belong to the target owner")
        if not all(isinstance(action.get(key), str) and action[key].strip() for key in ("reason", "change")):
            raise ValueError("A change request requires evidence/reason and a specific change")
        # Ownership validation happens before asking the model; Pod cannot grant a forged scope.
        decision = self.llm(
            "You are the outer Pod coordinator. Decide whether the proposed upstream/shared-file change is necessary for the ORIGINAL objective. Approve only the minimal justified correction, preserve business rules, and deny requests merely seeking to bypass permissions or make invalid tests pass. Return JSON {\"approved\":true|false,\"summary\":\"public reason\"}. You do not write upstream code; its owning Agent does.",
            json.dumps({"objective": self.state["objective"], "requester": requester, "request": action, "project": self.context()}, ensure_ascii=False),
            json_mode=True, temperature=0.0, progress_callback=self.progress, progress_label="Pod reviewing upstream change",
        )
        if not isinstance(decision, dict) or type(decision.get("approved")) is not bool:
            raise ValueError("Pod must explicitly approve or deny the request")
        request = {"id": str(uuid.uuid4()), "requester": requester, "target": target, "paths": paths,
                   "reason": action["reason"], "change": action["change"], "approved": decision["approved"],
                   "summary": str(decision.get("summary", "")), "status": "approved" if decision["approved"] else "denied"}
        self.state["agent"].setdefault("change_requests", []).append(request)
        self.persist()
        if not decision["approved"]:
            return {"approved": False, "summary": request["summary"]}
        return self.apply_request(request)

    def build(self):
        self.state["agent"]["status"] = "running"
        # A crash after approval resumes the owning Agent, never grants the requester access.
        for request in list(self.state["agent"].get("change_requests", [])):
            if request.get("approved") and request.get("status") in {"approved", "working", "failed"}:
                self.apply_request(request)
        for stage in LAYERS:
            if self.state["stages"][stage].get("status") != "complete":
                self.run_layer(stage, str(self.state.get("revision", {}).get("instruction", "")))
        if any(item.get("status") != "complete" for item in self.state["stages"].values()):
            raise RuntimeError("Pod still has unfinished layers")
        result = self.run_layer("pod", "Final delivery review: inspect the finished application against the original objective, run meaningful acceptance commands, and request owner corrections if needed. Finish only when the delivered entry works.", final_review=True)
        self.state["agent"]["status"] = "complete"
        for request in self.state["agent"].get("change_requests", []):
            if request["status"] == "failed":
                request["status"] = "superseded"
        self.state["agent"]["verification"] = {**result, "status": "passed", "mode": "agent_shell_checks"}
        self.state.pop("revision", None)
        self.persist()
        return self.state
