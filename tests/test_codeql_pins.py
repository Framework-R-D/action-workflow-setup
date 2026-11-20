"""Verify that coupled CodeQL actions stay on one immutable release."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate explicit keys while retaining YAML merge semantics."""


def construct_unique_mapping(
    loader: UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    """Reject repeated explicit keys before constructing a mapping."""
    keys: set[object] = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in keys:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        keys.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping
)


def codeql_references(content: str) -> list[str]:
    """Collect CodeQL action references from parsed workflow step mappings."""
    workflow = yaml.load(content, Loader=UniqueKeyLoader)
    jobs = workflow.get("jobs", {}) if isinstance(workflow, dict) else {}
    return [
        step["uses"]
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict)
        and isinstance(step.get("uses"), str)
        and step["uses"].startswith("github/codeql-action/")
    ]


def has_one_immutable_release(references: list[str]) -> bool:
    """Require init, analyze, and every CodeQL reference to share one full SHA."""
    parsed = [
        reference.removeprefix("github/codeql-action/").partition("@") for reference in references
    ]
    return (
        {name for name, separator, _ in parsed if separator} >= {"init", "analyze"}
        and all(separator and re.fullmatch(r"[0-9a-f]{40}", sha) for _, separator, sha in parsed)
        and len({sha for _, _, sha in parsed}) == 1
    )


class CodeQLPinTests(unittest.TestCase):
    """Reject independent component updates and mutable CodeQL references."""

    def test_one_immutable_release(self) -> None:
        """Require init, analyze, and any other CodeQL actions to share a commit."""
        directory = Path(__file__).resolve().parents[1] / ".github/workflows"
        files = [*directory.glob("*.yaml"), *directory.glob("*.yml")]
        references = [
            reference for path in files for reference in codeql_references(path.read_text())
        ]
        self.assertTrue(has_one_immutable_release(references))

    def test_flow_mappings_are_checked_and_run_text_is_ignored(self) -> None:
        """Parse real step values regardless of YAML layout."""
        sha = "a" * 40
        content = f"""jobs:
  scan:
    steps:
      - {{uses: github/codeql-action/init@{sha}}}
      - "uses": github/codeql-action/analyze@{sha}
      - {{uses: github/codeql-action/upload-sarif@v4}}
      - run: |
          uses: github/codeql-action/ignored@v4
"""
        references = codeql_references(content)
        self.assertEqual(len(references), 3)
        self.assertFalse(has_one_immutable_release(references))

    def test_duplicate_mapping_keys_are_rejected(self) -> None:
        """Duplicate YAML keys cannot hide a mutable action reference."""
        with self.assertRaises(ConstructorError):
            codeql_references(
                "jobs:\n  scan:\n    steps:\n"
                "      - uses: github/codeql-action/init@v4\n"
                "        uses: github/codeql-action/init@" + "a" * 40 + "\n"
            )
