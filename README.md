# `action-workflow-setup`

> Master setup action; combines ref resolution, environment detection, and change detection in a single step. Used by the majority of phlex workflows.

## Usage

```yaml
- uses: Framework-R-D/action-workflow-setup@v1  # pin to commit SHA in production
  with:
    input-name: value
```

## Inputs

| Name | Description | Required | Default |
|------|-------------|----------|---------|
| `mode` | Workflow mode ('check' or 'fix') | false | `check` |
| `ref` | Manual ref override | false | |
| `repo` | Manual repo override | false | |
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
| `repo` | Resolved repository |
| `base_sha` | Resolved base SHA |
| `pr_number` | PR number |
| `checkout_path` | Resolved checkout path |
| `build_path` | Resolved build path |
| `has_changes` | Whether relevant changes were detected |

## License

[Apache 2.0](LICENSE)
