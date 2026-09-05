#!/usr/bin/env bash
# Called only by the write-scoped publish job after artifact verification.
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${GH_TOKEN:?GitHub token is required}"
: "${GH_REPO:?GitHub repository is required}"
: "${GITHUB_REF_NAME:?Release tag is required}"
: "${RUNNER_TEMP:?Runner temporary directory is required}"

if gh release view "$GITHUB_REF_NAME" >/dev/null 2>&1; then
  if [[ $(gh release view "$GITHUB_REF_NAME" --json isDraft --jq .isDraft) != true ]]; then
    echo "::error::Release $GITHUB_REF_NAME is already published; use a new tag"
    exit 1
  fi
  gh release upload "$GITHUB_REF_NAME" --clobber dist/tracebox
else
  gh release create "$GITHUB_REF_NAME" --draft --verify-tag \
    --title "$GITHUB_REF_NAME" --generate-notes dist/tracebox
fi

# Only the raw executable is a public Release asset.
assets=$(gh release view "$GITHUB_REF_NAME" --json assets --jq '.assets | map(.name) | sort | join(",")')
if [[ "$assets" != tracebox ]]; then
  echo "::error::Expected exactly one release asset named tracebox"
  exit 1
fi

# Leave a failed upload/verification as a draft, never a published release.
release_probe=$(mktemp -d "$RUNNER_TEMP/tracebox-release.XXXXXX")
trap 'rm -f -- "$release_probe/tracebox"; rmdir -- "$release_probe"' EXIT
gh release download "$GITHUB_REF_NAME" --pattern tracebox --dir "$release_probe"
cmp -- dist/tracebox "$release_probe/tracebox"
gh release edit "$GITHUB_REF_NAME" --draft=false
