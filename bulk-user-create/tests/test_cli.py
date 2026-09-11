import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "bulk_user_create.py"


class BulkUserCreateCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.csv = self.directory / "users.csv"
        self.calls = self.directory / "calls.jsonl"
        src = self.directory / "src"
        src.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['CALLS'], 'a') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if '-username=fail' in sys.argv:\n"
            "    print('creation failed', file=sys.stderr)\n"
            "    sys.exit(1)\n"
        )
        src.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(self.directory),
            "CALLS": str(self.calls),
        }

    def run_cli(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.csv)],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_creates_users_in_order_with_csv_quoting_and_trimmed_values(self):
        self.csv.write_text(
            '\ufeffusername,email\n alice , alice@example.com \nbob,"bob@example.com"\n'
        )
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.recorded_calls(),
            [
                ["user", "create", "-username=alice", "-email=alice@example.com"],
                ["user", "create", "-username=bob", "-email=bob@example.com"],
            ],
        )

    def test_validates_entire_file_before_creating_any_users(self):
        for content in (
            "",
            "username,email\n",
            "name,email\nalice,alice@example.com\n",
            "username,email\nalice,alice@example.com\nbob,\n",
            "username,email\nalice,alice@example.com\nbob\n",
            "username,email\nalice,alice@example.com\nbob,b@example.com,extra\n",
            'username,email\nalice,alice@example.com\nbob,"unterminated\n',
        ):
            with self.subTest(content=content):
                self.csv.write_text(content)
                result = self.run_cli()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("error:", result.stderr)
                self.assertEqual(self.recorded_calls(), [])

    def test_stops_after_failure_without_rolling_back_previous_users(self):
        self.csv.write_text(
            "username,email\nalice,alice@example.com\n"
            "fail,fail@example.com\ncarol,carol@example.com\n"
        )
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("creation failed", result.stderr)
        self.assertIn("line 3", result.stderr)
        self.assertEqual(len(self.recorded_calls()), 2)

    def test_missing_file_or_cli_reports_actionable_error(self):
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.csv.write_text("username,email\nalice,alice@example.com\n")
        (self.directory / "src").unlink()
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("src", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
