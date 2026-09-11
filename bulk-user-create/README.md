# Bulk user creation

Create Sourcegraph accounts from a CSV file by running `src user create` once per
entry. Uses Python's standard library; no Python packages are required.

## Setup

From this directory, install the pinned Python and [Sourcegraph CLI](https://github.com/sourcegraph/src-cli)
versions with [mise](https://mise.jdx.dev/):

```sh
mise trust
mise install
mise run setup
```

Authenticate `src` to the intended Sourcegraph instance as a site administrator:

```sh
mise exec -- src login https://sourcegraph.example.com
```

The script inherits `src`'s configuration and environment, including `SRC_ENDPOINT`
and `SRC_ACCESS_TOKEN` when set. Keep access tokens out of CSV files and source
control. Check the configured destination before running: this creates real
accounts.

## Usage

Create a UTF-8 CSV file with exactly these two columns, including the header:

```csv
username,email
alice,alice@example.com
bob,bob@example.com
```

Run:

```sh
mise exec -- python bulk_user_create.py users.csv
```

If Python 3.13+ and `src` are already on your `PATH`, you can instead run
`python3 bulk_user_create.py users.csv` without mise.

- Validates the entire CSV's structure and required values before creating users.
  Blank lines are ignored, surrounding value whitespace is trimmed, and UTF-8
  files with a byte-order mark are supported.
- Runs `src user create -username=USERNAME -email=EMAIL` sequentially, passing
  arguments directly without a shell. Sourcegraph validates usernames, email
  addresses, and account uniqueness.
- Forwards `src` output and errors. Exits nonzero on invalid input or the first
  failed creation; previously created accounts remain. This is not an upsert and
  does not skip existing accounts. Before retrying, check the instance and remove
  already-created entries from the CSV, including a failed entry if its request
  succeeded on the server but the response was lost.

## Development

```sh
mise run check
mise run test
```

Tests invoke the Python CLI with a temporary fake `src` executable. They never
contact Sourcegraph or create accounts.
