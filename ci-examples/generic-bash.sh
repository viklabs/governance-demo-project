#!/usr/bin/env bash
set -euo pipefail

: "${GOVERNANCE_APP_URL:?}"
: "${RELEASE_ID:?}"
: "${SOURCE_REVISION:?}"

python -m pip install "${GOVERNANCE_CI_PACKAGE:?}"
governance-ci validate-release \
  --app-url "$GOVERNANCE_APP_URL" \
  --release-id "$RELEASE_ID" \
  --source-revision "$SOURCE_REVISION" \
  --source-dir . \
  --run-url "${CI_RUN_URL:-}"
