#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return value


def tags(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items()}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the demo Bundle before deployment.")
    parser.add_argument("--resolved-json", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--environment", choices=("dev", "prod"), required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schema", default="sample_project")
    args = parser.parse_args()

    resolved = load_json(args.resolved_json)
    resources = resolved.get("resources") or {}
    if not isinstance(resources, Mapping):
        raise ValueError("Resolved Bundle resources must be a mapping.")

    failures: list[str] = []
    allowed_types = {"jobs"}
    for resource_type, collection in resources.items():
        if resource_type not in allowed_types and collection:
            failures.append(f"Unexpected resource type in this demo: {resource_type}")

    jobs = resources.get("jobs") or {}
    if not isinstance(jobs, Mapping) or not jobs:
        failures.append("The Bundle must define at least one job.")
    expected = {"project_tag": args.project_id, "environment": args.environment}

    for logical_name, job in jobs.items() if isinstance(jobs, Mapping) else []:
        if not isinstance(job, Mapping):
            failures.append(f"jobs.{logical_name} is not an object")
            continue
        actual = tags(job.get("tags"))
        for key, value in expected.items():
            if actual.get(key) != value:
                failures.append(
                    f"jobs.{logical_name}.tags.{key} must be {value!r}; got {actual.get(key)!r}"
                )
        tasks = job.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            failures.append(f"jobs.{logical_name} must contain a task")
            continue
        for index, task in enumerate(tasks):
            if not isinstance(task, Mapping):
                failures.append(f"jobs.{logical_name}.tasks[{index}] is not an object")
                continue
            forbidden = [key for key in ("new_cluster", "existing_cluster_id", "job_cluster_key") if key in task]
            if forbidden:
                failures.append(
                    f"jobs.{logical_name}.tasks[{index}] must use serverless compute; found {forbidden}"
                )
            if "spark_python_task" in task and not str(task.get("environment_key") or "").strip():
                failures.append(
                    f"jobs.{logical_name}.tasks[{index}] requires environment_key for serverless Python"
                )

    payload = {
        "passed": not failures,
        "project_id": args.project_id,
        "environment": args.environment,
        "catalog": args.catalog,
        "schema": args.schema,
        "failures": failures,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
