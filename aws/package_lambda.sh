#!/bin/bash
# aws/package_lambda.sh — Build a Lambda deployment ZIP
#
# Usage:
#   cd MomentumStockScreener
#   bash aws/package_lambda.sh
#
# Output: aws/lambda_package.zip  (~15-30 MB with dependencies)
#
# What this does:
#   1. Installs Python dependencies into a local package/ directory
#   2. Copies all screener source files
#   3. Copies the Lambda handler
#   4. ZIPs everything into lambda_package.zip
#   5. Cleans up the temp directory

set -e   # exit on any error

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Pre-Market Screener — Lambda Package Builder"
echo "═══════════════════════════════════════════════════"
echo ""

# ── Paths ─────────────────────────────────────────────
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$( dirname "$SCRIPT_DIR" )"
BUILD_DIR="$SCRIPT_DIR/build"
OUTPUT_ZIP="$SCRIPT_DIR/lambda_package.zip"

echo "Project dir : $PROJECT_DIR"
echo "Build dir   : $BUILD_DIR"
echo "Output ZIP  : $OUTPUT_ZIP"
echo ""

# ── Clean previous build ──────────────────────────────
echo "▶ Cleaning previous build..."
rm -rf "$BUILD_DIR"
rm -f  "$OUTPUT_ZIP"
mkdir -p "$BUILD_DIR"

# ── Install dependencies into build/ ─────────────────
echo "▶ Installing Python dependencies..."
pip install \
    --target "$BUILD_DIR" \
    --upgrade \
    --quiet \
    pandas \
    requests \
    python-dotenv \
    yfinance \
    boto3 \
    twilio

echo "   ✓ Dependencies installed"

# ── Copy screener source files ────────────────────────
echo "▶ Copying screener source..."

# Core modules
cp -r "$PROJECT_DIR/core"    "$BUILD_DIR/core"
cp -r "$PROJECT_DIR/signals" "$BUILD_DIR/signals"
cp -r "$PROJECT_DIR/utils"   "$BUILD_DIR/utils"

# Config and entry points
cp "$PROJECT_DIR/config.py" "$BUILD_DIR/config.py"
cp "$PROJECT_DIR/main.py"   "$BUILD_DIR/main.py"

# Lambda handler goes to root of ZIP (Lambda looks for it there)
cp "$SCRIPT_DIR/lambda_handler.py" "$BUILD_DIR/lambda_handler.py"

echo "   ✓ Source files copied"

# ── Remove unnecessary files to keep ZIP small ────────
echo "▶ Pruning build directory..."
find "$BUILD_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -type d -name "*.egg-info"  -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "*.pyc"               -delete 2>/dev/null || true
find "$BUILD_DIR" -name "test_*.py"           -delete 2>/dev/null || true
find "$BUILD_DIR" -name "diagnose.py"         -delete 2>/dev/null || true
# Remove large unused pandas test data
find "$BUILD_DIR" -path "*/pandas/tests*"     -exec rm -rf {} + 2>/dev/null || true

echo "   ✓ Build pruned"

# ── Create ZIP ────────────────────────────────────────
echo "▶ Creating ZIP..."
cd "$BUILD_DIR"
zip -r "$OUTPUT_ZIP" . --quiet
cd "$PROJECT_DIR"

ZIP_SIZE=$(du -sh "$OUTPUT_ZIP" | cut -f1)
echo "   ✓ ZIP created: $OUTPUT_ZIP ($ZIP_SIZE)"

# ── Cleanup ───────────────────────────────────────────
echo "▶ Cleaning up build directory..."
rm -rf "$BUILD_DIR"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✓ Done!  lambda_package.zip is ready to upload"
echo "═══════════════════════════════════════════════════"
echo ""
echo "Next steps:"
echo "  1. AWS Console → Lambda → Create function"
echo "  2. Upload aws/lambda_package.zip"
echo "  3. Set Handler to:  lambda_handler.handler"
echo "  4. See aws/DEPLOY.md for full instructions"
echo ""
