# Sourcegraph Organization Management Script

## Usage
The required `SRC_ENDPOINT` & `SRC_ACCESS_TOKEN` environment variables must be set similar to the [Sourcegraph CLI](https://github.com/sourcegraph/src-cli?tab=readme-ov-file#configuration-mac-os--linux).

```shell
python organization-management.py --help
usage: 
  organization-management.py <command> [options]

Environment Variables:
  SRC_ENDPOINT: Sourcegraph endpoint to use (e.g., https://example.sourcegraph.com)
  SRC_ACCESS_TOKEN: Sourcegraph access token

Commands:
  list-organizations
  create-organization --csv <file>
  add-user-to-organization --organization <organization id> --user <username> --csv <file>

Sourcegraph organization management CLI

options:
  -h, --help  Show this help message and exit
```

## List organizations
Lists all organizations in the Sourcegraph instance using the [organizations](https://sourcegraph.com/docs/api/graphql/api-docs#query-organizations) graphQL query.

## Create organizations
Allows for the automated creation of Sourcegraph organizations defined in an input csv file using the [createOrganization](https://sourcegraph.com/docs/api/graphql/api-docs#mutation-createOrganization) graphQL mutation. The input CSV file should include the name and display name for each organization to be created. Example:
```
name,displayName
org-1,org-1-display-name
org-2,org-2-display-name
org-3,org-3-display-name
...
```

## Add users to organizations
Assigns users to organizations via the [AddUserToOrganization](https://sourcegraph.com/docs/api/graphql/api-docs#mutation-addUserToOrganization) graphQL mutation. The input CSV file should include the name of the organization and the user that should be added to that organization. Example:
```
organization,user
org-1,user1
org-1,user2
org-2,user1
...
```
