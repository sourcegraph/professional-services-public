# Redis Queue Logger

This directory contains a utility script `read_redis_queue.py` for reading and monitoring Redis List queues. It is specifically designed to help debug and log the **[Sourcegraph Outbound Request Log](https://sourcegraph.com/docs/admin/observability/outbound-request-log#outbound-request-log)** (i.e., the `tnt_1:v2:outbound-requests` queue).

## Context

The Sourcegraph Outbound Request Log is a feature that logs all HTTP/HTTPS requests made by Sourcegraph to external services. These logs are buffered in a Redis List queue before being processed.

## Setup

1.  Install the required dependencies:

    ```bash
    pip install -r requirements.txt
    ```

## Usage

The `redis-cache` service will need to be made accessible to the execution context of the script. For example, the `redis-cache` service can be exposed by port-forwarding the Kubernetes service in order to make it accessible from the default `host` & `port` options ( `localhost:6379`). 

```bash
kubectl port-forward svc/redis-cache 6379:6379
```

The script has two modes:
1.  **Snapshot Mode (Default):** Reads the current state of the queue and prints it to stdout.
2.  **Logger Mode:** Continuously polls the queue and appends new unique items to a log file.

### 1. Snapshot Mode

To read the current items in the queue (default is all items):

```bash
python read_redis_queue.py tnt_1:v2:outbound-requests
```

This will pretty-print the JSON payloads of all items currently in the queue to stdout.

**Options:**
*   `--start <N>`: Start index (default: 0)
*   `--end <N>`: End index (default: -1)
*   `--host <HOST>`: Redis host (default: localhost)
*   `--port <PORT>`: Redis port (default: 6379)

### 2. Logger Mode

To continuously monitor the queue and save unique items to a file:

```bash
python read_redis_queue.py tnt_1:v2:outbound-requests --log-file outbound_log.jsonl
```

This acts as a "best-effort" observer. It polls the queue at a regular interval and appends any new items (determined by their `id` field) to the log file. It keeps a history of seen IDs to prevent duplicates, even across restarts of the script.

**Options:**
*   `--log-file <FILE>`: Path to the output log file (required for logger mode).
*   `--interval <SECONDS>`: Polling interval in seconds (default: 5.0).
*   `--dedup-field <FIELD>`: The JSON field to use for deduplication (default: `id`).

**Example with custom interval:**

```bash
# Poll every 0.5 seconds to minimize chance of missing items
python read_redis_queue.py tnt_1:v2:outbound-requests \
    --log-file outbound_log.jsonl \
    --interval 0.5
```

**Note:** The script will continue running until it is manually stopped using `Ctrl+C`.
