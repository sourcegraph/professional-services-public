# Batch Change Retry Script

Scripted solution to automatically retry failed batch spec executions using the Sourcegraph GraphQL API.

## Overview

The `retry-batch-spec.py` script provides commands to list and retry batch specs with failed workspaces. It uses the Sourcegraph GraphQL API to query batch specs, filter for failures, and retry executions automatically.

## Prerequisites

- Python 3.6+
- `requests` library (`pip install requests`)
- Sourcegraph instance with Batch Changes enabled
- Access token with batch changes write permissions

## Setup

**Set environment variables:**

```bash
export SRC_ENDPOINT=https://sourcegraph.example.com
export SRC_ACCESS_TOKEN=sgp_your_token_here
```

## Usage

The script provides two commands:

### 1. List Batch Specs with Failed Workspaces

View all batch specs that have failed workspaces:

```bash
python retry-batch-spec.py list-batch-specs
```

This command:
- Fetches all batch specs (with pagination)
- Filters client-side for specs with `errored > 0`
- Displays the batch spec name, ID, state, and workspace statistics

### 2. Retry All Failed Batch Specs

Automatically retry all batch specs with failed workspaces:

```bash
python retry-batch-spec.py retry-failed-batch-specs
```

This command:
- Queries all batch specs with pagination
- Filters for specs with failed workspaces
- Retries each one automatically
- Provides a summary of retry successes and failures

**Include completed workspaces in retry:**

```bash
python retry-batch-spec.py retry-failed-batch-specs --include-completed
```

## References

- [Sourcegraph Batch Changes Documentation](https://sourcegraph.com/docs/batch-changes)
- [GraphQL API Documentation](https://sourcegraph.com/docs/api/graphql)
