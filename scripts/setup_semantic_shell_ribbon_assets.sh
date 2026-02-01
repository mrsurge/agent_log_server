#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p static/vendor/web-tree-sitter static/vendor/tree-sitter-bash

WEB_TREE_SITTER_VER="${WEB_TREE_SITTER_VER:-0.26.3}"
TREE_SITTER_BASH_VER="${TREE_SITTER_BASH_VER:-0.25.1}"

fetch() {
  url="$1"
  out="$2"
  echo "Downloading: $url -> $out"
  curl -L --fail --retry 3 --retry-delay 1 -o "$out" "$url"
}

fetch "https://unpkg.com/web-tree-sitter@${WEB_TREE_SITTER_VER}/web-tree-sitter.js" \
  "static/vendor/web-tree-sitter/web-tree-sitter.js"
fetch "https://unpkg.com/web-tree-sitter@${WEB_TREE_SITTER_VER}/web-tree-sitter.wasm" \
  "static/vendor/web-tree-sitter/web-tree-sitter.wasm"

fetch "https://unpkg.com/tree-sitter-bash@${TREE_SITTER_BASH_VER}/tree-sitter-bash.wasm" \
  "static/vendor/tree-sitter-bash/tree-sitter-bash.wasm"
fetch "https://unpkg.com/tree-sitter-bash@${TREE_SITTER_BASH_VER}/queries/highlights.scm" \
  "static/vendor/tree-sitter-bash/highlights.scm"

echo "Done."
