# LDAP to RBAC sync

## Experimental

This was created for Sourcegraph Implementation Engineering deployments, and is not intended, designed, built, or supported for use in any other scenario. Please open PRs with improvements, or issues with feedback; responses are best effort.

## Purpose

This Python script is an example of how customers can use Sourcegraph's GraphQL API to sync RBAC role assignments from an external directory service.

Sourcegraph is building out an internal Role Based Access Control framework, however, we have not yet implemented a way to sync role assignments from external directory services, where Enterprise customers have workflows in place to manage user access and roles. This feature request is in our Engineering team's backlog.

## How to Use

1. Install the needed Python modules as listed in the `requirements.txt` file, via `pip install -r requirements.txt --upgrade` or similar
2. Configure the environment variables, as shown in the `env_vars_dict` in the script, in either
    1. A `.env` file in the same directory the script runs in
    2. Or as environment variables accessible to the script during runtime
3. Schedule this script to run on an interval, if needed. It's pretty lightweight, and only takes a few seconds to run, so it can run frequently.

## Notes

- User accounts must exist in Sourcegraph before they can be assigned RBAC roles. This script outputs a warning and continues execution if it finds a username in the input list which doesn't have a Sourcegraph account. This script will need to be run between user account creation and the user expecting to use the assigned role.

## References

- Your Sourcegraph instance has a fully functional GraphQL API interface available at [https://sourcegraph.example.com/api/console](https://sourcegraph.example.com/api/console)
- See Sourcegraph's GraphQL schema [https://github.com/sourcegraph/artifacts/tree/main/gql](https://github.com/sourcegraph/artifacts/tree/main/gql)
- Sourcegraph's GraphQL API also supports introspection
