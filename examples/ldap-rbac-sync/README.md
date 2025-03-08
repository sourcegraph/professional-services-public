# LDAP to RBAC sync

Sourcegraph is building out an internal Role Based Access Control framework, however, we have not yet implemented a way to sync role assignments from external directory services, where Enterprise customers have workflows in place to manage user access and roles.

This Python script is an example of how customers can use Sourcegraph's GraphQL API to sync role assignments from an
external directory service.

# How to Use

Customers will need to:

1. Build out the `get_list_of_usernames_from_directory()` function to retrieve a list of usernames from their directory service, as a list of strings.
2. Schedule this script to run on a schedule, if needed. It's pretty lightweight, and only takes a few seconds to run, so it can run frequently.
3. Configure the `SRC_ENDPOINT`, `SRC_ACCESS_TOKEN`, and `SRC_RBAC_ROLE_NAME` environment variables, as shown below, in either a `.env` file in the same path as this script runs from, or as OS environment variables.

```env
SRC_ENDPOINT=https://sourcegraph.example.com
SRC_ACCESS_TOKEN=sgp_example
SRC_RBAC_ROLE_NAME="Cody Users"
LIST_OF_USERNAMES="user1,user2,user3"
```
