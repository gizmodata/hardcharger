# Source and extension ABI used by all Hardcharger builds.
DUCKDB_REPOSITORY=https://github.com/gizmodata/hardcharger-engine.git
DUCKDB_COMMIT=aef3086626218f878d821f7936e3c20cc4940b12
DUCKDB_VERSION_NAME=v2.0.1

prepare_duckdb_source() {
  if [ ! -e "$SRC/.git" ]; then
    git init --quiet "$SRC"
    git -C "$SRC" remote add origin "$DUCKDB_REPOSITORY"
  fi
  if ! git -C "$SRC" cat-file -e "$DUCKDB_COMMIT^{commit}" 2>/dev/null; then
    git -C "$SRC" fetch --quiet --depth 1 --no-tags "$DUCKDB_REPOSITORY" "$DUCKDB_COMMIT"
  fi
  git -C "$SRC" checkout --quiet --detach "$DUCKDB_COMMIT"
  cmake -DDUCKDB_SOURCE_DIR="$SRC" -P "$HERE/scripts/retire-ducklake-patch.cmake"
  local patch
  for patch in "$HERE"/scripts/patch-duckdb*.cmake; do
    cmake -DDUCKDB_SOURCE_DIR="$SRC" -P "$patch"
  done
}

build_native_duckdb() {
  local extensions="$1" profile="$2" prefix="$CHARGEWIRE_DEPS_DIR/duckdb-install"
  local build="$CHARGEWIRE_DEPS_DIR/duckdb-build"
  cmake -S "$SRC" -B "$build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DOVERRIDE_GIT_DESCRIBE="$DUCKDB_VERSION_NAME" -DDUCKDB_OPTIMIZATION_PROFILE="$profile" \
    '-DDUCKDB_CAPABILITIES=httplib;loadable_extensions' -DDUCKDB_EXTENSION_CONFIGS="$extensions" \
    -DBUILD_EXTENSIONS="" -DBUILD_SHELL=OFF -DBUILD_UNITTESTS=OFF -DENABLE_SANITIZER=OFF -DENABLE_UBSAN=OFF
  cmake --build "$build"
  cmake --install "$build"
  cp "$SRC/src/include/duckdb_v2.h" "$SRC/src/include/duckdb.hpp" "$prefix/include/"
  cp -R "$SRC/src/include/duckdb/." "$prefix/include/duckdb/"
}
