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

# Copy plugin source.
# Trailing slash on __pycache__/ tells rsync to match directories only.
rsync -a \
      --exclude='__pycache__/' \
      --exclude='*.pyc' \
      --exclude='*.pyo' \
      --exclude='.DS_Store' \
      --exclude='backend/weights/*.pt' \
      --exclude='backend/weights/*.pth' \
      --exclude='backend/weights/*.safetensors' \
      plugin/ "${STAGING}/"

# Belt-and-suspenders: purge any artefacts rsync may have missed.
find "${STAGING}" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "${STAGING}" -name '*.pyc' -delete 2>/dev/null || true
find "${STAGING}" -name '*.pyo' -delete 2>/dev/null || true
# Model weights are large and downloaded at runtime — never ship them.
find "${STAGING}/backend/weights" \
     \( -name '*.pt' -o -name '*.pth' -o -name '*.safetensors' \) \
     -delete 2>/dev/null || true

# Ensure the weights directory placeholder is present.
mkdir -p "${STAGING}/backend/weights"
touch    "${STAGING}/backend/weights/.gitkeep"

# Copy repo-level docs into the zip.
cp README.md LICENSE "${STAGING}/"

# Report staging size before zipping so bloat is immediately visible.
echo "   Staging size: $(du -sh "${STAGING}" | cut -f1)"

# Create the zip (top-level folder = plugin name, required by QGIS).
cd "${OUT_DIR}"
zip -r "${PLUGIN_NAME}-${VERSION}.zip" "${PLUGIN_NAME}"
cd ..

# Clean staging.
rm -rf "${STAGING}"

ZIP="${OUT_DIR}/${PLUGIN_NAME}-${VERSION}.zip"
echo "✓ Created ${ZIP} ($(du -sh "${ZIP}" | cut -f1))"
