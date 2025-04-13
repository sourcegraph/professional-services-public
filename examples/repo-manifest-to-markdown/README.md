# Project Structure Markdown Generator

This script parses a **repo manifest XML file**, extracts project details, organizes them into a hierarchical structure, and generates a Markdown file representing the project structure. It can also create a Sourcegraph search context containing all repositories in the manifest.

## How It Works

1. **Parses the manifest**:
   - Reads project paths, names, revisions, remotes, and linkfile mappings.
   - Extracts remote fetch URLs and default revision information.
2. **Builds a hierarchical tree**:
   - Organizes projects into a nested dictionary based on their paths.
3. **Generates a Markdown file**:
   - Outputs a structured view of projects with clickable links.
4. **Optional: Creates a Sourcegraph Search Context**:
   - Queries Sourcegraph API for the internal repository IDs of the projects in the manifest.
   - Creates a [Search Context](https://sourcegraph.com/docs/code-search/working/search_contexts) containing all repositories with their specified revisions.
   - Adds a link to the Search Context in the generated markdown file.

## Usage

```sh
python generate-markdown.py {XML manifest file} {default remote fetch URL} [options]
```

### Required Arguments
- `file_path`: Path to the XML manifest file to parse
- `remote_fetch`: Default remote fetch URL when not specified in manifest

### Optional Arguments
- `--create-context`: Enable search context creation (opt-in, requires SRC_ENDPOINT and SRC_ACCESS_TOKEN environment variables)
- `--context-name`: Name for the Search Context (alphanumeric and ._/- characters only). Defaults to manifest filename without extension when not specified

### Environment Variables
- `SRC_ENDPOINT`: Sourcegraph instance URL (required for context creation)
- `SRC_ACCESS_TOKEN`: Sourcegraph access token (required for context creation). A site admin access token is recommended to ensure the search context 
includes all of the repositories in the Sourcegraph instance irrespective of user permissions.

## Examples

### Basic Usage (Markdown Generation Only)
```sh
python generate-markdown.py asop-example/default.xml https://android.googlesource.com
```

This will output a markdown file with the same base name as the input file (e.g., `default.md`), containing a structured representation of the repositories.

### Creating a Search Context
```sh
# Set environment variables
export SRC_ENDPOINT="https://sourcegraph.example.com"
export SRC_ACCESS_TOKEN="YOUR_TOKEN"

# Run the script with context creation
python generate-markdown.py asop-example/default.xml https://android.googlesource.com --create-context --context-name asop-manifest
```

This will generate the markdown file and also create a Sourcegraph search context named "asop-manifest" containing all repositories in the manifest with their specified revisions. The markdown file will include a link to the search context.