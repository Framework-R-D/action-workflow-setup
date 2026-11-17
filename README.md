# `action-workflow-setup`

> Master setup action; combines ref resolution, environment detection, and change detection in a single step. Used by the majority of phlex workflows.

## Usage

```yaml
- uses: Framework-R-D/action-workflow-setup@f73307dd8c13cb66c2565c9ace32571517b1cea8 # v1
  with:
    input-name: value
```

## Inputs

| Name | Description | Required | Default |
|------|-------------|----------|---------|
| `mode` | Workflow mode ('check' or 'fix') | false | `check` |
| `ref` | Manual ref override | false | |
| `repo` | Manual repo override | false | |
| `sha` | Full immutable source SHA; caller-approved SHA required for comments | false | |
| `pr-base-sha` | Manual base SHA override (check mode only) | false | |
| `checkout-path` | Manual checkout path override | false | |
| `build-path` | Manual build path override | false | |
| `file-type` | File type for change detection (e.g., cpp, python) | false | |
| `include-globs` | Include globs for change detection | false | |
| `exclude-globs` | Exclude globs for change detection | false | |
| `head-ref` | Explicit head ref for change detection (overrides the resolved ref) | false | |

## Outputs

| Name | Description |
|------|-------------|
| `is_act` | Whether running in act |
| `ref` | Resolved ref |
| `sha` | Validated immutable source SHA, separate from the push ref |
| `repo` | Resolved repository |
| `base_sha` | Resolved base SHA |
| `pr_number` | PR number |
| `checkout_path` | Resolved checkout path |
| `build_path` | Resolved build path |
| `has_changes` | Whether relevant changes were detected |

## Immutable-source integration

Consumers must use `sha` for source checkout, builds, formatting, artifact production, and privileged consumption; retain `ref` only as the branch push destination. Issue-comment callers must authorize the exact repository and SHA before invoking this action. Missing approval or a moved/mismatched PR snapshot fails closed, including explicit `format-all` overrides. A current-head lookup does not authenticate the reviewer or prove comment-time approval. Commit timestamps are not used. `head-ref`, when supplied, must equal the snapshot. Unknown modes, invalid nested outputs, and escaping/overlapping paths are rejected.

The currently pinned preparation releases do not expose SHA. This action therefore validates their metadata and resolves/compares the immutable snapshot itself, then passes that SHA through the **existing** `ref` and `head-ref` inputs of the pinned change-detection action. It does not invent release SHAs or pass unsupported inputs to old pins. Once the new preparation releases are published, a reviewed follow-up must update their pins and forward the `sha` input/output through them. The pinned change detector still needs its own reviewed update for true empty sparse checkout and filename-output hardening. Those transitive mitigations are not deployed by changing this wrapper alone.

All reusable `format-all` and individual jobs must carry one `repo`/`ref`/`sha` tuple. Supply `sha` to `handle-fix-commit` as `pr-info-sha`; branch movement must produce a patch or require renewed approval, not an automatic rebase. Local action changes require reviewed releases and consumer-pin updates before Phlex adopts them.

Snapshot policy, preflight checks, and tests use Python 3.12 or newer with no third-party Python client or local Node requirement. Python runs in isolated mode to ignore target-controlled import configuration. The small standard-library API adapter makes bounded GET requests to the runner's configured HTTPS GitHub endpoint, rejects redirects, and exposes lookup failures without falling back to another snapshot.

## License

[Apache 2.0](LICENSE)
