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


def find_bundle_resource(root: Mapping[str, Any], resource_type: str, logical_name: str) -> Mapping[str, Any]:
    queue: list[Mapping[str, Any]] = [root]
    while queue:
        current = queue.pop(0)
        resources = current.get("resources")
        if isinstance(resources, Mapping):
            collection = resources.get(resource_type)
            if isinstance(collection, Mapping):
                item = collection.get(logical_name)
                if isinstance(item, Mapping):
                    return item
        for value in current.values():
            if isinstance(value, Mapping):
                queue.append(value)
    return {}


def first_text(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return ""


def find_existing(items: Any, tag_key: str) -> Any | None:
    for item in items:
        if str(getattr(item, "tag_key", "")) == tag_key:
            return item
    return None


def assign_uc_tag(client: Any, entity_type: str, entity_name: str, tag_key: str, tag_value: str) -> str:
    from databricks.sdk.service.catalog import EntityTagAssignment

    api = client.entity_tag_assignments
    existing = find_existing(api.list(entity_type, entity_name), tag_key)
    assignment = EntityTagAssignment(
        entity_type=entity_type,
        entity_name=entity_name,
        tag_key=tag_key,
        tag_value=tag_value,
    )
    if existing is None:
        api.create(assignment)
    elif str(getattr(existing, "tag_value", "")) != tag_value:
        api.update(entity_type, entity_name, tag_key, assignment, "tag_value")
    verified = api.get(entity_type, entity_name, tag_key)
    return str(getattr(verified, "tag_value", "") or "")


def result(
    *,
    resource_type: str,
    logical_name: str,
    identifier: str,
    tag_key: str,
    expected: str,
    actual: str,
    mode: str,
    detail: str,
) -> dict[str, str]:
    return {
        "resource_type": resource_type,
        "logical_name": logical_name,
        "resource_identifier": identifier,
        "tag_key": tag_key,
        "expected_value": expected,
        "actual_value": actual,
        "verification_mode": mode,
        "status": "PASS" if actual == expected else "FAIL",
        "detail": detail,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply and verify tags on the demo job, schema, and tables.")
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--environment", choices=("dev", "prod"), required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--schema", default="sample_project")
    parser.add_argument("--job-key", default="car_data_refresh")
    parser.add_argument("--table", action="append", dest="tables", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    expected_tags = {"project_tag": args.project_id, "environment": args.environment}
    results: list[dict[str, str]] = []
    failures: list[str] = []

    try:
        from databricks.sdk import WorkspaceClient

        client = WorkspaceClient()
        summary = load_json(args.summary_json)
        job_summary = find_bundle_resource(summary, "jobs", args.job_key)
        job_id = first_text(job_summary, "id", "resource_id", "job_id")
        if not job_id:
            raise RuntimeError(
                f"Could not find the deployed job ID for resources.jobs.{args.job_key} in bundle summary."
            )

        job = client.jobs.get(int(job_id))
        job_tags = dict(getattr(getattr(job, "settings", None), "tags", {}) or {})
        job_name = str(getattr(getattr(job, "settings", None), "name", "") or args.job_key)
        for tag_key, tag_value in expected_tags.items():
            actual = str(job_tags.get(tag_key, ""))
            item = result(
                resource_type="jobs",
                logical_name=args.job_key,
                identifier=job_id,
                tag_key=tag_key,
                expected=tag_value,
                actual=actual,
                mode="DATABRICKS_JOBS_API",
                detail=f"Read actual tag from deployed job {job_name!r}.",
            )
            results.append(item)
            if item["status"] != "PASS":
                failures.append(f"Job {job_id} has {tag_key}={actual!r}; expected {tag_value!r}")

        schema_full_name = f"{args.catalog}.{args.schema}"
        for tag_key, tag_value in expected_tags.items():
            try:
                actual = assign_uc_tag(client, "schemas", schema_full_name, tag_key, tag_value)
                item = result(
                    resource_type="schemas",
                    logical_name=args.schema,
                    identifier=schema_full_name,
                    tag_key=tag_key,
                    expected=tag_value,
                    actual=actual,
                    mode="UNITY_CATALOG_TAG_API",
                    detail="Applied and read back the schema tag.",
                )
            except Exception as exc:
                item = result(
                    resource_type="schemas",
                    logical_name=args.schema,
                    identifier=schema_full_name,
                    tag_key=tag_key,
                    expected=tag_value,
                    actual="",
                    mode="UNITY_CATALOG_TAG_API",
                    detail=str(exc),
                )
            results.append(item)
            if item["status"] != "PASS":
                failures.append(f"Schema {schema_full_name} tag {tag_key} failed: {item['detail']}")

        for table_name in args.tables:
            table_full_name = f"{args.catalog}.{args.schema}.{table_name}"
            for tag_key, tag_value in expected_tags.items():
                try:
                    actual = assign_uc_tag(client, "tables", table_full_name, tag_key, tag_value)
                    item = result(
                        resource_type="tables",
                        logical_name=table_name,
                        identifier=table_full_name,
                        tag_key=tag_key,
                        expected=tag_value,
                        actual=actual,
                        mode="UNITY_CATALOG_TAG_API",
                        detail="Applied and read back the table tag.",
                    )
                except Exception as exc:
                    item = result(
                        resource_type="tables",
                        logical_name=table_name,
                        identifier=table_full_name,
                        tag_key=tag_key,
                        expected=tag_value,
                        actual="",
                        mode="UNITY_CATALOG_TAG_API",
                        detail=str(exc),
                    )
                results.append(item)
                if item["status"] != "PASS":
                    failures.append(f"Table {table_full_name} tag {tag_key} failed: {item['detail']}")
    except Exception as exc:
        failures.append(str(exc))

    payload = {
        "passed": not failures and bool(results),
        "project_id": args.project_id,
        "environment": args.environment,
        "detail": (
            f"Verified {len(results)} tag assignments across the job, schema, and two tables."
            if not failures
            else "; ".join(failures)
        ),
        "results": results,
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
