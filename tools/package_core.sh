#!/usr/bin/env bash
# Package a relocatable Vade core tree for one Linux architecture.
# usage: tools/package_core.sh <version> <arch> <path-to-libvade_protocol.so> <out-dir>
set -euo pipefail

VERSION="${1:?version, e.g. 0.0.2-alpha}"
ARCH="${2:?arch, e.g. x86_64 or aarch64}"
SO="${3:?path to libvade_protocol.so}"
OUT="${4:?output directory}"

ROOT="vade-core-${VERSION}-linux-${ARCH}"
STAGE="${OUT}/${ROOT}"

rm -rf "${STAGE}"
mkdir -p "${STAGE}/protocol/core/python" "${STAGE}/sdk" "${STAGE}/admin-ui"
mkdir -p "${OUT}"

# Runtime Python package, without caches or test trees.
cp -a core "${STAGE}/core"
find "${STAGE}/core" -type d -name __pycache__ -prune -exec rm -rf {} +
rm -rf "${STAGE}/core/tests"

# Protocol: generated Python bindings plus the architecture-specific native lib.
cp -a protocol/core/python/vade_protocol.py "${STAGE}/protocol/core/python/"
cp -a protocol/core/python/README.md "${STAGE}/protocol/core/python/" 2>/dev/null || true
install -m 0755 "${SO}" "${STAGE}/protocol/core/python/libvade_protocol.so"

# Contracts and Python SDK are runtime inputs for modules and tooling.
cp -a schema "${STAGE}/schema"
cp -a sdk/python "${STAGE}/sdk/python"
find "${STAGE}/sdk" -type d -name __pycache__ -prune -exec rm -rf {} +

# The admin plane serves this prebuilt bundle; no Node.js on the target.
if [ -d admin-ui/dist ]; then
  cp -a admin-ui/dist "${STAGE}/admin-ui/dist"
else
  echo "package_core: admin-ui/dist missing; admin UI will be unavailable" >&2
fi

cp -a requirements.txt LICENSE NOTICE README.md README.zh-CN.md "${STAGE}/" 2>/dev/null || true
mkdir -p "${STAGE}/docs"
for f in DEPLOYMENT.md DEPLOYMENT.zh-CN.md PAIRING.md; do
  [ -f "docs/${f}" ] && cp -a "docs/${f}" "${STAGE}/docs/"
done
[ -d deploy ] && cp -a deploy "${STAGE}/deploy"

cat > "${STAGE}/install.sh" <<'INSTALL'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cat <<'EOF'
Installed. Start with:
  PYTHONPATH="core:protocol/core/python" .venv/bin/python -m vade --config core.toml
Copy deploy/core.toml.example to core.toml and edit it first.
EOF
INSTALL
chmod +x "${STAGE}/install.sh"

# Refuse to ship caches or local virtualenvs.
if find "${STAGE}" \( -name __pycache__ -o -name '*.pyc' -o -name '.venv' \) -print -quit | grep -q .; then
  echo "package_core: refused to package caches/venv" >&2
  exit 1
fi

tar -C "${OUT}" -czf "${OUT}/${ROOT}.tar.gz" "${ROOT}"
echo "${OUT}/${ROOT}.tar.gz"
