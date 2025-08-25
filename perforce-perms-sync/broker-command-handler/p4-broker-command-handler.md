# Objective

Sourcegraph has optimized our Perforce API client to require the lowest permissions possible, but still requires super access to sync permissions for other users using the `p4 protects -u <username>` command. [Other commands](https://docs.sourcegraph.com/admin/repo/perforce#depot-syncing) do not require super privileges.

It's general best practice to avoid providing service accounts with admin permissions wherever possible, so we've collaborated with our Perforce customers to create and refine the p4 broker command handler approach below. This approach involves configuring a command handler on the p4 broker server upstream from the Sourcegraph instance, so that only the `p4 protects -u <username>` command will be executed by another service account which does have the needed super permissions; all other p4 commands from the Sourcegraph instance will be executed with the instance's configured Perforce account, with its regular permissions.

This document outlines the steps required to set up this command handler.

# Perforce Documentation

https://www.perforce.com/manuals/p4sag/Content/P4SAG/broker-command-handler-specs.html

https://portal.perforce.com/s/article/10836

https://portal.perforce.com/s/article/11309

# Implementation

## 1. Create command handler script

The command handler script receives the command and its arguments and re-runs them as a privileged user. Here's an example written in Perl:

```perl
#!/usr/bin/env perl
# sourcegraph_cmd_handler.pl

use strict;
use warnings;

my $P4USER = 'USER_WITH_SUPER_PRIVILEGE';
my $P4PORT = 'P4PORT';
my %cmd_info = map { /(.*?)\s*:\s*(.*)/ ; ($1, $2) } <STDIN> ;

my $cmd = "p4 -p $P4PORT -u $P4USER ".$cmd_info{'command'};

for (my $i = 0; $i < $cmd_info{'argCount'}; $i++) {
    my $arg = 'Arg'."$i";
    $cmd .= " $cmd_info{$arg}";
}
chomp(my $output = `$cmd 2>&1`);
print "action: RESPOND\n";
print "message: \"$output\"";
```

## 2. Add command handler to Perforce Broker configuration

Add the command handler for the `p4 protects -u <username>` command:

```
command: protects
{
  user = svc_sourcegraph;
  flags = -u;
  action = filter;
  execute = /perforce/brokerroot/sourcegraph_cmd_handler.pl;
}
```

## Test

Before this change is in place, you should not be able to run any of the privileged commands.

`$ p4 -p [p4 port] -u svc_sourcegraph protects -u other_user //depot/...`

```
You don't have permission for this operation.
```

After this change is applied, these commands should work:

`$ p4 -p [p4 port] -u svc_sourcegraph protects -u other_user //depot/...`

```
list group everyone * -//...
list group everyone * -//...
write group * * //depot/...
write group * * //depot/...
```

However, other super commands should not permitted, as expected:

`$ p4 -p [p4 port] -u svc_sourcegraph protect -o`

```
You don't have permission for this operation.
```

Source team's Notion doc [here](https://www.notion.so/sourcegraph/Perforce-broker-setup-48b2ad6c4f3f44ba8f73d1a0b93bd789)
