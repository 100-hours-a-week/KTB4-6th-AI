# Issue tracker: GitHub

Issues and specifications for this repository live in GitHub Issues. Use the `gh` CLI when an engineering skill needs to create, read, list, comment on, or close an issue.

## Issue types

Choose one template and label for each issue:

| Type | Title prefix | Label |
| --- | --- | --- |
| Bug | `[BUG]` | `🐛 bug` |
| Feature | `[FEAT]` | `✨ enhancement` |
| Refactor | `[REFACTOR]` | `♻️ refactor` |

The templates live in `.github/ISSUE_TEMPLATE/`. Blank issues remain available for work that does not fit one of the three types.

## Workflow

- An open issue represents planned or ongoing work.
- Close the issue when its work is complete.
- Link the implementation pull request with `closes #<issue-number>`.
- Use labels for issue type. Do not add status labels unless the team later needs them.

## Pull requests

Use the Korean pull request template in `.github/PULL_REQUEST_TEMPLATE.md`.
Summarize the change, link the related issue with `closes #<issue-number>`, list the work performed, record the automated tests run, and add screenshots or results when useful.

## Operations

- Create: `gh issue create --title "..." --body "..."`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open`
- Comment: `gh issue comment <number> --body "..."`
- Label: `gh issue edit <number> --add-label "..."`
- Close: `gh issue close <number> --comment "..."`

Infer the repository from the Git remote; `gh` does this when run inside this clone.
