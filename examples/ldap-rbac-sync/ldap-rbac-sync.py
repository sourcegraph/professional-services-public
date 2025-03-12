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

TODO:
- Get access to an LDAP endpoint to query against
- Implement LDAP query in get_list_of_users_from_directory() function
- Output a summary and count of users added / removed
- Implement syncing multiple LDAP groups to RBAC roles
    - MAP_OF_LDAP_GROUPS_TO_RBAC_ROLES = {
        "ldap_group_name_1": "rbac_role_name_1",
        "ldap_group_name_2": "rbac_role_name_2",
        ...
    }
- Implement standard logging library and log levels
- Implement before and after comparison, to verify that:
    - The needed changes were made
    - No other changes were made
    - Alert the user if:
        - src_users_and_their_roles_at_end does not 1:1 match the list of usernames
        - Other diffs appear in the src_users_and_their_roles_at_start vs src_users_and_their_roles_at_end which are not anticipated
            - diff_src_user_and_roles(src_users_and_their_roles_at_start, src_users_and_their_roles_at_end)

"""


### Imports
from datetime import datetime
from dotenv import dotenv_values # https://pypi.org/project/python-dotenv/
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
import json
import os


### Global variables and their default values
env_vars_dict = {
    "LIST_OF_USERS" : {
        "description": "The list of usernames and/or verified email addresses from the customer's directory service to sync with the RBAC role, separated by commas",
        "validation_requirements": "Usernames or email addresses provided must match username or verified email address of users on your Sourcegraph instance, otherwise the RBAC role will be removed from all users",
        "required": True,
        "value": None
    },
    "REMOVE_ALL_USERS_FROM_RBAC_ROLE" : {
        "description": "Set to True if you're intentionally sending an empty list of users, and wish to remove all users from the RBAC role",
        "validation_requirements": "True or False",
        "required": False,
        "value": False
    },
    "SRC_ACCESS_TOKEN" : {
        "description": "Access Sourcegraph access token from https://sourcegraph.example.com/user/settings/tokens",
        "validation_requirements": "user:all token scope is sufficient, site-admin:sudo token scope is not required",
        "required": True,
        "value": None
    },
    "SRC_ENDPOINT" : {
        "description": "The URL of your Sourcegraph instance, e.g. https://sourcegraph.example.com",
        "validation_requirements": "Must begin with http:// or https://",
        "required": True,
        "value": None
    },
    "SRC_RBAC_ROLE_NAME" : {
        "description": "Display name of RBAC role in your Sourcegraph instance to sync users to",
        "required": True,
        "value": None
    },
    "SRC_TLS_VERIFY" : {
        "description": "Control if / how to verify your Sourcegraph instance's TLS certificate",
        "validation_requirements": [
            "True (default): Verify your Sourcegraph instance's TLS certificate with the running host OS' default settings",
            "False: Disable verification of your Sourcegraph instance's TLS certificate",
            "string: must be a path to a CA bundle to verify your Sourcegraph instance's TLS certificate against"
        ],
        "required": False,
        "value": True
    },
    "SRC_USERS_BACKUP_FILE" : {
        "description": "Path to the backup file of all users and their roles from the Sourcegraph instance, for safety, in case roles are inadvertently removed from users. Leave undeclared to use the default path, declare as an empty string to disable the backup, or provide a path to the backup destination",
        "required": False,
        "value": ".src_users_backup.json"
    },
}

graphql_client : Client = None
list_of_users = []
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


### Functions

def read_env_vars():
    """
    Read configuration from .env file and/or OS environment variables
    If the same env var is declared in both places, use the value from the .env file
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
            log(f"Optional env: {env_var} not provided, using default: {env_vars_dict[env_var]['value']}")

        # If the env var was found, print it out
        if env_var_found:

            # Don't print the access token
            if env_var == "SRC_ACCESS_TOKEN":
                log(f"Found env: {env_var}=***********")

            else:
                log(f"Found env: {env_var}={env_vars_dict[env_var]['value']}")

    # If any required environment variables are missing, raise an error and exit
    if missing_required_env_var:
        raise ValueError("One or more required environment variables are missing, please configure them in the .env file, or export them into environment variables")

    # Try casting REMOVE_ALL_USERS_FROM_RBAC_ROLE to a boolean
    remove_all_users_from_rbac_role = str(env_vars_dict['REMOVE_ALL_USERS_FROM_RBAC_ROLE']['value']).lower()
    if remove_all_users_from_rbac_role in ['true', 't', '1']:
        env_vars_dict['REMOVE_ALL_USERS_FROM_RBAC_ROLE']['value'] = True
    else:
        env_vars_dict['REMOVE_ALL_USERS_FROM_RBAC_ROLE']['value'] = False

    # If the endpoint URL does not begin with either the http:// or https:// scheme, raise an error
    # to let the user specify the scheme, instead of trying to fix it ourselves
    if not env_vars_dict['SRC_ENDPOINT']['value'].startswith(('http://', 'https://')):
        raise ValueError("Env: SRC_ENDPOINT must start with http:// or https://")

    # If the endpoint doesn't end with the GraphQL api path, then add it
    # The transport doesn't seem to care about a double // in the URL, if the user provided a trailing slash /
    # Beyond this, let the gql library raise any errors if the URL is invalid
    if not env_vars_dict['SRC_ENDPOINT']['value'].endswith('/.api/graphql'):
        env_vars_dict['SRC_ENDPOINT']['value'] = env_vars_dict['SRC_ENDPOINT']['value'] + '/.api/graphql'
        log(f"Env: SRC_ENDPOINT doesn't end with '/.api/graphql', appended it: {env_vars_dict['SRC_ENDPOINT']['value']}")

    # Try casting SRC_TLS_VERIFY to a boolean
    src_tls_verify = str(env_vars_dict['SRC_TLS_VERIFY']['value']).lower()
    if src_tls_verify in ['true', 't', '1']:
        env_vars_dict['SRC_TLS_VERIFY']['value'] = True
    elif src_tls_verify in ['false', 'f', '0', '']:
        env_vars_dict['SRC_TLS_VERIFY']['value'] = False
    # If the env var is something else, then pass it to the RequestsHTTPTransport as-is,
    # and let the gql library provide the error


def get_list_of_users_from_directory():
    """
    Get the list of usernames from the directory service
    """

    newline()
    log("Function: get_list_of_users_from_directory")

    # TODO: Implement this function
    # Current workaround is to read the list of usernames / email addresses from the LIST_OF_USERS env var

    # If we call this function after we get the list of all users from the Sourcegraph instance
    # then we could try to dedupe the provided list of usernames and email addresses
    # against the list of usernames and email addresses from the Sourcegraph instance
    # in the cases where both a username and email address are
        # provided in the list to be added
        # or removed from the list
    # in the same run of the script
    # but are attached to the same Sourcegraph account
    # But the only downside to duplicates
    # is duplicated GraphQL mutations sent
    # The end result is the same
    # There's no change even if a username is removed from the list and an email address is added

    global list_of_users # Global required when modifying global variable in function

    if env_vars_dict['LIST_OF_USERS']['value']:

        # Read the comma-delimited list of usernames / email addresses from the env var
        list_of_users = sorted(                                             # Sort the list with sorted()
            list(                                                           # Read into list() to sort
                set(                                                        # Read into a set() to deduplicate strings
                    env_vars_dict['LIST_OF_USERS']['value'].split(',')      # Split string to list, on commas
                )
            )
        )

        # Remove empty strings, ex. repeated or trailing commas
        list_of_users[:] = [string for string in list_of_users if string.strip()]

        log(f"List of usernames and/or email addresses provided in LIST_OF_USERS (sorted and deduplicated): {json.dumps(list_of_users, indent=4)}")

    else:

        if env_vars_dict['REMOVE_ALL_USERS_FROM_RBAC_ROLE']['value']:

            log("WARNING: LIST_OF_USERS is empty, and REMOVE_ALL_USERS_FROM_RBAC_ROLE is True, this will remove all users from RBAC role")

        else:
            raise ValueError("LIST_OF_USERS is empty, and REMOVE_ALL_USERS_FROM_RBAC_ROLE is False, please provide a list of usernames or email addresses from the customer's directory service to sync with the RBAC role, or set REMOVE_ALL_USERS_FROM_RBAC_ROLE to True to remove all users from the RBAC role")


def setup_graphql_client():
    """
    Create a GraphQL client with the given endpoint and authentication
    """

    newline()
    log("Function: setup_graphql_client")

    # Set authentication header
    headers={'Authorization': f'token {env_vars_dict['SRC_ACCESS_TOKEN']['value']}'}

    # Configure the transport
    transport = RequestsHTTPTransport(
        url=env_vars_dict['SRC_ENDPOINT']['value'],
        headers=headers,
        use_json=True,
        retries=10, # Retry 10 times, if the request fails for network transport reasons
        verify=env_vars_dict['SRC_TLS_VERIFY']['value']
    )

    # Create the client
    global graphql_client
    graphql_client = Client(
        transport=transport,
        fetch_schema_from_transport=True
    )


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
        }
    }
    """)

    # Run the query, capture the output
    current_user_gql_output = graphql_client.execute(current_user_gql_query)
    # Let the GraphQL client raise any network connectivity, TLS, etc. errors

    # If the current user is not a site admin, exit
    if current_user_gql_output['currentUser']['siteAdmin']:
        log(f"Verified Sourcegraph GraphQL API connection, authentication, and current user is Site Admin: \n{json.dumps(current_user_gql_output, indent=4)}")
    else:
        raise ValueError("Current user is not Site Admin, please use a SRC_ACCESS_TOKEN from a Sourcegraph user account with Site Admin permissions")


def get_rbac_role():
    """
    Get the RBAC role from the Sourcegraph instance by the name provided
    Sourcegraph requires that RBAC role names are unique, so it's safe to lookup the role by name
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

        # Sourcegraph requires that RBAC role names are unique, so we can just grab the first one with a matching name
        for role in rbac_role_id_gql_output['roles']['nodes']:
            if role['name'] == env_vars_dict['SRC_RBAC_ROLE_NAME']['value']:
                global rbac_role
                rbac_role = role
                break

        # Print out the permissions of the role for visual verification
        log(f"\"{rbac_role['name']}\" RBAC role found on Sourcegraph instance:\n{json.dumps(rbac_role, indent=4)}")

    else:
        raise ValueError(f"SRC_RBAC_ROLE_NAME \"{env_vars_dict['SRC_RBAC_ROLE_NAME']['value']}\" not found in RBAC roles from Sourcegraph instance:\n{json.dumps(rbac_role_names, indent=4)}")


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
                emails {
                    email
                    verified
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

    if not all_src_users_and_their_roles_gql_output:
        raise ValueError("GraphQL query returned no users from Sourcegraph instance")

    log(f"Count of users on Sourcegraph instance: {len(all_src_users_and_their_roles_gql_output['users']['nodes'])}")

    return all_src_users_and_their_roles_gql_output


def backup_src_users_and_their_roles_to_file():
    """
    Back up src_users_and_their_roles_at_start to a file, in case something goes sideways and a restore is needed
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
        with open(src_users_backup_file, 'a') as src_users_backup_file_outfile:

            log(f"Appending backup of all users and their roles to file: {src_users_backup_file}")

            # Add note in top level key of all_src_users_and_their_roles_gql_output with today's date and time
            src_users_and_their_roles_at_start['backup_info'] = {
                'backup_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f %z").strip()
            }

            # Write to the file
            json.dump(src_users_and_their_roles_at_start, src_users_backup_file_outfile, indent=4, sort_keys=True)

            # Let json.dump() raise an exception if writing to disk fails, don't want to proceed without a backup

    else:
        log("SRC_USERS_BACKUP_FILE is disabled, skipping backup of all users and their roles to file")


def extract_src_users_with_rbac_role(src_users_and_their_roles):
    """
    Extract the subset of user objects from the Sourcegraph instance with this RBAC role assigned
    """

    newline()
    log("Function: extract_src_users_with_rbac_role")

    # Use the same JSON schema as the GraphQL response
    src_users_with_rbac_role = {
        "users": {
            "nodes": []
        }
    }

    # Make variables easier to read
    rbac_role_name = rbac_role['name']

    # Get the users who have the RBAC role
    for user_object in src_users_and_their_roles['users']['nodes']:
        for role in user_object['roles']['nodes']:
            if role['name'] == rbac_role_name:
                src_users_with_rbac_role['users']['nodes'].append(user_object)

    # Output results
    log(f"Count of users with \"{rbac_role_name}\" RBAC role assigned on Sourcegraph instance: {len(src_users_with_rbac_role['users']['nodes'])}")

    log(f"List of users with \"{rbac_role_name}\" RBAC role assigned on Sourcegraph instance: \n{json.dumps(src_users_with_rbac_role, indent=4)}")

    # Return the list of users
    return src_users_with_rbac_role


def remove_rbac_role_from_users_not_in_list():
    """
    Remove the RBAC role from users who are not in the list
    """

    newline()
    log("Function: remove_rbac_role_from_users_not_in_list")

    # log(f"DEBUG: src_users_with_rbac_role_at_start: {json.dumps(src_users_with_rbac_role_at_start, indent=4)}")

    # Make variables easier to read
    rbac_role_user_objects = src_users_with_rbac_role_at_start['users']['nodes']
    rbac_role_name = rbac_role['name']

    # If no users have this RBAC role, return early
    if not rbac_role_user_objects:
        log(f"No users in the \"{rbac_role_name}\" RBAC role, skipping removal")
        return

    # Iterate through the list of users in the RBAC role
    for user_object in rbac_role_user_objects:

        # log(f"DEBUG: User object:\n{json.dumps(user_object, indent=4)}")

        # Get username and verified emails
        username = user_object['username']
        user_verified_emails = [email['email'] for email in user_object['emails'] if email['verified']]

        # Combine them in a set to deduplicate them, and sort them
        username_and_verified_emails = sorted(                 # Sort the list with sorted()
            list(                                              # Read into list() to sort
                set(                                           # Read into a set() to deduplicate strings
                    [username] + user_verified_emails          # Combine into an iterable
                )
            )
        )

        # log(f"DEBUG: Username_and_verified_emails:\n{json.dumps(username_and_verified_emails, indent=4)}")

        # If neither their username, nor any of their verified emails are in the list
        if not any(username_or_email in list_of_users for username_or_email in username_and_verified_emails):

            log(f"User \"{username}\" started in the \"{rbac_role_name}\" RBAC role, but not in the LDAP group; removing \"{rbac_role_name}\" from their list of RBAC roles")

            # Remove the RBAC role from their list of roles
            user_roles=user_object['roles']['nodes']

            # log(f"User \"{username}\" starting roles: {json.dumps(user_roles, indent=4)}")

            for role in user_roles:
                if role['name'] == rbac_role_name:
                    user_roles.remove(role)

            # log(f"User \"{username}\" ending roles: {json.dumps(user_roles, indent=4)}")

            # log(f"User \"{username}\" sending changes now")

            # Set their roles
            set_user_roles(user_object['id'], [user_role['id'] for user_role in user_roles])

            # log(f"User \"{username}\" changes sent")

        else:
            log(f"User \"{username}\" is in both the LDAP group and the \"{rbac_role_name}\" RBAC role, skipping removal")


def add_rbac_role_to_users_in_ldap_group():
    """
    Add the RBAC role to users who are in the LDAP group
    """

    newline()
    log("Function: add_rbac_role_to_users_in_ldap_group")

    # If no users were sent, skip adding them
    if not list_of_users:
        log("No users in LDAP group, skipping addition")
        return

    # Make variables easier to read
    rbac_role_name = rbac_role['name']

    # Get the list of usernames and emails already in the RBAC role
    rbac_role_user_objects  = [user_object for user_object in src_users_with_rbac_role_at_start['users']['nodes']]
    rbac_role_usernames     = [user_object['username'] for user_object in rbac_role_user_objects]
    rbac_role_user_emails   = [email['email'] for user_object in rbac_role_user_objects for email in user_object['emails'] if email['verified']]
    rbac_role_usernames_and_emails = sorted(list(set(rbac_role_usernames + rbac_role_user_emails))) # Sorted and deduped list

    # log(f"rbac_role_user_objects: \n{json.dumps(rbac_role_user_objects, indent=4)}")
    # log(f"rbac_role_usernames: \n{json.dumps(rbac_role_usernames, indent=4)}")
    # log(f"rbac_role_user_emails: \n{json.dumps(rbac_role_user_emails, indent=4)}")
    # log(f"rbac_role_usernames_and_emails: \n{json.dumps(rbac_role_usernames_and_emails, indent=4)}")

    # Iterate through the list of usernames in the LDAP group
    for username_or_email in list_of_users:

        # If they are not in the RBAC role
        if username_or_email not in rbac_role_usernames_and_emails:

            # Initialize the user_object variable
            user_object = None

            # Find the user's object in the dict of all user objects
            for src_user_object in src_users_and_their_roles_at_start['users']['nodes']:

                # Grab the first user object from the Sourcegraph instance which matches
                # Either the username or any of the verified emails
                if username_or_email in [
                    src_user_object['username'],
                    *[email['email'] for email in src_user_object['emails'] if email['verified']]
                ]:
                    user_object = src_user_object
                    break

            # If we found a user object for this username or email
            if user_object:

                log(f"User \"{username_or_email}\" in LDAP group, but not in \"{rbac_role_name}\" RBAC role; adding now")
                # log(f"User object:\n{json.dumps(user_object, indent=4)}")

                # Get the list of their current role IDs
                user_role_ids_list = [user_role['id'] for user_role in user_object['roles']['nodes']]

                #log(f"User \"{username_or_email}\" starting roles IDs:\n{json.dumps(user_role_ids_list, indent=4)}")

                # Add the RBAC role ID to their list of role IDs
                user_role_ids_list.append(rbac_role['id'])

                #log(f"User \"{username_or_email}\" ending roles IDs:\n{json.dumps(user_role_ids_list, indent=4)}")

                #log(f"User \"{username_or_email}\" sending changes now")

                # Set their roles
                set_user_roles(user_object['id'], user_role_ids_list)

                #log(f"User \"{username_or_email}\" changes sent")

            else:

                log(f"WARNING: User \"{username_or_email}\" does not match an account on this Sourcegraph instance, skipping addition")

        else:
            log(f"User \"{username_or_email}\" already in \"{rbac_role_name}\" RBAC role, skipping addition")


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

    # Pass in the input variable values from the function call parameters
    set_user_roles_gql_variables = {
        "userId": user_id,
        "roleIds": role_ids
    }

    # Run the mutation and capture the output
    set_user_roles_gql_output = graphql_client.execute(
        set_user_roles_gql_mutation,
        variable_values=set_user_roles_gql_variables
    )

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
    get_list_of_users_from_directory()

    # Create the client
    setup_graphql_client()

    # Test the client, verify the current user is a site admin
    test_connection_and_check_current_user_is_site_admin()

    # Get the RBAC role ID and permissions
    get_rbac_role()

    # Get a list of all users and their roles from the Sourcegraph instance
    # Store them for later comparison
    newline()
    log("Getting the starting list of all users and their roles from the Sourcegraph instance")
    global src_users_and_their_roles_at_start
    src_users_and_their_roles_at_start = get_all_src_users_and_their_roles()

    # Backup src_users_and_their_roles_at_start to a file
    backup_src_users_and_their_roles_to_file()

    # Extract the list of usernames who already have the RBAC role
    # Store them for later comparison
    global src_users_with_rbac_role_at_start
    src_users_with_rbac_role_at_start = extract_src_users_with_rbac_role(src_users_and_their_roles_at_start)

    # Sync the list of users in the RBAC role with the list of users in the LDAP group
    remove_rbac_role_from_users_not_in_list()

    # Sync the members of the LDAP group to the RBAC role
    add_rbac_role_to_users_in_ldap_group()

    # Query again to validate the list now matches, and print a success or failure
    # Store them for comparison
    newline()
    log("Getting the ending list of all users and their roles from the Sourcegraph instance for a visual check")
    global src_users_and_their_roles_at_end
    src_users_and_their_roles_at_end = get_all_src_users_and_their_roles()

    global src_users_with_rbac_role_at_end
    src_users_with_rbac_role_at_end = extract_src_users_with_rbac_role(src_users_and_their_roles_at_end)

    newline()
    log("------------------------------------------------------------")
    log("Finishing script")
    log("------------------------------------------------------------")
    newline()


### Script execution
if __name__ == "__main__":
    main()
