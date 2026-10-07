#!/usr/bin/env bash
# Build a static site (deploy.yml type: static) and write its wrangler config.
#   scripts/static_build.sh <test|prod> <app> <config-out>
# Run from the app repository root with the settings JSON in $SETTINGS (glwork_deploy.py settings).
# Installs dependencies with the package manager the lockfile belongs to, runs `build`, checks that
# `output` has an index.html and writes an assets-only Worker config bound to the app's host.
set -euo pipefail
env=$1 app=$2 out=$3
here=$(cd "$(dirname "$0")" && pwd)
get() { python3 -c 'import json,os,sys;print(json.loads(os.environ["SETTINGS"])[sys.argv[1]])' "$1"; }
workdir=$(get workdir) build=$(get build) output=$(get output)

cd "$workdir"
if [ -n "$build" ]; then
  if [ -f package.json ]; then
    pm=$(python3 -c 'import json;print(json.load(open("package.json")).get("packageManager",""))')
    if [ -f pnpm-lock.yaml ]; then npx --yes "${pm:-pnpm@9}" install --frozen-lockfile
    elif [ -f yarn.lock ]; then npx --yes "${pm:-yarn@1}" install --frozen-lockfile
    elif [ -f package-lock.json ]; then npm ci
    else npm install
    fi
  fi
  echo "== build: $build"
  bash -c "$build"
fi
[ -f "$output/index.html" ] || { echo "::error::$workdir/$output/index.html not found (check build and output in deploy.yml)" >&2; exit 1; }
assets=$(cd "$output" && pwd)
cd - >/dev/null
python3 "$here/glwork_deploy.py" wrangler-static "$env" "$app" "$assets" > "$out"
echo "== $(find "$assets" -type f | wc -l | tr -d ' ') files in $assets"
