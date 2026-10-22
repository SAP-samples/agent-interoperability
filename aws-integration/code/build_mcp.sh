#!/usr/bin/env bash
# Build the MCP runtime deployment zip for AgentCore (direct-code deploy).
#
# Vendors Linux/ARM64 (manylinux aarch64) wheels for Python 3.13 — matching the
# AgentCore Runtime execution environment — alongside the project's own source
# (mcp_server.py entrypoint + runtime/). Produces deployment-mcp.zip.
#
# Runs on macOS/Linux; no Docker needed (pip cross-downloads the aarch64 wheels).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

BUILD_DIR="$HERE/build-mcp"
ZIP_PATH="$HERE/deployment-mcp.zip"

echo "==> clean"
rm -rf "$BUILD_DIR" "$ZIP_PATH"
mkdir -p "$BUILD_DIR"

echo "==> vendor dependencies (linux/aarch64, cp313)"
python3 -m pip download \
  --platform manylinux2014_aarch64 \
  --python-version 3.13 \
  --implementation cp \
  --abi cp313 \
  --only-binary=:all: \
  --dest "$BUILD_DIR/.wheels" \
  -r "$HERE/requirements.txt"

# Wheels are zip archives; extract each onto sys.path directly. This avoids
# pip's host-platform check, which rejects aarch64 wheels on a macOS/x86 host.
for whl in "$BUILD_DIR"/.wheels/*.whl; do
  unzip -o -q "$whl" -d "$BUILD_DIR"
done
rm -rf "$BUILD_DIR/.wheels"

echo "==> copy application source"
cp "$HERE/mcp_server.py" "$BUILD_DIR/"
cp -R "$HERE/runtime" "$BUILD_DIR/runtime"
# drop caches that the MCP entrypoint never imports
find "$BUILD_DIR" -type d -name __pycache__ -prune -exec rm -rf {} +

echo "==> zip"
( cd "$BUILD_DIR" && zip -q -r "$ZIP_PATH" . )
echo "built: $ZIP_PATH ($(du -h "$ZIP_PATH" | cut -f1))"
echo "entrypoint check:"
unzip -l "$ZIP_PATH" | grep -E "mcp_server.py|runtime/ias.py|runtime/agent.py" | sed 's/^/    /'
