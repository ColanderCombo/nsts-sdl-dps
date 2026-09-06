#!/bin/bash
# setup cmake with 'build' and 'inst':
#
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$PROJECT_DIR/build"
INSTALL_DIR="$PROJECT_DIR/inst"

GENERATOR="Unix Makefiles"
#GENERATOR="Ninja"

# The project is C only; cmake's compiler check fails when CC names a C++
# compiler.  Substitute the matching C compiler and say so.
case "${CC:-}" in
    *clang++) echo "CC=$CC is a C++ compiler; using CC=clang"; export CC=clang ;;
    *g++)     echo "CC=$CC is a C++ compiler; using CC=gcc";   export CC=gcc ;;
esac

cmake -B "$BUILD_DIR" \
      -S "$PROJECT_DIR" \
      -G "$GENERATOR" \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX="$INSTALL_DIR"
