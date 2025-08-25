# Objective

Sourcegraph has optimized our Perforce API client to require the least privileges possible, requiring `super` privileges only to sync permissions for other users using the `p4 protects -u <username> //depot/path/...` command. [Other commands](https://docs.sourcegraph.com/admin/repo/perforce#depot-syncing) do not require `super` privileges.

It's general best practice to avoid providing service accounts with admin privileges wherever possible, so we've collaborated with our Perforce customers and Perforce Support to create and refine the p4 broker command handler approach below. This approach involves configuring a command handler on the p4 broker server upstream from the Sourcegraph instance, so that only the `p4 protects -u <username>` command will be executed by a different service account which has the needed `super` privileges. All other p4 commands from the Sourcegraph instance will be executed with the instance's configured Perforce account, with its regular privileges.

# Perforce Documentation

Overview and specifications of the Perforce Broker Command Handler:
https://www.perforce.com/manuals/p4sag/Content/P4SAG/broker-command-handler-specs.html

If you're already using a command handler, this support article demonstrates how to use multiple:
https://portal.perforce.com/s/article/11309

# Implementation

## 1. Create the filter program script

The filter program script receives the command and its arguments from the command handler, and re-runs them as a privileged user. Here's an example written in Perl:

```perl
#!/usr/bin/env perl
# /perforce/brokerroot/sourcegraph_cmd_handler.pl

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

## 2. Add the command handler to the Perforce Broker configuration

Add the command handler for the `p4 protects -u <username>` command:

```
# broker.conf
# See https://help.perforce.com/helix-core/server-apps/p4sag/current/Content/P4SAG/broker-command-handler-specs.html

command: protects
{
  user = svc_sourcegraph;
  flags = -u;
  action = filter;
  execute = /perforce/brokerroot/sourcegraph_cmd_handler.pl;
}
```

## 3. Verify

Before this change is configured, the `p4 protects -u <username>` command should fail

```shell
$ p4 -p [p4 port] -u svc_sourcegraph protects -u other_user //depot/...
You don't have permission for this operation.
```

After this change is configured, the `p4 protects -u <username>` command should work

```shell
$ p4 -p [p4 port] -u svc_sourcegraph protects -u other_user //depot/...
list group everyone * -//...
list group everyone * -//...
write group * * //depot/...
write group * * //depot/...
```

After this change is configured, any other commands which require `super` permissions should still fail

```shell
$ p4 -p [p4 port] -u svc_sourcegraph protect -o
You don't have permission for this operation.
```

Source team's internal Notion doc [here](https://www.notion.so/sourcegraph/Perforce-broker-setup-48b2ad6c4f3f44ba8f73d1a0b93bd789)
