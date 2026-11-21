#!/usr/bin/env bash
# Build libopenshot-audio + libopenshot (develop) for running openshot-qt from source on Windows.
#
# Run in the "MSYS2 UCRT64" shell (MSYS2 deprecated MINGW64 in 2026 and removed PyQt5 from it):
#     bash /c/path/to/openshot-autodub/tools/windows/build-libopenshot-msys2.sh
#
# Installs into /ucrt64 (C:\msys64\ucrt64) so that C:\msys64\ucrt64\bin\python.exe
# can `import openshot` without extra PATH/PYTHONPATH tweaks.
# Re-run any time to update both libraries to the latest develop.
set -euo pipefail

if [ "${MSYSTEM:-}" != "UCRT64" ]; then
  echo "Run this script in the "MSYS2 UCRT64" shell (C:\\msys64\\ucrt64.exe); current MSYSTEM=${MSYSTEM:-none}." >&2
  exit 1
fi
P="${MINGW_PACKAGE_PREFIX:-mingw-w64-ucrt-x86_64}"

SRC_ROOT="${OPENSHOT_DEV_DIR:-$HOME/openshot-dev}"
BRANCH="${OPENSHOT_BRANCH:-develop}"
JOBS="${JOBS:-$(nproc)}"
PREFIX="$(cygpath -m "${MINGW_PREFIX:-/ucrt64}")"

echo "==> 1/5 MSYS2 packages"
pacman -S --needed --noconfirm \
  git base-devel \
  $P-toolchain $P-cmake $P-ninja $P-pkgconf $P-swig \
  $P-ffmpeg $P-jsoncpp $P-zlib \
  $P-qt6-base $P-qt6-svg $P-qt6-imageformats $P-qt6-scxml \
  $P-python $P-python-pip \
  $P-python-pyqt6 $P-python-requests $P-python-numpy $P-python-certifi
# optional: colour-space engine used by the chroma key effect
pacman -S --needed --noconfirm $P-babl || echo "   (babl not available - skipped, optional)"
# defusedxml (SVG titles) — from pacman if packaged, otherwise pip
pacman -S --needed --noconfirm $P-python-defusedxml 2>/dev/null \
  || python -m pip install --break-system-packages defusedxml

echo "==> 2/5 Sources ($SRC_ROOT, branch $BRANCH)"
mkdir -p "$SRC_ROOT"
cd "$SRC_ROOT"
for repo in libopenshot-audio libopenshot; do
  if [ -d "$repo/.git" ]; then
    git -C "$repo" fetch --depth 1 origin "$BRANCH"
    git -C "$repo" checkout -q -B "$BRANCH" FETCH_HEAD
  else
    git clone --depth 1 -b "$BRANCH" "https://github.com/OpenShot/$repo.git"
  fi
done

# GCC 15 no longer pulls <cstdint> in transitively; libopenshot headers rely on it.
echo "==> 3/5 libopenshot-audio"
cmake -S libopenshot-audio -B libopenshot-audio/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_FLAGS="-include cstdint" \
  -DCMAKE_INSTALL_PREFIX="$PREFIX"
cmake --build libopenshot-audio/build --parallel "$JOBS"
cmake --install libopenshot-audio/build

echo "==> 4/5 libopenshot (+ Python module openshot)"
PY_SITE="$(python -c 'import sysconfig; print(sysconfig.get_path("platlib").replace("\\", "/"))')"
PY_INC="$(python -c 'import sysconfig; print(sysconfig.get_path("include").replace("\\", "/"))')"
PY_VER="$(python -c 'import sysconfig; print(sysconfig.get_config_var("LDVERSION") or sysconfig.get_python_version())')"
PY_LIB="$PREFIX/lib/libpython${PY_VER}.dll.a"
[ -f "$PY_LIB" ] || { echo "Not found: $PY_LIB" >&2; exit 1; }
cmake -S libopenshot -B libopenshot/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DOpenShotAudio_ROOT="$PREFIX" \
  -DUSE_QT6=ON \
  -DENABLE_OPENCV=OFF \
  -DENABLE_MAGICK=OFF \
  -DENABLE_LIB_DOCS=OFF \
  -DBUILD_TESTING=OFF \
  -DCMAKE_CXX_FLAGS="-include cstdint" \
  -DPYTHON_EXECUTABLE="$(cygpath -m "$(command -v python)")" \
  -DPYTHON_INCLUDE_DIR="$PY_INC" \
  -DPYTHON_LIBRARY="$PY_LIB" \
  -DPYTHON_MODULE_PATH="$PY_SITE"
cmake --build libopenshot/build --parallel "$JOBS"
cmake --install libopenshot/build
ls "$PY_SITE"/_openshot*.pyd >/dev/null 2>&1 || {
  echo "Python module openshot was not built: check the cmake output above (Python/SWIG)." >&2; exit 1; }

echo "==> 5/5 Verify"
python - <<'PY'
import openshot, PyQt6.QtCore as c, PyQt6.QtStateMachine, PyQt6.QtSvg, PyQt6.QtWidgets
print("openshot", openshot.OPENSHOT_VERSION_FULL, "| Qt", c.QT_VERSION_STR, "| PyQt", c.PYQT_VERSION_STR)
PY
echo
echo "Done. Next: python install.py (in openshot-autodub), then run tools\\windows\\run-openshot.cmd"
