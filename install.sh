#!/usr/bin/env bash
# Install this repo's SKILL.md folders into a Claude Code skills directory.
#
# Claude Code's Agent Skills feature discovers skills from two on-disk
# locations, each one `<name>/SKILL.md` folder deep:
#   personal (default): ~/.claude/skills/<skill>/SKILL.md
#   project:             <project>/.claude/skills/<skill>/SKILL.md
# This script only ever copies whole `<slug>/SKILL.md` folders from this repo
# into one of those two locations (or an explicit --target you choose). It
# never touches anything else on disk.
#
# Usage:
#   ./install.sh                 # install for this user: ~/.claude/skills/
#   ./install.sh --project       # install into ./.claude/skills/ (cwd)
#   ./install.sh --project DIR   # install into DIR/.claude/skills/
#   ./install.sh --target DIR    # install directly into DIR (advanced/testing)
#   ./install.sh --dry-run       # show what would happen, change nothing
#   ./install.sh --force         # overwrite name collisions with unmanaged dirs
#   ./install.sh --source DIR    # use a different ml-ai-skills checkout as source
#   ./install.sh --help
#
# Re-running this script is safe: it is the update mechanism. Skills it
# previously installed are refreshed in place; anything else at the target
# that it did not install is left untouched. Bash + standard coreutils
# (cp/mkdir/rm/sort) only — no other runtime required.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_NAME=".ml-ai-skills-manifest"

# Mirrors scripts/skills_lib.py NON_SKILL_DIRS — keep in sync if that changes.
NON_SKILL_DIRS=("_TEMPLATE" ".git" ".claude" "scripts" "schema" "tests" "docs" "evals" ".github")

SOURCE_DIR="$SCRIPT_DIR"
MODE="user"
PROJECT_DIR="."
TARGET_DIR=""
DRY_RUN=0
FORCE=0

usage() {
  sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

log()  { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    --user) MODE="user"; shift ;;
    --project)
      MODE="project"
      if [ $# -ge 2 ] && [[ "$2" != --* ]]; then PROJECT_DIR="$2"; shift 2; else shift; fi
      ;;
    --target)
      [ $# -ge 2 ] || { err "--target requires a directory argument"; exit 1; }
      MODE="target"; TARGET_DIR="$2"; shift 2
      ;;
    --source)
      [ $# -ge 2 ] || { err "--source requires a directory argument"; exit 1; }
      SOURCE_DIR="$2"; shift 2
      ;;
    *) err "unknown argument: $1 (see --help)"; exit 1 ;;
  esac
done

# --- Prerequisite checks -----------------------------------------------
for tool in cp mkdir rm sort date; do
  command -v "$tool" >/dev/null 2>&1 || { err "missing required standard tool: $tool"; exit 1; }
done

if [ ! -f "$SOURCE_DIR/INDEX.md" ] || [ ! -f "$SOURCE_DIR/schema/allowed_values.py" ]; then
  err "$SOURCE_DIR does not look like an ml-ai-skills checkout (missing INDEX.md / schema/allowed_values.py)."
  err "Run this script from the repo root, or pass --source /path/to/ml-ai-skills."
  exit 1
fi

case "$MODE" in
  user)    TARGET_DIR="${HOME:?HOME is not set}/.claude/skills" ;;
  project) TARGET_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd || { err "--project directory does not exist: $PROJECT_DIR"; exit 1; })/.claude/skills" ;;
  target)  : ;; # TARGET_DIR already set
esac

# --- Detect the target agent (informational only; never blocks install) --
if command -v claude >/dev/null 2>&1; then
  log "Detected 'claude' CLI on PATH."
else
  log "Note: no 'claude' CLI found on PATH. Proceeding anyway — Claude Code's"
  log "      skill discovery only needs the files on disk, not the CLI."
fi

log "Source: $SOURCE_DIR"
log "Target: $TARGET_DIR"
[ "$DRY_RUN" -eq 1 ] && log "(dry run — no files will be changed)"
log ""

# --- Enumerate source skills (mirrors skills_lib.py iter_skill_dirs) -----
is_non_skill_dir() {
  local name="$1"
  for d in "${NON_SKILL_DIRS[@]}"; do
    [ "$name" = "$d" ] && return 0
  done
  return 1
}

skills=()
for entry in "$SOURCE_DIR"/*/; do
  name="$(basename "$entry")"
  [[ "$name" == .* ]] && continue
  is_non_skill_dir "$name" && continue
  [ -f "$entry/SKILL.md" ] || continue
  skills+=("$name")
done

if [ "${#skills[@]}" -eq 0 ]; then
  err "no skills found under $SOURCE_DIR — nothing to install"
  exit 1
fi
log "Found ${#skills[@]} skill(s) in source."

# --- Load previously-managed slugs from an existing manifest, if any -----
# Manifest is plain text on purpose (not JSON): it is written and read only
# by this script and uninstall.sh, in the same bash process, so there is no
# need for a second interpreter — which also sidesteps POSIX-path vs.
# native-Windows-path mismatches between Git Bash and a separately invoked
# python.exe. Lines starting with '#' are metadata comments; every other
# non-blank line is a managed skill slug.
manifest_path="$TARGET_DIR/$MANIFEST_NAME"
managed_slugs=()
if [ -f "$manifest_path" ]; then
  while IFS= read -r line; do
    [[ "$line" == \#* ]] && continue
    [ -n "$line" ] && managed_slugs+=("$line")
  done < "$manifest_path"
fi

is_managed() {
  local slug="$1"
  for m in ${managed_slugs[@]+"${managed_slugs[@]}"}; do
    [ "$slug" = "$m" ] && return 0
  done
  return 1
}

# --- Install ---------------------------------------------------------
installed=0
updated=0
skipped=0
final_slugs=()

[ "$DRY_RUN" -eq 1 ] || mkdir -p "$TARGET_DIR"

for slug in "${skills[@]}"; do
  dest="$TARGET_DIR/$slug"
  if [ -e "$dest" ]; then
    if is_managed "$slug" || [ "$FORCE" -eq 1 ]; then
      log "  update  $slug"
      if [ "$DRY_RUN" -eq 0 ]; then
        rm -rf "$dest"
        cp -R "$SOURCE_DIR/$slug" "$dest"
      fi
      updated=$((updated + 1))
      final_slugs+=("$slug")
    else
      warn "  skip    $slug — already exists at $dest and is not managed by ml-ai-skills (use --force to overwrite)"
      skipped=$((skipped + 1))
    fi
  else
    log "  install $slug"
    if [ "$DRY_RUN" -eq 0 ]; then
      cp -R "$SOURCE_DIR/$slug" "$dest"
    fi
    installed=$((installed + 1))
    final_slugs+=("$slug")
  fi
done

# --- Write manifest ----------------------------------------------------
if [ "$DRY_RUN" -eq 0 ]; then
  {
    echo "# ml-ai-skills manifest — managed by install.sh/uninstall.sh, do not hand-edit"
    echo "# source: $SOURCE_DIR"
    echo "# updated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s\n' ${final_slugs[@]+"${final_slugs[@]}"} | sort
  } > "$manifest_path"
fi

log ""
log "Done. installed=$installed updated=$updated skipped=$skipped target=$TARGET_DIR"
if [ "$skipped" -gt 0 ]; then
  warn "$skipped skill(s) skipped due to unmanaged name collisions. Re-run with --force to overwrite them, if that's intended."
fi
exit 0
