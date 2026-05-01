# Agent Instructions for `repo-management/`

These instructions apply to any work under this directory.

## Before starting a task

- Ensure you are **not on the `main` branch**. If you are, create a new
  branch (e.g. `git switch -c <topic>`) before making any changes.

## After completing a task

- Lint the files you touched, using:
  - Ruff
  - Pyright
  - Markdownlint
- Fix every lint issue in the code itself.
  **Never** disable, suppress, ignore, or `# noqa`-out a lint check
  to make it pass.
- Only after the lint checks pass cleanly: `git commit` and
  `git push` the changes.

## Other

- Whenever changing / adding / removing / moving any columns in any of the CSV files,
  ensure the columns are updated in README.md printer function to match
- Keep this AGENTS.md file up to date as more instructions are provided
- Consult the Deep Search MCP and the Oracle as much as needed when
  you aren't sure about something
- Multiple editors may be working on the same files at the same time,
  humans and agents
  - Do not rely on your cached copy of the files, always read them
- This script runs standalone, do not worry about a calling parent Python module
  - Do not bother with prepending function names wih `_`
- Use only standard libraries, do not require customers to install additional
  packages
- Keep the minimum required versions of Python and Sourcegraph up to date in
  the README.md file
