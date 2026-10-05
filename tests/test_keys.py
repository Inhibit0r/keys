import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "keys.py"
# Path.home() reads USERPROFILE on Windows and HOME elsewhere.
HOME_VARS = ("HOME", "USERPROFILE")


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = {name: os.environ.get(name) for name in HOME_VARS}
        for name in HOME_VARS:
            os.environ[name] = self.tmp.name
        spec = importlib.util.spec_from_file_location("keys_under_test", SCRIPT)
        self.keys = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.keys)
        self.exported = []  # never touch the real user environment
        self.keys.export_windows = lambda var, value: self.exported.append((var, value))
        # the menu asks every key for its credits in the background: no network in tests
        self.keys.credits = lambda service, key, timeout=15: (30, 100, None)

    def tearDown(self):
        for name, value in self.old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def add(self, label, key):
        sys.stdin, old = io.StringIO(key + "\n"), sys.stdin
        try:
            with redirect_stdout(io.StringIO()):
                self.keys.main(["add", "tavily", label])
        finally:
            sys.stdin = old

    def test_add_use_next_and_private_files(self):
        self.add("anna", "tvly-aaaaaaaaaaaa")
        self.add("boris", "tvly-bbbbbbbbbbbb")
        env = Path(self.tmp.name) / ".config/tavily/env"
        self.assertEqual(
            env.read_text(), "export TAVILY_API_KEY=tvly-aaaaaaaaaaaa\n"
        )  # first key activates
        if os.name != "nt":  # Windows has no owner-only mode bits
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (Path(self.tmp.name) / ".config/api-keys/tavily").stat().st_mode
                & 0o777,
                0o600,
            )
        with redirect_stdout(io.StringIO()):
            self.keys.main(["next", "tavily"])
        self.assertEqual(self.keys.active("tavily"), "tvly-bbbbbbbbbbbb")
        with redirect_stdout(io.StringIO()):
            self.keys.main(["next", "tavily"])  # wraps around
        self.assertEqual(self.keys.active("tavily"), "tvly-aaaaaaaaaaaa")
        out = io.StringIO()
        with redirect_stdout(out):
            self.keys.main(["list", "tavily"])
        self.assertNotIn("tvly-bbbbbbbbbbbb", out.getvalue())  # keys are masked

    def test_firecrawl_cli_path_matches_cli(self):
        home = Path(self.tmp.name)
        path = self.keys.firecrawl_cli_credentials
        cli = "firecrawl-cli/credentials.json"
        self.assertEqual(
            path(home, "darwin"), home / "Library/Application Support" / cli
        )
        self.assertEqual(path(home, "win32"), home / "AppData/Roaming" / cli)
        self.assertEqual(path(home, "linux"), home / ".config" / cli)

    def test_activate_updates_cli_and_windows_environment(self):
        cli = self.keys.SERVICES["firecrawl"]["cli"]
        cli.parent.mkdir(parents=True)
        cli.write_text('{"apiKey": "fc-old", "apiUrl": "u"}', encoding="utf-8")
        self.keys.WINDOWS = True
        with redirect_stdout(io.StringIO()):
            self.keys.activate("firecrawl", "main", "fc-aaaaaaaaaaaa")
        data = json.loads(cli.read_text(encoding="utf-8"))
        self.assertEqual((data["apiKey"], data["apiUrl"]), ("fc-aaaaaaaaaaaa", "u"))
        self.assertEqual(self.exported, [("FIRECRAWL_API_KEY", "fc-aaaaaaaaaaaa")])
        self.keys.WINDOWS = False  # macOS/Linux rely on the sourced env file only
        with redirect_stdout(io.StringIO()):
            self.keys.activate("firecrawl", "main", "fc-bbbbbbbbbbbb")
        self.assertEqual(len(self.exported), 1)
        self.assertEqual(self.keys.active("firecrawl"), "fc-bbbbbbbbbbbb")

    def test_splash_geometry_and_silence_outside_terminal(self):
        self.assertEqual(
            {len(row) for row in self.keys.banner_rows("ULTRAS*LABS")}, {89}
        )
        self.assertEqual({len(row) for row in self.keys.FLASK}, {17})
        lines = self.keys.flask_frame(3, 5, [(8, 8), (2, 8), (-1, 8)])
        self.assertEqual(len(lines), 12)
        out = io.StringIO()  # not a TTY: hooks, pipes and agents get no escape codes
        self.keys.splash(out)
        self.assertEqual(out.getvalue(), "")

    def press(self, *keys):
        it = iter(keys)
        self.keys.read_key = lambda timeout=None: next(it)
        out = io.StringIO()
        with redirect_stdout(out):
            self.keys.interactive()
        return out.getvalue()

    def test_interactive_menu_switches_keys_and_survives_errors(self):
        self.add("anna", "tvly-aaaaaaaaaaaa")
        self.add("boris", "tvly-bbbbbbbbbbbb")
        # switch -> tavily -> down from the active anna to boris -> pick; "x" is ignored
        out = self.press("enter", "x", "enter", "down", "enter", "q")
        self.assertEqual(self.keys.active("tavily"), "tvly-bbbbbbbbbbbb")
        self.assertIn("\x1b[?1049h", out)  # drawn on the alternate screen...
        self.assertTrue(
            out.endswith("\x1b[?1049l")
        )  # ...and the shell screen is restored
        # next key -> firecrawl with no keys: the error is shown, the menu stays open
        out = self.press("down", "enter", "down", "enter", "esc")
        self.assertIn("✗ firecrawl has no stored keys", out)
        self.assertNotIn("tvly-bbbbbbbbbbbb", out)  # keys are masked on screen too

    def test_animation_sleeps_without_focus(self):
        waits, keys = [], iter(["blur", "down", "focus", "q"])

        def read_key(timeout=None):
            waits.append(timeout)
            return next(keys)

        self.keys.read_key = read_key
        with redirect_stdout(io.StringIO()):
            self.keys.interactive()
        # ticks while focused, a blocking wait (no timeout, no CPU) after "blur"
        self.assertEqual(waits, [self.keys.TICK, None, None, self.keys.TICK])

    def test_remove_from_menu_and_command_line(self):
        self.add("anna", "tvly-aaaaaaaaaaaa")
        self.add("boris", "tvly-bbbbbbbbbbbb")
        # remove -> tavily -> boris; "enter" on the default "No" keeps it
        self.press("6", "enter", "down", "enter", "enter", "q")
        self.assertEqual(len(self.keys.load("tavily")), 2)
        out = self.press("6", "enter", "down", "enter", "down", "enter", "q")
        self.assertEqual(self.keys.load("tavily"), [("anna", "tvly-aaaaaaaaaaaa")])
        self.assertIn("removed boris", out)
        with redirect_stdout(io.StringIO()) as out:
            self.keys.main(["remove", "tavily", "anna"])
        self.assertIn(
            "stays active", out.getvalue()
        )  # the active key is not switched off
        self.assertEqual(self.keys.active("tavily"), "tvly-aaaaaaaaaaaa")
        with self.assertRaises(SystemExit):
            self.keys.main(["remove", "tavily", "nobody"])

    def test_banner_and_credit_cells(self):
        k = self.keys
        self.assertEqual(len(k.banner(0.0, 89)), 6)
        self.assertEqual(len(k.banner(0.0, 40)), 1)  # narrow: one line
        head = k.header(0.0, k.HEAD_W)  # banner with the flask in the right corner
        self.assertEqual([k.vlen(line) for line in head], [k.HEAD_W] * len(k.FLASK))
        self.assertEqual(
            k.ANSI.sub("", k.credit_cell((30, 100, None), 0, 10, 9)),
            k.BAND * 10 + "      30/100",  # GAP + numbers right-aligned in 9 columns
        )
        # 35% of 10 cells: three full cells, a half-band edge, six track cells
        self.assertEqual(
            k.ANSI.sub("", k.credit_cell((35, 100, None), 0, 10, 9))[:10], k.BAND * 3 + k.HALF_BAND + k.BAND * 6
        )
        self.assertIn("HTTP 401", k.credit_cell((None, None, "HTTP 401"), 0, 10, 9))
        self.assertIn("checking", k.credit_cell(None, 0, 10, 9))
        # bars of every key stretch to the same length: the numbers end at the right edge
        self.add("anna", "tvly-aaaaaaaaaaaa")
        self.add("boris", "tvly-bbbbbbbbbbbb")
        k.CREDITS.update(
            {
                ("tavily", "tvly-aaaaaaaaaaaa"): (979, 1000, None),
                ("tavily", "tvly-bbbbbbbbbbbb"): (1000, 1000, None),
            }
        )
        rows = [r for r in k.overview(0.0, 110) if "tvly-" in r]
        self.assertEqual([k.vlen(r) for r in rows], [110 - 4] * 2)
        box = self.keys.box("t", ["a", "\x1b[1mb\x1b[0m"], 20)
        self.assertEqual({self.keys.vlen(line) for line in box}, {20})

    def test_rejects_shell_injection(self):
        with self.assertRaises(SystemExit):
            self.add("evil", "tvly-x; rm -rf ~")
        self.assertFalse((Path(self.tmp.name) / ".config/tavily/env").exists())


if __name__ == "__main__":
    unittest.main()
