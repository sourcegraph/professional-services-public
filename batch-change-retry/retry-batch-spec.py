import requests
import argparse
import os
import sys
from datetime import datetime

GRAPHQL_CONFIG = {
    "BatchSpecsWithFailedWorkflows": {
        "query": """
            query BatchSpecsWithFailedWorkflows($first: Int, $after: String) {
                batchSpecs(
                    first: $first
                    after: $after
                    includeLocallyExecutedSpecs: false
                    excludeEmptySpecs: true
                ) {
                    totalCount
                    pageInfo {
                        hasNextPage
                        endCursor
                    }
                    nodes {
                        id
                        state
                        createdAt
                        description {
                            name
                        }
                        namespace {
                            namespaceName
                        }
                        appliesToBatchChange {
                            id
                            name
                        }
                        workspaceResolution {
                            state
                            workspaces(first: 0) {
                                stats {
                                    errored
                                    completed
                                    processing
                                    queued
                                    ignored
                                }
                            }
                        }
                    }
                }
            }
        """,
        "variables": ["first", "after"],
        "success_key": "batchSpecs"
    },
    "RetryBatchSpec": {
        "query": """
            mutation RetryBatchSpec($batchSpec: ID!, $includeCompleted: Boolean!) {
                retryBatchSpecExecution(batchSpec: $batchSpec, includeCompleted: $includeCompleted) {
                    id
                    state
                }
            }
        """,
        "variables": ["batchSpec", "includeCompleted"],
        "success_key": "retryBatchSpecExecution"
    }
}

def execute_graphql_operation(endpoint, headers, operation_name, variables):
    operation = GRAPHQL_CONFIG[operation_name]["query"]

    graphql_endpoint = f"{endpoint}/.api/graphql"
    try:
        response = requests.post(
            graphql_endpoint,
            headers=headers,
            json={"query": operation, "variables": variables},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"errors": [{"message": str(e)}]}

def get_all_batch_specs_with_pagination(endpoint, headers):
    all_batch_specs = []
    has_next_page = True
    after = None
    
    while has_next_page:
        variables = {"first": 100}
        if after:
            variables["after"] = after
        
        result = execute_graphql_operation(endpoint, headers, "BatchSpecsWithFailedWorkflows", variables)
        
        if "errors" in result:
            raise ValueError(f"Failed to fetch batch specs: {result['errors'][0]['message']}")
        
        data = result.get("data", {}).get("batchSpecs", {})
        nodes = data.get("nodes", [])
        page_info = data.get("pageInfo", {})
        
        all_batch_specs.extend(nodes)
        
        has_next_page = page_info.get("hasNextPage", False)
        after = page_info.get("endCursor")
    
    return all_batch_specs

def filter_failed_batch_specs(batch_specs): 
    latest_specs = {}
    
    for spec in batch_specs:
        applies_to = spec.get("appliesToBatchChange")
        if not applies_to:
            continue
        
        batch_change_id = applies_to.get("id")
        if not batch_change_id:
            continue
        
        existing = latest_specs.get(batch_change_id)
        spec_created_at = datetime.fromisoformat(spec["createdAt"].replace("Z", "+00:00"))
        
        if not existing:
            latest_specs[batch_change_id] = spec
        else:
            existing_created_at = datetime.fromisoformat(existing["createdAt"].replace("Z", "+00:00"))
            if spec_created_at > existing_created_at:
                latest_specs[batch_change_id] = spec
        
    failed_specs = []
    for spec in latest_specs.values():
        workspace_resolution = spec.get("workspaceResolution")
        if not workspace_resolution:
            continue
        
        workspaces = workspace_resolution.get("workspaces")
        if not workspaces:
            continue
        
        stats = workspaces.get("stats", {})
        errored_count = stats.get("errored", 0)
        
        if errored_count > 0:
            failed_specs.append(spec)
    
    return failed_specs

def handle_list_batch_specs(endpoint, auth_token):
    headers = {
        "Authorization": f"token {auth_token}",
        "Content-Type": "application/json"
    }
    
    try:
        all_batch_specs = get_all_batch_specs_with_pagination(endpoint, headers)
        failed_specs = filter_failed_batch_specs(all_batch_specs)
        
        print(f"Batch Specs with Failed Workspaces: {len(failed_specs)}")
        print()
        
        if failed_specs:
            for spec in failed_specs:
                stats = spec.get("workspaceResolution", {}).get("workspaces", {}).get("stats", {})
                print(f"Name: {spec['namespace']['namespaceName']}/{spec['description']['name']}")
                print(f"  ID: {spec['id']}")
                print(f"  Created At: {spec['createdAt']}")
                print(f"  State: {spec['state']}")
                print(f"  Errored: {stats.get('errored', 0)}")
                print(f"  Completed: {stats.get('completed', 0)}")
                print(f"  Processing: {stats.get('processing', 0)}")
                print(f"  Queued: {stats.get('queued', 0)}")
                print()
        else:
            print("No batch specs with failed workspaces found.")
    
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

def handle_retry_failed_batch_spec(batch_spec_id, include_completed, endpoint, auth_token):
    headers = {
        "Authorization": f"token {auth_token}",
        "Content-Type": "application/json"
    }
    
    variables = {
        "batchSpec": batch_spec_id,
        "includeCompleted": include_completed
    }
    
    result = execute_graphql_operation(endpoint, headers, "RetryBatchSpec", variables)
    
    if "errors" in result:
        print(f"Failed to retry batch spec {batch_spec_id}: {result['errors'][0]['message']}")
        sys.exit(1)
    else:
        print(f"Successfully retried batch spec: {batch_spec_id}")
        print(f"  New state: {result['data']['retryBatchSpecExecution']['state']}")

def handle_retry_failed_batch_specs(include_completed, endpoint, auth_token):
    headers = {
        "Authorization": f"token {auth_token}",
        "Content-Type": "application/json"
    }
    
    try:
        all_batch_specs = get_all_batch_specs_with_pagination(endpoint, headers)
        failed_specs = filter_failed_batch_specs(all_batch_specs)
        
        if not failed_specs:
            print("No batch specs with failed workspaces found.")
            return
        
        print(f"Found {len(failed_specs)} batch specs with failed workspaces.")
        print()
        
        success_count = 0
        failure_count = 0
        
        for spec in failed_specs:
            batch_spec_id = spec["id"]
            batch_spec_name = f"{spec['namespace']['namespaceName']}/{spec['description']['name']}"
            variables = {
                "batchSpec": batch_spec_id,
                "includeCompleted": include_completed
            }
            
            result = execute_graphql_operation(endpoint, headers, "RetryBatchSpec", variables)
            
            if "errors" in result:
                print(f"Failed to retry {batch_spec_name} ({batch_spec_id}): {result['errors'][0]['message']}")
                failure_count += 1
            else:
                print(f"Successfully retried: {batch_spec_name} ({batch_spec_id})")
                success_count += 1
        
        print()
        print("Summary:")
        print(f"  Successfully retried: {success_count}")
        print(f"  Failed to retry: {failure_count}")
    
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

def get_required_env_vars():
    endpoint = os.getenv("SRC_ENDPOINT")
    auth_token = os.getenv("SRC_ACCESS_TOKEN")

    if not endpoint:
        raise EnvironmentError("Environment variable 'SRC_ENDPOINT' is required but not set.")
    if not auth_token:
        raise EnvironmentError("Environment variable 'SRC_ACCESS_TOKEN' is required but not set.")

    return endpoint, auth_token

class CleanHelpFormatter(argparse.HelpFormatter):
    def _format_action(self, action):
        if action.dest == "command":  
            return ""
        return super()._format_action(action)

    def add_arguments(self, actions):
        filtered_actions = [a for a in actions if a.option_strings]
        super().add_arguments(filtered_actions)

class CleanParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs['formatter_class'] = CleanHelpFormatter
        super().__init__(*args, **kwargs)

    def error(self, message):
        print(f"\nError: {message}\n")
        self.print_help()
        sys.exit(1)

def main():
    parser = CleanParser(
        description="Sourcegraph batch change retry CLI",
        usage=(
            "\n  retry-batch-spec.py <command> [options]\n\n"
            "Environment Variables:\n"
            "  SRC_ENDPOINT: Sourcegraph endpoint to use (e.g., https://example.sourcegraph.com)\n"
            "  SRC_ACCESS_TOKEN: Sourcegraph access token\n\n"
            "Commands:\n"
            "  list-batch-specs\n"
            "  retry-failed-batch-spec <batch_spec_id> [--include-completed]\n"
            "  retry-failed-batch-specs [--include-completed]\n"
        ),
        add_help=False
    )

    parser.add_argument(
        '-h', '--help', action='help', default=argparse.SUPPRESS,
        help='Show this help message and exit'
    )

    subparsers = parser.add_subparsers(dest="command", title="Commands", metavar="", help="")

    subparsers.add_parser(
        "list-batch-specs",
        help="List all batch specs with failed workspaces",
        add_help=False
    )

    retry_single_parser = subparsers.add_parser(
        "retry-failed-batch-spec",
        help="Retry a specific batch spec by ID",
        add_help=False
    )
    retry_single_parser.add_argument("batch_spec_id", help="The batch spec ID to retry")
    retry_single_parser.add_argument("--include-completed", action="store_true", default=False, help="Include completed workspaces in retry")

    retry_all_parser = subparsers.add_parser(
        "retry-failed-batch-specs",
        help="Automatically retry all batch specs with failed workspaces",
        add_help=False
    )
    retry_all_parser.add_argument("--include-completed", action="store_true", default=False, help="Include completed workspaces in retry")

    args = parser.parse_args()

    try:
        endpoint, auth_token = get_required_env_vars()
    except EnvironmentError as e:
        print(f"\nError: {e}\n")
        sys.exit(1)

    if args.command == "list-batch-specs":
        handle_list_batch_specs(endpoint, auth_token)
    elif args.command == "retry-failed-batch-spec":
        handle_retry_failed_batch_spec(args.batch_spec_id, args.include_completed, endpoint, auth_token)
    elif args.command == "retry-failed-batch-specs":
        handle_retry_failed_batch_specs(args.include_completed, endpoint, auth_token)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
