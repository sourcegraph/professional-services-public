# test case:
# 1. add single user to single org
# 2. add single user to multiple orgs
# 3. add multiple users to single org
# 4. add multiple users to multiple orgs
# 5. add user to non-existent org
# 6. add user to org where user is already a member
# 7. attempt to add user to org name when there are multiple orgs with the same name

# todo:
# test all test cases
# add github action for validating all test cases

import csv
import requests
import argparse
import os
import sys

GRAPHQL_CONFIG = {
    "ListOrganizations": {
        "query": """
            query organizations($first: Int, $query: String) {
                organizations(first: $first, query: $query) {
                    nodes {
                        name
                        displayName
                    }
                    totalCount
                }
            }
        """,
        "variables": ["first", "query"],
        "success_key": "organizations"
    },
    "CreateOrganization": {
        "query": """
            mutation CreateOrganization($name: String!, $displayName: String) {
                createOrganization(name: $name, displayName: $displayName) {
                    name
                    displayName
                }
            }
        """,
        "variables": ["name", "displayName"],
        "success_key": "createOrganization"
    },
    "AddUserToOrganization": {
        "query": """
            mutation addUserToOrganization($organization: ID!, $username: String!) {
                addUserToOrganization(organization: $organization, username: $username) {
                    alwaysNil
                }
            }
        """,
        "variables": ["organization", "username"],
        "success_key": "addUserToOrganization"
    }
}

def execute_graphql_operation(endpoint, headers, operation_name, variables):
    mutation = GRAPHQL_CONFIG[operation_name]["query"]

    endpoint = f"{endpoint}/.api/graphql"
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json={"query": mutation, "variables": variables},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"errors": [{"message": str(e)}]}

def validate_create_organization(name, display_name):
    if not isinstance(name, str):
        raise ValueError("Name must be a string.")
    if not isinstance(display_name, str):
        raise ValueError("Display name must be a string.")

def validate_add_user_to_organization(organization, username):
    if not isinstance(organization, str):
        raise ValueError("Organization name must be a string.")
    if not isinstance(username, str):
        raise ValueError("Username must be a string.")

def handle_list_organizations(endpoint, auth_token):
    headers = {
        "Authorization": f"token {auth_token}",
        "Content-Type": "application/json"
    }

    result = execute_graphql_operation(endpoint, headers, "ListOrganizations", {})

    if "errors" in result:
        print(f"Failed to retrieve organizations: {result['errors'][0]['message']}")
    else:
        organizations = result.get("data", {}).get("organizations", {}).get("nodes", [])
        total_count = result.get("data", {}).get("organizations", {}).get("totalCount", 0)

        print(f"Total Organizations: {total_count}")
        for org in organizations:
            print(f"- Name: {org['name']}, Display Name: {org['displayName']}")

def handle_create_organization(csv_file, endpoint, auth_token):
    headers = {
        "Authorization": f"token {auth_token}",
        "Content-Type": "application/json"
    }

    created_organizations = []
    existing_organizations = []
    failed_organizations = []

    with open(csv_file, mode='r') as file:
        csv_reader = csv.DictReader(file)
        csv_reader.fieldnames = [field.strip() for field in csv_reader.fieldnames]

        if set(csv_reader.fieldnames) != {"name", "displayName"}:
            raise ValueError("CSV file must contain 'name' and 'displayName' columns.")

        for row in csv_reader:
            validate_create_organization(row["name"], row["displayName"])
            variables = {
                "name": row["name"].strip(),
                "displayName": row["displayName"].strip()
            }

            result = execute_graphql_operation(endpoint, headers, "CreateOrganization", variables)

            if "errors" in result:
                error_message = result["errors"][0]["message"]
                if "already taken" in error_message:
                    existing_organizations.append(variables["name"])
                else:
                    print(f"Error creating organization {variables['name']}: {error_message}")
                    failed_organizations.append(variables["name"])
            else:
                created_organizations.append(variables["name"])

    print("\nSummary:")
    print("Created Organizations:", created_organizations if created_organizations else "N/A")
    print("Existing Organizations:", existing_organizations if existing_organizations else "N/A")
    print("Failed Organizations:", failed_organizations if failed_organizations else "N/A")

def handle_add_user_to_organization(organization, username, csv_file, endpoint, auth_token):
    headers = {
        "Authorization": f"token {auth_token}",
        "Content-Type": "application/json"
    }

    success_count = 0
    failure_count = 0

    organizations = execute_graphql_operation(endpoint, headers, "ListOrganizations", {})

    if "errors" in organizations:
        raise ValueError(f"Failed to fetch list of organizations: {result['errors'][0]['message']}")
    elif not organizations:
        raise ValueError("No organizations found. Cannot proceed with adding users.")
    else:
        organizations = organizations.get("data", {}).get("organizations", {}).get("nodes", [])

    if csv_file:
        with open(csv_file, mode='r') as file:
            csv_reader = csv.DictReader(file)
            csv_reader.fieldnames = [field.strip() for field in csv_reader.fieldnames]

            if set(csv_reader.fieldnames) != {"organization", "user"}:
                raise ValueError("CSV file must contain 'organization' and 'user' columns.")

            for row in csv_reader:
                try:
                    validate_add_user_to_organization(row["organization"], row["user"])

                    matched_organizations = [org for org in organizations if org["name"] == row["organization"]]

                    if len(matched_organizations) == 0:
                        raise ValueError(f"Organization '{row["organization"]}' does not exist.")
                    elif len(matched_organizations) > 1:
                        raise ValueError(f"Multiple organizations found with the name '{row["organization"]}'")

                    organization_id = matched_organizations[0]["id"]

                    variables = {
                        "organization": organization_id,
                        "username": row["user"]
                    }
                    result = execute_graphql_operation(endpoint, headers, "AddUserToOrganization", variables)
                    if "errors" in result:
                        print(f"Failed to add user {variables['username']} to organization {variables['organization']}: {result['errors'][0]['message']}")
                        failure_count += 1
                    else:
                        print(f"Successfully added user {variables['username']} to organization {variables['organization']}")
                        success_count += 1
                except ValueError as e:
                    print(f"Validation error: {e}")
                    failure_count += 1
    else:
        validate_add_user_to_organization(organization, username)

        matched_organizations = [org for org in organizations if org["name"] == organization]

        if len(matched_organizations) == 0:
            raise ValueError(f"Organization '{organization}' does not exist.")
        elif len(matched_organizations) > 1:
            raise ValueError(f"Multiple organizations found with the name '{organization}'")

        organization_id = matched_organizations[0]["id"]

        variables = {
            "organization": organization_id,
            "username": username
        }
        result = execute_graphql_operation(endpoint, headers, "AddUserToOrganization", variables)
        if "errors" in result:
            print(f"Failed to add user {username} to organization {organization}: {result['errors'][0]['message']}")
            failure_count += 1
        else:
            print(f"Successfully added user {username} to organization {organization}")
            success_count += 1

    print("\nSummary:")
    print(f"Successfully added: {success_count}")
    print(f"Failed: {failure_count}")

OPERATION_ROUTER = {
    "list-organizations": handle_list_organizations,
    "create-organization": handle_create_organization,
    "add-user-to-organization": handle_add_user_to_organization,
}

def get_required_env_vars():
    endpoint = os.getenv("SRC_ENDPOINT")
    auth_token = os.getenv("SRC_ACCESS_TOKEN")

    if not endpoint:
        raise EnvironmentError("Environment variable 'SRC_ENDPOINT' is required but not set.")
    if not auth_token:
        raise EnvironmentError("Environment variable 'SRC_ACCESS_TOKEN' is required but not set.")

    return endpoint, auth_token

# removes positional arguments from help output
class CleanHelpFormatter(argparse.HelpFormatter):
    def _format_action(self, action):
        if action.dest == "command":  
            return ""
        return super()._format_action(action)

    def add_arguments(self, actions):
        filtered_actions = [a for a in actions if a.option_strings]
        super().add_arguments(filtered_actions)

# custom error handler
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
        description="Sourcegraph organization management CLI",
        usage=(
            "\n  organization-management.py <command> [options]\n\n"
            "Environment Variables:\n"
            "  SRC_ENDPOINT: Sourcegraph endpoint to use (e.g., https://example.sourcegraph.com)\n"
            "  SRC_ACCESS_TOKEN: Sourcegraph access token\n\n"
            "Commands:\n"
            "  list-organizations\n"
            "  create-organization --csv <file>\n"
            "  add-user-to-organization --organization <organization id> --user <username> --csv <file>\n"
        ),
        add_help=False
    )

    parser.add_argument(
        '-h', '--help', action='help', default=argparse.SUPPRESS,
        help='Show this help message and exit'
    )

    subparsers = parser.add_subparsers(dest="command", title="Commands", metavar="", help="")

    subparsers.add_parser(
        "list-organizations",
        help="List all existing organizations",
        add_help=False
    )

    create_org_parser = subparsers.add_parser(
        "create-organization",
        help="Create organizations from a CSV file",
        add_help=False
    )
    create_org_parser.add_argument("--csv", required=True, help="Path to source .csv file")

    add_user_parser = subparsers.add_parser(
        "add-user-to-organization",
        help="Add user(s) to organization. If --csv is specified, the script will attempt to add users from the CSV file to the specified organization. Otherwise, the script will attempt to add the specified user to the specified organization.",
        add_help=False
    )
    add_user_parser.add_argument("--organization", type=str, help="Organization name")
    add_user_parser.add_argument("--user", type=str, help="Username")
    add_user_parser.add_argument("--csv", help="Path to CSV file containing list of organization names and usernames")

    args = parser.parse_args()

    try:
        endpoint, auth_token = get_required_env_vars()
    except EnvironmentError as e:
        print(f"\nError: {e}\n")
        sys.exit(1)

    if args.command == "list-organizations":
        handle_list_organizations(endpoint, auth_token)
    elif args.command == "create-organization":
        handle_create_organization(args.csv, endpoint, auth_token)
    elif args.command == "add-user-to-organization":
        handle_add_user_to_organization(args.organization, args.user, args.csv, endpoint, auth_token)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
