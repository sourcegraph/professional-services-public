#!/usr/bin/env python3
"""Create Sourcegraph users from a username,email CSV using src user create."""

import argparse
import csv
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", help="UTF-8 CSV file with a username,email header")
    args = parser.parse_args()

    try:
        users = []
        with open(args.csv_file, encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source, strict=True)
            if next(reader, None) != ["username", "email"]:
                raise ValueError("CSV must start with the header username,email")
            for row in reader:
                if not row:
                    continue
                if len(row) != 2 or not all(value.strip() for value in row):
                    raise ValueError(
                        f"line {reader.line_num}: expected a nonempty username and email"
                    )
                username, email = (value.strip() for value in row)
                users.append((reader.line_num, username, email))
        if not users:
            raise ValueError("CSV contains no users")
    except (OSError, UnicodeError, csv.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    for line, username, email in users:
        try:
            subprocess.run(
                ["src", "user", "create", f"-username={username}", f"-email={email}"],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            print(
                f"error: line {line}: src user create failed: {error}. "
                "Stopped; previously created users have not been rolled back.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
