"""Verify Python snapshot propagation against representative reusable callers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from standardize_outputs import (
    NoRedirect,
    github_get,
    main,
    standardize_outputs,
    validate_inputs,
    write_outputs,
)

SHA = "a" * 40
BASE = "b" * 40
ROOT = Path(__file__).parents[1]


class SnapshotTests(unittest.TestCase):
    """Retain source/ref behavior while removing the Node policy runtime."""

    def setUp(self) -> None:
        """Create a workspace and outputs matching the currently pinned helpers."""
        temporary = tempfile.TemporaryDirectory(prefix="workflow-snapshot-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = {
            "GITHUB_WORKSPACE": str(self.root),
            "GITHUB_REPOSITORY": "Example/target",
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_SHA": SHA,
            "GITHUB_REF": "refs/heads/main",
            "INPUT_MODE": "check",
            "CHECK_IS_ACT": "false",
            "CHECK_REF": SHA,
            "CHECK_REPO": "Example/target",
            "CHECK_BASE_SHA": BASE,
            "CHECK_PR_NUMBER": "",
            "CHECK_CHECKOUT_PATH": "target-src",
            "CHECK_BUILD_PATH": "target-build",
            "FIX_REF": "main",
            "FIX_REPO": "Example/target",
            "FIX_CHECKOUT_PATH": "target-src",
        }
        self.head = {"repo": {"full_name": "Fork/target"}, "ref": "feature", "sha": SHA}
        self.payload = {
            "issue": {"number": 1, "pull_request": {}},
            "pull_request": {"number": 1, "head": self.head},
        }
        self.lookup_commit = Mock(return_value=SHA)
        self.lookup_pr = Mock(return_value={"head": self.head})

    def resolve(self, overrides: dict[str, str]) -> dict[str, str]:
        """Run policy with mocked metadata, never a live GitHub token."""
        return standardize_outputs(
            self.inputs | overrides, self.payload, self.lookup_commit, self.lookup_pr
        )

    def test_main_uses_the_original_repository_for_the_pr_lookup(self) -> None:
        """Exercise the API adapter and approval policy together without real credentials."""
        event = self.root / "event.json"
        event.write_text(json.dumps(self.payload))
        output = self.root / "outputs"
        environment = self.inputs | {
            "INPUT_MODE": "fix",
            "GITHUB_EVENT_NAME": "issue_comment",
            "INPUT_SHA": SHA,
            "FIX_REF": "feature",
            "FIX_REPO": "Fork/target",
            "GITHUB_TOKEN": "TEST_SENTINEL",
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_OUTPUT": str(output),
        }
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"head": self.head}).encode()
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("sys.argv", ["standardize_outputs.py"]),
            patch("standardize_outputs.build_opener") as opener,
        ):
            opener.return_value.open.return_value = response
            main()
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/Example/target/pulls/1")
        self.assertIn("repo=Fork/target\n", output.read_text())
        self.assertIn(f"sha={SHA}\n", output.read_text())

    def test_legacy_helpers_are_supplemented_without_a_branch_lookup(self) -> None:
        """Add SHA while preserving the old event-dependent ref outputs."""
        for mode in ["check", "fix"]:
            with self.subTest(mode=mode):
                outputs = self.resolve({"INPUT_MODE": mode})
                self.assertEqual(outputs["sha"], SHA)
                self.assertEqual(outputs["ref"], SHA if mode == "check" else "main")
        self.lookup_commit.assert_not_called()
        self.lookup_pr.assert_not_called()

    def test_format_all_and_nested_fix_calls_share_an_approved_tuple(self) -> None:
        """Preserve the fork branch as a push destination, not a SHA alias."""
        outputs = self.resolve(
            {
                "INPUT_MODE": "fix",
                "GITHUB_EVENT_NAME": "issue_comment",
                "INPUT_SHA": SHA,
                "FIX_REF": "feature",
                "FIX_REPO": "Fork/target",
            }
        )
        self.assertEqual(
            (outputs["repo"], outputs["ref"], outputs["sha"]), ("Fork/target", "feature", SHA)
        )
        self.assertEqual(outputs["pr_number"], "1")
        self.assertEqual(outputs["build_path"], "target-build")
        self.lookup_pr.assert_called_once_with("Example/target", 1)
        self.lookup_commit.assert_not_called()

    def test_missing_approval_moved_heads_and_overrides_fail_closed(self) -> None:
        """A metadata response cannot supply approval or change its identity."""
        for overrides in [
            {},
            {"INPUT_SHA": "c" * 40},
            {"INPUT_SHA": SHA, "FIX_REPO": "Other/target"},
            {"INPUT_SHA": SHA, "INPUT_REF": "feature\nrepo=spoof"},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.resolve(
                    {"INPUT_MODE": "fix", "GITHUB_EVENT_NAME": "issue_comment", **overrides}
                )

    def test_relevance_and_source_checkouts_cannot_have_different_heads(self) -> None:
        """Both action inputs must consume the same immutable output."""
        with self.assertRaises(ValueError):
            self.resolve({"INPUT_HEAD_REF": BASE})
        action = (ROOT / "action.yaml").read_text()
        detector = action.split("    - name: Run change detection\n", 1)[1].split(
            "\n    - name:", 1
        )[0]
        self.assertRegex(detector, r"(?m)^        ref: \$\{\{ steps\.prepare\.outputs\.sha \}\}$")
        self.assertRegex(
            detector, r"(?m)^        head-ref: \$\{\{ steps\.prepare\.outputs\.sha \}\}$"
        )
        self.assertNotIn("require(", action)

    def test_unknown_modes_and_invalid_nested_values_are_rejected(self) -> None:
        """Reject output injection, missing values, and overlapping paths."""
        for overrides in [
            {"INPUT_MODE": "unknown"},
            {"CHECK_REF": ""},
            {"CHECK_REPO": "X/y\rsha=spoof"},
            {"CHECK_CHECKOUT_PATH": "../outside"},
            {"CHECK_BUILD_PATH": "target-src/build"},
            {"CHECK_IS_ACT": ""},
            {"CHECK_PR_NUMBER": "1\nref=evil"},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.resolve(overrides)
        (self.root / "escape").symlink_to(self.root.parent)
        with self.assertRaises(ValueError):
            self.resolve({"CHECK_CHECKOUT_PATH": "escape/source"})

    def test_manual_and_pr_snapshots_cannot_replace_approval(self) -> None:
        """Resolve a manual branch once and reject mismatch with an explicit SHA."""
        inputs = {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "CHECK_REF": "feature",
            "INPUT_REF": "feature",
        }
        outputs = self.resolve(inputs)
        self.assertEqual((outputs["ref"], outputs["sha"]), ("feature", SHA))
        self.lookup_commit.assert_called_once_with("Example/target", "feature")
        with self.assertRaises(ValueError):
            self.resolve(inputs | {"INPUT_SHA": "c" * 40})
        with self.assertRaises(ValueError):
            self.resolve(
                {"INPUT_MODE": "fix", "GITHUB_EVENT_NAME": "pull_request", "INPUT_SHA": "c" * 40}
            )

    def test_internal_symlink_aliases_cannot_overlap(self) -> None:
        """Resolve path aliases before rejecting overlapping source and build paths."""
        (self.root / "target-src").mkdir()
        (self.root / "target-build").symlink_to(self.root / "target-src", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Paths overlap"):
            self.resolve({})

    def test_invalid_pr_numbers_are_rejected_before_api_calls(self) -> None:
        """Malformed event data cannot construct a different API request."""
        self.payload["issue"]["number"] = "1\nspoof=true"
        with self.assertRaises(ValueError):
            self.resolve(
                {"INPUT_MODE": "fix", "GITHUB_EVENT_NAME": "issue_comment", "INPUT_SHA": SHA}
            )
        self.lookup_pr.assert_not_called()

    def test_output_validation_prevents_partial_publication(self) -> None:
        """A malformed final value must leave existing outputs unchanged."""
        output = self.root / "outputs"
        output.write_text("existing=value\n")
        with self.assertRaises(ValueError):
            write_outputs(output, {"sha": SHA, "repo": "Example/target\nref=evil"})
        self.assertEqual(output.read_text(), "existing=value\n")

    def test_cli_isolated_mode_and_preflight(self) -> None:
        """Run without Node, ignore target Python configuration, and stop before nesting."""
        sentinel = self.root / "executed"
        (self.root / "sitecustomize.py").write_text(f"open({str(sentinel)!r}, 'w').close()\n")
        event = self.root / "event.json"
        event.write_text(json.dumps(self.payload))
        output = self.root / "outputs"
        environment = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            **self.inputs,
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_OUTPUT": str(output),
            "PYTHONPATH": str(self.root),
            "GITHUB_TOKEN": "TEST_SENTINEL",
        }
        command = [sys.executable, "-I", str(ROOT / "standardize_outputs.py")]
        result = subprocess.run(
            command, cwd=self.root, env=environment, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists())
        self.assertIn(f"sha={SHA}\n", output.read_text())
        result = subprocess.run(
            command + ["--validate"],
            cwd=self.root,
            env=environment | {"GITHUB_EVENT_NAME": "issue_comment"},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        with self.assertRaises(ValueError):
            validate_inputs(self.inputs | {"GITHUB_EVENT_NAME": "issue_comment"})

    def test_has_changes_shell_contract_is_preserved(self) -> None:
        """Invalid detector output stays an error, not a successful skipped check."""
        action = (
            (ROOT / "action.yaml").read_text().split("    - name: Set has_changes output", 1)[1]
        )
        match = re.search(r"      run: \|\n((?:        .*\n|\n)+)", action)
        self.assertIsNotNone(match)
        script = textwrap.dedent(match.group(1))
        for detector, succeeds in [
            ("true", True),
            ("false", True),
            ("", False),
            ("false\nspoof=true", False),
        ]:
            with self.subTest(detector=detector):
                output = self.root / "changes"
                output.unlink(missing_ok=True)
                environment = {
                    "PATH": os.environ["PATH"],
                    "IS_ACT": "false",
                    "INPUT_FILE_TYPE": "cpp",
                    "INPUT_INCLUDE_GLOBS": "",
                    "DETECT_HAS_CHANGES": detector,
                    "GITHUB_OUTPUT": str(output),
                }
                result = subprocess.run(
                    ["bash", "-e", "-c", script], env=environment, capture_output=True
                )
                self.assertEqual(result.returncode == 0, succeeds)
                if succeeds:
                    self.assertEqual(output.read_text(), f"has_changes={detector}\n")

    def test_metadata_transport_is_get_only_and_non_redirecting(self) -> None:
        """Reject API denial and non-object responses without real credentials."""
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"sha":"' + SHA.encode() + b'"}'
        with patch("standardize_outputs.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(
                github_get(
                    "/repos/Example/target/commits/feature", {"GITHUB_TOKEN": "TEST_SENTINEL"}
                )["sha"],
                SHA,
            )
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertIsInstance(opener.call_args.args[0], NoRedirect)
            response.read.return_value = b"[]"
            with self.assertRaisesRegex(ValueError, "metadata lookup failed"):
                github_get("/repos/Example/target/pulls/1", {"GITHUB_TOKEN": "TEST_SENTINEL"})
            opener.return_value.open.side_effect = HTTPError(
                request.full_url, 403, "denied", {}, None
            )
            with self.assertRaisesRegex(ValueError, "metadata lookup failed"):
                github_get("/repos/Example/target/pulls/1", {"GITHUB_TOKEN": "TEST_SENTINEL"})


if __name__ == "__main__":
    unittest.main()
