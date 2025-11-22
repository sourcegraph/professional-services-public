import redis
import argparse
import sys
import json
import time
import os

def read_queue(args):
    try:
        r = redis.Redis(host=args.host, port=args.port, decode_responses=True)
        r.ping()
        items = r.lrange(args.queue_name, args.start, args.end)
    except redis.exceptions.ConnectionError as e:
        print(f"Error conecting to Redis: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)
        sys.exit(1)

    return items

def load_logged_ids(log_file, dedup_field):
    logged_ids = set()
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if dedup_field in data:
                            logged_ids.add(data[dedup_field])
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"Warning: Could not read existing log file: {e}", file=sys.stderr)
    return logged_ids

def run_logger(args):
    # Ensure log directory exists
    log_dir = os.path.dirname(args.log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    logged_ids = load_logged_ids(args.log_file, args.dedup_field)
    print(f"Loaded {len(logged_ids)} unique IDs from {args.log_file}")
    print(f"Starting logger on queue '{args.queue_name}' (polling every {args.interval}s)...")
    print("Press Ctrl+C to stop.")

    try:
        r = redis.Redis(host=args.host, port=args.port, decode_responses=True)
        r.ping()

        while True:
            try:
                # Optimization: Read last 500 items
                items = r.lrange(args.queue_name, -500, -1)
                
                new_items_count = 0
                with open(args.log_file, 'a') as f:
                    for item in items:
                        try:
                            data = json.loads(item)
                            item_id = data.get(args.dedup_field)
                            
                            if item_id and item_id not in logged_ids:
                                logged_ids.add(item_id)
                                # Write raw item string as received from Redis
                                f.write(item + '\n')
                                new_items_count += 1
                                print(f"Logged new item: {item_id}")
                        except json.JSONDecodeError:
                            continue
                    
            except redis.exceptions.ConnectionError as e:
                print(f"Redis connection error: {e}", file=sys.stderr)
            except Exception as e:
                print(f"Error during polling: {e}", file=sys.stderr)
            
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nLogger stopped.")
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description='Read from a Redis list queue.')
    parser.add_argument('queue_name', help='Name of the Redis key (queue) to read from')
    parser.add_argument('--host', default='localhost', help='Redis host (default: localhost)')
    parser.add_argument('--port', type=int, default=6379, help='Redis port (default: 6379)')
    parser.add_argument('--start', type=int, default=0, help='Start index for LRANGE (default: 0)')
    parser.add_argument('--end', type=int, default=-1, help='End index for LRANGE (default: -1, read full queue)')
    
    # Logging arguments
    parser.add_argument('--log-file', help='Path to log file. If specified, enables continuous logging mode.')
    parser.add_argument('--interval', type=float, default=5.0, help='Polling interval in seconds (only used with --log-file, default: 5.0)')
    parser.add_argument('--dedup-field', default='id', help='JSON field to use for deduplication (default: id)')

    args = parser.parse_args()

    if args.log_file:
        run_logger(args)
    else:
        items = read_queue(args)

        if len(items) <= 0:
            print(f"No elements found for redis queue '{args.queue_name}'")
        else:
            for item in items:
                print(json.dumps(json.loads(item),indent=2))

if __name__ == "__main__":
    main()
