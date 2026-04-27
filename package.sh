#!/usr/bin/env bash
# Build a QGIS-installable zip for AITracer by LAD.
# Usage: ./package.sh [version]
# Output: dist/aitracer-<version>.zip

set -euo pipefail

VERSION="${1:-$(grep '^version=' plugin/metadata.txt | cut -d= -f2)}"
PLUGIN_NAME="aitracer"
OUT_DIR="dist"
STAGING="${OUT_DIR}/${PLUGIN_NAME}"

echo "→ Packaging AITracer v${VERSION}"

# Clean staging area
rm -rf "${STAGING}"
mkdir -p "${STAGING}"

# Copy plugin source (exclude dev/cache artefacts)
rsync -a --exclude='__pycache__' \
         --exclude='*.pyc' \
         --exclude='*.pyo' \
         --exclude='.DS_Store' \
         --exclude='backend/weights/*.pt' \
         --exclude='backend/weights/*.pth' \
         --exclude='backend/__pycache__' \
         plugin/ "${STAGING}/"

# Ensure weights placeholder exists
mkdir -p "${STAGING}/backend/weights"
touch    "${STAGING}/backend/weights/.gitkeep"

# Copy repo-level docs into the zip
cp README.md LICENSE "${STAGING}/"

# Create the zip (top-level folder = plugin name, required by QGIS)
cd "${OUT_DIR}"
zip -r "${PLUGIN_NAME}-${VERSION}.zip" "${PLUGIN_NAME}" -x "*.DS_Store"
cd ..

# Clean staging
rm -rf "${STAGING}"

echo "✓ Created ${OUT_DIR}/${PLUGIN_NAME}-${VERSION}.zip"
