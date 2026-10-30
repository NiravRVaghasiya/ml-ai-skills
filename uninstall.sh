#!/usr/bin/env bash
# Remove skills previously installed by install.sh.
#
# Only removes directories this repo's manifest says it installed — never
# touches a same-named skill directory it didn't create, never removes a
# skill you edited after installing it (any file in its folder; unless
# --force), never moves a folder that is or contains a mount point, never follows
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
MANIFEST_FORMAT_CURRENT=3
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

# is_valid_relpath P: a '/'-separated relative path with no empty, '.' or
# '..' component and no control character. Hygiene for manifest file lines
# only: their paths are compared with what is on disk, never used as paths.
is_valid_relpath() {
  case "/$1/" in
    *//*|*/./*|*/../*|*[[:cntrl:]]*) return 1 ;;
  esac
  return 0
}

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
# ships bash 3.2, which has no associative arrays. Keys are validated slugs;
# values never contain a newline. Only keys ever appear inside a pattern, so
# a value may hold any other character (file lists hold file names).
kv_get() {  # kv_get MAP KEY -> REPLY=value; status 1 if absent
  case "$1" in
    *"$NL$2="*) REPLY="${1#*"$NL$2="}"; REPLY="${REPLY%%"$NL"*}"; return 0 ;;
  esac
  REPLY=""
  return 1
}
kv_del() {  # kv_del VARNAME KEY
  local m="${!1}$NL" tail
  case "$m" in
    *"$NL$2="*)
      tail="${m#*"$NL$2="}"
      m="${m%%"$NL$2="*}$NL${tail#*"$NL"}"
      ;;
  esac
  printf -v "$1" '%s' "${m%"$NL"}"
}
kv_set() {  # kv_set VARNAME KEY VALUE
  kv_del "$1" "$2"
  printf -v "$1" '%s' "${!1}$NL$2=$3"
}

SHA_CMD=()
detect_sha_tool() {
  # Refuse rather than degrade: without hashes we could not keep the promise
  # "never overwrite or delete a skill you edited". Each tool prints one
  # "<sha256> *<name>" line per file, in argument order.
  if command -v sha256sum >/dev/null 2>&1; then SHA_CMD=(sha256sum -b)
  elif command -v shasum >/dev/null 2>&1; then SHA_CMD=(shasum -a 256 -b)
  elif command -v openssl >/dev/null 2>&1 && openssl dgst -sha256 -r </dev/null >/dev/null 2>&1; then
    SHA_CMD=(openssl dgst -sha256 -r)
  else
    err "need 'sha256sum' (GNU coreutils), 'shasum' (macOS/Perl) or 'openssl' (with 'dgst -r') to detect locally modified skills; install one and re-run."
    exit "$EXIT_ERROR"
  fi
}

# tree_is_safe DIR: every entry below DIR is a real directory or a regular
# file, and no name in it has a control character (a TAB or newline would
# break the manifest's line format). Pure bash (no find(1)) and never follows
# links. Also lists, relative to DIR and in glob order, every regular file
# (TREE_FILES) and every directory (TREE_DIRS) below DIR.
UNSAFE_REASON=""
TREE_FILES=()
TREE_DIRS=()
tree_is_safe() {
  TREE_FILES=()
  TREE_DIRS=()
  tree_walk "$1" ""
}
tree_walk() {  # tree_walk DIR RELATIVE-PREFIX
  local e name
  for e in "$1"/*; do
    name="${e##*/}"
    case "$name" in
      *[[:cntrl:]]*) printf -v UNSAFE_REASON 'contains a name with a control character: %q' "$e"; return 1 ;;
    esac
    if [ -L "$e" ]; then UNSAFE_REASON="contains a symlink/junction: $e"; return 1
    elif [ -d "$e" ]; then TREE_DIRS+=("$2$name"); tree_walk "$e" "$2$name/" || return 1
    elif [ -f "$e" ]; then TREE_FILES+=("$2$name")
    else UNSAFE_REASON="contains a non-regular file: $e"; return 1
    fi
  done
  return 0
}

# hash_trees BASE SLUG... : sha256 of every regular file of every BASE/SLUG
# folder that is a real directory with a safe tree (the others are skipped;
# callers refuse them), in one process for all files (forks cost ~0.3s each
# on some Windows hosts), named by relative, checked paths. Results:
#   HB_MAP  slug=sha256 of its SKILL.md, when that is a regular file
#   HF_MAP  slug=<TAB>sha256 relpath<TAB>sha256 relpath... for every file,
#           relpath relative to the skill folder ("slug=" for an empty folder)
# Output lines are matched to files by position, never by parsing names back.
HB_MAP=""
HF_MAP=""
hash_trees() {
  local base="$1" s f line h out seen="" cur="" acc="" i=0
  shift
  local rels=() owners=() safe=()
  HB_MAP=""
  HF_MAP=""
  for s in "$@"; do
    kv_get "$seen" "$s" && continue
    seen="$seen$NL$s=1"
    if [ -L "$base/$s" ] || [ ! -d "$base/$s" ] || ! tree_is_safe "$base/$s"; then continue; fi
    safe+=("$s")
    for f in ${TREE_FILES[@]+"${TREE_FILES[@]}"}; do rels+=("$s/$f"); owners+=("$s"); done
  done
  if [ "${#rels[@]}" -gt 0 ]; then
    if ! out="$(cd "$base" && "${SHA_CMD[@]}" "${rels[@]}")"; then
      err "failed to hash the skill files under $base"
      return 1
    fi
    while IFS= read -r line; do
      line="${line%$'\r'}"
      line="${line#\\}"  # sha256sum/shasum prefix '\' when they had to escape a name
      h="${line:0:64}"
      if [ "$i" -ge "${#rels[@]}" ] || ! [[ $h =~ $HASH_RE ]]; then
        err "unexpected output from ${SHA_CMD[0]} while hashing the skill files under $base"
        return 1
      fi
      s="${owners[$i]}"
      f="${rels[$i]:$((${#s} + 1))}"
      if [ "$s" != "$cur" ]; then
        [ -z "$cur" ] || HF_MAP="$HF_MAP$NL$cur=$acc"
        cur="$s"
        acc=""
      fi
      acc="$acc$TAB$h $f"
      [ "$f" != "SKILL.md" ] || HB_MAP="$HB_MAP$NL$s=$h"
      i=$((i + 1))
    done <<EOF
$out
EOF
    [ -z "$cur" ] || HF_MAP="$HF_MAP$NL$cur=$acc"
    if [ "$i" -ne "${#rels[@]}" ]; then
      err "unexpected output from ${SHA_CMD[0]} while hashing the skill files under $base"
      return 1
    fi
  fi
  for s in ${safe[@]+"${safe[@]}"}; do
    kv_get "$HF_MAP" "$s" || HF_MAP="$HF_MAP$NL$s="
  done
  return 0
}

# _list_add LISTVAR COUNTVAR NAME: count NAME, and append it to the
# ", "-separated LISTVAR while that holds fewer than 3 names.
_list_add() {
  local _la_list _la_n
  _la_list="${!1}"
  _la_n=$((${!2} + 1))
  printf -v "$2" '%s' "$_la_n"
  [ "$_la_n" -le 3 ] || return 0
  if [ -n "$_la_list" ]; then _la_list="$_la_list, $3"; else _la_list="$3"; fi
  printf -v "$1" '%s' "$_la_list"
}
_list_more() {  # _list_more COUNT -> REPLY=" (+N more)" past 3 names, else ""
  REPLY=""
  [ "$1" -le 3 ] || REPLY=" (+$(($1 - 3)) more)"
}

# files_diff RECORDED CURRENT: status 0 if two HF_MAP-style file lists hold
# the same files with the same hashes; otherwise DIFF_SUMMARY names the
# changed, added and removed files.
DIFF_SUMMARY=""
files_diff() {
  local rec="$1$TAB" rest e p rec_paths="$TAB" cur_paths="$TAB"
  local changed="" added="" removed="" nc=0 na=0 nr=0
  rest="$1"
  while [ -n "$rest" ]; do
    rest="${rest#"$TAB"}"; e="${rest%%"$TAB"*}"; rest="${rest:${#e}}"
    rec_paths="$rec_paths${e:65}$TAB"
  done
  rest="$2"
  while [ -n "$rest" ]; do
    rest="${rest#"$TAB"}"; e="${rest%%"$TAB"*}"; rest="${rest:${#e}}"
    p="${e:65}"
    cur_paths="$cur_paths$p$TAB"
    case "$rec" in *"$TAB$e$TAB"*) continue ;; esac
    case "$rec_paths" in
      *"$TAB$p$TAB"*) _list_add changed nc "$p" ;;
      *) _list_add added na "$p" ;;
    esac
  done
  rest="$1"
  while [ -n "$rest" ]; do
    rest="${rest#"$TAB"}"; e="${rest%%"$TAB"*}"; rest="${rest:${#e}}"
    p="${e:65}"
    case "$cur_paths" in *"$TAB$p$TAB"*) ;; *) _list_add removed nr "$p" ;; esac
  done
  DIFF_SUMMARY=""
  if [ "$nc" -gt 0 ]; then _list_more "$nc"; DIFF_SUMMARY="changed: $changed$REPLY"; fi
  if [ "$na" -gt 0 ]; then _list_more "$na"; DIFF_SUMMARY="${DIFF_SUMMARY:+$DIFF_SUMMARY; }added: $added$REPLY"; fi
  if [ "$nr" -gt 0 ]; then _list_more "$nr"; DIFF_SUMMARY="${DIFF_SUMMARY:+$DIFF_SUMMARY; }removed: $removed$REPLY"; fi
  [ -z "$DIFF_SUMMARY" ]
}

# entry_status SLUG -> ENTRY_STATUS (and ENTRY_DETAIL, naming files): is the
# managed TARGET_DIR/SLUG still exactly what install.sh put there? Reads
# STATE/FILES (the manifest), CUR_MAP/CUR_FILES (hash_trees of TARGET_DIR)
# and SRC_FILES (hash_trees of the source; empty in uninstall.sh).
#   ok         with a per-file record (format 3): the same files with the same
#              hashes, nothing added or removed. An entry carried over from
#              format 1/2 records SKILL.md only: SKILL.md matches, and every
#              other file is byte-identical to the source's copy.
#   modified   differs from the record (a missing SKILL.md counts)
#   unhashed   no install-time hash at all ('-': carried over from format 1)
#   untracked  carried over from format 1/2 and SKILL.md matches, but the
#              folder holds other files that no manifest recorded and that
#              the source does not have byte for byte: they may be the user's
ENTRY_STATUS=""
ENTRY_DETAIL=""
CUR_MAP=""
CUR_FILES=""
SRC_FILES=""
entry_status() {
  local slug="$1" recorded current curf rest e src extra="" nextra=0
  ENTRY_DETAIL=""
  kv_get "$STATE" "$slug"; recorded="$REPLY"
  kv_get "$CUR_MAP" "$slug" || REPLY="absent"; current="$REPLY"
  kv_get "$CUR_FILES" "$slug" || REPLY=""; curf="$REPLY"
  if kv_get "$FILES" "$slug"; then
    if files_diff "$REPLY" "$curf" && [ "$current" = "$recorded" ]; then ENTRY_STATUS="ok"; return 0; fi
    ENTRY_STATUS="modified"
    ENTRY_DETAIL="${DIFF_SUMMARY:-SKILL.md does not match the hash on its skill line}"
    return 0
  fi
  if [ "$recorded" = "-" ]; then ENTRY_STATUS="unhashed"; return 0; fi
  if [ "$current" != "$recorded" ]; then
    ENTRY_STATUS="modified"
    if [ "$current" = "absent" ]; then ENTRY_DETAIL="removed: SKILL.md"; else ENTRY_DETAIL="changed: SKILL.md"; fi
    return 0
  fi
  kv_get "$SRC_FILES" "$slug" || REPLY=""
  src="$REPLY$TAB"
  rest="$curf"
  while [ -n "$rest" ]; do
    rest="${rest#"$TAB"}"; e="${rest%%"$TAB"*}"; rest="${rest:${#e}}"
    [ "${e:65}" != "SKILL.md" ] || continue
    case "$src" in *"$TAB$e$TAB"*) continue ;; esac
    _list_add extra nextra "${e:65}"
  done
  if [ "$nextra" -eq 0 ]; then ENTRY_STATUS="ok"; return 0; fi
  _list_more "$nextra"
  ENTRY_STATUS="untracked"
  ENTRY_DETAIL="$extra$REPLY"
}

# read_manifest PATH -> STATE (slug=sha256 of SKILL.md, or slug=- when
# unknown: a format-1 entry), FILES (slug=file list as in hash_trees; format
# 3 only), MANIFEST_FORMAT, MANIFEST_SOURCE.
#   format 1: bare slug lines
#   format 2: '# format: 2' header + 'slug<TAB>sha256' lines
#   format 3: format 2, plus one 'slug<TAB>sha256<TAB>relpath' line for every
#             file in that skill's folder. A skill line without file lines is
#             an entry carried over from format 1/2.
# A file line's path is only ever compared with what is on disk, never used
# as a path, so a wrong, missing or extra one can only make a skill count as
# modified (kept), never touch anything else.
STATE=""
FILES=""
MANIFEST_FORMAT=0
MANIFEST_SOURCE=""
read_manifest() {
  local path="$1" line slug hash rel rest shown lineno=0 fmt=1 flines="" fpaths=""
  STATE=""
  FILES=""
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
      *"$TAB"*"$TAB"*)
        # File line. Its path is everything after the second TAB, untrimmed,
        # so a name ending in a space still matches the disk.
        rest="${line#"${line%%[![:space:]]*}"}"
        slug="${rest%%"$TAB"*}"; rest="${rest#*"$TAB"}"
        hash="${rest%%"$TAB"*}"; rel="${rest#*"$TAB"}"
        trim "$slug"; slug="$REPLY"
        trim "$hash"; hash="$REPLY"
        if ! is_valid_slug "$slug" || ! [[ $hash =~ $HASH_RE ]] || ! is_valid_relpath "$rel"; then
          warn "ignoring manifest line $lineno: not a valid 'slug<TAB>sha256<TAB>file' entry (never used as a path; a skill whose files no longer match its record is kept as modified)"
          continue
        fi
        flines="$flines$NL$lineno$TAB$slug$TAB$hash $rel"
        continue
        ;;
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
    1|2|3) MANIFEST_FORMAT="$fmt" ;;
    *)
      printf -v shown '%q' "$fmt"
      err "manifest $path declares format $shown; this script understands formats 1 to 3. Update your ml-ai-skills checkout."
      return 1
      ;;
  esac
  [ -n "$flines" ] || return 0
  if [ "$fmt" != 3 ]; then
    warn "ignoring the per-file lines in manifest $path: only a format 3 manifest has them"
    return 0
  fi
  # Attach file lines to their skills; the first line for a file wins.
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    lineno="${line%%"$TAB"*}"; rest="${line#*"$TAB"}"
    slug="${rest%%"$TAB"*}"; rest="${rest#*"$TAB"}"
    rel="${rest:65}"
    if ! kv_get "$STATE" "$slug"; then
      warn "ignoring manifest line $lineno: no skill line for '$slug'"
      continue
    fi
    kv_get "$fpaths" "$slug" || REPLY=""
    case "$REPLY$TAB" in
      *"$TAB$rel$TAB"*) warn "ignoring manifest line $lineno: a second entry for a file of '$slug' (the first one counts)"; continue ;;
    esac
    kv_set fpaths "$slug" "$REPLY$TAB$rel"
    kv_get "$FILES" "$slug" || REPLY=""
    kv_set FILES "$slug" "$REPLY$TAB$rest"
  done <<EOF
$flines
EOF
}

# Lock = a directory created with mkdir (atomic everywhere, incl. Git Bash).
# It also holds this run's staging area and the previous copies of replaced
# skills, so a crash leaves everything in one obvious place, on the target's
# own filesystem (see check_filesystems).
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

# check_filesystems SLUG... : TARGET_DIR/SLUG, every directory below it and
# the staging area must all be on TARGET_DIR's filesystem (the same `stat`
# device id: GNU `stat -c %d`, else BSD `stat -f %d`, in one process). A
# folder that is, or contains, a mount point can't be moved aside with one
# atomic rename (mv may fall back to copy+delete), and deleting the old copy
# with the lock directory would delete files on that other filesystem. Each
# such SLUG is reported and added to FS_REFUSED. Status 1 (with an error) if
# the device ids can't be read, or the staging area is on another filesystem:
# the caller then stops before changing anything. Device ids can't see a bind
# mount of the same filesystem, so the mount table is checked too
# (mounts_below).
FS_REFUSED=""
check_filesystems() {
  local s d out line first i=0 paths owners ids
  paths=("$TARGET_DIR")
  owners=(".")
  ids=()
  FS_REFUSED=""
  [ "$#" -gt 0 ] || return 0
  if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then paths+=("$WORK_DIR"); owners+=(".."); fi
  for s in "$@"; do
    paths+=("$TARGET_DIR/$s"); owners+=("$s")
    tree_is_safe "$TARGET_DIR/$s" || continue
    for d in ${TREE_DIRS[@]+"${TREE_DIRS[@]}"}; do paths+=("$TARGET_DIR/$s/$d"); owners+=("$s"); done
  done
  if ! out="$(stat -c %d -- "${paths[@]}" 2>/dev/null)" && ! out="$(stat -f %d -- "${paths[@]}" 2>/dev/null)"; then
    err "could not read which filesystem $TARGET_DIR and the folders this run would replace or remove are on (stat failed), so a mount point there can't be ruled out; nothing was changed."
    return 1
  fi
  while IFS= read -r line; do
    ids+=("${line%$'\r'}")
  done <<EOF
$out
EOF
  if [ "${#ids[@]}" -ne "${#paths[@]}" ]; then
    err "unexpected output from stat while checking for mount points under $TARGET_DIR; nothing was changed."
    return 1
  fi
  first="${ids[0]}"
  while [ "$i" -lt "${#paths[@]}" ]; do
    if [ "${ids[$i]}" != "$first" ]; then
      s="${owners[$i]}"
      if [ "$s" = ".." ]; then
        err "the staging area $WORK_DIR is not on the same filesystem as $TARGET_DIR; refusing (nothing was changed)."
        return 1
      fi
      if ! kv_get "$FS_REFUSED" "$s"; then
        FS_REFUSED="$FS_REFUSED$NL$s=1"
        warn "  refuse  $s — ${paths[$i]} is on a different filesystem than $TARGET_DIR (a mount point): it can't be moved with one atomic rename, and deleting the old copy would reach into that filesystem. Unmount or move it, then re-run"
      fi
    fi
    i=$((i + 1))
  done
  mounts_below "$@"
}

# mounts_below SLUG... : where the kernel's mount table is readable
# (/proc/self/mountinfo: Linux, also Cygwin/MSYS), every SLUG whose
# TARGET_DIR/SLUG is, or has below it, a mount point is reported and added to
# FS_REFUSED. This catches a bind mount of the same filesystem, which has the
# same device id: deleting the old copy would delete the files mounted there,
# which live elsewhere. Field 5 of a line is the mount point, with ' ', TAB,
# newline and backslash written as \040, \011, \012 and \134; TARGET_DIR is a
# physical path (cd -P), as the mount points are. Without a mount table (macOS,
# BSD) only the device ids are checked. ML_AI_SKILLS_TEST_MOUNTINFO names an
# extra file in the same format, read *in addition*, so tests can only add
# mount points, never hide one.
MOUNTINFO="/proc/self/mountinfo"
mounts_below() {
  local f mp s rest out bs="\\" want="" prefix="${TARGET_DIR%/}/"
  for s in "$@"; do want="$want$NL$s=1"; done
  for f in "$MOUNTINFO" ${ML_AI_SKILLS_TEST_MOUNTINFO:+"$ML_AI_SKILLS_TEST_MOUNTINFO"}; do
    [ -f "$f" ] && [ -r "$f" ] || continue
    while IFS=' ' read -r _ _ _ _ mp _; do
      case "$mp" in
        *"$bs"*)
          out=""
          while :; do
            case "$mp" in *"$bs"*) ;; *) break ;; esac
            out="$out${mp%%"$bs"*}"
            mp="${mp#*"$bs"}"
            case "$mp" in
              040*) out="$out "; mp="${mp:3}" ;;
              011*) out="$out$TAB"; mp="${mp:3}" ;;
              012*) out="$out$NL"; mp="${mp:3}" ;;
              134*) out="$out$bs"; mp="${mp:3}" ;;
              *) out="$out$bs" ;;
            esac
          done
          mp="$out$mp"
          ;;
      esac
      case "$mp" in "$prefix"?*) ;; *) continue ;; esac
      rest="${mp#"$prefix"}"
      s="${rest%%/*}"
      if ! is_valid_slug "$s" || ! kv_get "$want" "$s"; then continue; fi
      kv_get "$FS_REFUSED" "$s" && continue
      FS_REFUSED="$FS_REFUSED$NL$s=1"
      warn "  refuse  $s — $mp is a mount point (listed in $f): deleting the old copy would delete the files mounted there, which are not part of $TARGET_DIR. Unmount it, then re-run"
    done < "$f"
  done
  return 0
}

# write_manifest: atomic (temp file + rename). Only entries that still exist
# on disk are recorded, so the manifest describes reality after a partial run.
# MANIFEST_WRITE_FORMAT is the current format for install.sh. uninstall.sh
# keeps the format it read: format 1 keeps the legacy layout (no '# format:'
# line, bare slugs for unknown hashes) so a later run still knows install.sh
# has not yet tried to prove those copies; format 2 has no file lines.
MANIFEST_WRITE_FORMAT="$MANIFEST_FORMAT_CURRENT"
write_manifest() {
  local path="$TARGET_DIR/$MANIFEST_NAME" tmp="$WORK_DIR/manifest.tmp" k v body="" src rest e
  while IFS='=' read -r k v; do
    [ -n "$k" ] || continue
    if [ -e "$TARGET_DIR/$k" ] || [ -L "$TARGET_DIR/$k" ]; then
      if [ "$MANIFEST_WRITE_FORMAT" = 1 ] && [ "$v" = "-" ]; then body="$body$k$NL"; else body="$body$k$TAB$v$NL"; fi
      if [ "$MANIFEST_WRITE_FORMAT" -ge 3 ] && kv_get "$FILES" "$k"; then
        rest="$REPLY"
        while [ -n "$rest" ]; do
          rest="${rest#"$TAB"}"; e="${rest%%"$TAB"*}"; rest="${rest:${#e}}"
          body="$body$k$TAB${e:0:64}$TAB${e:65}$NL"
        done
      fi
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
    # By slug, then by file path (a skill line has no 3rd field, so it comes first).
    printf '%s' "$body" | sort -t "$TAB" -k1,1 -k3
  } > "$tmp" || return 1
  mv -f "$tmp" "$path" || return 1
}

# record_entry SLUG SKILL_MD_HASH FILE_LIST: a skill this run installed.
record_entry() {
  kv_set STATE "$1" "$2"
  kv_set FILES "$1" "$3"
}

# In-flight replace of one skill (install.sh). TX_SLUG is armed BEFORE
# anything moves, and rollback decides from the disk, not from a progress
# flag: bash runs a signal trap only after the running child (e.g. the
# move-aside mv) exits, so a flag set after that mv would lag behind reality.
#   staged copy gone, dest present  -> the new copy landed: record its hashes
#   dest present otherwise          -> nothing was replaced, or a folder appeared
#                                      there: never delete an old copy we hold
#   dest absent, old copy present   -> moved aside but not replaced: restore it
# TX_FAILED=1 (an explicit failure) never counts as "the new copy landed".
TX_SLUG=""
TX_NEW_HASH=""
TX_NEW_FILES=""
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
      record_entry "$slug" "$TX_NEW_HASH" "$TX_NEW_FILES"
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

for tool in mkdir mv rm sort date stat; do
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
# uninstall.sh never records a hash, so it must not upgrade the manifest's
# format: in format 2 or 3, a '-' means "install.sh tried and could not
# prove this copy", and the next run would then send the user to --force
# instead of to install.sh; a format-2 entry would claim a per-file record it
# does not have. It also leaves the manifest alone when nothing changed.
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

hash_trees "$TARGET_DIR" "${to_check[@]}" || exit "$EXIT_ERROR"
CUR_MAP="$HB_MAP"
CUR_FILES="$HF_MAP"
SRC_FILES=""  # no source here: a file no manifest recorded can't be proven ours

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
  if [ "$FORCE" -eq 0 ]; then
    entry_status "$slug"
    case "$ENTRY_STATUS" in
      ok) ;;
      unhashed)
        if [ "$MANIFEST_FORMAT" -eq 1 ]; then
          warn "  keep    $slug — the manifest predates modification tracking (format 1), so local edits can't be ruled out. Run ./install.sh once (it records hashes for copies it can prove unedited, including earlier releases from a git checkout), or remove just this skill with --force --skill $slug."
        else
          warn "  keep    $slug — no install-time hash (carried over from a format 1 manifest; install.sh could not prove this copy unedited), so it may hold your edits. Inspect it, then remove just this skill with --force --skill $slug."
        fi
        skipped=$((skipped + 1)); continue
        ;;
      untracked)
        warn "  keep    $slug — the manifest recorded only its SKILL.md (format $MANIFEST_FORMAT predates per-file tracking, or the entry was carried over from it), and the folder also holds files no manifest recorded ($ENTRY_DETAIL), which may be yours. Run ./install.sh once (it records per-file hashes when those files match the repo copy), or inspect them and remove just this skill with --force --skill $slug."
        skipped=$((skipped + 1)); continue
        ;;
      *)
        warn "  keep    $slug — modified locally since it was installed ($ENTRY_DETAIL); not removing. To remove just this skill anyway: --force --skill $slug"
        skipped=$((skipped + 1)); continue
        ;;
    esac
  fi
  log "  remove  $slug"
  to_remove+=("$slug")
done

# A folder that is (or contains) a mount point is refused (check_filesystems).
check_filesystems ${to_remove[@]+"${to_remove[@]}"} || exit "$EXIT_ERROR"
if [ -n "$FS_REFUSED" ]; then
  keep=()
  for slug in "${to_remove[@]}"; do
    if kv_get "$FS_REFUSED" "$slug"; then failed=$((failed + 1)); else keep+=("$slug"); fi
  done
  to_remove=(${keep[@]+"${keep[@]}"})
fi

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
