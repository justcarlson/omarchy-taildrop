import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class ShellHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("runtime", "state", "cache", "data", "config"):
            (self.root / name).mkdir()
        (self.root / "runtime").chmod(0o700)
        self.env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            XDG_RUNTIME_DIR=str(self.root / "runtime"),
            XDG_STATE_HOME=str(self.root / "state"),
            XDG_CACHE_HOME=str(self.root / "cache"),
            XDG_DATA_HOME=str(self.root / "data"),
            XDG_CONFIG_HOME=str(self.root / "config"),
        )
        self.mock("wl-paste", """#!/bin/bash
if [[ " $* " == *" --list-types "* ]]; then
  if [[ " $* " == *" --primary "* ]]; then exit 0; fi
  case "$TEST_CLIPBOARD" in
    image) echo image/png ;;
    sensitive) printf 'text/plain\\nx-kde-passwordManagerHint\\n' ;;
    *) echo text/plain ;;
  esac
elif [[ " $* " == *" --type text "* ]]; then
  printf 'clipboard fixture'
else
  printf 'image fixture'
fi
""")

    def mock(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)
        return path

    def run_script(self, name, *args, **variables):
        return subprocess.run(
            ["bash", str(REPO / name), *args],
            env=dict(self.env, **variables),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_clipboard_image_is_staged_without_using_history(self):
        result = self.run_script("send.sh", "stage-clipboard", "--no-primary", TEST_CLIPBOARD="image")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual((info["kind"], info["source"], info["mime"]), ("image", "clipboard", "image/png"))
        self.assertEqual(Path(info["path"]).read_bytes(), b"image fixture")

    def test_clipboard_text_still_works(self):
        result = self.run_script("send.sh", "stage-clipboard", "--no-primary", TEST_CLIPBOARD="text")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual((info["kind"], info["source"]), ("text", "clipboard"))
        self.assertEqual(Path(info["path"]).read_text(), "clipboard fixture")

    def test_password_manager_hint_is_respected(self):
        result = self.run_script("send.sh", "stage-clipboard", TEST_CLIPBOARD="sensitive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"kind": "sensitive"})
        self.assertFalse((self.root / "runtime" / "omarchy-taildrop").exists())

    def test_clipboard_staging_requires_a_private_runtime_directory(self):
        runtime = self.root / "runtime"
        link = self.root / "runtime-link"
        link.symlink_to(runtime, target_is_directory=True)
        for path in ("", "relative", str(self.root / "missing"), str(link)):
            with self.subTest(path=path):
                result = self.run_script("send.sh", "stage-clipboard", "--no-primary",
                                         XDG_RUNTIME_DIR=path, TEST_CLIPBOARD="text")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("private XDG_RUNTIME_DIR", result.stderr)
                self.assertEqual(result.stdout, "")
        runtime.chmod(0o755)
        result = self.run_script("send.sh", "stage-clipboard", "--no-primary",
                                 TEST_CLIPBOARD="image")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((runtime / "omarchy-taildrop").exists())

    def test_clipboard_staging_rejects_another_owner(self):
        self.mock("stat", "#!/bin/bash\nprintf '%s:700\\n' \"$((UID + 1))\"\n")
        result = self.run_script("send.sh", "stage-clipboard", "--no-primary",
                                 TEST_CLIPBOARD="text")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "runtime" / "omarchy-taildrop").exists())

    def test_clipboard_staging_stops_after_directory_setup_failure(self):
        for name in ("rm", "mkdir"):
            with self.subTest(command=name):
                mock = self.mock(name, "#!/bin/bash\nexit 1\n")
                result = self.run_script("send.sh", "stage-clipboard", "--no-primary",
                                         TEST_CLIPBOARD="text")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("could not create private staging directory", result.stderr)
                self.assertEqual(result.stdout, "")
                mock.unlink()

    def test_clipboard_staging_replaces_a_symlink_without_touching_its_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "clipboard.txt"
        sentinel.write_text("keep this file")
        stage = self.root / "runtime" / "omarchy-taildrop"
        stage.symlink_to(outside, target_is_directory=True)
        result = self.run_script("send.sh", "stage-clipboard", "--no-primary",
                                 TEST_CLIPBOARD="text")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sentinel.read_text(), "keep this file")
        self.assertFalse(stage.is_symlink())
        staged = Path(json.loads(result.stdout)["path"])
        self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)

    def test_history_staging_stops_when_runtime_is_unsafe(self):
        history = self.root / "state" / "omarchy"
        history.mkdir()
        image = self.root / "history.png"
        image.write_bytes(b"history fixture")
        (history / "clipboard-history.json").write_text(json.dumps([
            {"type": "image", "path": str(image), "mime": "image/png"}
        ]))
        result = self.run_script("send.sh", "stage-image", XDG_RUNTIME_DIR="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_history_image_still_works(self):
        history = self.root / "state" / "omarchy"
        history.mkdir()
        image = self.root / "history.png"
        image.write_bytes(b"history fixture")
        (history / "clipboard-history.json").write_text(json.dumps([
            {"type": "image", "path": str(image), "mime": "image/png", "capturedAt": "test"}
        ]))
        result = self.run_script("send.sh", "stage-image", "--back", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual((info["kind"], info["source"], info["total"]), ("image", "history", 1))
        self.assertEqual(Path(info["path"]).read_bytes(), b"history fixture")

    def test_setup_ignores_a_python_shim_without_system_gi_bindings(self):
        marker = self.root / "python-shim-used"
        self.mock("python3", '#!/bin/bash\ntouch "$TEST_SHIM_MARKER"\nexit 1\n')
        self.mock("pgrep", "#!/bin/bash\nexit 1\n")
        self.mock("notify-send", "#!/bin/bash\nexit 0\n")
        system_gi = subprocess.run(
            ["/usr/bin/python3", "-c", 'import gi; gi.require_version("Nautilus", "4.1")'],
            capture_output=True,
            timeout=10,
        )
        result = self.run_script("send.sh", "setup", TEST_SHIM_MARKER=str(marker))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists(), "setup used the user's Python shim")
        extension = self.root / "data" / "nautilus-python" / "extensions" / "taildrop.py"
        if system_gi.returncode == 0:
            self.assertEqual(extension.read_bytes(), (REPO / "nautilus" / "taildrop.py").read_bytes())
        else:
            self.assertFalse(extension.exists())

    def test_lint_fails_when_the_linter_is_missing(self):
        result = self.run_script("dev.sh", "lint", QMLLINT=str(self.root / "missing-qmllint"))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("lint ok", result.stdout)

    def test_lint_fails_when_the_linter_crashes(self):
        linter = self.mock("broken-qmllint", "#!/bin/bash\necho 'linter crashed' >&2\nexit 139\n")
        result = self.run_script("dev.sh", "lint", QMLLINT=str(linter))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("linter crashed", result.stdout + result.stderr)
        self.assertNotIn("lint ok", result.stdout)

    def test_lint_preserves_failure_even_after_a_suppressed_warning(self):
        linter = self.mock("failed-qmllint", "#!/bin/bash\nprintf '%s\\n' 'Warning: Member not found on type \"QObject\"' 'Fatal: linter failed' >&2\nexit 2\n")
        result = self.run_script("dev.sh", "lint", QMLLINT=str(linter))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Fatal: linter failed", result.stderr)
        self.assertNotIn("lint ok", result.stdout)

