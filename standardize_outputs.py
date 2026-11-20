"""Validate legacy helper metadata and share one immutable workflow snapshot."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CommitLookup = Callable[[str, str], str]
PrLookup = Callable[[str, int], Mapping[str, Any]]


class NoRedirect(HTTPRedirectHandler):
    """Keep authorization headers on the configured GitHub API endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Reject metadata redirects instead of forwarding credentials."""
        return None


def github_get(route: str, environment: Mapping[str, str]) -> Mapping[str, Any]:
    """Read bounded JSON metadata through a GET-only standard-library client."""
    api = environment.get("GITHUB_API_URL", "https://api.github.com")
    parsed = urlsplit(api)
    token = environment.get("GITHUB_TOKEN", "")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not token
        or "\r" in token
        or "\n" in token
    ):
        raise ValueError("A trusted HTTPS GitHub API endpoint and token are required")
    request = Request(
        api.rstrip("/") + route,
        method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise ValueError("Metadata response is too large")
        metadata = json.loads(body)
        if not isinstance(metadata, Mapping):
            raise ValueError("Metadata must be an object")
        return metadata
    except (URLError, OSError, ValueError, TypeError) as error:
        raise ValueError("GitHub metadata lookup failed") from error


def validate_sha(value: str) -> str:
    """Require a complete, nonzero immutable SHA."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
        raise ValueError("Expected an immutable source SHA")
    return value


def validate_repository(value: str) -> str:
    """Reject repository values that could alter API routes or runner outputs."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value or ""):
        raise ValueError("Invalid repository")
    if value.split("/")[1] in {".", ".."}:
        raise ValueError("Invalid repository")
    return value


def validate_ref(value: str) -> str:
    """Reject refspecs, option-like refs, and command-injection characters."""
    if (
        not value
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value)
        or ".." in value
        or "@{" in value
        or value.startswith("-")
        or value.endswith(".")
        or any(
            not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/")
        )
    ):
        raise ValueError("Invalid ref")
    return value


def contained_path(value: str, workspace: str) -> str:
    """Require a relative path with no existing ancestor outside the workspace."""
    if (
        not value
        or Path(value).is_absolute()
        or re.search(r"[\x00-\x1f\x7f\\]", value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Invalid output path")
    root = Path(workspace).resolve(strict=True)
    current = root
    for part in value.split("/"):
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            break
        current = current.resolve(strict=True)
        if current == root or not current.is_relative_to(root):
            raise ValueError("Output path escapes workspace")
    return value


def validate_inputs(inputs: Mapping[str, str]) -> None:
    """Fail closed before invoking legacy nested helpers with invalid inputs."""
    if inputs.get("INPUT_MODE") not in {"check", "fix"}:
        raise ValueError("Invalid workflow mode")
    validate_repository(inputs["GITHUB_REPOSITORY"])
    if inputs.get("INPUT_REF"):
        validate_ref(inputs["INPUT_REF"])
    if inputs.get("INPUT_REPO"):
        validate_repository(inputs["INPUT_REPO"])
    if inputs.get("INPUT_SHA") or inputs["GITHUB_EVENT_NAME"] == "issue_comment":
        validate_sha(inputs.get("INPUT_SHA", ""))
    for key in ["INPUT_CHECKOUT_PATH", "INPUT_BUILD_PATH"]:
        if inputs.get(key):
            contained_path(inputs[key], inputs["GITHUB_WORKSPACE"])
    if inputs.get("INPUT_PR_BASE_SHA") and not re.fullmatch(
        r"[0-9a-f]{40}", inputs["INPUT_PR_BASE_SHA"]
    ):
        raise ValueError("Invalid base SHA")


def standardize_outputs(
    inputs: Mapping[str, str],
    payload: Mapping[str, Any],
    lookup_commit: CommitLookup,
    lookup_pr: PrLookup,
) -> dict[str, str]:
    """Supplement pinned helper outputs without independently widening approval."""
    validate_inputs(inputs)
    mode = inputs["INPUT_MODE"]
    prefix = "CHECK_" if mode == "check" else "FIX_"
    repository = validate_repository(inputs.get(prefix + "REPO", ""))
    ref = validate_ref(inputs.get(prefix + "REF", ""))
    sha = inputs.get("INPUT_SHA", "")
    number = inputs.get("CHECK_PR_NUMBER", "")
    base = inputs.get("CHECK_BASE_SHA", "") if mode == "check" else ""
    event = inputs["GITHUB_EVENT_NAME"]
    if event == "issue_comment":
        issue = payload["issue"]
        if not isinstance(issue.get("pull_request"), Mapping):
            raise ValueError("Comment is not on a PR")
        number = str(issue["number"])
        if not re.fullmatch(r"[1-9][0-9]*", number):
            raise ValueError("Invalid PR number")
        head = lookup_pr(inputs["GITHUB_REPOSITORY"], int(number))["head"]
        head_repo = head.get("repo") or {}
        if (
            head_repo.get("full_name") != repository
            or head.get("ref") != ref
            or head.get("sha") != sha
        ):
            raise ValueError("Approved snapshot or PR identity has moved")
    elif (
        event == "pull_request"
        and mode == "fix"
        and not inputs.get("INPUT_REF")
        and not inputs.get("INPUT_REPO")
    ):
        head = payload["pull_request"]["head"]
        repository = validate_repository(head["repo"]["full_name"])
        ref = validate_ref(head["ref"])
        if sha and sha != head["sha"]:
            raise ValueError("Snapshot differs from PR event")
        sha = sha or head["sha"]
        number = str(payload["pull_request"]["number"])
    elif re.fullmatch(r"[0-9a-f]{40}", ref):
        if sha and sha != ref:
            raise ValueError("Snapshot differs from immutable ref")
        sha = ref
    elif (
        event != "workflow_dispatch"
        and not inputs.get("INPUT_REF")
        and not inputs.get("INPUT_REPO")
    ):
        if sha and sha != inputs["GITHUB_SHA"]:
            raise ValueError("Snapshot differs from event")
        sha = inputs["GITHUB_SHA"]
    else:
        if not sha and repository != inputs["GITHUB_REPOSITORY"]:
            raise ValueError("Cross-repository overrides require an approved SHA")
        resolved = lookup_commit(repository, ref)
        if sha and sha != resolved:
            raise ValueError("Target branch moved")
        sha = resolved
    validate_sha(sha)
    if mode == "fix":
        if event != "issue_comment" and (event != "pull_request" or inputs.get("INPUT_REF")):
            ref = ref.removeprefix("refs/heads/")
        if ref.startswith("refs/") or re.fullmatch(r"[0-9a-f]{40}", ref):
            raise ValueError("Fix mode requires an unambiguous branch push destination")
    if inputs.get("INPUT_HEAD_REF") and inputs["INPUT_HEAD_REF"] != sha:
        raise ValueError("Detection head differs from snapshot")
    if base and not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("Invalid base SHA")
    if number and not re.fullmatch(r"[1-9][0-9]*", number):
        raise ValueError("Invalid PR number")
    checkout = contained_path(inputs.get(prefix + "CHECKOUT_PATH", ""), inputs["GITHUB_WORKSPACE"])
    build_value = (
        inputs.get("CHECK_BUILD_PATH", "")
        if mode == "check"
        else inputs.get("INPUT_BUILD_PATH") or f"{checkout.removesuffix('-src')}-build"
    )
    build = contained_path(build_value, inputs["GITHUB_WORKSPACE"])
    workspace = Path(inputs["GITHUB_WORKSPACE"]).resolve(strict=True)
    checkout_resolved = (workspace / checkout).resolve()
    build_resolved = (workspace / build).resolve()
    if (
        checkout_resolved == build_resolved
        or checkout_resolved.is_relative_to(build_resolved)
        or build_resolved.is_relative_to(checkout_resolved)
    ):
        raise ValueError("Paths overlap")
    is_act = inputs.get("CHECK_IS_ACT", "") if mode == "check" else "false"
    if is_act not in {"true", "false"}:
        raise ValueError("Invalid act detection output")
    return {
        "ref": ref,
        "sha": sha,
        "repo": repository,
        "base_sha": base,
        "pr_number": number,
        "checkout_path": checkout,
        "build_path": build,
        "is_act": is_act,
    }


def write_outputs(output_path: Path, outputs: Mapping[str, str]) -> None:
    """Validate the full output set before appending any runner commands."""
    for name, value in outputs.items():
        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]*", name)
            or not isinstance(value, str)
            or re.search(r"[\x00-\x1f\x7f]", value)
        ):
            raise ValueError("Invalid action output")
    with output_path.open("a", encoding="utf-8") as output:
        output.write("".join(f"{name}={value}\n" for name, value in outputs.items()))


def main() -> None:
    """Run either preflight validation or immutable output resolution."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    environment = dict(os.environ)
    validate_inputs(environment)
    if args.validate:
        return
    payload = json.loads(Path(environment["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    outputs = standardize_outputs(
        environment,
        payload,
        lambda repo, ref: validate_sha(
            github_get(f"/repos/{repo}/commits/{quote(ref, safe='')}?per_page=1", environment)[
                "sha"
            ]
        ),
        lambda repo, number: github_get(f"/repos/{repo}/pulls/{number}", environment),
    )
    write_outputs(Path(environment["GITHUB_OUTPUT"]), outputs)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError):
        raise SystemExit(
            "Workflow snapshot resolution failed closed; verify inputs and metadata"
        ) from None
