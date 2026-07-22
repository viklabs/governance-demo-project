#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlparse

from databricks import sql


PROJECT_ACTIVE = "ACTIVE"
RELEASE_VALIDATING = "VALIDATING"
RELEASE_CHECKS_FAILED = "CHECKS_FAILED"
RELEASE_READY = "READY_FOR_APPROVAL"
RELEASE_APPROVED = "APPROVED"
RELEASE_DEPLOYING = "DEPLOYING"
RELEASE_DEPLOYED = "DEPLOYED"
RELEASE_FAILED = "DEPLOY_FAILED"


class GateError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise GateError(f"Invalid {label}: {value!r}")
    return value


def normalize_repository(value: str) -> str:
    text = value.strip()
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text.split(":", 1)[1]
    parsed = urlparse(text)
    if parsed.hostname and parsed.hostname.lower() != "github.com":
        raise GateError("This v2 demo supports github.com repositories only.")
    path = parsed.path.strip("/") if parsed.hostname else text.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        raise GateError("Repository must be owner/repository or a GitHub repository URL.")
    return f"{parts[0].lower()}/{parts[1].lower()}"


def validate_sha(value: str) -> str:
    sha = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise GateError("commit-sha must be a full 40- or 64-character Git commit ID.")
    return sha


def load_tag_results(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"passed": False, "detail": "No tag evidence file was available.", "results": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise GateError("Tag result JSON must be an object containing a results list.")
    return value


class Registry:
    def __init__(self) -> None:
        host = os.environ.get("REGISTRY_DATABRICKS_HOST", "").strip()
        token = os.environ.get("REGISTRY_DATABRICKS_TOKEN", "").strip()
        warehouse = os.environ.get("REGISTRY_WAREHOUSE_ID", "").strip()
        if not host or not token or not warehouse:
            raise GateError(
                "REGISTRY_DATABRICKS_HOST, REGISTRY_DATABRICKS_TOKEN, and "
                "REGISTRY_WAREHOUSE_ID are required."
            )
        self.catalog = validate_identifier(os.environ.get("REGISTRY_CATALOG", "it_dev"), "registry catalog")
        self.schema = validate_identifier(
            os.environ.get("REGISTRY_SCHEMA", "project_registry"), "registry schema"
        )
        parsed = urlparse(host if "://" in host else f"https://{host}")
        self.server_hostname = parsed.hostname or host.replace("https://", "").strip("/")
        self.http_path = f"/sql/1.0/warehouses/{warehouse}"
        self.token = token

    def table(self, name: str) -> str:
        return f"`{self.catalog}`.`{self.schema}`.`{validate_identifier(name, 'table name')}`"

    @property
    def projects(self) -> str:
        return self.table("governed_projects")

    @property
    def cicd(self) -> str:
        return self.table("governed_project_cicd")

    @property
    def releases(self) -> str:
        return self.table("governed_releases")

    @property
    def tags(self) -> str:
        return self.table("governed_resource_tags")

    @property
    def audit_table(self) -> str:
        return self.table("governance_audit")

    @contextmanager
    def connection(self) -> Iterator[Any]:
        connection = sql.connect(
            server_hostname=self.server_hostname,
            http_path=self.http_path,
            access_token=self.token,
            catalog=self.catalog,
            schema=self.schema,
            _use_arrow_native_complex_types=False,
        )
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def rows(cursor: Any, values: Sequence[Any]) -> list[dict[str, Any]]:
        columns = [item[0] if isinstance(item, tuple) else item.name for item in (cursor.description or [])]
        return [dict(zip(columns, row, strict=False)) for row in values]

    def execute(self, statement: str, parameters: Mapping[str, Any] | None = None) -> None:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters or {})

    def fetch_one(self, statement: str, parameters: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters or {})
            values = self.rows(cursor, cursor.fetchall())
            return values[0] if values else None

    def current_user(self) -> str:
        row = self.fetch_one("SELECT current_user() AS current_user")
        return str((row or {}).get("current_user") or "unknown-ci-identity")

    def release(self, release_id: str) -> dict[str, Any] | None:
        return self.fetch_one(
            f"""
            SELECT r.*, p.name AS project_name, p.lifecycle_status,
                   c.repository_slug, c.bundle_path, c.dev_branch, c.prod_branch,
                   c.dev_target, c.prod_target
            FROM {self.releases} r
            JOIN {self.projects} p ON r.project_id = p.project_id
            JOIN {self.cicd} c ON r.project_id = c.project_id
            WHERE r.release_id = :release_id
            LIMIT 1
            """,
            {"release_id": release_id},
        )

    def checked_release(self, release_id: str, commit_sha: str, repository: str) -> dict[str, Any]:
        release = self.release(release_id)
        if not release:
            raise GateError("Release not found in the v2 Project Registry.")
        sha = validate_sha(commit_sha)
        repo = normalize_repository(repository)
        if str(release["commit_sha"]).lower() != sha:
            raise GateError("Commit SHA does not match the registered release.")
        if str(release["repository_slug"]).lower() != repo:
            raise GateError("GitHub repository does not match the registered project.")
        if str(release["source_branch"]) != str(release["prod_branch"]):
            raise GateError("Release source branch does not match the configured production branch.")
        if str(release["lifecycle_status"]) != PROJECT_ACTIVE:
            raise GateError("Project is not ACTIVE.")
        return release

    def audit(
        self,
        *,
        event_type: str,
        release: Mapping[str, Any],
        actor: str,
        detail: Mapping[str, Any],
    ) -> None:
        self.execute(
            f"""
            INSERT INTO {self.audit_table} (
              event_id, entity_type, entity_id, project_id, release_id,
              event_type, actor, detail_json, created_at
            ) VALUES (
              :event_id, 'RELEASE', :entity_id, :project_id, :release_id,
              :event_type, :actor, :detail_json, :created_at
            )
            """,
            {
                "event_id": f"EVT-{uuid.uuid4().hex[:12].upper()}",
                "entity_id": release["release_id"],
                "project_id": release["project_id"],
                "release_id": release["release_id"],
                "event_type": event_type,
                "actor": actor,
                "detail_json": json.dumps(dict(detail), sort_keys=True, default=str),
                "created_at": utc_now(),
            },
        )

    def replace_tag_results(
        self,
        release: Mapping[str, Any],
        environment: str,
        results: Sequence[Mapping[str, Any]],
        actor: str,
    ) -> None:
        self.execute(
            f"DELETE FROM {self.tags} WHERE release_id = :release_id AND environment = :environment",
            {"release_id": release["release_id"], "environment": environment},
        )
        checked_at = utc_now()
        for item in results:
            self.execute(
                f"""
                INSERT INTO {self.tags} (
                  tag_record_id, release_id, project_id, environment, resource_type,
                  logical_name, resource_identifier, tag_key, expected_value, actual_value,
                  verification_mode, tag_status, detail, checked_at, checked_by
                ) VALUES (
                  :tag_record_id, :release_id, :project_id, :environment, :resource_type,
                  :logical_name, :resource_identifier, :tag_key, :expected_value, :actual_value,
                  :verification_mode, :tag_status, :detail, :checked_at, :checked_by
                )
                """,
                {
                    "tag_record_id": f"TAG-{uuid.uuid4().hex[:12].upper()}",
                    "release_id": release["release_id"],
                    "project_id": release["project_id"],
                    "environment": environment,
                    "resource_type": str(item.get("resource_type") or ""),
                    "logical_name": str(item.get("logical_name") or ""),
                    "resource_identifier": str(item.get("resource_identifier") or ""),
                    "tag_key": str(item.get("tag_key") or ""),
                    "expected_value": str(item.get("expected_value") or ""),
                    "actual_value": str(item.get("actual_value") or ""),
                    "verification_mode": str(item.get("verification_mode") or ""),
                    "tag_status": str(item.get("status") or "FAIL"),
                    "detail": str(item.get("detail") or "")[:12000],
                    "checked_at": checked_at,
                    "checked_by": actor,
                },
            )


def emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), indent=2, sort_keys=True, default=str))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Minimal CI client for the v2 Project Registry App.")
    commands = root.add_subparsers(dest="command", required=True)

    def identity(command: argparse.ArgumentParser) -> None:
        command.add_argument("--release-id", required=True)
        command.add_argument("--commit-sha", required=True)
        command.add_argument("--repository", required=True)

    info = commands.add_parser("release-info")
    identity(info)

    dev = commands.add_parser("record-dev")
    identity(dev)
    dev.add_argument("--tag-results", type=Path, required=True)
    dev.add_argument("--run-url", default="")

    fail = commands.add_parser("fail-dev")
    identity(fail)
    fail.add_argument("--message", required=True)
    fail.add_argument("--run-url", default="")
    fail.add_argument("--tag-results", type=Path)

    auth = commands.add_parser("authorize-prod")
    identity(auth)

    complete = commands.add_parser("complete-prod")
    identity(complete)
    complete.add_argument("--status", choices=("DEPLOYED", "FAILED"), required=True)
    complete.add_argument("--tag-results", type=Path)
    complete.add_argument("--run-url", default="")
    complete.add_argument("--message", default="")

    return root


def main() -> int:
    args = parser().parse_args()
    try:
        registry = Registry()
        actor = registry.current_user()
        release = registry.checked_release(args.release_id, args.commit_sha, args.repository)

        if args.command == "release-info":
            emit(
                {
                    "release_id": release["release_id"],
                    "project_id": release["project_id"],
                    "project_name": release["project_name"],
                    "repository": release["repository_slug"],
                    "commit_sha": release["commit_sha"],
                    "bundle_path": release["bundle_path"],
                    "dev_target": release["dev_target"],
                    "prod_target": release["prod_target"],
                    "prod_branch": release["prod_branch"],
                    "release_status": release["release_status"],
                }
            )
            return 0

        if args.command in {"record-dev", "fail-dev"}:
            if release["release_status"] not in {
                RELEASE_VALIDATING,
                RELEASE_CHECKS_FAILED,
                RELEASE_READY,
            }:
                raise GateError("Dev validation can update only an unapproved release.")
            evidence = load_tag_results(args.tag_results)
            passed = args.command == "record-dev" and bool(evidence.get("passed"))
            detail = (
                str(evidence.get("detail") or "")
                if args.command == "record-dev"
                else str(args.message)
            )
            registry.replace_tag_results(release, "dev", evidence.get("results") or [], actor)
            now = utc_now()
            registry.execute(
                f"""
                UPDATE {registry.releases}
                SET release_status = :release_status,
                    dev_deployment_status = :dev_status,
                    dev_tag_check_passed = :passed,
                    dev_tag_check_detail = :detail,
                    dev_deployment_run_url = :run_url,
                    dev_deployed_at = :deployed_at,
                    approved_at = NULL,
                    approved_by = NULL,
                    decision_comment = NULL,
                    updated_at = :updated_at,
                    updated_by = :actor
                WHERE release_id = :release_id
                """,
                {
                    "release_status": RELEASE_READY if passed else RELEASE_CHECKS_FAILED,
                    "dev_status": "DEPLOYED" if passed else "FAILED",
                    "passed": passed,
                    "detail": detail[:12000],
                    "run_url": args.run_url,
                    "deployed_at": now,
                    "updated_at": now,
                    "actor": actor,
                    "release_id": release["release_id"],
                },
            )
            registry.audit(
                event_type="DEV_VALIDATION_PASSED" if passed else "DEV_VALIDATION_FAILED",
                release=release,
                actor=actor,
                detail={"detail": detail, "run_url": args.run_url},
            )
            emit({"passed": passed, "release_id": release["release_id"]})
            return 0 if passed else 2

        if args.command == "authorize-prod":
            if release["release_status"] != RELEASE_APPROVED:
                raise GateError("Release is not APPROVED in the Project Registry App.")
            if release["dev_deployment_status"] != "DEPLOYED" or not bool(
                release["dev_tag_check_passed"]
            ):
                raise GateError("The exact commit has not passed dev deployment and tag validation.")
            now = utc_now()
            registry.execute(
                f"""
                UPDATE {registry.releases}
                SET release_status = :new_status,
                    prod_deployment_status = 'DEPLOYING',
                    updated_at = :updated_at,
                    updated_by = :actor
                WHERE release_id = :release_id AND release_status = :old_status
                """,
                {
                    "new_status": RELEASE_DEPLOYING,
                    "updated_at": now,
                    "actor": actor,
                    "release_id": release["release_id"],
                    "old_status": RELEASE_APPROVED,
                },
            )
            updated = registry.release(release["release_id"])
            if not updated or updated["release_status"] != RELEASE_DEPLOYING:
                raise GateError("Production authorization was not acquired; the release changed concurrently.")
            registry.audit(
                event_type="PROD_DEPLOYMENT_AUTHORIZED",
                release=release,
                actor=actor,
                detail={"commit_sha": release["commit_sha"], "repository": release["repository_slug"]},
            )
            emit(
                {
                    "authorized": True,
                    "release_id": release["release_id"],
                    "project_id": release["project_id"],
                    "prod_target": release["prod_target"],
                    "bundle_path": release["bundle_path"],
                }
            )
            return 0

        if args.command == "complete-prod":
            current = registry.release(release["release_id"])
            if not current or current["release_status"] != RELEASE_DEPLOYING:
                raise GateError("Release is not in DEPLOYING state.")
            evidence = load_tag_results(args.tag_results)
            deployment_ok = args.status == "DEPLOYED"
            tags_ok = bool(evidence.get("passed")) if deployment_ok else False
            success = deployment_ok and tags_ok
            registry.replace_tag_results(current, "prod", evidence.get("results") or [], actor)
            now = utc_now()
            message = args.message
            if deployment_ok and not tags_ok:
                message = f"Deployment completed, but required tags failed verification. {message}".strip()
            registry.execute(
                f"""
                UPDATE {registry.releases}
                SET release_status = :release_status,
                    prod_deployment_status = :prod_status,
                    prod_tag_check_passed = :tags_ok,
                    prod_tag_check_detail = :tag_detail,
                    prod_deployment_run_url = :run_url,
                    prod_deployed_at = :deployed_at,
                    deployment_message = :message,
                    updated_at = :updated_at,
                    updated_by = :actor
                WHERE release_id = :release_id AND release_status = :expected_status
                """,
                {
                    "release_status": RELEASE_DEPLOYED if success else RELEASE_FAILED,
                    "prod_status": "DEPLOYED" if success else "FAILED",
                    "tags_ok": tags_ok,
                    "tag_detail": str(evidence.get("detail") or "")[:12000],
                    "run_url": args.run_url,
                    "deployed_at": now,
                    "message": message[:12000],
                    "updated_at": now,
                    "actor": actor,
                    "release_id": release["release_id"],
                    "expected_status": RELEASE_DEPLOYING,
                },
            )
            registry.audit(
                event_type="PROD_DEPLOYED" if success else "PROD_DEPLOYMENT_FAILED",
                release=release,
                actor=actor,
                detail={"message": message, "run_url": args.run_url, "tags_ok": tags_ok},
            )
            emit({"release_id": release["release_id"], "deployed": success, "tags_passed": tags_ok})
            return 0 if success else 2

        raise GateError(f"Unsupported command: {args.command}")
    except Exception as exc:
        print(json.dumps({"error": str(exc), "command": args.command}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
