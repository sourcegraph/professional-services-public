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
- Rework user ID / email to only match on email, because creating users requires username and email
- Provide an env var to create Sourcegraph users if they don't already exist
    - See GraphQL mutation here: https://sourcegraph.slack.com/archives/C05EMJM2SLR/p1741044637157049?thread_ts=1741044624.870229&cid=C05EMJM2SLR
- Match users more atomically
    - If the LDAP attribute to match is mail
    - Users can have multiple email addresses, but are still one identity
    - For each LDAP user which is is a member of the group
        - Add one object to a dict, including all of their email addresses
    - Marshall / match these user objects against the user objects from Sourcegraph
        - Such that if one LDAP user has 2 email addresses,
        - they cannot be matched to 2 different Sourcegraph users
- src_extract_users_with_rbac_role: List of users with \"{rbac_role_name}\" RBAC role assigned on Sourcegraph instance
    - Filter attributes to print to console
        - username
        - email
        - id
- Implement standard logging library and log levels
- Implement before and after comparison, to verify that:
    - The needed changes were made
    - No other changes were made
    - Alert the user if:
        - src_all_users_and_their_roles_at_end does not 1:1 match the list of usernames
        - Other diffs appear in the src_all_users_and_their_roles_at_start vs src_all_users_and_their_roles_at_end which are not anticipated
            - diff_src_user_and_roles(src_all_users_and_their_roles_at_start, src_all_users_and_their_roles_at_end)
- Implement syncing multiple LDAP groups to RBAC roles
    - MAP_OF_LDAP_GROUPS_TO_RBAC_ROLES = {
        "ldap_group_name_1": "rbac_role_name_1",
        "ldap_group_name_2": "rbac_role_name_2",
        ...
    }
"""


### Imports
from datetime import datetime
from dotenv import dotenv_values # https://pypi.org/project/python-dotenv/
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
import json
import ldap # https://www.python-ldap.org/en/python-ldap-3.3.0/reference/index.html
import os


### Global variables and their default values
env_vars_dict = {
    "ADD_ONLY_SKIP_REMOVE" : {
        "description": "Only add users to RBAC role, do not remove users from RBAC role",
        "validation_requirements": "True or False",
        "required": False,
        "value": False
    },
    "LDAP_BIND_DN" : {
        "description": "LDAP service account to login (bind) to LDAP server",
        "validation_requirements": "Valid LDAP account, in DN format, ex. cn=admin,dc=example,dc=org",
        "required": False,
        "value": None
    },
    "LDAP_BIND_PASSWORD" : {
        "description": "LDAP service account password",
        "validation_requirements": "",
        "required": False,
        "value": None
    },
    "LDAP_GROUP_DN" : {
        "description": "LDAP group which contains users to sync with RBAC role",
        "validation_requirements": "Valid LDAP group, in DN format, ex. cn=sourcegraph-cody-users,ou=groups,dc=example,dc=org",
        "required": False,
        "value": None
    },
    "LDAP_GROUP_MEMBER_ATTRIBUTE" : {
        "description": "Attribute of the LDAP group which contains the list of members' DNs",
        "validation_requirements": "Valid LDAP group attribute, ex. member",
        "required": False,
        "value": "member"
    },
    "LDAP_TRACE_LEVEL" : {
        "description": "Set the LDAP trace level; 0 for no logging, 1 for only logging the method calls with arguments, 2 for also logging the complete results, and 9 for also logging the traceback of method calls",
        "validation_requirements": "0, 1, 2, or 9",
        "required": False,
        "value": 0
    },
    "LDAP_URL" : {
        "description": "URL to your LDAP directory service",
        "validation_requirements": "Must include URL schema, host, and port, ex. ldap://localhost:1389",
        "required": False,
        "value": None
    },
    "LDAP_USER_ID_ATTRIBUTE" : {
        "description": "Attribute of the LDAP user which contains the user's ID to be matched to a Sourcegraph username or verified email address",
        "validation_requirements": "Valid LDAP user attribute, ex. mail",
        "required": False,
        "value": "mail"
    },
    "LIST_OF_USERS" : {
        "description": "In addition to, or instead of an LDAP query, provide a list of usernames and/or email addresses to sync with the RBAC role, separated by commas",
        "validation_requirements": "Must match username or verified email address of users on your Sourcegraph instance",
        "required": False,
        "value": None
    },
    "REMOVE_ALL_USERS_FROM_RBAC_ROLE" : {
        "description": "Safeguard against removing all users from the RBAC role, in case an empty list is provided. If intentionally removing all users from the RBAC role, then set to True",
        "validation_requirements": "True or False",
        "required": False,
        "value": False
    },
    "SRC_ACCESS_TOKEN" : {
        "description": "Sourcegraph access token from https://sourcegraph.example.com/user/settings/tokens",
        "validation_requirements": "user:all token scope is sufficient, site-admin:sudo token scope is not required",
        "required": True,
        "value": None
    },
    "SRC_ENDPOINT" : {
        "description": "The URL of your Sourcegraph instance, e.g. https://sourcegraph.example.com",
        "validation_requirements": "Must begin with either http:// or https://",
        "required": True,
        "value": None
    },
    "SRC_RBAC_ROLE_NAME" : {
        "description": "Display name of RBAC role in your Sourcegraph instance to sync users to, ex. 'Cody Users'",
        "validation_requirements": "Must match the name of an existing RBAC role in your Sourcegraph instance",
        "required": True,
        "value": None
    },
    "SRC_TLS_VERIFY" : {
        "description": "Control if / how to verify your Sourcegraph instance's TLS certificate",
        "validation_requirements": [
            "True: Verify your Sourcegraph instance's TLS certificate with the host OS' trust store",
            "False: Disable verification of your Sourcegraph instance's TLS certificate",
            "string: must be a path to a CA bundle to verify your Sourcegraph instance's TLS certificate against"
        ],
        "required": False,
        "value": True
    },
    "SRC_USERS_BACKUP_FILE" : {
        "description": "Path to the backup file of all users and their roles from the Sourcegraph instance, for safety, in case roles are inadvertently removed from users. ",
        "validation_requirements": [
            "Leave undeclared to use the default path",
            "Provide a path to the backup destination",
            "Declare as an empty string to disable the backup",
        ],
        "required": False,
        "value": ".src_users_backup.json"
    },
}

count_of_users_added_to_rbac_role = 0
count_of_users_already_in_the_rbac_role = 0
count_of_users_created = 0
count_of_users_failed_to_create = 0
count_of_users_removed_from_rbac_role = 0
ldap_client : ldap.ldapobject = None
ldap_error = False
ldap_users_to_sync = []
list_of_users_to_sync = []
src_all_users_and_their_roles_at_end = {}
src_all_users_and_their_roles_at_start = {}
src_graphql_client : Client = None
src_rbac_role = {}
src_users_with_rbac_role_at_end = {"users": {"nodes": []}}
src_users_with_rbac_role_at_start = {"users": {"nodes": []}}
user_objects_to_sync = {"users": {"nodes": []}}


### Functions

def read_env_vars():
    """
    Read configuration from .env file and/or OS environment variables
    If the same env var is declared in both places, use the value from the .env file
    """

    newline()
    log("Function: read_env_vars")

    dot_env_file_path = ".env"

    # Check the existing env vars to see if a .env file path has been provided
    if os.getenv("SRC_DOT_ENV_PATH"):
        dot_env_file_path = os.getenv("SRC_DOT_ENV_PATH")
        log(f"Found env: SRC_DOT_ENV_PATH={dot_env_file_path}")

    # Try reading environment variables from .env file
    dot_env_file_content = dotenv_values(dot_env_file_path)
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

            # Don't print credentials
            if env_var in [
                "SRC_ACCESS_TOKEN",
                "LDAP_BIND_PASSWORD"
            ]:
                log(f"Found env: {env_var}=***********")

            else:
                log(f"Found env: {env_var}={env_vars_dict[env_var]['value']}")

    # If any required environment variables are missing, raise an error and exit
    if missing_required_env_var:
        raise ValueError("One or more required environment variables are missing, please configure them in the .env file, or export them into environment variables")

    # # If a LDAP_SEARCH_ATTRIBUTES_LIST value is passed in, split it into an array of strings
    # if env_vars_dict['LDAP_SEARCH_ATTRIBUTES_LIST']['value']:
    #     env_vars_dict['LDAP_SEARCH_ATTRIBUTES_LIST']['value'] = env_vars_dict['LDAP_SEARCH_ATTRIBUTES_LIST']['value'].split(",")

    # Validate LDAP_TRACE_LEVEL is either 0, 1, 2, or 9
    if int(env_vars_dict['LDAP_TRACE_LEVEL']['value']) in (0, 1, 2, 9):
        # If yes, convert to int
        env_vars_dict['LDAP_TRACE_LEVEL']['value'] = int(env_vars_dict['LDAP_TRACE_LEVEL']['value'])
    else:
        raise ValueError("LDAP_TRACE_LEVEL must be one of: 0, 1, 2, or 9")

    # if LDAP_URL in env vars
    # verify it starts with ldap:// or ldaps://
    # verify it ends with :port

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


def src_setup_graphql_client():
    """
    Create a GraphQL client with the given endpoint and authentication
    """

    newline()
    log("Function: src_setup_graphql_client")

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
    global src_graphql_client
    src_graphql_client = Client(
        transport=transport,
        fetch_schema_from_transport=True
    )


def src_test_graphql_connection_and_check_current_user_is_site_admin():
    """
    Check that the current user is a site admin, and the connection to the GraphQL API is working
    """

    newline()
    log("Function: src_test_graphql_connection_and_check_current_user_is_site_admin")

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
    current_user_gql_output = src_graphql_client.execute(current_user_gql_query)
    # Let the GraphQL client raise any network connectivity, TLS, etc. errors

    # If the current user is not a site admin, exit
    if current_user_gql_output['currentUser']['siteAdmin']:
        log(f"Verified Sourcegraph GraphQL API connection, authentication, and current user is Site Admin: \n{json.dumps(current_user_gql_output, indent=4)}")
    else:
        raise ValueError("Current user is not Site Admin, please use a SRC_ACCESS_TOKEN from a Sourcegraph user account with Site Admin permissions")


def src_get_rbac_role():
    """
    Get the RBAC role from the Sourcegraph instance by the name provided
    Sourcegraph requires that RBAC role names are unique, so it's safe to lookup the role by name
    """

    newline()
    log("Function: src_get_rbac_role")

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
    rbac_role_id_gql_output = src_graphql_client.execute(rbac_role_id_gql_query)

    # Get the Role names from the output
    rbac_role_names = [role['name'] for role in rbac_role_id_gql_output['roles']['nodes']]

    # If the configured role name is in the list, grab it
    if env_vars_dict['SRC_RBAC_ROLE_NAME']['value'] in rbac_role_names:

        # Sourcegraph requires that RBAC role names are unique, so we can just grab the first one with a matching name
        for role in rbac_role_id_gql_output['roles']['nodes']:
            if role['name'] == env_vars_dict['SRC_RBAC_ROLE_NAME']['value']:
                global src_rbac_role
                src_rbac_role = role
                break

        # Print out the permissions of the role for visual verification
        log(f"\"{src_rbac_role['name']}\" RBAC role found on Sourcegraph instance:\n{json.dumps(src_rbac_role, indent=4)}")

    else:
        raise ValueError(f"SRC_RBAC_ROLE_NAME \"{env_vars_dict['SRC_RBAC_ROLE_NAME']['value']}\" not found in RBAC roles from Sourcegraph instance:\n{json.dumps(rbac_role_names, indent=4)}")


def src_get_all_users_and_their_roles():
    """
    Get the list of all users and their roles from the Sourcegraph instance
    """

    newline()
    log("Function: src_get_all_users_and_their_roles")

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
    all_src_users_and_their_roles_gql_output = src_graphql_client.execute(all_src_users_and_their_roles_gql_query)

    if not all_src_users_and_their_roles_gql_output:
        raise ValueError("GraphQL query returned no users from Sourcegraph instance")

    log(f"Count of users on Sourcegraph instance: {len(all_src_users_and_their_roles_gql_output['users']['nodes'])}")

    return all_src_users_and_their_roles_gql_output


def src_backup_all_users_and_their_roles_to_file():
    """
    Back up src_all_users_and_their_roles_at_start to a file, in case something goes sideways and a restore is needed
    """

    newline()
    log("Function: src_backup_all_users_and_their_roles_to_file")


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
            src_all_users_and_their_roles_at_start['backup_info'] = {
                'backup_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f %z").strip()
            }

            # Write to the file
            json.dump(src_all_users_and_their_roles_at_start, src_users_backup_file_outfile, indent=4, sort_keys=True)

            # Let json.dump() raise an exception if writing to disk fails, don't want to proceed without a backup

    else:
        log("SRC_USERS_BACKUP_FILE is disabled, skipping backup of all users and their roles to file")


def src_extract_users_with_rbac_role(src_users_and_their_roles):
    """
    Extract the subset of user objects from the Sourcegraph instance with this RBAC role assigned
    """

    newline()
    log("Function: src_extract_users_with_rbac_role")

    # Use the same JSON schema as the GraphQL response
    src_users_with_rbac_role = {
        "users": {
            "nodes": []
        }
    }

    # Make variables easier to read
    rbac_role_name = src_rbac_role['name']

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


def ldap_setup_and_test_client():
    """
    Create an LDAP client with the given URL
    """

    newline()
    log("Function: ldap_setup_and_test_client")

    # Global variables to be modified
    global ldap_client
    global ldap_error

    # If LDAP_URL is not set, skip LDAP client setup
    if not env_vars_dict['LDAP_URL']['value']:
        log("LDAP_URL is not set, skipping LDAP client setup")
        return

    try:

        # Create the client
        ldap_client = ldap.initialize(
            uri=env_vars_dict['LDAP_URL']['value'],
            trace_level=env_vars_dict['LDAP_TRACE_LEVEL']['value']
        )

        ldap_client.simple_bind_s(
            who=env_vars_dict['LDAP_BIND_DN']['value'],
            cred=env_vars_dict['LDAP_BIND_PASSWORD']['value']
        )

    except Exception as e:
        log(f"Error creating LDAP client, setting ADD_ONLY_SKIP_REMOVE to True to avoid removing the role from users due to LDAP server connectivity issues. Exception: {e}")
        env_vars_dict['ADD_ONLY_SKIP_REMOVE']['value'] = True
        ldap_client = None
        ldap_error = True
        return

def ldap_get_user_ids():
    """
    Get the list of user IDs from the LDAP group's members
    """

    newline()
    log("Function: ldap_get_user_ids")

    # Modifying the global variable
    global ldap_users_to_sync

    # If LDAP client isn't initiated, skip trying to query it
    if not ldap_client:
        log("LDAP client not initialized, skipping LDAP query")
        return

    try:

        # Get the list of members from the LDAP group
        ldap_group_members_list_of_tuples = ldap_client.search_s(
            base=env_vars_dict['LDAP_GROUP_DN']['value'],
            scope=ldap.SCOPE_BASE,
            attrlist=[env_vars_dict['LDAP_GROUP_MEMBER_ATTRIBUTE']['value']]
        )

        # [
        #     (
        #         'cn=sourcegraph-cody-users,ou=groups,dc=example,dc=org',
        #         {
        #             'member': [
        #                 b'cn=user1,ou=users,dc=example,dc=org',
        #                 b'cn=user2,ou=users,dc=example,dc=org'
        #             ]
        #         }
        #     )
        # ]

    except Exception as e:
        log(f"Error querying LDAP group members: {e}")
        ldap_error = True
        return

    # If the length of ldap_group_members_list_of_tuples is not equal to 1 then the provided group DN is not valid
    if len(ldap_group_members_list_of_tuples) != 1:
        raise ValueError(f"LDAP group search returned {len(ldap_group_members_list_of_tuples)} groups, expected 1")

    # Get the list of members from the LDAP group, as a list of strings instead of byte_strings
    ldap_group_members_dn_list = [
        member.decode('utf-8')
        for member in ldap_group_members_list_of_tuples[0][1][env_vars_dict['LDAP_GROUP_MEMBER_ATTRIBUTE']['value']]
    ]

    # Output the results
    log(f"LDAP group members' DNs:\n{json.dumps(ldap_group_members_dn_list, indent=4)}")


    # For each group member DN, query the LDAP server to get their LDAP_USER_ID_ATTRIBUTE
    # and add it to the list of users to sync
    for group_member_dn in ldap_group_members_dn_list:

        # Query the LDAP server to get their LDAP_USER_ID_ATTRIBUTE
        # and add it to the list of users to sync
        ldap_user_object = ldap_client.search_s(
            base=group_member_dn,
            scope=ldap.SCOPE_BASE,
            attrlist=[env_vars_dict['LDAP_USER_ID_ATTRIBUTE']['value']]
        )

        # [
        #     (
        #         'cn=user1,ou=users,dc=example,dc=org',
        #         {
        #             'mail':
        #             [
        #                 b'user1@example.com'
        #             ]
        #         }
        #     )
        # ]

        ldap_user_id_list = [
            id_attribute_instance.decode('utf-8')
            for id_attribute_instance in ldap_user_object[0][1][env_vars_dict['LDAP_USER_ID_ATTRIBUTE']['value']]
        ]

        log(f"LDAP user IDs from DN \"{group_member_dn}\":\n{json.dumps(ldap_user_id_list, indent=4)}")

        # It's fine if the user has multiple IDs, just add them all for now, and we'll deduplicate them at marshalling time
        if ldap_user_id_list:
            ldap_users_to_sync += ldap_user_id_list


def combine_and_dedupe_list_of_users_to_sync():
    """
    Combine list of users from the env var and the LDAP query, dedupe and sort them
    """

    newline()
    log("Function: combine_and_dedupe_list_of_users_to_sync")

    # Global required when modifying global variable in function
    global list_of_users_to_sync

    if ldap_users_to_sync:

        list_of_users_to_sync += ldap_users_to_sync

    if env_vars_dict['LIST_OF_USERS']['value']:

        # Read the comma-delimited list of usernames / email addresses from the env var
        # Split string to list, on commas, and remove whitespace
        list_of_users_to_sync += env_vars_dict['LIST_OF_USERS']['value'].split(',')

    if list_of_users_to_sync:

        list_of_users_to_sync = sorted(                                 # Sort the list
            list(                                                       # Read into list() to sort
                set(                                                    # Read into a set() to deduplicate strings
                    [user.strip() for user in list_of_users_to_sync]    # Strip starting / trailing whitespace from each string
                )
            )
        )

        # Remove empty strings, ex. repeated or trailing commas
        list_of_users_to_sync[:] = [string for string in list_of_users_to_sync if string.strip()]

        log(f"List of usernames and/or email addresses to sync with RBAC role (sorted and deduplicated):\n{json.dumps(list_of_users_to_sync, indent=4)}")

    else:

        if env_vars_dict['REMOVE_ALL_USERS_FROM_RBAC_ROLE']['value']:

            log("WARNING: List of users is empty, and REMOVE_ALL_USERS_FROM_RBAC_ROLE is True, this will remove all users from RBAC role")

        else:
            raise ValueError("List of users is empty, and REMOVE_ALL_USERS_FROM_RBAC_ROLE is False, please provide a list of usernames or email addresses from the customer's directory service to sync with the RBAC role, or set REMOVE_ALL_USERS_FROM_RBAC_ROLE to True to remove all users from the RBAC role")


def marshall_list_of_users_to_sync_to_src_user_objects():
    """
    Match the list of usernames and email addresses to the list of user objects from the Sourcegraph instance
    """

    newline()
    log("Function: marshall_list_of_users_to_sync_to_src_user_objects")

    # Global required when modifying global variable in function
    global user_objects_to_sync

    # Dedupe the list of usernames and email addresses
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


def src_remove_rbac_role_from_users_not_in_list():
    """
    Remove the RBAC role from users who are not in the list
    """

    newline()
    log("Function: src_remove_rbac_role_from_users_not_in_list")

    if env_vars_dict['ADD_ONLY_SKIP_REMOVE']['value']:
        log("ADD_ONLY_SKIP_REMOVE is True, skipping removal of RBAC role from users not in list")
        return

    # Global required when modifying global variable in function
    global count_of_users_removed_from_rbac_role

    # log(f"DEBUG: src_users_with_rbac_role_at_start: {json.dumps(src_users_with_rbac_role_at_start, indent=4)}")

    # Make variables easier to read
    rbac_role_user_objects = src_users_with_rbac_role_at_start['users']['nodes']
    rbac_role_name = src_rbac_role['name']

    # If no users have this RBAC role, return early
    if rbac_role_user_objects:
        log(f"Count of users in the \"{rbac_role_name}\" RBAC role before removing users: {len(rbac_role_user_objects)}")
    else:
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
        if not any(username_or_email in list_of_users_to_sync for username_or_email in username_and_verified_emails):

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
            src_set_user_roles(user_object['id'], [user_role['id'] for user_role in user_roles])
            count_of_users_removed_from_rbac_role += 1

            # log(f"User \"{username}\" changes sent")

        else:
            log(f"User \"{username}\" is in both the LDAP group and the \"{rbac_role_name}\" RBAC role, skipping removal")


def src_add_rbac_role_to_users_in_list():
    """
    Add the RBAC role to users who are in the list
    """

    newline()
    log("Function: src_add_rbac_role_to_users_in_list")

    # If no users were sent, skip adding them
    if list_of_users_to_sync:
        log(f"Count of user IDs in the list to try and add: {len(list_of_users_to_sync)}")
    else:
        log("No users in the list to add, skipping addition")
        return

    # Global required when modifying global variable in function
    global count_of_users_added_to_rbac_role
    global count_of_users_already_in_the_rbac_role
    global count_of_users_failed_to_create
    global count_of_users_created

    # Make variables easier to read
    rbac_role_name = src_rbac_role['name']

    # Get the list of usernames and emails already in the RBAC role
    rbac_role_user_objects  = [user_object for user_object in src_users_with_rbac_role_at_start['users']['nodes']]
    rbac_role_usernames     = [user_object['username'] for user_object in rbac_role_user_objects]
    rbac_role_user_emails   = [email['email'] for user_object in rbac_role_user_objects for email in user_object['emails'] if email['verified']]
    rbac_role_usernames_and_emails = sorted(list(set(rbac_role_usernames + rbac_role_user_emails))) # Sorted and deduped list

    # log(f"rbac_role_user_objects: \n{json.dumps(rbac_role_user_objects, indent=4)}")
    # log(f"rbac_role_usernames: \n{json.dumps(rbac_role_usernames, indent=4)}")
    # log(f"rbac_role_user_emails: \n{json.dumps(rbac_role_user_emails, indent=4)}")
    # log(f"rbac_role_usernames_and_emails: \n{json.dumps(rbac_role_usernames_and_emails, indent=4)}")

    # Iterate through the list of usernames in the list
    for username_or_email in list_of_users_to_sync:

        # If they are already in the RBAC role, then count and skip
        if username_or_email in rbac_role_usernames_and_emails:

            count_of_users_already_in_the_rbac_role += 1
            log(f"User \"{username_or_email}\" already in \"{rbac_role_name}\" RBAC role, skipping addition")
            continue

        # Initialize the user_object variable
        user_object = None

        # Find the user's object in the dict of all user objects
        for src_user_object in src_all_users_and_their_roles_at_start['users']['nodes']:

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

            log(f"User \"{username_or_email}\" in list of users to sync, but not already in \"{rbac_role_name}\" RBAC role; adding now")
            # log(f"User object:\n{json.dumps(user_object, indent=4)}")

            # Get the list of their current role IDs
            user_role_ids_list = [user_role['id'] for user_role in user_object['roles']['nodes']]

            #log(f"User \"{username_or_email}\" starting roles IDs:\n{json.dumps(user_role_ids_list, indent=4)}")

            # Add the RBAC role ID to their list of role IDs
            user_role_ids_list.append(src_rbac_role['id'])

            #log(f"User \"{username_or_email}\" ending roles IDs:\n{json.dumps(user_role_ids_list, indent=4)}")

            #log(f"User \"{username_or_email}\" sending changes now")

            # Set their roles
            user_role_set = src_set_user_roles(user_object['id'], user_role_ids_list)

            if user_role_set:
                count_of_users_added_to_rbac_role += 1

            #log(f"User \"{username_or_email}\" changes sent")

        else:

            log(f"User \"{username_or_email}\" does not match an account on this Sourcegraph instance, creating user")

            # Create the user using GraphQL API
            created_user = src_create_user(username_or_email)

            if created_user:

                log(f"Created user \"{username_or_email}\" successfully, adding to role")

                # Add the RBAC role ID to the newly created user
                src_set_user_roles(created_user['id'], [src_rbac_role['id']])
                count_of_users_created += 1
                count_of_users_added_to_rbac_role += 1

            else:

                count_of_users_failed_to_create += 1
                log(f"Failed to create user \"{username_or_email}\", skipping addition")


def src_create_user(username_or_email):
    """
    Create a new user via GraphQL API
    Returns the created user object on success, None on failure
    """

    newline()
    log("Function: src_create_user")

    # Determine if the input is an email or username
    is_email = '@' in username_or_email

    # Write the mutation
    create_user_gql_mutation = gql("""
    mutation createUser($username: String!, $email: String) {
        createUser(username: $username, email: $email) {
            user {
                id
                username
                emails {
                    email
                    verified
                }
            }
        }
    }
    """)

    # Set up variables based on whether we have an email or username
    if is_email:
        create_user_gql_variables = {
            "username": username_or_email.split('@')[0],  # Use part before @ as username
            "email": username_or_email
        }
    else:
        create_user_gql_variables = {
            "username": username_or_email,
            "email": None  # No email provided
        }

    try:
        # Run the mutation and capture the output
        create_user_gql_output = src_graphql_client.execute(
            create_user_gql_mutation,
            variable_values=create_user_gql_variables
        )

        log(f"Create user GraphQL mutation response: {json.dumps(create_user_gql_output, indent=4)}")

        return create_user_gql_output['createUser']['user']

    except Exception as e:
        log(f"Error creating user: {str(e)}")
        return None


def src_set_user_roles(user_id, role_ids):
    """
    Set the RBAC roles of a user
    Must include all roles, the list of roles sent overwrite all of the user's roles
    """

    newline()
    log("Function: src_set_user_roles")

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
    set_user_roles_gql_output = src_graphql_client.execute(
        set_user_roles_gql_mutation,
        variable_values=set_user_roles_gql_variables
    )

    if 'errors' in set_user_roles_gql_output:
        # If output contains an error
        log(f"ERROR: Failed to set user roles: {json.dumps(set_user_roles_gql_output, indent=4)}")
        return False

    else:
        # If successful, then return true
        log(f"Set user roles GraphQL mutation response: {json.dumps(set_user_roles_gql_output, indent=4)}")
        return True


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


    ## Setup

    # Get configuration from environment variables
    read_env_vars()

    ## Sourcegraph

    # Create the client
    src_setup_graphql_client()

    # Test the client, verify the current user is a site admin
    src_test_graphql_connection_and_check_current_user_is_site_admin()

    # Get the RBAC role ID and permissions
    src_get_rbac_role()

    # Get a list of all users and their roles from the Sourcegraph instance
    # Store them for later comparison
    newline()
    log("Getting the starting list of all users and their roles from the Sourcegraph instance")
    global src_all_users_and_their_roles_at_start
    src_all_users_and_their_roles_at_start = src_get_all_users_and_their_roles()

    # Backup src_all_users_and_their_roles_at_start to a file
    src_backup_all_users_and_their_roles_to_file()

    # Extract the list of usernames who already have the RBAC role
    # Store them for later comparison
    global src_users_with_rbac_role_at_start
    src_users_with_rbac_role_at_start = src_extract_users_with_rbac_role(src_all_users_and_their_roles_at_start)

    ## LDAP

    ldap_setup_and_test_client()
    ldap_get_user_ids()

    # Get the list of usernames from the directory service
    combine_and_dedupe_list_of_users_to_sync()
    # marshall_list_of_users_to_sync_to_src_user_objects()

    # Sync the list of users in the RBAC role with the list of users in the LDAP group
    src_remove_rbac_role_from_users_not_in_list()

    # Sync the members of the LDAP group to the RBAC role
    src_add_rbac_role_to_users_in_list()

    # Query again to validate the list now matches, and print a success or failure
    # Store them for comparison
    newline()
    log("Getting the ending list of all users and their roles from the Sourcegraph instance for a visual check")
    global src_all_users_and_their_roles_at_end
    src_all_users_and_their_roles_at_end = src_get_all_users_and_their_roles()

    global src_users_with_rbac_role_at_end
    src_users_with_rbac_role_at_end = src_extract_users_with_rbac_role(src_all_users_and_their_roles_at_end)

    # Make variables easier to read
    rbac_role_name = src_rbac_role['name']

    newline()
    log("------------------------------------------------------------")
    log("Finishing script")
    log(f"Count of users with the \"{rbac_role_name}\" RBAC role at the start: {len(src_users_with_rbac_role_at_start['users']['nodes'])}")
    log(f"Count of unique user IDs to try to sync to the \"{rbac_role_name}\" RBAC role: {len(list_of_users_to_sync)}")
    if ldap_error:
        log("ERROR: Failed to query LDAP group members, skipped removing users from the role")
    log(f"Count of users removed from the \"{rbac_role_name}\" RBAC role: {count_of_users_removed_from_rbac_role}")
    log(f"Count of users added to the \"{rbac_role_name}\" RBAC role: {count_of_users_added_to_rbac_role}")
    log(f"Count of user IDs which matched user accounts already in the \"{rbac_role_name}\" RBAC role (may include many-to-one): {count_of_users_already_in_the_rbac_role}")
    log(f"Count of users created on the Sourcegraph instance: {count_of_users_created}")
    log(f"Count of user IDs which failed to be created on the Sourcegraph instance: {count_of_users_failed_to_create}")
    log(f"Count of users with the \"{rbac_role_name}\" RBAC role at the end: {len(src_users_with_rbac_role_at_end['users']['nodes'])}")
    log("------------------------------------------------------------")
    newline()


### Script execution
if __name__ == "__main__":
    main()
