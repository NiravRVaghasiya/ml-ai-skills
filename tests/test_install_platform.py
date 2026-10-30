"""Platform assumptions of install.sh / uninstall.sh / bin/ml-ai-skills.

- Required tools: with no SHA-256 tool (sha256sum, shasum, openssl) or no
  `stat` on PATH the scripts refuse before writing anything; shasum and
  openssl work as fallbacks. Simulated with a PATH that holds only shims for
  an explicit allowlist of tools.
- Mount points: a folder to be replaced or removed that is, or contains, a
  mount point is refused (docs/INSTALLER.md, guarantee 12). Simulated with a
  `stat` shim (GNU and BSD styles) for another filesystem, and with extra
  mount-table lines (ML_AI_SKILLS_TEST_MOUNTINFO) for a bind mount of the same
  filesystem; with a real tmpfs mount and a real bind mount on Linux when an
  unprivileged mount namespace (`unshare -rm`) is available.
- Portability: a static check that the scripts use no bash-4-only syntax and
  no GNU-only flags (macOS ships bash 3.2 and BSD tools; neither is run
  here), with positive controls so the check cannot pass vacuously.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from testutil import REPO_ROOT

from test_install_security import (BASH, INSTALL_SH, LOCK_NAME, SCRIPT_TIMEOUT, UNINSTALL_SH, Base,
                                   skill_text, snapshot)

SCRIPTS = (INSTALL_SH, UNINSTALL_SH, REPO_ROOT / "bin" / "ml-ai-skills")
BASE_TOOLS = ("cp", "mkdir", "mv", "rm", "sort", "date", "stat")


def which_in_bash(tool: str) -> str:
    if not BASH:
        return ""
    r = subprocess.run([BASH, "-c", f"command -v {tool}"], capture_output=True, text=True, timeout=60)
    path = r.stdout.strip()
    return path if r.returncode == 0 and path.startswith("/") else ""


class ShimBase(Base):
    """PATH = one directory of `exec /real/tool "$@"` shims, nothing else."""

    def shim_path(self, tools, extra: dict[str, str] | None = None) -> str:
        d = self.root / "shimbin"
        if d.exists():
            shutil.rmtree(d)
        d.mkdir()
        for tool in tools:
            real = which_in_bash(tool)
            if not real:
                self.skipTest(f"'{tool}' not found in bash's PATH")
            self.write_exe(d / tool, f'#!/bin/sh\nexec "{real}" "$@"\n')
        for name, body in (extra or {}).items():
            self.write_exe(d / name, body)
        return str(d)

    @staticmethod
    def write_exe(path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8", newline="\n")
        os.chmod(path, 0o755)  # noqa: S103 - shims must be executable to be found on PATH


class TestRequiredTools(ShimBase):
    def test_no_sha256_tool_refuses_before_writing_anything(self):
        self.ok(self.install())
        self.bump_source("2")
        env = self.env(PATH=self.shim_path(BASE_TOOLS))
        before = snapshot(self.root)
        for r in (self.install(env=env), self.install("--dry-run", env=env), self.uninstall("--force", env=env),
                  self.install(env=env, target=self.root / "fresh")):
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("need 'sha256sum' (GNU coreutils), 'shasum' (macOS/Perl) or 'openssl'", r.stderr)
        self.assertEqual(snapshot(self.root), before, "no lock, manifest or target directory was created")

    def test_no_stat_refuses_before_writing_anything(self):
        self.ok(self.install())
        sha = next((t for t in ("sha256sum", "openssl") if which_in_bash(t)), None)
        if sha is None:
            self.skipTest("neither sha256sum nor openssl found in bash's PATH")
        env = self.env(PATH=self.shim_path(("cp", "mkdir", "mv", "rm", "sort", "date", sha)))
        before = snapshot(self.root)
        for r in (self.install(env=env), self.uninstall(env=env)):
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("missing required standard tool: stat", r.stderr)
        self.assertEqual(snapshot(self.root), before)

    def check_fallback(self, tool: str) -> None:
        if not which_in_bash(tool):
            self.skipTest(f"'{tool}' not found in bash's PATH")
        if tool == "shasum" and not which_in_bash("perl"):
            self.skipTest("shasum needs perl")
        tools = (*BASE_TOOLS, tool) + (("perl",) if tool == "shasum" else ())
        env = self.env(PATH=self.shim_path(tools))
        (self.src / "beta" / "ref").mkdir()
        (self.src / "beta" / "ref" / "notes é.md").write_text("n", encoding="utf-8")
        self.ok(self.install(env=env))
        self.assert_manifest_matches_disk()
        self.assertEqual(set(self.manifest_files()["beta"]), {"SKILL.md", "ref/notes é.md"})
        (self.target / "alpha" / "SKILL.md").write_text("edit", encoding="utf-8")
        r = self.ok(self.uninstall(env=env))
        self.assertIn("keep    alpha", r.stderr)
        self.assertEqual(sorted(p.name for p in self.target.iterdir() if p.is_dir()), ["alpha"])

    def test_shasum_fallback(self):
        self.check_fallback("shasum")

    def test_openssl_fallback(self):
        self.check_fallback("openssl")


# GNU-style unless ML_TEST_STAT_MODE says otherwise; paths matching the glob
# ML_TEST_STAT_FOREIGN report another device id, as a mount point would.
STAT_SHIM = """#!/bin/sh
case "${ML_TEST_STAT_MODE:-gnu}:$1" in
  fail:*) echo "stat: simulated failure" >&2; exit 1 ;;
  bsd:-c) echo "stat: illegal option -- c" >&2; exit 1 ;;
  gnu:-f) echo "stat: cannot read file system information for '%d'" >&2; exit 1 ;;
esac
[ "$2" = "%d" ] && [ "$3" = "--" ] || exec "$ML_TEST_REAL_STAT" "$@"
shift 3
for p in "$@"; do
  case "$p" in
    $ML_TEST_STAT_FOREIGN) echo 4242 ;;
    *) "$ML_TEST_REAL_STAT" "$ML_TEST_REAL_STAT_STYLE" %d -- "$p" || exit 1 ;;
  esac
done
"""


class TestMountPoints(ShimBase):
    def stat_env(self, foreign: str = "no-match", mode: str = "gnu") -> dict[str, str]:
        real = which_in_bash("stat")
        style = "-c" if subprocess.run([BASH, "-c", 'stat -c %d / >/dev/null 2>&1'],
                                       timeout=60).returncode == 0 else "-f"
        d = self.root / "statbin"
        d.mkdir(exist_ok=True)
        self.write_exe(d / "stat", STAT_SHIM)
        return self.env(PATH=str(d) + os.pathsep + os.environ.get("PATH", ""), ML_TEST_REAL_STAT=real,
                        ML_TEST_REAL_STAT_STYLE=style, ML_TEST_STAT_FOREIGN=foreign, ML_TEST_STAT_MODE=mode)

    def test_bsd_style_stat_is_used_when_gnu_style_fails(self):
        self.ok(self.install())
        self.bump_source("2")
        r = self.ok(self.install(env=self.stat_env(mode="bsd")))
        self.assertIn("updated=3 ", r.stdout)
        r = self.install("--force", env=self.stat_env(foreign="*/beta", mode="bsd"))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("refuse  beta", r.stderr)

    def test_folder_on_another_filesystem_is_refused_and_kept(self):
        (self.src / "beta" / "sub").mkdir()
        (self.src / "beta" / "sub" / "x.md").write_text("x", encoding="utf-8")
        self.ok(self.install())
        self.bump_source("2")
        for foreign in ("*/skills/beta", "*/skills/beta/sub"):  # beta is, or contains, a mount point
            with self.subTest(foreign=foreign):
                env = self.stat_env(foreign=foreign)
                beta_before = self.content("beta")
                before = snapshot(self.root)
                r = self.install("--dry-run", env=env)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("is on a different filesystem than", r.stderr)
                self.assertEqual(snapshot(self.root), before)
                r = self.install(env=env)
                self.assertEqual(r.returncode, 1, r.stderr)
                refuse = [ln for ln in r.stderr.splitlines() if "refuse  beta" in ln]
                self.assertEqual(len(refuse), 1, r.stderr)
                self.assertIn("a mount point", refuse[0])
                self.assertEqual(self.content("beta"), beta_before, "not moved, not replaced")
                self.assertEqual(self.content("alpha"), skill_text("alpha", "2"))
                self.assert_manifest_matches_disk()
                self.assert_no_lock()
                r = self.uninstall("--force", env=env)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("refuse  beta", r.stderr)
                self.assertTrue((self.target / "beta" / "sub" / "x.md").is_file())
                self.assertEqual(set(self.manifest_entries()), {"beta"})
                self.ok(self.install())  # restore alpha and gamma for the next subtest

    def test_staging_area_on_another_filesystem_aborts_before_any_change(self):
        self.ok(self.install())
        self.bump_source("2")
        before = snapshot(self.target)
        r = self.install(env=self.stat_env(foreign=f"*/{LOCK_NAME}"))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("staging area", r.stderr)
        self.assertIn("nothing was changed", r.stderr)
        self.assertEqual(snapshot(self.target), before)

    def test_unreadable_device_ids_abort_before_any_change(self):
        self.ok(self.install())
        self.bump_source("2")
        before = snapshot(self.target)
        env = self.stat_env(mode="fail")
        for r in (self.install(env=env), self.uninstall(env=env)):
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("a mount point there can't be ruled out; nothing was changed", r.stderr)
        self.assertEqual(snapshot(self.target), before)
        r = self.ok(self.install(env=env, target=self.root / "fresh"))
        self.assertIn("installed=3", r.stdout, "a fresh install moves nothing aside, so needs no device ids")

    # --- the mount table: bind mounts of the same filesystem ---------------
    def bash_path(self, p: Path) -> str:
        """p as the scripts see it after `cd -P` (an MSYS path on Windows)."""
        r = subprocess.run([BASH, "-c", 'cd -P -- "$1" && pwd', "_", str(p)], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.rstrip("\n")

    def mount_table_env(self, *mount_points: str) -> dict[str, str]:
        """ML_AI_SKILLS_TEST_MOUNTINFO: extra /proc/self/mountinfo lines, escaped as the kernel does."""
        def esc(m: str) -> str:
            return m.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011").replace("\n", "\\012")
        f = self.root / "mountinfo"
        f.write_text("".join(f"901 25 8:1 /precious {esc(m)} rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
                             for m in mount_points), encoding="utf-8", newline="\n")
        return self.env(ML_AI_SKILLS_TEST_MOUNTINFO=str(f))

    def test_mount_point_in_the_mount_table_is_refused_with_the_same_device_id(self):
        # A bind mount has the same device id, so only the mount table shows it.
        # A space (and on POSIX a backslash) in the path exercises the \040 / \134 escapes.
        self.target = self.root / ("inst root" if os.name == "nt" else "inst\\ root") / "skills"
        (self.src / "beta" / "sub").mkdir()
        (self.src / "beta" / "sub" / "x.md").write_text("x", encoding="utf-8")
        self.ok(self.install())
        t = self.bash_path(self.target)
        env = self.mount_table_env(
            f"{t}/beta/sub",        # beta contains a mount point
            f"{t}/gamma",           # gamma is one
            t, t.rsplit("/", 1)[0],  # the target itself and its parent: not refused
            f"{t}x/alpha", f"{t}/alpha-two", f"{t}/Not_A_Slug/x", f"{t}/{LOCK_NAME}x")  # look-alikes
        self.bump_source("2")
        beta_before, gamma_before = self.content("beta"), self.content("gamma")
        before = snapshot(self.root)
        r = self.install("--force", "--dry-run", env=env)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(snapshot(self.root), before)
        dry_refused = sorted(ln for ln in r.stderr.splitlines() if "refuse" in ln)
        r = self.install("--force", env=env)
        self.assertEqual(r.returncode, 1, r.stderr)
        refused = sorted(ln for ln in r.stderr.splitlines() if "refuse" in ln)
        self.assertEqual(refused, dry_refused, "a dry run reports the same refusals")
        self.assertEqual(len(refused), 2, r.stderr)
        self.assertIn("refuse  beta", refused[0])
        self.assertIn(f"{t}/beta/sub is a mount point (listed in", refused[0])
        self.assertIn("refuse  gamma", refused[1])
        self.assertIn("updated=1 ", r.stdout)
        self.assertIn("failed=2 ", r.stdout)
        self.assertEqual(self.content("alpha"), skill_text("alpha", "2"))
        self.assertEqual((self.content("beta"), self.content("gamma")), (beta_before, gamma_before))
        self.assert_manifest_matches_disk()
        self.assert_no_lock()
        r = self.uninstall("--force", env=env)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("refuse  beta", r.stderr)
        self.assertIn("refuse  gamma", r.stderr)
        self.assertFalse((self.target / "alpha").exists())
        self.assertTrue((self.target / "beta" / "sub" / "x.md").is_file())
        self.assertEqual(set(self.manifest_entries()), {"beta", "gamma"})
        r = self.ok(self.uninstall("--force"))  # the extra table only ever adds refusals
        self.assertIn("removed=2", r.stdout)

    def unshare_or_skip(self) -> str:
        unshare = shutil.which("unshare")
        probe = subprocess.run([unshare, "-rm", "sh", "-c", "mount -t tmpfs mlai-probe /mnt 2>/dev/null"]
                               if unshare else ["false"], capture_output=True, timeout=60)
        if probe.returncode != 0:
            self.skipTest("no unprivileged mount namespace here (`unshare -rm` + tmpfs mount failed; e.g. "
                          "kernel.apparmor_restrict_unprivileged_userns=1 on Ubuntu 24.04 runners)")
        return unshare

    @unittest.skipUnless(sys.platform.startswith("linux"), "real mount points need Linux mount namespaces")
    def test_real_bind_mount_on_the_same_filesystem_is_refused(self):
        unshare = self.unshare_or_skip()
        self.ok(self.install())
        (self.target / "beta" / "mnt").mkdir()
        self.bump_source("2")
        # Inside a private mount namespace: the bind mount vanishes with it.
        # self.victim (outside the target) is what --force would otherwise delete.
        script = r'''
set -u
mount --bind "$VICTIM" "$TARGET/beta/mnt" || exit 97
stat -c %d "$TARGET" "$TARGET/beta/mnt" > "$OUT/devices"
bash "$INSTALL" --source "$SRC" --target "$TARGET" --force > "$OUT/install.out" 2> "$OUT/install.err"
echo "$?" > "$OUT/install.rc"
bash "$UNINSTALL" --target "$TARGET" --force > "$OUT/uninstall.out" 2> "$OUT/uninstall.err"
echo "$?" > "$OUT/uninstall.rc"
'''
        out = self.root / "out"
        out.mkdir()
        env = self.env(TARGET=str(self.target), SRC=str(self.src), OUT=str(out), VICTIM=str(self.victim),
                       INSTALL=str(INSTALL_SH), UNINSTALL=str(UNINSTALL_SH))
        r = subprocess.run([unshare, "-rm", "bash", "-c", script], capture_output=True, text=True, env=env,
                           timeout=SCRIPT_TIMEOUT)
        self.assertEqual(r.returncode, 0, r.stderr)
        devices = (out / "devices").read_text(encoding="utf-8").split()
        self.assertEqual(len(set(devices)), 1, f"a bind mount keeps the device id: {devices}")
        err = (out / "install.err").read_text(encoding="utf-8")
        self.assertEqual((out / "install.rc").read_text(encoding="utf-8").strip(), "1", err)
        self.assertIn("refuse  beta", err)
        self.assertIn("is a mount point (listed in /proc/self/mountinfo)", err)
        uerr = (out / "uninstall.err").read_text(encoding="utf-8")
        self.assertEqual((out / "uninstall.rc").read_text(encoding="utf-8").strip(), "1", uerr)
        self.assertIn("refuse  beta", uerr)
        self.assert_victim_intact()
        self.assertEqual(self.content("beta"), skill_text("beta"))
        self.assertEqual(set(self.manifest_entries()), {"beta"})
        self.assert_no_lock()

    @unittest.skipUnless(sys.platform.startswith("linux"), "real mount points need Linux mount namespaces")
    def test_real_tmpfs_mount_point_is_refused(self):
        unshare = self.unshare_or_skip()
        self.ok(self.install())
        (self.target / "beta" / "mnt").mkdir()
        self.bump_source("2")
        # Inside a private mount namespace: the tmpfs vanishes with it, nothing outside is mounted.
        script = r'''
set -u
mount -t tmpfs mlai-test "$TARGET/beta/mnt" || exit 97
printf 'on the other filesystem' > "$TARGET/beta/mnt/data.md"
bash "$INSTALL" --source "$SRC" --target "$TARGET" --force > "$OUT/install.out" 2> "$OUT/install.err"
echo "$?" > "$OUT/install.rc"
bash "$UNINSTALL" --target "$TARGET" --force > "$OUT/uninstall.out" 2> "$OUT/uninstall.err"
echo "$?" > "$OUT/uninstall.rc"
cat "$TARGET/beta/mnt/data.md" > "$OUT/data.after"
'''
        out = self.root / "out"
        out.mkdir()
        env = self.env(TARGET=str(self.target), SRC=str(self.src), OUT=str(out), INSTALL=str(INSTALL_SH),
                       UNINSTALL=str(UNINSTALL_SH))
        r = subprocess.run([unshare, "-rm", "bash", "-c", script], capture_output=True, text=True, env=env,
                           timeout=SCRIPT_TIMEOUT)
        self.assertEqual(r.returncode, 0, r.stderr)
        err = (out / "install.err").read_text(encoding="utf-8")
        self.assertEqual((out / "install.rc").read_text(encoding="utf-8").strip(), "1", err)
        self.assertIn("refuse  beta", err)
        self.assertIn("is on a different filesystem than", err)
        self.assertEqual((out / "uninstall.rc").read_text(encoding="utf-8").strip(), "1")
        self.assertEqual((out / "data.after").read_text(encoding="utf-8"), "on the other filesystem",
                         "files on the mounted filesystem were not deleted")
        self.assertEqual(self.content("beta"), skill_text("beta"))
        self.assertEqual(set(self.manifest_entries()), {"beta"})
        self.assert_no_lock()


class TestPortability(unittest.TestCase):
    """macOS ships bash 3.2 and BSD userland. Neither is available on the test
    machines this was written on, so this static check guards the syntax; the
    CI installer job runs the tests on macOS with /bin/bash (3.2)."""

    BASH4 = {
        "associative/nameref/case-attr declare": r"\b(declare|local|typeset)\s+-[a-zA-Z]*[AnluNcg]\b",
        "mapfile/readarray/coproc": r"\b(mapfile|readarray|coproc)\b",
        "case modification ${x,,} ${x^^}": r"\$\{[#!]?\w+(\[[^]]*\])?(,,?|\^\^?)",
        "parameter transformation ${x@Q}": r"\$\{[^}]*@[QEPAaKkUuL]\}",
        "|& and &>> redirections": r"\|&|&>>",
        "fall-through ;& and ;;&": r";;&|\s;&\s*$",
        "[ -v ] / [[ -v ]] / test -v": r"(\[\[?|\btest)\s+-v\s",
        "negative array subscript": r"\$\{\w+\[-\d+\]\}",
        "negative substring length": r"\$\{\w+:[^}:\-][^}:]*:-\d",
        "bash 4+ variables": r"\$\{?(EPOCHSECONDS|EPOCHREALTIME|BASHPID|SRANDOM|BASH_ARGV0)\b",
        "printf %(...)T": r"printf\s+(-v\s+\w+\s+)?['\"][^'\"]*%\(",
        "shopt globstar/lastpipe/autocd": r"\bshopt\s+-s\s+[^;]*\b(globstar|lastpipe|autocd|direxpand|globasciiranges)\b",
        "wait -n / read -N / read -i": r"\bwait\s+-n\b|\bread\s+(-\w+\s+)*-[a-zA-Z]*[Ni]\b",
        "named file descriptors {fd}>": r"(?<!\$)\{\w+\}[<>]",
        "brace expansion step": r"\{-?\d+\.\.-?\d+\.\.-?\d+\}",
    }
    GNU_ONLY = {
        "sed -i / sed -r": r"\bsed\s+(-\w*[ir]\b|--)",
        "readlink -f/-e/-m": r"\breadlink\s+(--\s+)?-\w*[fem]\b",
        "long options on standard tools": r"\b(cp|mv|rm|mkdir|sort|date|stat|ln|sed|grep|head|tail|cut|tr|xargs|"
                                          r"find|du|df|mktemp|touch|chmod|ls|wc|basename|dirname|readlink)\s+"
                                          r"([^|;&#\n]*\s)?--[a-z]",
        "cp -t/-d/-T, mv -t/-T": r"\b(cp|mv)\s+-\w*[tdT]\b",
        "sort -V / date -d / grep -P": r"\bsort\s+(-\w+\s+)*-\w*V|\bdate\s+(-\w+\s+)*-\w*d\b|\bgrep\s+(-\w+\s+)*-\w*P",
        "find -printf / xargs -r": r"\bfind\b[^|;&]*-printf|\bxargs\s+(-\w+\s+)*-\w*r",
        "echo -e": r"\becho\s+-e\b",
    }
    # Allowed on purpose: GNU `stat -c` with the BSD `stat -f` fallback on the same line.
    STAT_WITH_FALLBACK = r"stat -c %d -- .*\bstat -f %d -- "

    @staticmethod
    def code_lines(path: Path):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            yield n, line

    def findings(self, text_lines) -> list[str]:
        out = []
        for n, line in text_lines:
            for label, pattern in {**self.BASH4, **self.GNU_ONLY}.items():
                if re.search(pattern, line):
                    out.append(f"{n}: {label}: {line.strip()}")
            if re.search(r"\bstat\s+-c\b", line) and not re.search(self.STAT_WITH_FALLBACK, line):
                out.append(f"{n}: GNU stat -c without the BSD stat -f fallback: {line.strip()}")
        return out

    def test_scripts_use_no_bash4_syntax_or_gnu_only_flags(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                self.assertEqual(self.findings(self.code_lines(script)), [])

    def test_the_check_catches_known_bad_snippets(self):
        bad = ['declare -A map', 'local -n ref=$1', 'mapfile -t lines < f', 'x="${y,,}"', 'x="${y^^}"',
               'echo "${v@Q}"', 'cmd |& tee', 'cmd &>> log', '  a) x ;;&', '[[ -v VAR ]]', 'echo "${a[-1]}"',
               'x="${s:1:-2}"', 'echo $EPOCHSECONDS', "printf '%(%F)T'", 'shopt -s globstar', 'wait -n',
               'read -N 3 x', 'exec {fd}>file', 'echo {1..9..2}', 'sed -i s/a/b/ f', 'sed -r s/a/b/',
               'readlink -f "$x"', 'cp --reflink a b', 'mv -T a b', 'sort -V', 'date -d yesterday', 'grep -P x',
               'find . -printf x', 'xargs -r rm', 'echo -e x', 'stat -c %d "$x"', 'rm --one-file-system x']
        for snippet in bad:
            with self.subTest(snippet=snippet):
                self.assertNotEqual(self.findings([(1, snippet)]), [], snippet)

    def test_known_good_lines_are_not_flagged(self):
        good = ['shopt -s nullglob dotglob', 'mv -f "$tmp" "$path"', 'cp -R "${srcs[@]}" "$d/"',
                'printf -v "$1" \'%s\' "$m"', 'sort -t "$TAB" -k1,1 -k3', 'date -u +%Y',
                'rm -rf "$LOCK_DIR"', 'x="${y:-default}"', 'readlink -- "$SELF"',
                'if ! out="$(stat -c %d -- "${paths[@]}" 2>/dev/null)" && ! out="$(stat -f %d -- "${paths[@]}")"',
                'git --no-pager -C "$S" log --format= --raw', 'mv "$old" "$dest"', 'echo "a --b"']
        self.assertEqual(self.findings(list(enumerate(good, 1))), [])

    @unittest.skipUnless(BASH, "bash not found on PATH")
    def test_scripts_parse(self):
        for script in SCRIPTS:
            r = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, f"{script.name}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
