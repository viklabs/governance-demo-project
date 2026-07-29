# Simplified governed car ETL demo

This repository is intentionally a normal Databricks workload. It contains no registry database code, approval logic, tag-assignment implementation, or governance web application.

## Workload

The Bundle deploys a serverless job named:

```text
<project_id>-car-data-refresh
```

The job refreshes:

```text
it_dev.sample_project_v3.car_makes
it_dev.sample_project_v3.car_models
it_prod.sample_project_v3.car_makes
it_prod.sample_project_v3.car_models
```

The `sample_project_v3` schemas must be created once by an administrator before the first run. The ETL identity then needs only schema-level table permissions.

## Required Bundle tags

The job declares the two mandatory tags:

```yaml
tags:
  project_tag: ${var.project_id}
  environment: ${var.environment_name}
```

The central `governance-ci` package validates the resolved Bundle before deployment, reads the deployed job tags back, discovers the registered schema and its tables, applies/reads their Unity Catalog tags, and sends evidence to the App.

## Branch-neutral behavior

The same Git branch may be used for dev and prod. Branch/ref is optional display metadata. The release and production authorization use the exact full Git revision instead.

## GitHub demo flow

1. Register this repository in the App using `project-registration.json` or the UI.
2. Copy the generated project ID into the `GOVERNANCE_PROJECT_ID` repository variable.
3. Create a release in the App for the full commit SHA.
4. Run **01 - Validate exact release in it-dev** with the release ID and SHA.
5. Confirm the release becomes `READY_FOR_APPROVAL` and review all tag evidence.
6. Approve it in the App.
7. Run **02 - Deploy App-approved release to it-prod** with the same release ID and SHA.
8. Run `sql/verify.sql`.


Remove `project_tag` from `resources/car_demo.job.yml`, commit the change, create a new release, and run Workflow 01. Bundle policy must fail and the release must become `CHECKS_FAILED`; it cannot appear in the approval queue.


## Production reporting recovery

The production workflow always uploads `governance-prod-result.json`. If the Databricks deployment and tag verification finished but the final App request failed temporarily, rerun the report with the protected production identity:

```bash
governance-ci complete-prod \
  --app-url "$GOVERNANCE_APP_URL" \
  --release-id "$RELEASE_ID" \
  --result-file governance-prod-result.json
```

If the production process stops after authorization and no valid result file exists, an App administrator must mark the release failed in the release page. Create a fresh release before retrying production.
