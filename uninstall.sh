#!/usr/bin/env bash
# Remove skills previously installed by install.sh.
#
# Only removes directories this repo's manifest says it installed — never
# touches a same-named skill directory it didn't create, never removes a
# skill you edited after installing it (unless --force), never follows
# symlinks, and never removes the parent skills/ directory itself (other
# tools or the user may use it). See docs/INSTALLER.md.
#
# Usage:
#   ./uninstall.sh                 # remove from ~/.claude/skills/
#   ./uninstall.sh --project       # remove from ./.claude/skills/ (cwd)
#   ./uninstall.sh --project DIR   # remove from DIR/.claude/skills/
#   ./uninstall.sh --target DIR    # remove from DIR directly (advanced/testing)
#   ./uninstall.sh --skill SLUG    # remove only this one skill (repeatable)
#   ./uninstall.sh --force         # also remove skills modified since install
#   ./uninstall.sh --dry-run       # show what would happen, write nothing at all
#   ./uninstall.sh --help
#
# Exit codes: 0 ok (skills kept to protect local edits are warnings) |
# 1 error | 2 usage error (e.g. an invalid --skill slug) | 3 another run
# holds the lock.
set -euo pipefail
# C locale: deterministic `sort`, and ASCII-only ranges in the slug regex.
export LC_ALL=C
# An exported CDPATH would make `cd DIR` (relative DIR) resolve elsewhere and
# echo the new directory into every `$(cd DIR && pwd)`.
unset CDPATH

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

MODE="user"
PROJECT_DIR="."
TARGET_DIR=""
DRY_RUN=0
FORCE=0
ONLY_SLUGS=()

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
      if [ $# -lt 2 ] || [ -z "$2" ]; then err "--target requires a directory argument"; exit "$EXIT_USAGE"; fi
      MODE="target"; TARGET_DIR="$2"; shift 2
      ;;
    --skill)
      [ $# -ge 2 ] || { err "--skill requires a slug argument"; exit "$EXIT_USAGE"; }
      if ! is_valid_slug "$2"; then
        printf -v shown '%q' "$2"
        err "--skill $shown is not a valid skill slug (lowercase-kebab, e.g. data-preprocessing); refusing."
        exit "$EXIT_USAGE"
      fi
      ONLY_SLUGS+=("$2"); shift 2
      ;;
    *) err "unknown argument: $1 (see --help)"; exit "$EXIT_USAGE" ;;
  esac
done
not_an_option "$PROJECT_DIR"; PROJECT_DIR="$REPLY"
not_an_option "$TARGET_DIR"; TARGET_DIR="$REPLY"

for tool in mkdir mv rm sort date; do
  command -v "$tool" >/dev/null 2>&1 || { err "missing required standard tool: $tool"; exit "$EXIT_ERROR"; }
done
detect_sha_tool

case "$MODE" in
  user)    TARGET_DIR="${HOME:?HOME is not set}/.claude/skills" ;;
  project)
    [ -d "$PROJECT_DIR" ] || { err "--project directory does not exist: $PROJECT_DIR"; exit "$EXIT_ERROR"; }
    TARGET_DIR="$(cd "$PROJECT_DIR" && pwd)/.claude/skills"
    ;;
  target)  : ;;
esac

manifest_path="$TARGET_DIR/$MANIFEST_NAME"
if [ -L "$manifest_path" ]; then
  err "$manifest_path is a symlink; refusing to read or replace it."
  exit "$EXIT_ERROR"
fi
if [ ! -f "$manifest_path" ]; then
  err "no ml-ai-skills manifest found at $manifest_path — nothing to uninstall (or wrong --target/--project)"
  exit "$EXIT_ERROR"
fi
TARGET_DIR="$(cd -P "$TARGET_DIR" && pwd)"
manifest_path="$TARGET_DIR/$MANIFEST_NAME"

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$DRY_RUN" -eq 0 ]; then
  RUN_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  acquire_lock
fi

shopt -s nullglob dotglob
read_manifest "$manifest_path" || exit "$EXIT_ERROR"
STATE_LOADED=1
# uninstall.sh never records a hash, so it must not turn a format-1 manifest
# into format 2: in format 2, a '-' means "install.sh tried and could not
# prove this copy", and the next run would then send the user to --force
# instead of to install.sh. It also leaves the manifest alone when nothing
# changed.
MANIFEST_WRITE_FORMAT="$MANIFEST_FORMAT"
STATE_AT_LOAD="$STATE"
finalize_if_changed() { [ "$STATE" = "$STATE_AT_LOAD" ] || FINALIZE=1; }

managed=()
while IFS='=' read -r k v; do
  [ -n "$k" ] && managed+=("$k")
done <<EOF
$STATE
EOF

if [ "${#managed[@]}" -eq 0 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    warn "manifest at $manifest_path lists no valid skills; a real run would remove it"
  else
    warn "manifest at $manifest_path lists no valid skills; removing it"
  fi
  FINALIZE=1
  exit 0
fi

if [ "${#ONLY_SLUGS[@]}" -gt 0 ]; then
  to_check=()
  seen=""
  for slug in "${ONLY_SLUGS[@]}"; do
    kv_get "$seen" "$slug" && continue
    seen="$seen$NL$slug=1"
    if kv_get "$STATE" "$slug"; then
      to_check+=("$slug")
    else
      warn "  '$slug' is not tracked in the manifest — skipping (not ours to remove)"
    fi
  done
else
  to_check=("${managed[@]}")
fi

if [ "${#to_check[@]}" -eq 0 ]; then
  log "Nothing to remove."
  finalize_if_changed
  exit 0
fi

log "Target: $TARGET_DIR"
[ "$DRY_RUN" -eq 1 ] && log "(dry run — nothing will be written, not even a lock or manifest)"

hash_batch "$TARGET_DIR" "${to_check[@]}" || exit "$EXIT_ERROR"
CUR_MAP="$HB_MAP"

removed=0
skipped=0
failed=0
to_remove=()
for slug in "${to_check[@]}"; do
  dest="$TARGET_DIR/$slug"
  if [ -L "$dest" ]; then
    warn "  refuse  $slug — $dest is a symlink/junction; not following it (remove it manually if intended)"
    failed=$((failed + 1)); continue
  fi
  if [ ! -e "$dest" ]; then
    warn "  '$slug' was in the manifest but not found on disk at $dest (already removed?)"
    kv_del STATE "$slug"
    continue
  fi
  if [ ! -d "$dest" ]; then
    warn "  refuse  $slug — $dest is not a directory"
    failed=$((failed + 1)); continue
  fi
  if ! tree_is_safe "$dest"; then
    warn "  refuse  $slug — installed folder $UNSAFE_REASON; inspect it manually"
    failed=$((failed + 1)); continue
  fi
  kv_get "$STATE" "$slug"; recorded="$REPLY"
  kv_get "$CUR_MAP" "$slug" || REPLY="absent"; current="$REPLY"
  if [ "$FORCE" -eq 0 ]; then
    if [ "$recorded" = "-" ]; then
      if [ "$MANIFEST_FORMAT" -eq 1 ]; then
        warn "  keep    $slug — the manifest predates modification tracking (format 1), so local edits can't be ruled out. Run ./install.sh once (it records hashes for copies it can prove unedited, including earlier releases from a git checkout), or remove just this skill with --force --skill $slug."
      else
        warn "  keep    $slug — no install-time hash (carried over from a format 1 manifest; install.sh could not prove this copy unedited), so it may hold your edits. Inspect it, then remove just this skill with --force --skill $slug."
      fi
      skipped=$((skipped + 1)); continue
    fi
    if [ "$current" != "$recorded" ]; then
      warn "  keep    $slug — modified locally since it was installed; not removing. To remove just this skill anyway: --force --skill $slug"
      skipped=$((skipped + 1)); continue
    fi
  fi
  log "  remove  $slug"
  to_remove+=("$slug")
done

summary() {
  log ""
  log "Done. removed=$removed skipped=$skipped failed=$failed target=$TARGET_DIR"
  log "(the $TARGET_DIR directory itself was left in place)"
  if [ "$failed" -gt 0 ]; then err "$failed skill(s) could not be processed safely (see messages above)."; fi
}

if [ "$DRY_RUN" -eq 1 ]; then
  removed="${#to_remove[@]}"
  summary
  log "(dry run — nothing was written)"
  [ "$failed" -eq 0 ] || exit "$EXIT_ERROR"
  exit 0
fi

# Move everything aside in one mv (forks are slow on some Windows hosts);
# the folders are deleted with the lock directory once the manifest is safe.
if [ "${#to_remove[@]}" -gt 0 ]; then
  DISK_CHANGED=1  # from here on the manifest must be rewritten even on failure
  mkdir "$WORK_DIR/old" || exit "$EXIT_ERROR"
  paths=()
  for slug in "${to_remove[@]}"; do paths+=("$TARGET_DIR/$slug"); done
  mv "${paths[@]}" "$WORK_DIR/old/" || true
  for slug in "${to_remove[@]}"; do
    if [ ! -e "$TARGET_DIR/$slug" ] && [ ! -L "$TARGET_DIR/$slug" ] && [ -d "$WORK_DIR/old/$slug" ]; then
      kv_del STATE "$slug"
      removed=$((removed + 1))
    else
      err "  $slug: could not remove it"
      failed=$((failed + 1))
    fi
  done
fi

finalize_if_changed
summary
[ "$failed" -eq 0 ] || exit "$EXIT_ERROR"
exit 0
