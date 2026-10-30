"""SIGINT, SIGHUP and SIGTERM interrupting install.sh / uninstall.sh.

Two ways of delivering a signal, both sent only to the PID of the script
this test started (never by name, never to a process group we did not
create):

- from a PATH wrapper around `mv` (test_install_security.MV_WRAPPER) that
  signals its parent right after a chosen rename, i.e. while the script is
  waiting for that child: the interesting rollback points of a commit;
- from the test, while a PATH shim holds a child (`cp` or `mv`) blocked
  until the test releases it: the script must wait for the child, then run
  its trap, roll back, rewrite the manifest if the disk changed, delete the
  lock/staging directory, and exit 128+N. These tests show that the script
  does not clean up under a child that is still running (the child is alive
  and the lock is in place until the test releases it) and that no child is
  left behind; the child exits because it is released, not because the
  script killed it (bash does not kill its foreground child).

The child being killed by the signal itself, as with a terminal Ctrl-C, is
covered by the process-group test only.

Signals are sent with bash's `kill` builtin, so they reach the script the
same way on Linux, macOS and Git Bash (MSYS2), where a native Windows
process cannot deliver POSIX signals to an MSYS process. A terminal Ctrl-C
(SIGINT to the whole foreground process group) is reproduced with a new
session and os.killpg, which Windows does not have; that one test is
skipped there.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
import unittest
from pathlib import Path

from test_install_security import (BASH, INSTALL_SH, LOCK_NAME, MANIFEST_NAME, MV_WRAPPER, SCRIPT_TIMEOUT,
                                   SLUGS, UNINSTALL_SH, Base, skill_text, snapshot)

EXIT_CODE = {"HUP": 129, "INT": 130, "TERM": 143}

# Holds the wrapped command when "$*" matches ML_TEST_BLOCK_GLOB: records the
# caller's PID ($PPID) and its own, then waits (bounded) for the release file.
# BLOCK_WHEN=after runs the real command first (e.g. the move has happened).
BLOCK_SECONDS = 60  # the shim's own bound (600 x 0.1 s)
BLOCK_SHIM = """#!/bin/sh
real="$ML_TEST_BLOCK_REAL"
block() {
  echo "$$" > "$ML_TEST_BLOCK_DIR/child.pid"
  echo "$PPID" > "$ML_TEST_BLOCK_DIR/caller.pid"
  : > "$ML_TEST_BLOCK_DIR/ready"
  n=0
  while [ ! -e "$ML_TEST_BLOCK_DIR/release" ] && [ "$n" -lt 600 ]; do sleep 0.1; n=$((n + 1)); done
}
case "$*" in
  $ML_TEST_BLOCK_GLOB)
    if [ "${ML_TEST_BLOCK_WHEN:-before}" = after ]; then
      "$real" "$@"; rc=$?; block; exit $rc
    fi
    block ;;
esac
exec "$real" "$@"
"""


def bash_kill(sig: str, pid: str) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "-c", f'kill -{sig} "$1"', "_", pid], capture_output=True, text=True, timeout=60)


def alive(pid: str) -> bool:
    return subprocess.run([BASH, "-c", 'kill -0 "$1" 2>/dev/null', "_", pid], timeout=60).returncode == 0


class SignalBase(Base):
    def wrapped_env(self, trigger: str, signal_name: str, trigger2: str = "") -> dict[str, str]:
        wrap = self.root / "wrapbin"
        wrap.mkdir(exist_ok=True)
        (wrap / "mv").write_text(MV_WRAPPER, encoding="utf-8", newline="\n")
        os.chmod(wrap / "mv", 0o755)  # noqa: S103 - the fake `mv` must be executable to shadow the real one
        return self.env(PATH=str(wrap) + os.pathsep + os.environ.get("PATH", ""), ML_TEST_REAL_MV=self.real("mv"),
                        ML_TEST_MV_TRIGGER=trigger, ML_TEST_MV_ACTION="kill", ML_TEST_MV_TRIGGER2=trigger2,
                        ML_TEST_MV_SIGNAL=signal_name)

    def real(self, tool: str) -> str:
        path = subprocess.run([BASH, "-c", f"command -v {tool}"], capture_output=True, text=True,
                              timeout=60).stdout.strip()
        self.assertTrue(path, tool)
        return path

    def blocking_env(self, tool: str, glob: str, when: str = "before") -> dict[str, str]:
        shim = self.root / "blockbin"
        shim.mkdir(exist_ok=True)
        (shim / tool).write_text(BLOCK_SHIM, encoding="utf-8", newline="\n")
        os.chmod(shim / tool, 0o755)  # noqa: S103 - the shim must be executable to shadow the real tool
        self.block_dir = self.root / "block"
        self.block_dir.mkdir(exist_ok=True)
        return self.env(PATH=str(shim) + os.pathsep + os.environ.get("PATH", ""), ML_TEST_BLOCK_REAL=self.real(tool),
                        ML_TEST_BLOCK_GLOB=glob, ML_TEST_BLOCK_DIR=str(self.block_dir), ML_TEST_BLOCK_WHEN=when)

    def start(self, script: Path, args: list[str], env: dict[str, str], **popen) -> subprocess.Popen:
        return subprocess.Popen([BASH, str(script), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", env=env, **popen)

    def wait_ready(self, proc: subprocess.Popen) -> tuple[str, str]:
        deadline = time.monotonic() + SCRIPT_TIMEOUT
        while not (self.block_dir / "ready").exists():
            if proc.poll() is not None:
                out, errs = proc.communicate(timeout=SCRIPT_TIMEOUT)
                self.fail(f"script exited ({proc.returncode}) before reaching the blocked command:\n{out}\n{errs}")
            if time.monotonic() > deadline:
                self.reap(proc)
                self.fail("timed out waiting for the blocked command")
            time.sleep(0.05)
        return ((self.block_dir / "caller.pid").read_text(encoding="utf-8").strip(),
                (self.block_dir / "child.pid").read_text(encoding="utf-8").strip())

    def finish(self, proc: subprocess.Popen) -> tuple[int, str, str]:
        (self.block_dir / "release").touch()
        try:
            out, errs = proc.communicate(timeout=SCRIPT_TIMEOUT)
        except subprocess.TimeoutExpired:
            out, errs = self.reap(proc)
            self.fail(f"script did not exit after the signal:\n{out}\n{errs}")
        return proc.returncode, out, errs

    def reap(self, proc: subprocess.Popen, group: bool = False) -> tuple[str, str]:
        """Kill the script we started (or the session it leads) and collect its output, bounded;
        the release file lets a held shim child exit too, so the pipes close."""
        if getattr(self, "block_dir", None) is not None:
            (self.block_dir / "release").touch()
        if group:
            os.killpg(proc.pid, signal.SIGKILL)  # the session this test created, nothing else
        else:
            proc.kill()  # our own child only
        try:
            return proc.communicate(timeout=BLOCK_SECONDS + 30)
        except subprocess.TimeoutExpired:
            return "", "(output not collected: the pipes stayed open)"

    def interrupt_blocked(self, script: Path, args: list[str], env: dict[str, str], sig: str):
        proc = self.start(script, args, env)
        caller, child = self.wait_ready(proc)
        k = bash_kill(sig, caller)
        self.assertEqual(k.returncode, 0, k.stderr)
        time.sleep(0.5)
        self.assertIsNone(proc.poll(), "bash runs the trap only after the running child exits")
        self.assertTrue(alive(child), "the child is still running")
        self.assertTrue(os.path.lexists(self.target / LOCK_NAME), "no cleanup under a running child")
        rc, out, errs = self.finish(proc)
        self.assertFalse(alive(child), f"child {child} outlived the interrupted script")
        return rc, out, errs


class TestSignalsDuringCommit(SignalBase):
    """INT and HUP at the commit points TestInterruptedCommit covers for TERM."""

    def test_signal_while_moving_old_copy_aside_restores_it(self):
        for sig in ("INT", "HUP"):
            with self.subTest(sig=sig):
                self.ok(self.install("--force"))
                self.bump_source("2" + sig)
                r = self.install(env=self.wrapped_env("*|*/old/beta", sig))
                self.assertEqual(r.returncode, EXIT_CODE[sig], r.stderr)
                self.assertIn("restored previous copy of beta", r.stdout)
                self.assertNotEqual(self.content("beta"), skill_text("beta", "2" + sig))
                self.assertEqual(set(self.manifest_entries()), set(SLUGS))
                self.assert_manifest_matches_disk()
                self.assert_no_lock()

    def test_signal_right_after_new_copy_lands_records_it(self):
        for sig in ("INT", "HUP"):
            with self.subTest(sig=sig):
                self.ok(self.install("--force"))
                self.bump_source("3" + sig)
                r = self.install(env=self.wrapped_env("*/stage/beta|*", sig))
                self.assertEqual(r.returncode, EXIT_CODE[sig], r.stderr)
                self.assertEqual(self.content("beta"), skill_text("beta", "3" + sig))
                self.assertEqual(set(self.manifest_entries()), set(SLUGS))
                self.assert_manifest_matches_disk()
                self.assert_no_lock()

    def test_second_signal_during_cleanup_is_ignored(self):
        for sig in ("INT", "HUP"):
            with self.subTest(sig=sig):
                self.ok(self.install("--force"))
                self.bump_source("4" + sig)
                r = self.install(env=self.wrapped_env("no-match", sig, trigger2="*manifest.tmp*"))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.content("beta"), skill_text("beta", "4" + sig))
                self.assert_manifest_matches_disk()
                self.assert_no_lock()


class TestSignalsFromOutside(SignalBase):
    def test_signal_while_staging_changes_nothing_and_leaves_no_child(self):
        self.ok(self.install())
        before = snapshot(self.target)
        self.bump_source("2")
        for sig in ("INT", "HUP", "TERM"):
            with self.subTest(sig=sig):
                env = self.blocking_env("cp", "-R *")
                rc, out, errs = self.interrupt_blocked(
                    INSTALL_SH, ["--source", str(self.src), "--target", str(self.target)], env, sig)
                self.assertEqual(rc, EXIT_CODE[sig], out + errs)
                self.assertEqual(snapshot(self.target), before, "nothing changed, staging area removed")
                for f in ("ready", "release"):
                    (self.block_dir / f).unlink()

    def test_signal_during_commit_rolls_back_and_cleans_up(self):
        for sig in ("INT", "HUP", "TERM"):
            with self.subTest(sig=sig):
                self.write_source({s: skill_text(s) for s in SLUGS})
                self.ok(self.install())
                self.bump_source("2")
                # Held right after beta's old copy was moved aside: the worst moment.
                env = self.blocking_env("mv", "* */old/beta", when="after")
                rc, out, errs = self.interrupt_blocked(
                    INSTALL_SH, ["--source", str(self.src), "--target", str(self.target)], env, sig)
                self.assertEqual(rc, EXIT_CODE[sig], out + errs)
                self.assertIn("restored previous copy of beta", out)
                self.assertEqual(self.content("beta"), skill_text("beta", "1"))
                self.assertEqual(self.content("alpha"), skill_text("alpha", "2"))
                self.assertEqual(set(self.manifest_entries()), set(SLUGS))
                self.assert_manifest_matches_disk()
                self.assert_no_lock()
                for f in ("ready", "release"):
                    (self.block_dir / f).unlink()

    def test_signal_during_uninstall_keeps_manifest_accurate(self):
        for sig in ("INT", "HUP", "TERM"):
            with self.subTest(sig=sig):
                self.ok(self.install())
                env = self.blocking_env("mv", "*/old/", when="after")  # all folders already moved aside
                rc, out, errs = self.interrupt_blocked(UNINSTALL_SH, ["--target", str(self.target)], env, sig)
                self.assertEqual(rc, EXIT_CODE[sig], out + errs)
                self.assertEqual(sorted(p.name for p in self.target.iterdir()), [],
                                 "removed folders are gone with the lock dir, and the manifest with them")
                for f in ("ready", "release"):
                    (self.block_dir / f).unlink()

    @unittest.skipUnless(hasattr(os, "killpg"), "os.killpg is not available on Windows; a terminal Ctrl-C to "
                         "an MSYS process group cannot be reproduced from a native Windows process")
    def test_ctrl_c_to_the_process_group_while_staging(self):
        self.ok(self.install())
        before = snapshot(self.target)
        self.bump_source("2")
        env = self.blocking_env("cp", "-R *")
        proc = self.start(INSTALL_SH, ["--source", str(self.src), "--target", str(self.target)], env,
                          start_new_session=True)
        _, child = self.wait_ready(proc)
        sent = time.monotonic()
        os.killpg(proc.pid, signal.SIGINT)  # the new session we just created: the script and its child
        try:
            # Never released: the child must die from the signal, well before its own bound.
            out, errs = proc.communicate(timeout=BLOCK_SECONDS / 2)
        except subprocess.TimeoutExpired:
            out, errs = self.reap(proc, group=True)
            self.fail(f"script did not exit within {BLOCK_SECONDS / 2:.0f} s of SIGINT to its process group "
                      f"(the held child was not terminated by the signal):\n{out}\n{errs}")
        elapsed = time.monotonic() - sent
        self.assertEqual(proc.returncode, 130, out + errs)
        self.assertFalse(alive(child))
        self.assertFalse((self.block_dir / "release").exists(), "the child was never released")
        self.assertLess(elapsed, BLOCK_SECONDS / 2)
        self.assertEqual(snapshot(self.target), before)
        self.assertTrue((self.target / MANIFEST_NAME).is_file())
        self.assert_no_lock()


if __name__ == "__main__":
    unittest.main()
