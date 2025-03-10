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
2. Configure the environment variables, as shown in the `env_vars_dict` in the script, in either
    1. A `.env` file in the same directory the script runs in
    2. Or as environment variables accessible to the script during runtime
3. Schedule this script to run on a schedule, if needed. It's pretty lightweight, and only takes a few seconds to run, so it can run frequently.

## References

- Your Sourcegraph instance has a fully functional GraphQL API interface available at [https://sourcegraph.example.com/api/console](https://sourcegraph.example.com/api/console)
- See Sourcegraph's GraphQL schema [https://github.com/sourcegraph/artifacts/tree/main/gql](https://github.com/sourcegraph/artifacts/tree/main/gql)
- Sourcegraph's GraphQL API also supports introspection
