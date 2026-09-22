#!/usr/bin/env bash
# Remove skills previously installed by install.sh.
#
# Only removes directories this repo's manifest says it installed — never
# touches a same-named skill directory it didn't create, and never removes
# the parent skills/ directory itself (other tools or the user may use it).
#
# Usage:
#   ./uninstall.sh                 # remove from ~/.claude/skills/
#   ./uninstall.sh --project       # remove from ./.claude/skills/ (cwd)
#   ./uninstall.sh --project DIR   # remove from DIR/.claude/skills/
#   ./uninstall.sh --target DIR    # remove from DIR directly (advanced/testing)
#   ./uninstall.sh --skill SLUG    # remove only this one skill (repeatable)
#   ./uninstall.sh --dry-run       # show what would happen, change nothing
#   ./uninstall.sh --help
set -euo pipefail

MANIFEST_NAME=".ml-ai-skills-manifest"

MODE="user"
PROJECT_DIR="."
TARGET_DIR=""
DRY_RUN=0
ONLY_SLUGS=()

usage() {
  sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

log()  { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --user) MODE="user"; shift ;;
    --project)
      MODE="project"
      if [ $# -ge 2 ] && [[ "$2" != --* ]]; then PROJECT_DIR="$2"; shift 2; else shift; fi
      ;;
    --target)
      [ $# -ge 2 ] || { err "--target requires a directory argument"; exit 1; }
      MODE="target"; TARGET_DIR="$2"; shift 2
      ;;
    --skill)
      [ $# -ge 2 ] || { err "--skill requires a slug argument"; exit 1; }
      ONLY_SLUGS+=("$2"); shift 2
      ;;
    *) err "unknown argument: $1 (see --help)"; exit 1 ;;
  esac
done

for tool in rm sort date; do
  command -v "$tool" >/dev/null 2>&1 || { err "missing required standard tool: $tool"; exit 1; }
done

case "$MODE" in
  user)    TARGET_DIR="${HOME:?HOME is not set}/.claude/skills" ;;
  project) TARGET_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd || { err "--project directory does not exist: $PROJECT_DIR"; exit 1; })/.claude/skills" ;;
  target)  : ;;
esac

manifest_path="$TARGET_DIR/$MANIFEST_NAME"
if [ ! -f "$manifest_path" ]; then
  err "no ml-ai-skills manifest found at $manifest_path — nothing to uninstall (or wrong --target/--project)"
  exit 1
fi

managed_slugs=()
while IFS= read -r line; do
  [[ "$line" == \#* ]] && continue
  [ -n "$line" ] && managed_slugs+=("$line")
done < "$manifest_path"

if [ "${#managed_slugs[@]}" -eq 0 ]; then
  warn "manifest at $manifest_path lists no skills; removing empty manifest"
  [ "$DRY_RUN" -eq 1 ] || rm -f "$manifest_path"
  exit 0
fi

if [ "${#ONLY_SLUGS[@]}" -gt 0 ]; then
  to_remove=()
  for slug in "${ONLY_SLUGS[@]}"; do
    found=0
    for m in "${managed_slugs[@]}"; do [ "$slug" = "$m" ] && found=1; done
    if [ "$found" -eq 1 ]; then
      to_remove+=("$slug")
    else
      warn "  '$slug' is not tracked in the manifest — skipping (not ours to remove)"
    fi
  done
else
  to_remove=("${managed_slugs[@]}")
fi

if [ "${#to_remove[@]}" -eq 0 ]; then
  log "Nothing to remove."
  exit 0
fi

log "Target: $TARGET_DIR"
[ "$DRY_RUN" -eq 1 ] && log "(dry run — no files will be changed)"

removed=0
for slug in "${to_remove[@]}"; do
  dest="$TARGET_DIR/$slug"
  if [ -e "$dest" ]; then
    log "  remove  $slug"
    [ "$DRY_RUN" -eq 1 ] || rm -rf "$dest"
    removed=$((removed + 1))
  else
    warn "  '$slug' was in the manifest but not found on disk at $dest (already removed?)"
  fi
done

if [ "$DRY_RUN" -eq 0 ]; then
  remaining=()
  for m in "${managed_slugs[@]}"; do
    keep=1
    for r in "${to_remove[@]}"; do [ "$m" = "$r" ] && keep=0; done
    [ "$keep" -eq 1 ] && remaining+=("$m")
  done
  if [ "${#remaining[@]}" -eq 0 ]; then
    rm -f "$manifest_path"
  else
    {
      echo "# ml-ai-skills manifest — managed by install.sh/uninstall.sh, do not hand-edit"
      echo "# updated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
      printf '%s\n' "${remaining[@]}" | sort
    } > "$manifest_path"
  fi
fi

log ""
log "Done. removed=$removed target=$TARGET_DIR"
log "(the $TARGET_DIR directory itself was left in place)"
exit 0
