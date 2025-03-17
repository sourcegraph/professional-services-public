# Project Structure Markdown Generator

This script parses a **repo manifest XML file**, extracts project details, organizes them into a hierarchical structure, and generates a Markdown file representing the project structure.

## How It Works

1. **Parses the manifest**:
   - Reads project paths, names, remotes, and linkfile mappings.
   - Extracts remote fetch URLs.
2. **Builds a hierarchical tree**:
   - Organizes projects into a nested dictionary based on their paths.
3. **Generates a Markdown file**:
   - Outputs a structured view of projects with clickable links.

## Usage

```sh
python generate_markdown.py {XML manifest file} {default remote eetch URL}
```
- XML manifest file path: gerrit repo manifest file to parse
- default remote fetch url: remote fetch url when not specified in manifest

## Example

```sh
python generate-markdown.py asop-example/default.xml https://android.googlesource.com
```

This will output `project_structure.md`, which contains a structured Markdown representation of the repositories.

## Requirements

- Python 3.x



