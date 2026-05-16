#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3.14}"
BUILD_DIR=".lambda_build"
DIST_DIR="dist"
ZIP_FILE="${DIST_DIR}/ec2-disk-cleanup-lambda.zip"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Missing Python interpreter: ${PYTHON_BIN}" >&2
  exit 1
fi

rm -rf "${BUILD_DIR}" "${ZIP_FILE}"
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

"${PYTHON_BIN}" -m pip install -r requirements.txt -t "${BUILD_DIR}"
cp ec2_disk_cleanup.py "${BUILD_DIR}/ec2_disk_cleanup.py"

find "${BUILD_DIR}" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "${BUILD_DIR}" -type f -name '*.pyc' -delete

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${BUILD_DIR}" "${PYTHON_BIN}" -c "import ec2_disk_cleanup; import boto3"

(
  cd "${BUILD_DIR}"
  zip -qr "../${ZIP_FILE}" .
)

if ! unzip -l "${ZIP_FILE}" | grep -q 'ec2_disk_cleanup.py'; then
  echo "Package verification failed: ec2_disk_cleanup.py missing from ${ZIP_FILE}" >&2
  exit 1
fi

ls -lh "${ZIP_FILE}"
