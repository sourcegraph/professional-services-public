"""
EXAMPLE: Sync RBAC role assignment by LDAP group membership

ex. Provide access to Cody via Sourcegraph RBAC roles by LDAP group membership

NOTE: THIS IS NOT A SUPPORTED SOURCEGRAPH PRODUCT. This script was written for Sourcegraph Implementation Engineering deployments, and is not intended, designed, built, or supported for use in any other scenario. Feel free to open issues or PRs, but responses are best effort.

Sourcegraph doesn't currently (v6.2) have a built-in way to assign RBAC roles to users based on LDAP group membership

The current workaround is to write and run a script (like this one) which:

- Queries your directory service to get a list of members of your group
    - Returns their email address which will matches on their Sourcegraph account (username or UPN may also work, depending on your SAML auth provider config)
    - Print the count / list of users in the group for visual verification

- Queries your Sourcegraph instance's GraphQL API to get:
    - The RBAC role ID and permissions
    - A list of users, with usernames, email addresses, IDs, and Roles, write it to a backup file, as this is a destructive process

- Compares the list of users in the directory group with the list of users who have the RBAC role
    - List of users who already have the RBAC role, and their roles
    - If a user is in the RBAC role but not in the group
        - Get their current set of roles
        - Remove the RBAC role from their list of roles
        - Set their roles
    - If a user is in the group but not in the RBAC role
        - Get their current set of roles
        - Add the RBAC role to their list of roles
        - Set their roles

- Sends the GraphQL mutation to your Sourcegraph instance to update the list of users with the RBAC role

- Repeats this on a scheduled interval of your choosing
    - It should be pretty lightweight
    - It could query your LDAP service every few minutes to check for new group members
    - If you have webhooks / a serverless environment, this sync script could be triggered by the webhook

Alternatively, write a Bash script to run the GraphQL mutations via src cli: https://sourcegraph.com/docs/cli/references/api, but managing the user data attributes via Bash would probably be a struggle bus.

"""


# Imports
from datetime import datetime
from dotenv import dotenv_values # https://pypi.org/project/python-dotenv/
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
import json
import os


# Global variables
env_vars_dict = {
    "SRC_ENDPOINT" : {
        "description": "The URL of your Sourcegraph instance, e.g. https://sourcegraph.example.com",
        "required": True,
        "value": None
    },
    "SRC_ACCESS_TOKEN" : {
        "description": "Your Sourcegraph site-admin:sudo access token from https://sourcegraph.example.com/user/settings/tokens",
        "required": True,
        "value": None
    },
    "SRC_RBAC_ROLE_NAME" : {
        "description": "The name of the RBAC role to sync with the LDAP group",
        "required": True,
        "value": None
    },
    "SRC_USERS_BACKUP_FILE" : {
        "description": "Path to the backup file of all users and their roles from the Sourcegraph instance",
        "required": False,
        "value": ".src_users_backup.json"
    },
    "LIST_OF_USERNAMES" : {
        "description": "The list of usernames from the customer's directory service to sync with the RBAC role, separated by commas",
        "required": False,
        "value": None
    }
}

graphql_client : Client = None
rbac_role = {}
src_users_and_their_roles_at_start = {}
src_users_and_their_roles_at_end = {}

# Use the same JSON dictionary schema as the GraphQL query output
src_users_with_rbac_role_at_start = {
    "users": {
        "nodes": []
    }
}

src_users_with_rbac_role_at_end = {
    "users": {
        "nodes": []
    }
}

def read_env_vars():
    """
    Read configuration from either .env file or OS environment variables
    """

    newline()
    log("Function: read_env_vars")

    # Try reading environment variables from .env file
    dot_env_file_content = dotenv_values(".env")
    # dot_env_file_content = {
    #   "SRC_ENDPOINT": "https://sourcegraph.example.com",
    #   "SRC_ACCESS_TOKEN": "sgp_example",
    #   ...
    # }

    missing_required_env_var = False

    for env_var in env_vars_dict:

        env_var_found = False

        # Prioritize env vars set in the .env file
        if env_var in dot_env_file_content:
            env_vars_dict[env_var]['value'] = dot_env_file_content[env_var]
            env_var_found = True

        # If the env var is missing from the .env file, then check the OS environment variables
        elif env_var in os.environ:
            env_vars_dict[env_var]['value'] = os.environ[env_var]
            env_var_found = True

        # If the env var isn't in either location, but is required, then error and exit,
        # after checking all env vars so the user can see all missing env vars at once
        elif env_vars_dict[env_var]['required']:
            log(f"ERROR: Missing env: {env_var} is required")
            missing_required_env_var = True

        else:
            log(f"Missing env: {env_var} but not required, continuing with default value: {env_vars_dict[env_var]['value']}")

        # If the env var was found, print it out
        if env_var_found:

            # Don't print the access token
            if env_var == "SRC_ACCESS_TOKEN":
                log(f"Found env: {env_var}=***********")

            else:
                log(f"Found env: {env_var}={env_vars_dict[env_var]['value']}")

    # If any required environment variables are missing, raise an error and exit
    if missing_required_env_var:
        raise ValueError("One or more required environment variables are missing, please configure it in the .env file, or export it into environment variables")

    # If the endpoint URL does not begin with either the http:// or https:// scheme, raise an error
    # to let the user specify the scheme, instead of trying to fix it ourselves
    if not env_vars_dict['SRC_ENDPOINT']['value'].startswith(('http://', 'https://')):
        raise ValueError("Env: SRC_ENDPOINT must start with http:// or https://")

    # If the endpoint doesn't end with the graphql api path, then add it
    # The transport doesn't seem to care about double / if the URL already ends with /
    if not env_vars_dict['SRC_ENDPOINT']['value'].endswith('/.api/graphql'):
        env_vars_dict['SRC_ENDPOINT']['value'] = env_vars_dict['SRC_ENDPOINT']['value'] + '/.api/graphql'
        log(f"Env: SRC_ENDPOINT doesn't end with '/.api/graphql', appended it: {env_vars_dict['SRC_ENDPOINT']['value']}")


def get_list_of_usernames_from_directory():
    """
    Get the list of usernames from the directory service
    """

    newline()
    log("Function: get_list_of_usernames_from_directory")

    # TODO: Implement this function

    # Current workaround is to read the list of usernames from the LIST_OF_USERNAMES env var
    if env_vars_dict['LIST_OF_USERNAMES']['value']:

        log(f"List of usernames from LIST_OF_USERNAMES: {json.dumps(env_vars_dict['LIST_OF_USERNAMES']['value'].split(','), indent=4)}")

    else:

        log("No usernames in LIST_OF_USERNAMES, skipping list of usernames from directory service")


def setup_graphql_client():
    """
    Create a GraphQL client with the given endpoint and authentication
    """

    newline()
    log("Function: setup_graphql_client")

    # Set authentication header
    headers={'Authorization': f'token-sudo token={env_vars_dict['SRC_ACCESS_TOKEN']['value']}'}

    # Configure the transport
    transport = RequestsHTTPTransport(
        url=env_vars_dict['SRC_ENDPOINT']['value'],
        headers=headers,
        use_json=True,
        retries=10, # retry 10 times, if the request fails for network transport reasons
    )

    # Create the client
    global graphql_client
    graphql_client = Client(transport=transport, fetch_schema_from_transport=True)


def test_connection_and_check_current_user_is_site_admin():
    """
    Check that the current user is a site admin, and the connection to the GraphQL API is working
    """

    newline()
    log("Function: test_connection_and_check_current_user_is_site_admin")

    # Write the query
    current_user_gql_query = gql("""
    query {
        currentUser {
            username
            siteAdmin
            id
            primaryEmail {
                email
            }
        }
    }
    """)

    # Run the query, capture the output
    current_user_gql_output = graphql_client.execute(current_user_gql_query)

    # If the current user is not a site admin, exit
    if current_user_gql_output['currentUser']['siteAdmin']:
        log(f"Verifying Sourcegraph GraphQL API connection, authentication, and current user is Site Admin: \n{json.dumps(current_user_gql_output, indent=4)}")
    else:
        raise ValueError("Current user is not Site Admin, please use a SRC_ACCESS_TOKEN from a Site Admin with sudo token scope")


def get_rbac_role():
    """
    Get the RBAC role from the Sourcegraph instance by the name provided
    Sourcegraph requires that RBAC role names are unique, so it's safe to use the name
    """

    newline()
    log("Function: get_rbac_role")

    # Write the query
    rbac_role_id_gql_query = gql("""
    query {
        roles {
            nodes {
                name
                id
                permissions {
                    nodes {
                        displayName
                        namespace
                        action
                        id
                    }
                }
            }
        }
    }
    """)

    # Run the query, capture the output
    rbac_role_id_gql_output = graphql_client.execute(rbac_role_id_gql_query)

    # Get the Role names from the output
    rbac_role_names = [role['name'] for role in rbac_role_id_gql_output['roles']['nodes']]

    # If the configured role name is in the list, grab it
    if env_vars_dict['SRC_RBAC_ROLE_NAME']['value'] in rbac_role_names:

        # Sourcegraph ensures that roles have unique names, so we can just grab the first one with a matching name
        for role in rbac_role_id_gql_output['roles']['nodes']:
            if role['name'] == env_vars_dict['SRC_RBAC_ROLE_NAME']['value']:
                global rbac_role
                rbac_role = role
                break

        log(f"\"{rbac_role['name']}\" RBAC role found on Sourcegraph instance with role ID: {rbac_role['id']}")

    else:
        raise ValueError(f"SRC_RBAC_ROLE_NAME \"{env_vars_dict['SRC_RBAC_ROLE_NAME']['value']}\" not found in RBAC roles from Sourcegraph instance: {json.dumps(rbac_role_names, indent=4)}")

    # Print out the permissions of the role for visual verification
    log(f"\"{rbac_role['name']}\" RBAC role permissions: \n{json.dumps(rbac_role['permissions']['nodes'], indent=4)}")


def get_all_src_users_and_their_roles():
    """
    Get the list of all users and their roles from the Sourcegraph instance
    """

    newline()
    log("Function: get_all_src_users_and_their_roles")

    # Write the query
    all_src_users_and_their_roles_gql_query = gql("""
    query {
        users {
            nodes {
                id
                username
                primaryEmail {
                    email
                }
                roles {
                    nodes {
                        id
                        name
                    }
                }
            }
        }
    }
    """)

    # Run the query, capture the output
    all_src_users_and_their_roles_gql_output = graphql_client.execute(all_src_users_and_their_roles_gql_query)

    log(f"Count of users on Sourcegraph instance: {len(all_src_users_and_their_roles_gql_output['users']['nodes'])}")

    return all_src_users_and_their_roles_gql_output


def backup_src_users_and_their_roles_to_file(all_src_users_and_their_roles_gql_output):
    """
    Write the output to a backup file
    """

    newline()
    log("Function: backup_src_users_and_their_roles_to_file")


    # If env_vars_dict['SRC_USERS_BACKUP_FILE']['value'] is not an empty string
    # then backup the list of users and their roles to a file
    if env_vars_dict['SRC_USERS_BACKUP_FILE']['value']:

        # Get the file name and path
        src_users_backup_file = env_vars_dict['SRC_USERS_BACKUP_FILE']['value']

        # Get the file path
        src_users_backup_file_path = os.path.dirname(src_users_backup_file)

        # If the path doesn't exist, then create it
        if src_users_backup_file_path and not os.path.exists(src_users_backup_file_path):
            log(f"Path to SRC_USERS_BACKUP_FILE does not exist, creating directory: {src_users_backup_file_path}")
            os.makedirs(src_users_backup_file_path)

        # Back the users and roles JSON blob up to the file
        with open(src_users_backup_file, 'w') as src_users_backup_file_outfile:
            log(f"Writing backup of all users and their roles to file: {src_users_backup_file}")
            json.dump(all_src_users_and_their_roles_gql_output, src_users_backup_file_outfile, indent=4)

    else:
        log("SRC_USERS_BACKUP_FILE is disabled, skipping backup of all users and their roles to file")


def extract_src_users_with_rbac_role(src_users_and_their_roles):
    """
    Get all the users in the RBAC role from the Sourcegraph instance
    """

    newline()
    log("Function: extract_src_users_with_rbac_role")

    src_users_with_rbac_role = {
        "users": {
            "nodes": []
        }
    }

    # Get the users who have the RBAC role at the start
    for user_object in src_users_and_their_roles['users']['nodes']:
        for role in user_object['roles']['nodes']:
            if role['name'] == rbac_role['name']:
                src_users_with_rbac_role['users']['nodes'].append(user_object)

    log(f"Count of users with \"{rbac_role['name']}\" RBAC role assigned on Sourcegraph instance: {len(src_users_with_rbac_role['users']['nodes'])}")
    log(f"List of users with \"{rbac_role['name']}\" RBAC role assigned on Sourcegraph instance: \n{json.dumps(src_users_with_rbac_role, indent=4)}")

    return src_users_with_rbac_role


def remove_rbac_role_from_users_not_in_ldap_group():
    """
    Remove the RBAC role from users who are not in the LDAP group
    """

    newline()
    log("Function: remove_rbac_role_from_users_not_in_ldap_group")

    # Get the list of usernames in the LDAP group
    ldap_group_usernames = env_vars_dict['LIST_OF_USERNAMES']['value'].split(',')

    # log(f"DEBUG: List of usernames in LDAP group: {ldap_group_usernames}")

    # Iterate through the list of users in the RBAC role
    global src_users_with_rbac_role_at_start
    # log(f"DEBUG: src_users_with_rbac_role_at_start: {json.dumps(src_users_with_rbac_role_at_start, indent=4)}")


    for user_object in src_users_with_rbac_role_at_start['users']['nodes']:

        # log(f"DEBUG: User object: {json.dumps(user_object, indent=4)}")

        # If they are not a member of the LDAP group
        if user_object['username'] not in ldap_group_usernames:

            log(f"User \"{user_object['username']}\" started in the \"{rbac_role['name']}\" RBAC role, but not in the LDAP group; removing \"{rbac_role['name']}\" from their list of RBAC roles")

            # Remove the RBAC role from their list of roles
            user_roles=user_object['roles']['nodes']

            log(f"User \"{user_object['username']}\" starting roles: {json.dumps(user_roles, indent=4)}")

            for role in user_roles:
                if role['name'] == rbac_role['name']:
                    user_roles.remove(role)

            log(f"User \"{user_object['username']}\" ending roles: {json.dumps(user_roles, indent=4)}")

            log(f"User \"{user_object['username']}\" sending changes now")

            # Set their roles
            set_user_roles(user_object['id'], [user_role['id'] for user_role in user_roles])

            log(f"User \"{user_object['username']}\" changes sent")

        else:
            log(f"User \"{user_object['username']}\" is in both the LDAP group and the \"{rbac_role['name']}\" RBAC role, skipping removal")


def add_rbac_role_to_users_in_ldap_group():
    """
    Add the RBAC role to users who are in the LDAP group
    """

    newline()
    log("Function: add_rbac_role_to_users_in_ldap_group")

    # Loop through the list of usernames in the LDAP group
        # If a user is in the LDAP group but not in the RBAC role
            # Get their current set of roles
            # Add the RBAC role to their list of roles
            # Set their roles

    # Get the list of usernames in the RBAC role
    rbac_role_user_objects = [user_object for user_object in src_users_with_rbac_role_at_start['users']['nodes']]

    #log(f"rbac_role_user_objects: \n{json.dumps(rbac_role_user_objects, indent=4)}")

    rbac_role_usernames =    [user_object['username'] for user_object in rbac_role_user_objects]

    #log(f"rbac_role_usernames: \n{json.dumps(rbac_role_usernames, indent=4)}")

    # Get the list of usernames in the LDAP group
    list_of_usernames_in_ldap_group = env_vars_dict['LIST_OF_USERNAMES']['value'].split(',')

    # Iterate through the list of usernames in the LDAP group
    for username in list_of_usernames_in_ldap_group:

        # If they are not in the RBAC role
        if username not in rbac_role_usernames:

            log(f"User \"{username}\" not in \"{rbac_role['name']}\" RBAC role, adding now")

            user_object = None

            # Find the user's object in the dict of all user objects
            for src_user_object in src_users_and_their_roles_at_start['users']['nodes']:
                if src_user_object['username'] == username:
                    user_object = src_user_object
                    break

            log(f"User object: {json.dumps(user_object, indent=4)}")

            # Get the list of their current role IDs
            user_role_ids_list=[user_role['id'] for user_role in user_object['roles']['nodes']]

            log(f"User \"{username}\" starting roles IDs: {json.dumps(user_role_ids_list, indent=4)}")

            # Add the RBAC role ID to their list of role IDs
            user_role_ids_list.append(rbac_role['id'])

            log(f"User \"{username}\" ending roles IDs: {json.dumps(user_role_ids_list, indent=4)}")

            log(f"User \"{username}\" sending changes now")

            # Set their roles
            set_user_roles(user_object['id'], user_role_ids_list)

            log(f"User \"{username}\" changes sent")

        else:
            log(f"User \"{username}\" already in \"{rbac_role['name']}\" RBAC role, skipping addition")


def set_user_roles(user_id, role_ids):
    """
    Set the RBAC roles of a user
    Must include all roles, the list of roles sent overwrite all of the user's roles
    """

    newline()
    log("Function: set_user_roles")

    # Write the mutation
    set_user_roles_gql_mutation = gql("""
    mutation setRoles($userId: ID!, $roleIds: [ID!]!) {
        setRoles(user: $userId, roles: $roleIds) {
            alwaysNil
        }
    }
    """)

    # Run the mutation, providing the input variable values, and capture the output
    set_user_roles_gql_output = graphql_client.execute(set_user_roles_gql_mutation, variable_values={
        'userId': user_id,
        'roleIds': role_ids
    })

    log(f"Set user roles GraphQL mutation response: {json.dumps(set_user_roles_gql_output, indent=4)}")


def log(log_message):
    """
    Log a message to the console
    """

    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}: {log_message}")


def newline():
    print("")


def main():
    """
    Main function
    """

    newline()
    log("------------------------------------------------------------")
    log("Script started")
    log("------------------------------------------------------------")

    # Get configuration from environment variables
    read_env_vars()

    # Get the list of usernames from the directory service
    get_list_of_usernames_from_directory()

    # Create the client
    setup_graphql_client()

    # Test the client, verify the current user is a site admin
    test_connection_and_check_current_user_is_site_admin()

    # Get the RBAC role ID and permissions
    get_rbac_role()

    # Get a list of all users and their roles from the Sourcegraph instance
    newline()
    log("Getting the starting list of all users and their roles from the Sourcegraph instance")
    global src_users_and_their_roles_at_start
    src_users_and_their_roles_at_start = get_all_src_users_and_their_roles()

    # Backup the list of users and their roles to a file
    backup_src_users_and_their_roles_to_file(src_users_and_their_roles_at_start)

    # Extract the list of usernames who already have the RBAC role
    global src_users_with_rbac_role_at_start
    src_users_with_rbac_role_at_start = extract_src_users_with_rbac_role(src_users_and_their_roles_at_start)

    # Sync the list of users in the RBAC role with the list of users in the LDAP group
    remove_rbac_role_from_users_not_in_ldap_group()

    # Sync the members of the LDAP group to the RBAC role
    add_rbac_role_to_users_in_ldap_group()

    # Query again to validate the list now matches, and print a success or failure
    newline()
    log("Getting the ending list of all users and their roles from the Sourcegraph instance for a visual check")
    global src_users_and_their_roles_at_end
    src_users_and_their_roles_at_end = get_all_src_users_and_their_roles()

    global src_users_with_rbac_role_at_end
    src_users_with_rbac_role_at_end = extract_src_users_with_rbac_role(src_users_and_their_roles_at_end)

    # TODO: Compare the starting and ending lists to verify the sync worked
    # Alert the user if:
        # src_users_and_their_roles_at_end does not 1:1 match the list of usernames which should be in the role
        # Other diffs appear in the src_users_and_their_roles_at_start vs src_users_and_their_roles_at_end which are not anticipated
    # diff_src_user_and_roles(src_users_and_their_roles_at_start, src_users_and_their_roles_at_end)

    newline()
    log("------------------------------------------------------------")
    log("Finishing script")
    log("------------------------------------------------------------")
    newline()

# Script execution
if __name__ == "__main__":
    main()
