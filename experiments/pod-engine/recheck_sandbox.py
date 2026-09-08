"""Regression: the new verifier must reject the unchanged, known-broken engine.

Run with the same Python environment as the engine:
python experiments/pod-engine/recheck_sandbox.py /path/to/generated-project
Exit zero means detection worked; it does not mean the engine passed acceptance.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from ai_pod_cli.contracts import analyze_pipeline_contracts
from ai_pod_cli.pod.verification import _application_verification_issues
from ai_pod_cli.sandbox import verify_pipeline_candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    source = args.project.resolve()
    experiment = Path(__file__).resolve().parent
    root = Path.cwd()
    out = experiment / "results/sandbox-fix"
    out.mkdir(parents=True, exist_ok=True)
    os.environ.update(SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy", PYGAME_HIDE_SUPPORT_PROMPT="1")
    with tempfile.TemporaryDirectory(prefix="aipod-sandbox-regression-") as tmp:
        project = Path(tmp) / "project"
        shutil.copytree(source, project, ignore=shutil.ignore_patterns("__pycache__", "*.log", "*.png"))
        (project / "tests").mkdir(exist_ok=True)
        shutil.copy2(experiment / "test_behavior_acceptance.py", project / "tests/test_behavior.py")
        def redact(text):
            text = text.replace(str(project), "<engine-copy>").replace(str(root), "<aipod-checkout>")
            return re.sub(r'/private/var/folders/[^"\s]+/aipod_pipeline_[^/]+', '<pipeline-sandbox>', text)
        beans = json.loads((project / "beans_config.json").read_text())
        contract = analyze_pipeline_contracts(
            ["BreakoutSimulationService", "PngSnapshotService", "RunSummaryService"], beans["beans"],
        )
        errors = verify_pipeline_candidate(
            project, (project / "pipelines/run_headless_breakout.py").read_text(), contract["inputs"],
            cases=[{"name": "default_headless_120", "params": {
                "max_frames": 120, "seed": 7, "screenshot_path": "frame.png", "summary_path": "summary.json",
            }}],
        )
        completed = subprocess.run(
            [sys.executable, "-m", "ai_pod_cli.behavior_tests", "tests/test_behavior.py"],
            cwd=project, capture_output=True, text=True, timeout=30,
        )
        (out / "behavior-proof.json").write_text(redact(completed.stdout))
        (out / "behavior-stderr.log").write_text(redact(completed.stderr))
        state = json.loads((project / "aipod_plan.json").read_text())
        try:
            os.chdir(project)
            old_issues = _application_verification_issues(state, require_files=False)
        finally:
            os.chdir(root)
        proof = json.loads(completed.stdout)
        failed = {row["id"].rsplit(".", 1)[-1] for row in proof["tests"] if row["outcome"] != "passed"}
        checks = {
            "original_contract_compatible": contract["valid"],
            "missing_fields_not_synthesized": bool(errors) and all(
                name in "\n".join(errors)
                for name in ("total_bricks", "remaining_bricks", "ball_position", "paddle_position")
            ),
            "smoke_only_plan_rejected": any(item["code"] == "missing_behavior_verification" for item in old_issues),
            "behavior_failure_nonzero": completed.returncode != 0 and proof["status"] == "failed",
            "eight_real_tests_executed": proof["tests_run"] == 8,
            "three_known_failures_detected": failed == {"test_brick", "test_default_headless", "test_held_pause"},
        }
        report = {
            "purpose": "Detection regression, not a successful engine build",
            "manual_engine_source_edits": False, "detection_checks": checks,
            "detection_status": "passed" if all(checks.values()) else "failed",
            "explicit_entry_sandbox": {"status": "failed" if errors else "passed", "errors": errors},
            "legacy_smoke_plan_issues": old_issues, "behavior_exit_code": completed.returncode,
            "behavior": {key: proof[key] for key in (
                "status", "tests_run", "successful_tests", "failures", "errors", "route_calls", "assertions", "tests",
            )},
        }
        text = redact(json.dumps(report, ensure_ascii=False, indent=2))
        (out / "summary.json").write_text(text + "\n")
        print(json.dumps({"detection_status": report["detection_status"], "checks": checks,
                          "behavior_status": proof["status"], "tests_run": proof["tests_run"],
                          "successful_tests": proof["successful_tests"]}, ensure_ascii=False, indent=2))
        return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
