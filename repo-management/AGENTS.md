# Agent Instructions for `repo-management/`

These instructions apply to any work under this directory.

## Before starting a task

- Ensure you are **not on the `main` branch**. If you are, create a new
  branch (e.g. `git switch -c <topic>`) before making any changes.

## After completing a task

- Lint the script(s) you touched (this directory uses
  [`ruff`](https://docs.astral.sh/ruff/); run `ruff check` and
  `ruff format --check` as appropriate).
- Fix every lint issue in the code itself.
  **Never** disable, suppress, ignore, or `# noqa`-out a lint check
  to make it pass.
- Only after the lint checks pass cleanly: `git commit` and
  `git push` the changes.
