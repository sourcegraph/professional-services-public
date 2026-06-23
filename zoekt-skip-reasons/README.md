# Troubleshooting why the Zoekt indexer skips files

`zoekt_skip_reasons.py` demonstrates the file checks Zoekt uses before indexing a file

It prints each skip reason in Zoekt's check order,
and if the file would match that reason,
using Sourcegraph's default settings for Zoekt:

- Maximum file size: 1 MiB
- Maximum unique trigrams: 20,000

## Requirements

- Python >= 3.9

## Usage

```sh
./zoekt_skip_reasons.py /path/to/file
```

Example 1

```sh
./zoekt_skip_reasons.py "test-files/4 - Contains too many trigrams.txt"
```

Example 2

```sh
for file in test-files/*; do ./zoekt_skip_reasons.py "$file"; echo; done
```
