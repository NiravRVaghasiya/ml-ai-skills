#!/usr/bin/env bash
# Install this repo's SKILL.md folders into a Claude Code skills directory.
#
# Claude Code's Agent Skills feature discovers skills from two on-disk
# locations, each one `<name>/SKILL.md` folder deep:
#   personal (default): ~/.claude/skills/<skill>/SKILL.md
#   project:             <project>/.claude/skills/<skill>/SKILL.md
# This script only ever copies whole `<slug>/SKILL.md` folders from this repo
# into one of those two locations (or an explicit --target you choose), plus
# its own manifest and a short-lived lock directory there. It never writes
# anywhere else. See docs/INSTALLER.md for the full contract.
#
# Usage:
#   ./install.sh                 # install for this user: ~/.claude/skills/
#   ./install.sh --project       # install into ./.claude/skills/ (cwd)
#   ./install.sh --project DIR   # install into DIR/.claude/skills/
#   ./install.sh --target DIR    # install directly into DIR (advanced/testing)
#   ./install.sh --dry-run       # show what would happen, write nothing at all
#   ./install.sh --force         # overwrite locally modified skills and
#                                #   same-named folders this script didn't install
#   ./install.sh --prune         # remove installed skills that no longer exist
#                                #   in the source (unmodified ones; all with --force)
#   ./install.sh --skill SLUG    # act on SLUG only (repeatable), e.g. to --force
#                                #   one edited skill without touching the rest
#   ./install.sh --source DIR    # use a different ml-ai-skills checkout as source
#   ./install.sh --help
#
# Re-running this script is safe: it is the update mechanism. Skills it
# previously installed are refreshed in place unless you edited them (those
# are kept, with a warning); anything at the target it did not install is
# left untouched.
#
# Exit codes: 0 ok (protective skips are warnings, not errors) | 1 error
# (some skill could not be processed safely; everything else completed and
# the manifest matches the disk) | 2 usage error | 3 another run holds the lock.
set -euo pipefail
# C locale: deterministic `sort`, and ASCII-only ranges in the slug regex.
export LC_ALL=C
# An exported CDPATH would make `cd DIR` (relative DIR) resolve elsewhere and
# echo the new directory into every `$(cd DIR && pwd)`.
unset CDPATH

# Script dir without dirname(1); also accepts C:\-style paths (Git Bash).
case "${BASH_SOURCE[0]}" in
  */*|*\\*) SCRIPT_DIR="${BASH_SOURCE[0]%[/\\]*}" ;;
  *)        SCRIPT_DIR="." ;;
esac
SCRIPT_DIR="$(cd "${SCRIPT_DIR:-/}" && pwd)"

# >>> shared helpers (byte-identical in install.sh and uninstall.sh; tests/test_install_security.py enforces it)
MANIFEST_NAME=".ml-ai-skills-manifest"
LOCK_NAME=".ml-ai-skills.lock"
MANIFEST_FORMAT_CURRENT=2
# Slugs become path components under TARGET_DIR, so anything outside this
# grammar ('../x', 'a/b', '.hidden', 'x\r') is rejected, never "sanitised".
SLUG_RE='^[a-z0-9]+(-[a-z0-9]+)*$'
HASH_RE='^[0-9a-f]{64}$'
NL=$'\n'
TAB=$'\t'
EXIT_ERROR=1
EXIT_USAGE=2
EXIT_LOCKED=3

log()  { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

usage() {
  local line n=0
  while IFS= read -r line; do
    n=$((n + 1))
    [ "$n" -eq 1 ] && continue
    case "$line" in
      \#*) line="${line#\#}"; printf '%s\n' "${line# }" ;;
      *) break ;;
    esac
  done < "${BASH_SOURCE[0]}"
}

is_valid_slug() { [[ $1 =~ $SLUG_RE ]]; }

# trim STRING -> REPLY without leading/trailing whitespace (incl. CR).
trim() {
  REPLY="$1"
  REPLY="${REPLY#"${REPLY%%[![:space:]]*}"}"
  REPLY="${REPLY%"${REPLY##*[![:space:]]}"}"
}

# not_an_option PATH -> REPLY: a path starting with '-' would be read as an
# option by cd/mkdir, and `cd -` (even `cd -- -`) jumps to $OLDPWD, so such a
# (necessarily relative) path gets a './' prefix.
not_an_option() {
  case "$1" in
    -*) REPLY="./$1" ;;
    *) REPLY="$1" ;;
  esac
}

# Tiny slug->value maps kept in a string ("\nslug=value" per entry): macOS
# ships bash 3.2, which has no associative arrays. Keys are validated slugs
# and values are hex or "-", so neither can contain '=', glob chars or NL.
kv_get() {  # kv_get MAP KEY -> REPLY=value; status 1 if absent
  case "$1" in
    *"$NL$2="*) REPLY="${1#*"$NL$2="}"; REPLY="${REPLY%%"$NL"*}"; return 0 ;;
  esac
  REPLY=""
  return 1
}
kv_del() {  # kv_del VARNAME KEY
  local m="${!1}$NL"
  if kv_get "$m" "$2"; then m="${m/"$NL$2=$REPLY$NL"/$NL}"; fi
  printf -v "$1" '%s' "${m%"$NL"}"
}
kv_set() {  # kv_set VARNAME KEY VALUE
  kv_del "$1" "$2"
  printf -v "$1" '%s' "${!1}$NL$2=$3"
}

SHA_CMD=()
detect_sha_tool() {
  # Refuse rather than degrade: without hashes we could not keep the promise
  # "never overwrite or delete a skill you edited".
  if command -v sha256sum >/dev/null 2>&1; then SHA_CMD=(sha256sum -b)
  elif command -v shasum >/dev/null 2>&1; then SHA_CMD=(shasum -a 256 -b)
  else
    err "need 'sha256sum' (GNU coreutils) or 'shasum' (macOS/Perl) to detect locally modified skills; install one and re-run."
    exit "$EXIT_ERROR"
  fi
}

# hash_batch BASE SLUG... -> HB_MAP (slug=sha256 of BASE/slug/SKILL.md) for
# every slug whose SKILL.md is a regular, non-symlinked file. One process for
# all files (forks cost ~0.3s each on some Windows hosts), and relative,
# validated names so the tool never has to escape odd characters.
HB_MAP=""
hash_batch() {
  local base="$1" s line h rest out
  shift
  local rels=()
  HB_MAP=""
  for s in "$@"; do
    if [ ! -L "$base/$s" ] && [ ! -L "$base/$s/SKILL.md" ] && [ -f "$base/$s/SKILL.md" ]; then
      rels+=("$s/SKILL.md")
    fi
  done
  [ "${#rels[@]}" -gt 0 ] || return 0
  if ! out="$(cd "$base" && "${SHA_CMD[@]}" "${rels[@]}")"; then
    err "failed to hash SKILL.md files under $base"
    return 1
  fi
  while IFS= read -r line; do
    line="${line%$'\r'}"
    h="${line:0:64}"
    rest="${line:64}"; rest="${rest# }"; rest="${rest#\*}"; rest="${rest# }"
    s="${rest%/SKILL.md}"
    if [[ $h =~ $HASH_RE ]] && is_valid_slug "$s"; then HB_MAP="$HB_MAP$NL$s=$h"; fi
  done <<EOF
$out
EOF
}

# tree_is_safe DIR: every entry below DIR is a real directory or a regular
# file. Pure bash (no find(1)) and never follows links.
UNSAFE_REASON=""
tree_is_safe() {
  local e
  for e in "$1"/*; do
    if [ -L "$e" ]; then UNSAFE_REASON="contains a symlink/junction: $e"; return 1
    elif [ -d "$e" ]; then tree_is_safe "$e" || return 1
    elif [ ! -f "$e" ]; then UNSAFE_REASON="contains a non-regular file: $e"; return 1
    fi
  done
  return 0
}

# read_manifest PATH -> STATE (slug=sha256, or slug=- when unknown, i.e. a
# format-1 entry), MANIFEST_FORMAT, MANIFEST_SOURCE. Format 1 = bare slug
# lines; format 2 = '# format: 2' header + 'slug<TAB>sha256' lines.
STATE=""
MANIFEST_FORMAT=0
MANIFEST_SOURCE=""
read_manifest() {
  local path="$1" line slug hash shown lineno=0 fmt=1
  STATE=""
  # `read` silently drops NUL bytes ('al\0pha' would become 'alpha'), so a
  # manifest containing one is refused, not "sanitised". read -d '' succeeds
  # only if it finds a NUL before EOF.
  if IFS= read -r -d '' line < "$path"; then
    err "manifest $path contains a NUL byte; it is corrupt or not a manifest. Inspect it (or move it aside) and re-run."
    return 1
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    line="${line%$'\r'}"
    case "$line" in
      "# format:"*) trim "${line#\# format:}"; fmt="$REPLY"; continue ;;
      "# source:"*) trim "${line#\# source:}"; MANIFEST_SOURCE="$REPLY"; continue ;;
      \#*) continue ;;
    esac
    trim "$line"
    [ -n "$REPLY" ] || continue
    case "$REPLY" in
      *"$TAB"*) slug="${REPLY%%"$TAB"*}"; hash="${REPLY#*"$TAB"}" ;;
      *) slug="$REPLY"; hash="-" ;;
    esac
    trim "$slug"; slug="$REPLY"
    trim "$hash"; hash="$REPLY"
    if ! is_valid_slug "$slug"; then
      printf -v shown '%q' "$slug"
      warn "ignoring manifest line $lineno: $shown is not a valid skill slug (lowercase-kebab, e.g. data-preprocessing); it is never used as a path, and is dropped the next time the manifest is rewritten"
      continue
    fi
    if ! [[ $hash =~ $HASH_RE ]]; then
      [ "$hash" = "-" ] || warn "manifest line $lineno: unrecognised hash for '$slug'; treating it as unverifiable"
      hash="-"
    fi
    kv_get "$STATE" "$slug" && continue
    STATE="$STATE$NL$slug=$hash"
  done < "$path"
  case "$fmt" in
    1|2) MANIFEST_FORMAT="$fmt" ;;
    *)
      printf -v shown '%q' "$fmt"
      err "manifest $path declares format $shown; this script understands formats 1 and 2. Update your ml-ai-skills checkout."
      return 1
      ;;
  esac
}

# Lock = a directory created with mkdir (atomic everywhere, incl. Git Bash).
# It also holds this run's staging area and the previous copies of replaced
# skills, so a crash leaves everything in one obvious place.
LOCK_DIR=""
WORK_DIR=""
LOCK_HELD=0
KEEP_LOCK=0
RUN_TIMESTAMP=""
acquire_lock() {
  local owner=""
  LOCK_DIR="$TARGET_DIR/$LOCK_NAME"
  if [ -L "$LOCK_DIR" ]; then
    err "$LOCK_DIR is a symlink; refusing to use it (remove it if you did not create it)."
    exit "$EXIT_ERROR"
  fi
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [ -e "$LOCK_DIR" ] && [ ! -d "$LOCK_DIR" ]; then
      # Not a lock any run of ours could have made, so not "lock held" (exit 3).
      err "$LOCK_DIR exists but is not a directory; ml-ai-skills never creates a file there. Inspect it, remove it if it is not yours, and re-run."
      exit "$EXIT_ERROR"
    fi
    if [ -d "$LOCK_DIR" ]; then
      if [ -f "$LOCK_DIR/owner" ]; then IFS= read -r owner < "$LOCK_DIR/owner" || true; fi
      err "another ml-ai-skills install/uninstall holds the lock $LOCK_DIR${owner:+ ($owner)}."
      err "If no other run is active, a previous run was interrupted: move any folder in $LOCK_DIR/old/ back to $TARGET_DIR/ if the same-named folder is missing there, then delete $LOCK_DIR and re-run."
      exit "$EXIT_LOCKED"
    fi
    err "could not create lock directory $LOCK_DIR (check permissions)."
    exit "$EXIT_ERROR"
  fi
  LOCK_HELD=1
  WORK_DIR="$LOCK_DIR"
  printf 'pid=%s started=%s\n' "$$" "$RUN_TIMESTAMP" > "$LOCK_DIR/owner" || true
}
release_lock() {
  if [ "$KEEP_LOCK" -eq 1 ]; then
    warn "leaving $LOCK_DIR in place: it holds a copy that could not be restored automatically (see errors above)."
    return 0
  fi
  rm -rf "$LOCK_DIR" || warn "could not fully remove $LOCK_DIR; delete it manually before the next run."
}

# write_manifest: atomic (temp file + rename). Only entries that still exist
# on disk are recorded, so the manifest describes reality after a partial run.
# MANIFEST_WRITE_FORMAT=1 (uninstall.sh on a format-1 manifest) keeps the
# legacy layout: no '# format:' line, bare slugs for unknown hashes. That way
# a later run still knows install.sh has not yet tried to prove those copies.
MANIFEST_WRITE_FORMAT="$MANIFEST_FORMAT_CURRENT"
write_manifest() {
  local path="$TARGET_DIR/$MANIFEST_NAME" tmp="$WORK_DIR/manifest.tmp" k v body="" src
  while IFS='=' read -r k v; do
    [ -n "$k" ] || continue
    if [ -e "$TARGET_DIR/$k" ] || [ -L "$TARGET_DIR/$k" ]; then
      if [ "$MANIFEST_WRITE_FORMAT" = 1 ] && [ "$v" = "-" ]; then body="$body$k$NL"; else body="$body$k$TAB$v$NL"; fi
    fi
  done <<EOF
$STATE
EOF
  if [ -z "$body" ]; then
    if [ -f "$path" ] && [ ! -L "$path" ]; then rm -f "$path" || return 1; fi
    return 0
  fi
  src="${MANIFEST_SOURCE//$NL/ }"
  src="${src//$'\r'/ }"
  {
    printf '%s\n' "# ml-ai-skills manifest — managed by install.sh/uninstall.sh, do not hand-edit"
    if [ "$MANIFEST_WRITE_FORMAT" != 1 ]; then printf '# format: %s\n' "$MANIFEST_WRITE_FORMAT"; fi
    if [ -n "$src" ]; then printf '# source: %s\n' "$src"; fi
    printf '# updated_at: %s\n' "$RUN_TIMESTAMP"
    printf '%s' "$body" | sort
  } > "$tmp" || return 1
  mv -f "$tmp" "$path" || return 1
}

# In-flight replace of one skill (install.sh). TX_SLUG is armed BEFORE
# anything moves, and rollback decides from the disk, not from a progress
# flag: bash runs a signal trap only after the running child (e.g. the
# move-aside mv) exits, so a flag set after that mv would lag behind reality.
#   staged copy gone, dest present  -> the new copy landed: record its hash
#   dest present otherwise          -> nothing was replaced, or a folder appeared
#                                      there: never delete an old copy we hold
#   dest absent, old copy present   -> moved aside but not replaced: restore it
# TX_FAILED=1 (an explicit failure) never counts as "the new copy landed".
TX_SLUG=""
TX_NEW_HASH=""
TX_FAILED=0
rollback_tx() {
  local slug="$TX_SLUG" dest old staged
  TX_SLUG=""
  [ -n "$slug" ] || return 0
  dest="$TARGET_DIR/$slug"
  old="$WORK_DIR/old/$slug"
  staged="$WORK_DIR/stage/$slug"
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    if [ "$TX_FAILED" -eq 0 ] && [ ! -e "$staged" ] && [ ! -L "$staged" ]; then
      kv_set STATE "$slug" "$TX_NEW_HASH"
      return 0
    fi
    if [ -e "$old" ] || [ -L "$old" ]; then
      err "could not finish replacing $slug: $dest is not the copy this run installed; your previous copy is kept at $old"
      KEEP_LOCK=1
      return 1
    fi
    return 0
  fi
  if [ -e "$old" ] || [ -L "$old" ]; then
    if mv "$old" "$dest"; then
      log "  restored previous copy of $slug"
    else
      err "could not restore $slug; your previous copy is at $old"
      KEEP_LOCK=1
      return 1
    fi
  fi
  return 0
}

STATE_LOADED=0
FINALIZE=0
DISK_CHANGED=0
cleanup() {
  local rc=$?
  set +e
  trap '' HUP INT TERM  # a second Ctrl-C must not abort the rollback half-way
  rollback_tx || rc="$EXIT_ERROR"
  if [ "$LOCK_HELD" -eq 1 ]; then
    if [ "$STATE_LOADED" -eq 1 ] && { [ "$FINALIZE" -eq 1 ] || [ "$DISK_CHANGED" -eq 1 ]; }; then
      if ! write_manifest; then
        err "failed to write $TARGET_DIR/$MANIFEST_NAME"
        rc="$EXIT_ERROR"
      fi
    fi
    release_lock
  fi
  exit "$rc"
}
# <<< shared helpers

SOURCE_DIR="$SCRIPT_DIR"
MODE="user"
PROJECT_DIR="."
TARGET_DIR=""
DRY_RUN=0
FORCE=0
PRUNE=0
ONLY_SET=""  # --skill filter ("\nslug=1" per entry); empty = every skill

# Mirrors scripts/skills_lib.py NON_SKILL_DIRS — keep in sync if that changes.
NON_SKILL_DIRS=("_TEMPLATE" ".git" ".claude" "scripts" "schema" "tests" "docs" "evals" ".github")

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    --prune) PRUNE=1; shift ;;
    --user) MODE="user"; shift ;;
    --project)
      MODE="project"
      if [ $# -ge 2 ] && [[ "$2" != --* ]]; then PROJECT_DIR="$2"; shift 2; else shift; fi
      ;;
    --target)
      if [ $# -lt 2 ] || [ -z "$2" ]; then err "--target requires a directory argument"; exit "$EXIT_USAGE"; fi
      MODE="target"; TARGET_DIR="$2"; shift 2
      ;;
    --source)
      if [ $# -lt 2 ] || [ -z "$2" ]; then err "--source requires a directory argument"; exit "$EXIT_USAGE"; fi
      SOURCE_DIR="$2"; shift 2
      ;;
    --skill)
      [ $# -ge 2 ] || { err "--skill requires a slug argument"; exit "$EXIT_USAGE"; }
      if ! is_valid_slug "$2"; then
        printf -v shown '%q' "$2"
        err "--skill $shown is not a valid skill slug (lowercase-kebab, e.g. data-preprocessing); refusing."
        exit "$EXIT_USAGE"
      fi
      ONLY_SET="$ONLY_SET$NL$2=1"; shift 2
      ;;
    *) err "unknown argument: $1 (see --help)"; exit "$EXIT_USAGE" ;;
  esac
done
not_an_option "$SOURCE_DIR"; SOURCE_DIR="$REPLY"
not_an_option "$PROJECT_DIR"; PROJECT_DIR="$REPLY"
not_an_option "$TARGET_DIR"; TARGET_DIR="$REPLY"

# --- Prerequisite checks -----------------------------------------------
for tool in cp mkdir mv rm sort date; do
  command -v "$tool" >/dev/null 2>&1 || { err "missing required standard tool: $tool"; exit "$EXIT_ERROR"; }
done
detect_sha_tool

if [ ! -f "$SOURCE_DIR/INDEX.md" ] || [ ! -f "$SOURCE_DIR/schema/allowed_values.py" ]; then
  err "$SOURCE_DIR does not look like an ml-ai-skills checkout (missing INDEX.md / schema/allowed_values.py)."
  err "Run this script from the repo root, or pass --source /path/to/ml-ai-skills."
  exit "$EXIT_ERROR"
fi
SOURCE_DIR="$(cd -P "$SOURCE_DIR" && pwd)"

case "$MODE" in
  user)    TARGET_DIR="${HOME:?HOME is not set}/.claude/skills" ;;
  project)
    [ -d "$PROJECT_DIR" ] || { err "--project directory does not exist: $PROJECT_DIR"; exit "$EXIT_ERROR"; }
    TARGET_DIR="$(cd "$PROJECT_DIR" && pwd)/.claude/skills"
    ;;
  target)  : ;;
esac
if [ -L "$TARGET_DIR" ] && [ ! -d "$TARGET_DIR" ]; then
  err "target $TARGET_DIR is a dangling symlink; refusing to create anything through it."
  exit "$EXIT_ERROR"
fi
if [ -e "$TARGET_DIR" ] && [ ! -d "$TARGET_DIR" ]; then
  err "target $TARGET_DIR exists but is not a directory."
  exit "$EXIT_ERROR"
fi

# --- Detect the target agent (informational only; never blocks install) --
if command -v claude >/dev/null 2>&1; then
  log "Detected 'claude' CLI on PATH."
else
  log "Note: no 'claude' CLI found on PATH. Proceeding anyway — Claude Code's"
  log "      skill discovery only needs the files on disk, not the CLI."
fi

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM


# --- Enumerate source skills (mirrors skills_lib.py iter_skill_dirs) -----
shopt -s nullglob dotglob

is_non_skill_dir() {
  local name="$1" d
  for d in "${NON_SKILL_DIRS[@]}"; do
    [ "$name" = "$d" ] && return 0
  done
  return 1
}

skills=()
SRC_SET=""
for entry in "$SOURCE_DIR"/*/; do
  name="${entry%/}"
  name="${name##*/}"
  [[ "$name" == .* ]] && continue
  is_non_skill_dir "$name" && continue
  [ -f "$entry/SKILL.md" ] || continue
  if ! is_valid_slug "$name"; then
    printf -v shown '%q' "$name"
    warn "  ignore  source folder $shown — not a valid skill slug (lowercase-kebab); rename it to install it"
    continue
  fi
  skills+=("$name")
  SRC_SET="$SRC_SET$NL$name=1"
done

if [ "${#skills[@]}" -eq 0 ]; then
  err "no skills found under $SOURCE_DIR — nothing to install"
  exit "$EXIT_ERROR"
fi

if [ "$DRY_RUN" -eq 0 ]; then
  RUN_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$TARGET_DIR" || { err "could not create target directory $TARGET_DIR"; exit "$EXIT_ERROR"; }
fi
if [ -d "$TARGET_DIR" ]; then
  TARGET_DIR="$(cd -P "$TARGET_DIR" && pwd)"
  if [ "$TARGET_DIR" = "$SOURCE_DIR" ]; then
    err "target $TARGET_DIR is the source checkout itself; refusing."
    exit "$EXIT_ERROR"
  fi
fi

log "Source: $SOURCE_DIR"
log "Target: $TARGET_DIR"
[ "$DRY_RUN" -eq 1 ] && log "(dry run — nothing will be written, not even a lock or manifest)"
log ""
log "Found ${#skills[@]} skill(s) in source."

# --- Lock, then load what we previously installed ------------------------
[ "$DRY_RUN" -eq 1 ] || acquire_lock
[ "$DRY_RUN" -eq 0 ] || [ ! -e "$TARGET_DIR/$LOCK_NAME" ] || \
  warn "lock $TARGET_DIR/$LOCK_NAME exists — a real run would fail until it is cleared"

manifest_path="$TARGET_DIR/$MANIFEST_NAME"
if [ -L "$manifest_path" ]; then
  err "$manifest_path is a symlink; refusing to read or replace it."
  exit "$EXIT_ERROR"
fi
if [ -e "$manifest_path" ]; then
  [ -f "$manifest_path" ] || { err "$manifest_path exists but is not a regular file."; exit "$EXIT_ERROR"; }
  read_manifest "$manifest_path" || exit "$EXIT_ERROR"
  if [ "$MANIFEST_FORMAT" -eq 1 ]; then
    log "Note: upgrading manifest from format 1 to 2 (adds a per-skill sha256 so local edits are detected)."
  fi
fi
MANIFEST_SOURCE="$SOURCE_DIR"
STATE_LOADED=1

# Hash what is currently installed (managed slugs + potential collisions).
managed=()
while IFS='=' read -r k v; do
  [ -n "$k" ] && managed+=("$k")
done <<EOF
$STATE
EOF
hash_batch "$TARGET_DIR" "${skills[@]}" ${managed[@]+"${managed[@]}"} || exit "$EXIT_ERROR"
CUR_MAP="$HB_MAP"
SRC_MAP=""

# Format-1 entries ("-") carry no install-time hash, so we try to prove each
# one unedited and record its current sha256 as that hash:
#  - its SKILL.md equals the current source copy, or
#  - the source is a git checkout and the file's git blob id equals a blob
#    that <slug>/SKILL.md has had in HEAD's history (an older release; this
#    is the usual case after `git pull`).
# Anything else stays "-" and is never overwritten or removed without --force.
# Two git processes in total; no git, or no repo, just means fewer proofs.
GIT_CHECKED=""  # kv set of the slugs the git-history comparison really covered
verify_unhashed_entries() {
  local s cur out line meta rest path hist="" n=0 i=0
  local cands=() rels=() files=() ids=()
  for s in ${managed[@]+"${managed[@]}"}; do
    kv_get "$STATE" "$s" && [ "$REPLY" = "-" ] || continue
    kv_get "$CUR_MAP" "$s" || continue
    cur="$REPLY"
    if kv_get "$SRC_MAP" "$s" && [ "$REPLY" = "$cur" ]; then
      kv_set STATE "$s" "$cur"; continue
    fi
    cands+=("$s"); rels+=("$s/SKILL.md"); files+=("$TARGET_DIR/$s/SKILL.md")
  done
  [ "${#cands[@]}" -gt 0 ] || return 0
  if ! command -v git >/dev/null 2>&1 || ! out="$(git --no-pager -C "$SOURCE_DIR" --literal-pathspecs \
      log --format= --raw --no-abbrev --no-renames --relative -- "${rels[@]}" 2>/dev/null)"; then
    log "Note: ${#cands[@]} skill(s) with no install-time hash (carried over from a format 1 manifest) differ from the source, and the source is not a git checkout (or git is missing), so they can't be checked against earlier releases."
    return 0
  fi
  # --raw lines: ":<mode> <mode> <old-blob> <new-blob> <status><TAB><path>"
  while IFS= read -r line; do
    line="${line%$'\r'}"
    case "$line" in :*"$TAB"*) ;; *) continue ;; esac
    path="${line#*"$TAB"}"; path="${path%/SKILL.md}"
    meta="${line%%"$TAB"*}"
    rest="${meta#* }"; rest="${rest#* }"
    hist="$hist$NL$path:${rest%% *}"
    rest="${rest#* }"
    hist="$hist$NL$path:${rest%% *}"
  done <<EOF
$out
EOF
  # --no-filters: hash the exact bytes on disk, as the commit stored them.
  out="$(git --no-pager -C "$SOURCE_DIR" hash-object --no-filters -- "${files[@]}" 2>/dev/null)" || return 0
  while IFS= read -r line; do
    line="${line%$'\r'}"
    [ -n "$line" ] && ids+=("$line")
  done <<EOF
$out
EOF
  [ "${#ids[@]}" -eq "${#cands[@]}" ] || return 0
  for s in "${cands[@]}"; do
    if [[ ${ids[$i]} =~ ^[0-9a-f]{40,64}$ ]]; then
      case "$hist$NL" in
        *"$NL$s:${ids[$i]}$NL"*) kv_get "$CUR_MAP" "$s"; kv_set STATE "$s" "$REPLY"; n=$((n + 1)) ;;
      esac
    fi
    GIT_CHECKED="$GIT_CHECKED$NL$s=1"
    i=$((i + 1))
  done
  log "Checked ${#cands[@]} skill(s) with no install-time hash (carried over from a format 1 manifest) against the source's git history: $n match an earlier release (unedited), $((${#cands[@]} - n)) don't."
}
case "$STATE" in
  *=-*)
    hash_batch "$SOURCE_DIR" "${skills[@]}" || exit "$EXIT_ERROR"
    SRC_MAP="$HB_MAP"
    verify_unhashed_entries
    ;;
esac

# --skill filter: say so when a requested slug is neither in the source nor ours.
while IFS='=' read -r k v; do
  [ -n "$k" ] || continue
  kv_get "$SRC_SET" "$k" || kv_get "$STATE" "$k" || \
    warn "  '$k' is neither in the source nor installed by ml-ai-skills — ignoring --skill $k"
done <<EOF
$ONLY_SET
EOF
in_scope() { [ -z "$ONLY_SET" ] || kv_get "$ONLY_SET" "$1"; }

# --- Plan --------------------------------------------------------------
installed=0
updated=0
skipped=0
failed=0
pruned=0
orphaned=0
to_stage=()
ACTIONS=""

# is_unmodified SLUG -> 0 if the installed SKILL.md matches what we recorded
# (format-1 entries: matches the source copy, i.e. nothing to lose).
is_unmodified() {
  local recorded current
  kv_get "$STATE" "$1"; recorded="$REPLY"
  kv_get "$CUR_MAP" "$1" || REPLY="absent"; current="$REPLY"
  if [ "$recorded" = "-" ]; then
    kv_get "$SRC_MAP" "$1" || return 1
    [ "$current" = "$REPLY" ]
    return
  fi
  [ "$current" = "$recorded" ]
}

for slug in "${skills[@]}"; do
  in_scope "$slug" || continue
  src="$SOURCE_DIR/$slug"
  dest="$TARGET_DIR/$slug"
  if [ -L "$src" ]; then
    warn "  refuse  $slug — source folder $src is a symlink; refusing to copy links"
    failed=$((failed + 1)); continue
  fi
  if ! tree_is_safe "$src"; then
    warn "  refuse  $slug — source folder $UNSAFE_REASON; refusing to copy links or special files"
    failed=$((failed + 1)); continue
  fi
  if [ -L "$dest" ]; then
    warn "  refuse  $slug — $dest is a symlink/junction; not following it (remove it, then re-run)"
    failed=$((failed + 1)); continue
  fi
  action="install"
  if [ -e "$dest" ]; then
    if [ ! -d "$dest" ]; then
      warn "  refuse  $slug — $dest exists but is not a directory"
      failed=$((failed + 1)); continue
    fi
    if ! tree_is_safe "$dest"; then
      warn "  refuse  $slug — installed folder $UNSAFE_REASON; inspect it manually"
      failed=$((failed + 1)); continue
    fi
    action="update"
    if kv_get "$STATE" "$slug"; then
      recorded="$REPLY"
      if ! is_unmodified "$slug"; then
        if [ "$FORCE" -eq 1 ]; then
          warn "  update  $slug — discarding local modifications (--force)"
        elif [ "$recorded" = "-" ]; then
          if ! kv_get "$CUR_MAP" "$slug"; then
            why="its folder has no SKILL.md to compare with the repo copy"
          elif kv_get "$GIT_CHECKED" "$slug"; then
            why="it matches neither the repo copy nor an earlier release in the source's git history"
          else
            why="it does not match the repo copy, and it could not be checked against earlier releases (the source is not a git checkout, git is missing, or the git lookup failed)"
          fi
          warn "  keep    $slug — no install-time hash (carried over from a format 1 manifest) and $why, so it may hold your edits; not overwriting. To replace just this skill: ./install.sh --force --skill $slug"
          skipped=$((skipped + 1)); continue
        else
          warn "  keep    $slug — modified locally since it was installed; not overwriting. To replace just this skill: ./install.sh --force --skill $slug"
          skipped=$((skipped + 1)); continue
        fi
      fi
    elif [ "$FORCE" -eq 1 ]; then
      warn "  update  $slug — replacing a folder ml-ai-skills did not install (--force)"
    else
      warn "  skip    $slug — already exists at $dest and is not managed by ml-ai-skills (use --force to overwrite)"
      skipped=$((skipped + 1)); continue
    fi
  fi
  if [ "$action" = "install" ]; then log "  install $slug"; else log "  update  $slug"; fi
  to_stage+=("$slug")
  ACTIONS="$ACTIONS$NL$slug=$action"
done

# Managed skills that disappeared from the source stay tracked (so a later
# --prune or uninstall can still remove them) instead of being orphaned.
to_prune=()
for slug in ${managed[@]+"${managed[@]}"}; do
  kv_get "$SRC_SET" "$slug" && continue
  in_scope "$slug" || continue
  dest="$TARGET_DIR/$slug"
  if [ ! -e "$dest" ] && [ ! -L "$dest" ]; then
    log "  forget  $slug — no longer in the source and not on disk; dropping it from the manifest"
    kv_del STATE "$slug"
    continue
  fi
  if [ "$PRUNE" -eq 0 ]; then
    warn "  orphan  $slug — no longer in the source; left installed and still tracked (re-run with --prune to remove it)"
    orphaned=$((orphaned + 1)); continue
  fi
  if [ -L "$dest" ] || [ ! -d "$dest" ]; then
    warn "  refuse  $slug — $dest is a symlink or not a directory; not pruning it"
    failed=$((failed + 1)); continue
  fi
  if ! tree_is_safe "$dest"; then
    warn "  refuse  $slug — installed folder $UNSAFE_REASON; not pruning it"
    failed=$((failed + 1)); continue
  fi
  if ! is_unmodified "$slug" && [ "$FORCE" -eq 0 ]; then
    warn "  keep    $slug — no longer in the source but modified locally (or unverifiable); not pruning. To remove just this skill: ./install.sh --prune --force --skill $slug"
    skipped=$((skipped + 1)); continue
  fi
  log "  prune   $slug"
  to_prune+=("$slug")
done

summary() {
  log ""
  log "Done. installed=$installed updated=$updated skipped=$skipped failed=$failed pruned=$pruned orphaned=$orphaned target=$TARGET_DIR"
  if [ "$skipped" -gt 0 ]; then
    warn "$skipped skill(s) skipped to protect local edits or folders ml-ai-skills didn't install. To overwrite specific ones: --force --skill SLUG (repeatable); --force alone overwrites all of them."
  fi
  if [ "$failed" -gt 0 ]; then
    err "$failed skill(s) could not be processed safely (see messages above)."
  fi
}

if [ "$DRY_RUN" -eq 1 ]; then
  for slug in ${to_stage[@]+"${to_stage[@]}"}; do
    kv_get "$ACTIONS" "$slug" || true
    if [ "$REPLY" = "install" ]; then installed=$((installed + 1)); else updated=$((updated + 1)); fi
  done
  pruned="${#to_prune[@]}"
  summary
  log "(dry run — nothing was written)"
  [ "$failed" -eq 0 ] || exit "$EXIT_ERROR"
  exit 0
fi

# --- Stage everything first: a failed copy changes nothing ----------------
if [ "${#to_stage[@]}" -gt 0 ]; then
  mkdir "$WORK_DIR/stage" "$WORK_DIR/old" || exit "$EXIT_ERROR"
  srcs=()
  for slug in "${to_stage[@]}"; do srcs+=("$SOURCE_DIR/$slug"); done
  if [ "${ML_AI_SKILLS_FAULT_INJECT:-}" = "stage" ] || ! cp -R "${srcs[@]}" "$WORK_DIR/stage/"; then
    err "copying skills into the staging area failed; nothing was changed."
    exit "$EXIT_ERROR"
  fi
  # Re-check the copies (closes the window between the source check and cp).
  commit_list=()
  for slug in "${to_stage[@]}"; do
    if [ -L "$WORK_DIR/stage/$slug" ] || ! tree_is_safe "$WORK_DIR/stage/$slug"; then
      warn "  refuse  $slug — staged copy ${UNSAFE_REASON:-is a symlink}"
      failed=$((failed + 1)); continue
    fi
    commit_list+=("$slug")
  done
  hash_batch "$WORK_DIR/stage" ${commit_list[@]+"${commit_list[@]}"} || exit "$EXIT_ERROR"
  NEW_MAP="$HB_MAP"
else
  commit_list=()
  NEW_MAP=""
fi

# --- Commit: per skill, move the old copy aside, then rename the new one in.
# Each rename is atomic; on any failure the old copy is put back.
commit_skill() {  # commit_skill SLUG NEW_HASH
  local slug="$1" dest="$TARGET_DIR/$1" old="$WORK_DIR/old/$1" staged="$WORK_DIR/stage/$1"
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    if [ -L "$dest" ] || [ ! -d "$dest" ] || [ -e "$old" ] || [ -L "$old" ]; then
      err "  $slug: $dest changed during the run; not replacing it"
      return 1
    fi
  fi
  # Armed before the first mv: a signal during it must still trigger rollback.
  TX_NEW_HASH="$2"; TX_FAILED=0; TX_SLUG="$slug"
  if [ -e "$dest" ] && ! mv "$dest" "$old"; then
    err "  $slug: could not move the installed copy aside"
    TX_FAILED=1; rollback_tx; return 1
  fi
  if [ "${ML_AI_SKILLS_FAULT_INJECT:-}" = "commit:$slug" ]; then
    err "  $slug: simulated failure (ML_AI_SKILLS_FAULT_INJECT)"
    TX_FAILED=1; rollback_tx; return 1
  fi
  if [ -e "$dest" ] || [ -L "$dest" ] || ! mv "$staged" "$dest"; then
    err "  $slug: could not move the new copy into place"
    TX_FAILED=1; rollback_tx; return 1
  fi
  kv_set STATE "$slug" "$2"
  TX_SLUG=""
  return 0
}

DISK_CHANGED=1  # from here on the manifest must be rewritten even on failure
for slug in ${commit_list[@]+"${commit_list[@]}"}; do
  if ! kv_get "$NEW_MAP" "$slug"; then
    warn "  refuse  $slug — staged copy has no readable SKILL.md"
    failed=$((failed + 1)); continue
  fi
  if commit_skill "$slug" "$REPLY"; then
    kv_get "$ACTIONS" "$slug" || true
    if [ "$REPLY" = "install" ]; then installed=$((installed + 1)); else updated=$((updated + 1)); fi
  else
    failed=$((failed + 1))
  fi
done

for slug in ${to_prune[@]+"${to_prune[@]}"}; do
  [ -d "$WORK_DIR/old" ] || mkdir "$WORK_DIR/old" || exit "$EXIT_ERROR"
  if mv "$TARGET_DIR/$slug" "$WORK_DIR/old/$slug"; then
    kv_del STATE "$slug"
    pruned=$((pruned + 1))
  else
    err "  $slug: could not remove it"
    failed=$((failed + 1))
  fi
done

FINALIZE=1
summary
[ "$failed" -eq 0 ] || exit "$EXIT_ERROR"
exit 0
