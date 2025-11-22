import redis
import argparse
import sys
import json

def read_queue(host, port, queue_name, start, end):
    try:
        r = redis.Redis(host=host, port=port, decode_responses=True)
        r.ping()
        items = r.lrange(queue_name, start, end)
    except redis.exceptions.ConnectionError as e:
        print(f"Error conecting to Redis: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)
        sys.exit(1)

    return items

def main():
    parser = argparse.ArgumentParser(description='Read from a Redis list queue.')
    parser.add_argument('queue_name', help='Name of the Redis key (queue) to read from')
    parser.add_argument('--host', default='localhost', help='Redis host (default: localhost)')
    parser.add_argument('--port', type=int, default=6379, help='Redis port (default: 6379)')
    parser.add_argument('--start', type=int, default=0, help='Start index for LRANGE (default: 0)')
    parser.add_argument('--end', type=int, default=-1, help='End index for LRANGE (default: -1)')

    args = parser.parse_args()

    items = read_queue(args.host, args.port, args.queue_name, args.start, args.end)

    if len(items) <= 0:
        print(f"No elements found for redis queue '{args.queue_name}'")
    else:
        for item in items:
            print(json.dumps(json.loads(item),indent=2))

if __name__ == "__main__":
    main()
