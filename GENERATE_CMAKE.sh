#!/bin/bash
# setup cmake with 'build' and 'inst':
#
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$PROJECT_DIR/build"
INSTALL_DIR="$PROJECT_DIR/inst"

GENERATOR="Unix Makefiles"
#GENERATOR="Ninja"

cmake -B "$BUILD_DIR" \
      -S "$PROJECT_DIR" \
      -G "$GENERATOR" \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX="$INSTALL_DIR"
