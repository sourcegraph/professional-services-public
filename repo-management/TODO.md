# TODO: Auto-generated CSV column documentation in README.md

Goal: every CSV column in every output file (main repos CSV, cloning-errors,
indexing-errors, skipped-files, and the optional `--count-commits` /
`--run-search` column groups) is documented in `README.md` with the
description sourced from `schema.graphql`, so the docs can never drift from
the API the script actually queries.

## Design decisions

- **Source of truth = `schema.graphql`**, not hand-typed strings in
  `list-repos.py`. The script already pins each column to a GraphQL path
  (e.g. `mirrorInfo.byteSize`); a small SDL parser walks the schema once
  and resolves each path to its field's `"""docstring"""`.
- **Doc generation is a separate subcommand**, not something that runs on
  every invocation. Normal users running the script just want CSVs; they
  shouldn't need `schema.graphql` present, and we don't want to race two
  concurrent runs writing the same README.
- **One README, multiple tables** — one per CSV file, plus one per optional
  column group (`--count-commits`, `--run-search`), each in its own section
  so users can see exactly what they get with which flags.
- **`README.md` stays hand-maintained** for everything except the auto-
  generated tables. The generator only writes between explicit marker
  comments (e.g. `<!-- BEGIN AUTOGEN: main-csv -->` …
  `<!-- END AUTOGEN: main-csv -->`) so the surrounding prose is preserved.
  Update `AGENTS.md` to reflect that those marker blocks ARE generated.

## Steps

1. **Extend the COLUMNS entries** in `list-repos.py` so each entry carries
   enough metadata to document itself:
   - Replace the current `(name, extractor)` tuples with a small dataclass
     (or `NamedTuple`) `Column(name, extractor, path=None, note=None)`.
   - For columns that map 1:1 to a GraphQL field, set `path` to the dotted
     path already used by `get_path` / `get_path_mb` (e.g.
     `mirrorInfo.byteSize`). Drop the redundant string from the lambda.
   - For derived columns with no single schema field
     (`externalServices`, `mirrorInfo.status`, `textSearchIndex.status`,
     `commitCount.queryTimeSeconds`, `runSearch.*`,
     `skippedIndexed.totalCount`, `skippedIndexed.refsWithSkips`,
     `skippedIndexed.headQuery`, `mirrorInfo.corruptionLogs` join, etc.),
     set `note` to a short hand-written description.
   - Apply the same treatment to `COMMIT_COUNT_OPTIMIZATION_COLUMNS`,
     `COMMIT_COUNT_COLUMNS`, `RUN_SEARCH_COLUMNS`,
     `CLONING_ERROR_EXTRA_COLUMNS`, `SKIPPED_FILES_EXTRA_COLUMNS`.

2. **Write a minimal SDL doc-string extractor**, standard-library only:
   - Parse `schema.graphql` into `{TypeName: {fieldName: docstring}}`.
   - Resolve a dotted path against the `Repository` root (configurable for
     `commitCount.*` / `runSearch.*` paths that anchor on other roots).
   - Follow nested object types as needed (e.g.
     `mirrorInfo.updateSchedule.intervalSeconds` →
     `Repository.mirrorInfo` → `MirrorInfo.updateSchedule` →
     `UpdateSchedule.intervalSeconds`).
   - Strip leading/trailing whitespace and collapse multi-line docstrings
     to a single line for the table cell (keep full text for a possible
     future verbose mode).

3. **Add a `--emit-readme` subcommand** to `list-repos.py`:
   - Reads `schema.graphql` from the working directory (path overridable
     via `--schema PATH`).
   - For each CSV (main, cloning-errors, indexing-errors, skipped-files,
     `--count-commits` group, `--run-search` group) emits a Markdown
     table: `| Column | GraphQL path | Description |`.
   - Writes the tables into `README.md` between the autogen markers,
     leaving all other content untouched.
   - Writes nothing if the result is byte-identical to the existing
     `README.md` (so it's idempotent and CI-friendly).

4. **Wire it into `.pre-commit-config.yaml`** alongside the existing
   `--write-csv-schema` hook, so editing `list-repos.py` regenerates the
   README tables automatically. Mirror the existing hook's pattern: run
   the subcommand, fail if the file changed.

5. **Cover the parser with a tiny smoke test or `--check` mode**:
   - At minimum, run `python3 list-repos.py --emit-readme --check` in CI
     to assert the committed `README.md` matches the generator's output.
   - Optionally assert that every column with a `path` resolves to a
     real field in `schema.graphql`, so a typo in a path fails loudly
     instead of silently emitting an empty description cell.

6. **Update `AGENTS.md`**:
   - Note that `README.md`'s autogen blocks ARE generated and must not be
     edited by hand (only the surrounding prose is hand-maintained).
   - Note that adding/removing/renaming any column requires re-running
     `--emit-readme` (handled by the pre-commit hook).

## Out of scope (for now)

- Generating per-flag usage examples in README.md (keep those hand-written).
- Documenting columns whose values come from neither the schema nor a
  simple derivation (none today, but flag if one is added later).
- Pulling descriptions from the live GraphQL introspection endpoint
  instead of `schema.graphql` — the checked-in SDL is already the
  contract this script targets.
