# TODO

## CSV_SCHEMA.md

- Add a column for GraphQL doc strings from `schema.gql`
- Add a column for GraphQL paths
  - For columns that map 1:1 to a GraphQL field, set `path` to the dotted
  path already used by `get_path` / `get_path_mb` (e.g `mirrorInfo.byteSize`)
    - "Drop the redundant string from the lambda" - I don't remember what
      I meant by this
  - For columns which do not map 1:1 to GraphQL fields, write a note of which
    fields were used in deriving the column's value, and how
