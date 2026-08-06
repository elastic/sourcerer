#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
cd "$ROOT_DIR"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  ./scripts/release.sh prepare vMAJOR.MINOR.PATCH
      Bump pyproject.toml, uv.lock, .claude-plugin/marketplace.json, and
      README.md to the new version and commit the result locally. Review and
      merge that commit before running publish.

  ./scripts/release.sh publish vMAJOR.MINOR.PATCH
      Validate that the repo is ready (versions consistent, tests pass) and
      push the release tag. Requires a clean main that matches origin/main.
EOF
}

version_gt() {
  local left_major left_minor left_patch
  local right_major right_minor right_patch

  IFS=. read -r left_major left_minor left_patch <<<"${1#v}"
  IFS=. read -r right_major right_minor right_patch <<<"${2#v}"

  ((left_major > right_major)) ||
    ((left_major == right_major && left_minor > right_minor)) ||
    ((left_major == right_major && left_minor == right_minor && left_patch > right_patch))
}

latest_release_tag() {
  local tag latest=""

  while IFS= read -r tag; do
    [[ "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || continue
    if [[ -z "$latest" ]] || version_gt "$tag" "$latest"; then
      latest="$tag"
    fi
  done < <(git tag --list)

  printf '%s' "$latest"
}

[[ "${1:-}" != "-h" && "${1:-}" != "--help" ]] || {
  usage
  exit 0
}
[[ $# -eq 2 ]] || {
  usage >&2
  exit 1
}

readonly SUBCOMMAND="$1"
[[ "$SUBCOMMAND" == "prepare" || "$SUBCOMMAND" == "publish" ]] ||
  die "unknown subcommand '$SUBCOMMAND'; expected 'prepare' or 'publish'"

readonly TAG="$2"
[[ "$TAG" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] ||
  die "version must use strict vMAJOR.MINOR.PATCH format (for example, v1.0.0)"
readonly VERSION="${TAG#v}"

command -v git >/dev/null 2>&1 || die "git is required"
command -v uv >/dev/null 2>&1 || die "uv is required"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not in a Git repository"
[[ "$(git rev-parse --show-toplevel)" == "$ROOT_DIR" ]] || die "script is not in the repository root"

if [[ "$SUBCOMMAND" == "prepare" ]]; then
  if ! git diff --quiet || ! git diff --cached --quiet; then
    die "tracked files contain uncommitted changes"
  fi

  LATEST_TAG="$(latest_release_tag)"
  readonly LATEST_TAG
  if [[ -n "$LATEST_TAG" ]] && ! version_gt "$TAG" "$LATEST_TAG"; then
    die "$TAG must be newer than the latest release, $LATEST_TAG"
  fi

  printf 'Bumping pyproject.toml to %s...\n' "$VERSION"
  uv version "$VERSION"

  printf 'Updating lockfile...\n'
  uv lock

  printf 'Updating .claude-plugin/marketplace.json...\n'
  MARKETPLACE_JSON="$ROOT_DIR/.claude-plugin/marketplace.json"
  [[ -f "$MARKETPLACE_JSON" ]] || die ".claude-plugin/marketplace.json not found"
  python3 -c "
import json, sys
path = '$MARKETPLACE_JSON'
data = json.load(open(path))
plugins = data.get('plugins', [])
if not plugins:
    sys.exit('no plugins in marketplace.json')
plugins[0]['version'] = '$VERSION'
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
"

  printf 'Updating README.md...\n'
  OLD_VERSION="$(git show HEAD:pyproject.toml | grep -m1 '^version = ' | sed 's/version = "\(.*\)"/\1/')"
  if [[ -n "$OLD_VERSION" && "$OLD_VERSION" != "$VERSION" ]]; then
    sed -i '' "s/v${OLD_VERSION}/v${VERSION}/g" "$ROOT_DIR/README.md"
  fi

  printf 'Committing...\n'
  git add pyproject.toml uv.lock .claude-plugin/marketplace.json README.md
  git commit -m "Release $TAG"

  printf '\nPrepared release commit for %s. Review, then merge to main before running:\n' "$TAG"
  printf '  ./scripts/release.sh publish %s\n' "$TAG"

elif [[ "$SUBCOMMAND" == "publish" ]]; then
  [[ "$(git branch --show-current)" == "main" ]] || die "releases must be published from main"
  if ! git diff --quiet || ! git diff --cached --quiet; then
    die "tracked files contain uncommitted changes"
  fi

  printf 'Refreshing origin/main and release tags...\n'
  git fetch --quiet origin main --tags

  HEAD_SHA="$(git rev-parse HEAD)"
  MAIN_SHA="$(git rev-parse origin/main)"
  readonly HEAD_SHA MAIN_SHA
  [[ "$HEAD_SHA" == "$MAIN_SHA" ]] ||
    die "local main must exactly match origin/main"

  if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
    die "tag $TAG already exists on origin"
  fi
  if git show-ref --verify --quiet "refs/tags/$TAG"; then
    die "tag $TAG already exists locally"
  fi

  PROJECT_VERSION="$(uv version --short)"
  readonly PROJECT_VERSION
  [[ "$PROJECT_VERSION" == "$VERSION" ]] ||
    die "tag $TAG does not match pyproject.toml version $PROJECT_VERSION"

  MARKETPLACE_JSON="$ROOT_DIR/.claude-plugin/marketplace.json"
  [[ -f "$MARKETPLACE_JSON" ]] || die ".claude-plugin/marketplace.json not found"
  MARKETPLACE_VERSION="$(python3 -c "
import json, sys
data = json.load(open('$MARKETPLACE_JSON'))
plugins = data.get('plugins', [])
if not plugins:
    sys.exit('no plugins in marketplace.json')
print(plugins[0].get('version', ''))
")"
  readonly MARKETPLACE_VERSION
  [[ "$MARKETPLACE_VERSION" == "$VERSION" ]] ||
    die "tag $TAG does not match .claude-plugin/marketplace.json plugin version $MARKETPLACE_VERSION"

  LATEST_TAG="$(latest_release_tag)"
  readonly LATEST_TAG
  if [[ -n "$LATEST_TAG" ]] && ! version_gt "$TAG" "$LATEST_TAG"; then
    die "$TAG must be newer than the latest release, $LATEST_TAG"
  fi

  printf 'Checking lockfile...\n'
  uv lock --check

  printf 'Syncing environment...\n'
  uv sync --locked --reinstall --all-extras

  printf 'Running tests...\n'
  uv run --locked pytest tests/

  printf 'Building package...\n'
  uv build

  printf '\nReady to create and push %s at %s.\n' "$TAG" "$HEAD_SHA"
  read -r -p "Continue? [y/N] " CONFIRM
  [[ "$CONFIRM" == "y" || "$CONFIRM" == "Y" ]] || die "release cancelled"

  git tag -a "$TAG" -m "Release $TAG"
  if ! git push origin "refs/tags/$TAG"; then
    printf 'error: push failed; remove the local tag with: git tag -d %s\n' "$TAG" >&2
    exit 1
  fi

  printf '\nPublished %s. GitHub Actions will test it and create the GitHub release.\n' "$TAG"
fi