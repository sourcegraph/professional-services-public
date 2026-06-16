#!/usr/bin/env bash

# Run list-repos.py through every finite flag/value-state combination against a
# real Sourcegraph instance. The free-form inputs are represented by one
# configured sample value each (repo, revision, search pattern, limit).
#
# Required, either in the shell environment or in .env beside this script:
#   SRC_ENDPOINT=https://sourcegraph.example.com
#   SRC_ACCESS_TOKEN=sgp_...
#
# Useful overrides:
#   LIST_REPOS_TEST_REPO=github.com/org/repo
#   LIST_REPOS_TEST_REV=main
#   LIST_REPOS_TEST_LIMIT=1
#   LIST_REPOS_TEST_SEARCH_PATTERN=LIST_REPOS_PERMUTATION_NEEDLE
#   LIST_REPOS_TEST_OUTPUT_DIR=/tmp/list-repos-permutation-run
#   LIST_REPOS_TEST_INCLUDE_UNLIMITED=1
#   LIST_REPOS_TEST_INCLUDE_MUTATIONS=1
#   LIST_REPOS_TEST_INCLUDE_GLOBAL_MUTATIONS=1
#   LIST_REPOS_TEST_INCLUDE_CREDENTIAL_ARGS=1
#   LIST_REPOS_TEST_INCLUDE_WRITE_CSV_SCHEMA=1
#   LIST_REPOS_TEST_STOP_ON_FAILURE=1

set -u
set -o pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
SCRIPT_UNDER_TEST=${LIST_REPOS_SCRIPT:-"$SCRIPT_DIR/list-repos.py"}
PYTHON_BIN=${PYTHON_BIN:-python3}

TEST_REPO=${LIST_REPOS_TEST_REPO:-}
TEST_REV=${LIST_REPOS_TEST_REV:-}
TEST_LIMIT=${LIST_REPOS_TEST_LIMIT:-1}
TEST_SEARCH_PATTERN=${LIST_REPOS_TEST_SEARCH_PATTERN:-LIST_REPOS_PERMUTATION_NEEDLE}
TEST_OUTPUT_DIR=${LIST_REPOS_TEST_OUTPUT_DIR:-}

INCLUDE_UNLIMITED=${LIST_REPOS_TEST_INCLUDE_UNLIMITED:-0}
INCLUDE_MUTATIONS=${LIST_REPOS_TEST_INCLUDE_MUTATIONS:-0}
INCLUDE_GLOBAL_MUTATIONS=${LIST_REPOS_TEST_INCLUDE_GLOBAL_MUTATIONS:-0}
INCLUDE_CREDENTIAL_ARGS=${LIST_REPOS_TEST_INCLUDE_CREDENTIAL_ARGS:-0}
INCLUDE_WRITE_CSV_SCHEMA=${LIST_REPOS_TEST_INCLUDE_WRITE_CSV_SCHEMA:-0}
STOP_ON_FAILURE=${LIST_REPOS_TEST_STOP_ON_FAILURE:-0}

case_count=0
failure_count=0
summary_path=""
MODE_ARGS=()

usage() {
  cat <<'EOF'
Usage: ./test-list-repos-permutations.sh

Runs list-repos.py against a real Sourcegraph instance for every supported
finite CLI flag/value-state combination.

Configure Sourcegraph credentials with SRC_ENDPOINT and SRC_ACCESS_TOKEN, or put
those two variables in .env beside this script.

By default the suite is bounded and non-mutating:
  - Full-listing permutations include --limit 1.
  - --reclone and --reindex permutations are skipped.
  - The no-limit full-instance permutations are skipped.

Set these environment variables to broaden coverage:
  LIST_REPOS_TEST_INCLUDE_UNLIMITED=1          include no --limit permutations
  LIST_REPOS_TEST_INCLUDE_MUTATIONS=1         include scoped --reclone/--reindex
  LIST_REPOS_TEST_INCLUDE_GLOBAL_MUTATIONS=1  include global mutation forms too
  LIST_REPOS_TEST_INCLUDE_CREDENTIAL_ARGS=1   also test --src-* CLI args
  LIST_REPOS_TEST_INCLUDE_WRITE_CSV_SCHEMA=1  also test --write-csv-schema
  LIST_REPOS_TEST_STOP_ON_FAILURE=1           stop at the first failed command

Sample input values can be overridden with:
  LIST_REPOS_TEST_REPO=github.com/org/repo
  LIST_REPOS_TEST_REV=main
  LIST_REPOS_TEST_LIMIT=1
  LIST_REPOS_TEST_SEARCH_PATTERN=LIST_REPOS_PERMUTATION_NEEDLE
  LIST_REPOS_TEST_OUTPUT_DIR=/tmp/list-repos-permutation-run
EOF
}

enabled() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|y|Y) return 0 ;;
    *) return 1 ;;
  esac
}

fail() {
  echo "error: $*" >&2
  exit 1
}

load_dotenv() {
  local dotenv_path=${LIST_REPOS_TEST_DOTENV:-"$SCRIPT_DIR/.env"}
  local line key value

  [[ -f "$dotenv_path" ]] || return 0

  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      ""|\#*) continue ;;
    esac
    line=${line#export }
    [[ "$line" == *=* ]] || continue
    key=${line%%=*}
    value=${line#*=}
    key=$(printf '%s' "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    value=$(printf '%s' "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    value=${value%\"}
    value=${value#\"}
    value=${value%\'}
    value=${value#\'}

    case "$key" in
      SRC_ENDPOINT)
        if [[ -z "${SRC_ENDPOINT:-}" ]]; then
          export SRC_ENDPOINT=$value
        fi
        ;;
      SRC_ACCESS_TOKEN)
        if [[ -z "${SRC_ACCESS_TOKEN:-}" ]]; then
          export SRC_ACCESS_TOKEN=$value
        fi
        ;;
    esac
  done < "$dotenv_path"
}

discover_repo_and_rev() {
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "$PYTHON_BIN is required"

  local discovered
  discovered=$(
    "$PYTHON_BIN" - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

query = """
query ListReposPermutationProbe {
  repositories(first: 50) {
    nodes {
      name
      defaultBranch { displayName }
      textSearchIndex {
        refs {
          indexed
          ref { displayName }
        }
      }
    }
  }
}
"""
url = os.environ["SRC_ENDPOINT"].rstrip("/") + "/.api/graphql"
body = json.dumps({"query": query, "variables": {}}).encode()
request = urllib.request.Request(
    url,
    data=body,
    headers={
        "Authorization": f"token {os.environ['SRC_ACCESS_TOKEN']}",
        "Content-Type": "application/json",
        "User-Agent": "list-repos-permutations/0.0.1",
    },
)
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
except urllib.error.HTTPError as error:
    print(
        f"repo discovery HTTP error from {url}: {error.code} {error.reason}",
        file=sys.stderr,
    )
    sys.exit(1)
except OSError as error:
    print(f"repo discovery failed against {url}: {error}", file=sys.stderr)
    sys.exit(1)

errors = payload.get("errors")
if errors:
    print(f"GraphQL errors during repo discovery: {errors}", file=sys.stderr)
    sys.exit(1)

nodes = (((payload.get("data") or {}).get("repositories") or {}).get("nodes") or [])
if not nodes:
    print("no repositories were returned by the Sourcegraph instance", file=sys.stderr)
    sys.exit(1)

fallback = nodes[0]
chosen = None
for node in nodes:
    default_branch = (node.get("defaultBranch") or {}).get("displayName")
    refs = (((node.get("textSearchIndex") or {}).get("refs")) or [])
    indexed_names = {
        ((ref.get("ref") or {}).get("displayName"))
        for ref in refs
        if ref.get("indexed")
    }
    if default_branch and default_branch in indexed_names:
        chosen = node
        break

chosen = chosen or fallback
name = chosen.get("name")
rev = (chosen.get("defaultBranch") or {}).get("displayName") or "HEAD"
if not name:
    print("discovered repository is missing a name", file=sys.stderr)
    sys.exit(1)
print(name)
print(rev)
PY
  ) || fail "failed to parse repo discovery response"

  TEST_REPO=$(printf '%s\n' "$discovered" | sed -n '1p')
  if [[ -z "$TEST_REV" ]]; then
    TEST_REV=$(printf '%s\n' "$discovered" | sed -n '2p')
  fi
}

redacted_command() {
  local redact_next=0
  local arg
  local redacted=()

  for arg in "$@"; do
    if [[ $redact_next -eq 1 ]]; then
      redacted+=("REDACTED")
      redact_next=0
      continue
    fi

    case "$arg" in
      --src-access-token)
        redacted+=("$arg")
        redact_next=1
        ;;
      --src-access-token=*)
        redacted+=("--src-access-token=REDACTED")
        ;;
      *)
        redacted+=("$arg")
        ;;
    esac
  done

  printf '%q ' "${redacted[@]}"
}

append_mode_args() {
  local limit_mode=$1
  local skipped_files_mode=$2
  local skipped_reason_mode=$3
  local count_commits_mode=$4
  local run_search_mode=$5
  local statistics_mode=$6
  local reclone_mode=$7
  local reindex_mode=$8

  case "$limit_mode" in
    bounded) MODE_ARGS+=(--limit "$TEST_LIMIT") ;;
    none) ;;
    *) fail "unknown limit mode: $limit_mode" ;;
  esac

  [[ "$skipped_files_mode" == "on" ]] && MODE_ARGS+=(--skipped-files)
  [[ "$skipped_reason_mode" == "repo_rev" ]] && MODE_ARGS+=(--skipped-files-reason "$TEST_REPO@$TEST_REV")

  case "$count_commits_mode" in
    off) ;;
    all) MODE_ARGS+=(--count-commits) ;;
    repo) MODE_ARGS+=(--count-commits "$TEST_REPO") ;;
    repo_rev) MODE_ARGS+=(--count-commits "$TEST_REPO@$TEST_REV") ;;
    *) fail "unknown count-commits mode: $count_commits_mode" ;;
  esac

  [[ "$run_search_mode" == "on" ]] && MODE_ARGS+=(--run-search "$TEST_SEARCH_PATTERN")
  [[ "$statistics_mode" == "on" ]] && MODE_ARGS+=(--statistics)

  case "$reclone_mode" in
    off) ;;
    all_errors) MODE_ARGS+=(--reclone) ;;
    repo) MODE_ARGS+=(--reclone "$TEST_REPO") ;;
    *) fail "unknown reclone mode: $reclone_mode" ;;
  esac

  case "$reindex_mode" in
    off) ;;
    all_errors) MODE_ARGS+=(--reindex) ;;
    repo) MODE_ARGS+=(--reindex "$TEST_REPO") ;;
    *) fail "unknown reindex mode: $reindex_mode" ;;
  esac
}

run_case() {
  local credential_mode=$1
  local limit_mode=$2
  local skipped_files_mode=$3
  local skipped_reason_mode=$4
  local count_commits_mode=$5
  local run_search_mode=$6
  local statistics_mode=$7
  local reclone_mode=$8
  local reindex_mode=$9
  local case_dir started ended status duration command_text label
  local command=("${BASE_COMMAND[@]}")

  MODE_ARGS=()
  append_mode_args \
    "$limit_mode" \
    "$skipped_files_mode" \
    "$skipped_reason_mode" \
    "$count_commits_mode" \
    "$run_search_mode" \
    "$statistics_mode" \
    "$reclone_mode" \
    "$reindex_mode"

  command+=("${MODE_ARGS[@]}")
  if [[ "$credential_mode" == "cli" ]]; then
    command+=(--src-endpoint "$SRC_ENDPOINT" --src-access-token "$SRC_ACCESS_TOKEN")
  fi

  case_count=$((case_count + 1))
  case_dir=$(printf '%s/case-%04d' "$TEST_OUTPUT_DIR" "$case_count")
  mkdir -p "$case_dir"

  label="credentials=$credential_mode limit=$limit_mode skipped_files=$skipped_files_mode skipped_reason=$skipped_reason_mode count_commits=$count_commits_mode run_search=$run_search_mode statistics=$statistics_mode reclone=$reclone_mode reindex=$reindex_mode"
  command_text=$(redacted_command "${command[@]}")
  printf '[%04d] %s\n       %s\n' "$case_count" "$label" "$command_text"

  started=$(date +%s)
  if [[ "$credential_mode" == "cli" ]]; then
    (
      cd "$case_dir" || exit 1
      unset SRC_ENDPOINT SRC_ACCESS_TOKEN
      "${command[@]}"
    ) >"$case_dir/stdout.txt" 2>"$case_dir/stderr.txt"
    status=$?
  else
    (
      cd "$case_dir" || exit 1
      SRC_ENDPOINT=$SRC_ENDPOINT SRC_ACCESS_TOKEN=$SRC_ACCESS_TOKEN "${command[@]}"
    ) >"$case_dir/stdout.txt" 2>"$case_dir/stderr.txt"
    status=$?
  fi
  ended=$(date +%s)
  duration=$((ended - started))

  printf '%04d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$case_count" \
    "$status" \
    "$duration" \
    "$credential_mode" \
    "$limit_mode" \
    "$skipped_files_mode" \
    "$skipped_reason_mode" \
    "$count_commits_mode" \
    "$run_search_mode" \
    "$statistics_mode" \
    "$reclone_mode/$reindex_mode" \
    "$command_text" >> "$summary_path"

  if [[ $status -ne 0 ]]; then
    failure_count=$((failure_count + 1))
    echo "       FAILED status=$status logs=$case_dir" >&2
    if enabled "$STOP_ON_FAILURE"; then
      echo "stopping after first failure; summary: $summary_path" >&2
      exit "$status"
    fi
  fi
}

main() {
  if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
    usage
    exit 0
  fi
  [[ $# -eq 0 ]] || fail "unexpected argument: $1"

  [[ -f "$SCRIPT_UNDER_TEST" ]] || fail "list-repos.py not found: $SCRIPT_UNDER_TEST"
  if [[ -x "$SCRIPT_UNDER_TEST" ]]; then
    BASE_COMMAND=("$SCRIPT_UNDER_TEST")
  else
    command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "$PYTHON_BIN is required"
    BASE_COMMAND=("$PYTHON_BIN" "$SCRIPT_UNDER_TEST")
  fi

  load_dotenv
  [[ -n "${SRC_ENDPOINT:-}" ]] || fail "set SRC_ENDPOINT or add it to $SCRIPT_DIR/.env"
  [[ -n "${SRC_ACCESS_TOKEN:-}" ]] || fail "set SRC_ACCESS_TOKEN or add it to $SCRIPT_DIR/.env"
  [[ "$SRC_ACCESS_TOKEN" == sgp_* ]] || fail "SRC_ACCESS_TOKEN must start with sgp_"
  case "$TEST_LIMIT" in
    ""|*[!0-9]*) fail "LIST_REPOS_TEST_LIMIT must be a positive integer" ;;
  esac
  [[ "$TEST_LIMIT" -ge 1 ]] || fail "LIST_REPOS_TEST_LIMIT must be >= 1"

  if [[ -z "$TEST_REPO" ]]; then
    discover_repo_and_rev
  elif [[ -z "$TEST_REV" ]]; then
    TEST_REV=HEAD
  fi

  if [[ -z "$TEST_OUTPUT_DIR" ]]; then
    TEST_OUTPUT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/list-repos-permutations.XXXXXX")
  fi
  mkdir -p "$TEST_OUTPUT_DIR"
  summary_path="$TEST_OUTPUT_DIR/summary.tsv"
  printf 'case\tstatus\tduration_seconds\tcredentials\tlimit\tskipped_files\tskipped_reason\tcount_commits\trun_search\tstatistics\tmutations\tcommand\n' > "$summary_path"

  echo "Testing: $SCRIPT_UNDER_TEST"
  echo "Endpoint: $SRC_ENDPOINT"
  echo "Repo: $TEST_REPO"
  echo "Rev: $TEST_REV"
  echo "Output: $TEST_OUTPUT_DIR"
  echo

  local credential_modes=(env)
  local limit_modes=(bounded)
  local skipped_files_modes=(off on)
  local skipped_reason_modes=(off repo_rev)
  local count_commits_modes=(off all repo repo_rev)
  local run_search_modes=(off on)
  local statistics_modes=(off on)
  local reclone_modes=(off)
  local reindex_modes=(off)

  enabled "$INCLUDE_CREDENTIAL_ARGS" && credential_modes=(env cli)
  enabled "$INCLUDE_UNLIMITED" && limit_modes=(none bounded)
  if enabled "$INCLUDE_MUTATIONS"; then
    reclone_modes=(off repo)
    reindex_modes=(off repo)
  fi
  if enabled "$INCLUDE_GLOBAL_MUTATIONS"; then
    enabled "$INCLUDE_MUTATIONS" || fail "LIST_REPOS_TEST_INCLUDE_GLOBAL_MUTATIONS=1 requires LIST_REPOS_TEST_INCLUDE_MUTATIONS=1"
    reclone_modes=(off repo all_errors)
    reindex_modes=(off repo all_errors)
  fi

  if enabled "$INCLUDE_WRITE_CSV_SCHEMA"; then
    local schema_command=("${BASE_COMMAND[@]}" --write-csv-schema)
    case_count=$((case_count + 1))
    local schema_dir
    schema_dir=$(printf '%s/case-%04d' "$TEST_OUTPUT_DIR" "$case_count")
    mkdir -p "$schema_dir"
    echo "[$(printf '%04d' "$case_count")] --write-csv-schema"
    (
      cd "$schema_dir" || exit 1
      "${schema_command[@]}"
    ) >"$schema_dir/stdout.txt" 2>"$schema_dir/stderr.txt"
    local schema_status=$?
    printf '%04d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$case_count" "$schema_status" 0 "n/a" "n/a" "n/a" "n/a" "n/a" "n/a" "n/a" "n/a" "$(redacted_command "${schema_command[@]}")" >> "$summary_path"
    if [[ $schema_status -ne 0 ]]; then
      failure_count=$((failure_count + 1))
    fi
  fi

  local credential_mode limit_mode skipped_files_mode skipped_reason_mode count_commits_mode run_search_mode statistics_mode reclone_mode reindex_mode
  for credential_mode in "${credential_modes[@]}"; do
    for limit_mode in "${limit_modes[@]}"; do
      for skipped_files_mode in "${skipped_files_modes[@]}"; do
        for skipped_reason_mode in "${skipped_reason_modes[@]}"; do
          for count_commits_mode in "${count_commits_modes[@]}"; do
            for run_search_mode in "${run_search_modes[@]}"; do
              for statistics_mode in "${statistics_modes[@]}"; do
                for reclone_mode in "${reclone_modes[@]}"; do
                  for reindex_mode in "${reindex_modes[@]}"; do
                    run_case \
                      "$credential_mode" \
                      "$limit_mode" \
                      "$skipped_files_mode" \
                      "$skipped_reason_mode" \
                      "$count_commits_mode" \
                      "$run_search_mode" \
                      "$statistics_mode" \
                      "$reclone_mode" \
                      "$reindex_mode"
                  done
                done
              done
            done
          done
        done
      done
    done
  done

  echo
  echo "Ran $case_count case(s); failures: $failure_count"
  echo "Summary: $summary_path"
  if [[ $failure_count -ne 0 ]]; then
    exit 1
  fi
}

main "$@"
