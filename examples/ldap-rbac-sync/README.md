# LDAP to RBAC sync

## Experimental
This was created for Sourcegraph Implementation Engineering deployments, and is not intended, designed, built, or supported for use in any other scenario. Feel free to open issues or PRs, but responses are best effort.

## Purpose

This Python script is an example of how customers can use Sourcegraph's GraphQL API to sync RBAC role assignments from an external directory service.

Sourcegraph is building out an internal Role Based Access Control framework, however, we have not yet implemented a way to sync role assignments from external directory services, where Enterprise customers have workflows in place to manage user access and roles. This feature request is in our Engineering team's backlog.

## How to Use

Customers will need to:

1. Either:
    1. Build out the `get_list_of_usernames_from_directory()` function to retrieve a list of usernames from their directory service, as a list of strings
    2. Or provide the list of usernames as a comma-delimited list in the `LIST_OF_USERNAMES` environment variable
2. Configure the `SRC_ENDPOINT`, `SRC_ACCESS_TOKEN`, and `SRC_RBAC_ROLE_NAME` environment variables, as shown below, in either
    1. A `.env` file in the same directory the script runs in
    2. Or as environment variables accessible to the script during runtime
3. Schedule this script to run on a schedule, if needed. It's pretty lightweight, and only takes a few seconds to run, so it can run frequently.

```env
SRC_ENDPOINT=https://sourcegraph.example.com

SRC_ACCESS_TOKEN=sgp_example_site_admin_sudo_token # Sourcegraph access token from a Site Admin user, with site-admin:sudo token scope

SRC_RBAC_ROLE_NAME="Cody Users"

LIST_OF_USERNAMES="user1,user2,user3"

SRC_USERS_BACKUP_FILE='' # Optional, to provide either a path to a file to write the backup file, or set as empty string to disable
```

Note: For safety, this script queries your Sourcegraph instance's GraphQL API for a list of all users and their RBAC role memberships, and writes this to a file at the path in `SRC_USERS_BACKUP_FILE` if provided, or at `./.src_users_backup.json` by default, in the same directory the script is running in. To disable this behaviour, run with configure `SRC_USERS_BACKUP_FILE=''`

## References

- Your Sourcegraph instance has a fully functional GraphQL API interface available at [https://sourcegraph.example.com/api/console](https://sourcegraph.example.com/api/console)
- See Sourcegraph's GraphQL schema [https://github.com/sourcegraph/artifacts/tree/main/gql](https://github.com/sourcegraph/artifacts/tree/main/gql)
- Sourcegraph's GraphQL API also supports introspection
