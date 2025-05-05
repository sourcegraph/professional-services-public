import argparse
import xml.etree.ElementTree as ET
import requests
from collections import defaultdict
import os

GRAPHQL_CONFIG = {
    "RepositoriesByNames": {
        "query": """
            query RepositoriesByNames($names: [String!]!, $first: Int!, $after: String) {
                repositories(names: $names, first: $first, after: $after) {
                    nodes {
                        id
                        name
                    }
                    pageInfo {
                        endCursor
                        hasNextPage
                    }
                }
            }
        """,
        "variables": ["names", "first", "after"],
        "success_key": "repositories"
    },
    "CreateSearchContext": {
        "query": """
            mutation CreateSearchContext($input: SearchContextInput!, $repositories: [SearchContextRepositoryRevisionsInput!]!) {
                createSearchContext(searchContext: $input, repositories: $repositories) {
                    id
                }
            }
        """,
        "variables": ["input", "repositories"],
        "success_key": "createSearchContext"
    }
}

def execute_graphql_operation(endpoint, headers, operation_name, variables):
    query = GRAPHQL_CONFIG[operation_name]["query"]

    endpoint = f"{endpoint}/.api/graphql"
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json={"query": query, "variables": variables},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"errors": [{"message": str(e)}]}

def parse_manifest(xml_file, default_fetch):
    tree = ET.parse(xml_file)
    root = tree.getroot()

    default_remote = root.find("default").get("remote")
    default_revision = root.find("default").get("revision")

    remotes = {}
    for remote in root.findall("remote"):
        name = remote.get("name")
        fetch_value = remote.get("fetch")
        is_url = fetch_value and (fetch_value.startswith('http://') or fetch_value.startswith('https://'))
        fetch = fetch_value if is_url else default_fetch
        remotes[name] = fetch

    projects = []
    for project in root.findall("project"):
        path = project.get("path")
        name = project.get("name")
        revision = project.get("revision", default_revision)
        groups = project.get("groups", "")
        remote = project.get("remote", default_remote)
        fetch = remotes.get(remote, "")

        linkfiles = [
            (linkfile.get("src"), linkfile.get("dest"))
            for linkfile in project.findall("linkfile")
        ]

        copyfiles = [
            (copyfile.get("src"), copyfile.get("dest"))
            for copyfile in project.findall("copyfile")
        ]

        projects.append({
            "path": path,
            "name": name,
            "revision": revision,
            "groups": groups,
            "remote_url": f"{fetch}/{name}",
            "linkfiles": linkfiles,
            "copyfiles": copyfiles
        })
    return projects

def build_tree(projects):
    tree = {}
    for project in projects:
        parts = project["path"].split("/")
        current = tree
        for part in parts[:-1]:
            if part in current and not isinstance(current[part], dict):
                current[part] = {"__project__": current[part]}
            current = current.setdefault(part, {})
        
        if parts[-1] in current and isinstance(current[parts[-1]], dict):
            current[parts[-1]]["__project__"] = {
                "name": project["name"],
                "groups": project["groups"],
                "remote_url": project["remote_url"],
                "linkfiles": project["linkfiles"],
                "copyfiles": project["copyfiles"]
            }
        else:
            current[parts[-1]] = {
                "name": project["name"],
                "groups": project["groups"],
                "remote_url": project["remote_url"],
                "linkfiles": project["linkfiles"],
                "copyfiles": project["copyfiles"]
            }
    return tree

def generate_markdown(tree, indent=0):
    markdown = ""
    for key, value in sorted(tree.items()):
        if key == "__project__":
            continue
            
        if isinstance(value, dict):
            if "name" in value and "__project__" not in value:
                markdown += " " * indent + f"- [{key}/]({value['remote_url']})\n"
                for src, dest in value.get("linkfiles", []):
                    markdown += " " * (indent + 2) + f"- [↪ {src}]({value['remote_url']}/{src}) → [{dest}]({value['remote_url']}/{dest})\n"
                for src, dest in value.get("copyfiles", []):
                    markdown += " " * (indent + 2) + f"- [⎘ {src}]({value['remote_url']}/{src}) → [{dest}]({value['remote_url']}/{dest})\n"
                child_keys = [k for k in value.keys() if k not in ["name", "groups", "remote_url", "linkfiles", "copyfiles"] and not k.startswith("__")]
                if child_keys:
                    child_dict = {k: value[k] for k in child_keys}
                    markdown += generate_markdown(child_dict, indent + 2)
            else:
                project_info = value.get("__project__")
                if project_info:
                    markdown += " " * indent + f"- [{key}/]({project_info['remote_url']})\n"
                    for src, dest in project_info.get("linkfiles", []):
                        markdown += " " * (indent + 2) + f"- [↪ {src}]({project_info['remote_url']}/{src}) → [{dest}]({project_info['remote_url']}/{dest})\n"
                    for src, dest in project_info.get("copyfiles", []):
                        markdown += " " * (indent + 2) + f"- [⎘ {src}]({project_info['remote_url']}/{src}) → [{dest}]({project_info['remote_url']}/{dest})\n"
                    child_dict = {k: v for k, v in value.items() if k != "__project__"}
                    if child_dict:
                        markdown += generate_markdown(child_dict, indent + 2)
                else:
                    markdown += " " * indent + f"- {key}/\n"
                    markdown += generate_markdown(value, indent + 2)
        else:
            markdown += " " * indent + f"- {key} = {value}\n"
    return markdown


def create_search_context(endpoint, headers, projects, context_name):
    repo_names = [project["remote_url"].replace("http://", "").replace("https://", "") for project in projects]
    
    all_repos = []
    after_cursor = None
    page_size = 1000
    
    # Fetch repositories with pagination support
    while True:
        repo_variables = {
            "names": repo_names,
            "first": page_size,
            "after": after_cursor
        }
        
        repo_result = execute_graphql_operation(endpoint, headers, "RepositoriesByNames", repo_variables)
        
        if "errors" in repo_result:
            return {"error": f"Failed to fetch repositories: {repo_result['errors']}"}
        
        if "data" not in repo_result or "repositories" not in repo_result["data"]:
            return {"error": "Invalid response from GraphQL API"}
            
        nodes = repo_result["data"]["repositories"]["nodes"]
        all_repos.extend(nodes)
        
        # Check if we need to fetch more pages
        page_info = repo_result["data"]["repositories"]["pageInfo"]
        if not page_info["hasNextPage"]:
            break
            
        after_cursor = page_info["endCursor"]
    
    if not all_repos:
        return {"error": "No repositories found. Check that repository URLs are correct and accessible in Sourcegraph."}
    
    repositories = []
    
    # Attempt to match projects by name or remote URL for flexibility      
    project_by_name = {}
    project_by_url = {}

    for project in projects:
        project_by_name[project["name"]] = project
        project_by_url[project["remote_url"].replace("http://", "").replace("https://", "")] = project
        
    for repo in all_repos:
        matched_project = None
        
        if repo["name"] in project_by_name:
            matched_project = project_by_name[repo["name"]]
        elif repo["name"] in project_by_url:
            matched_project = project_by_url[repo["name"]]
        else:
            clean_name = repo["name"].strip('/')
            for url, project in project_by_url.items():
                if url.strip('/') == clean_name:
                    matched_project = project
                    break
        
        if matched_project:
            repositories.append({
                "repositoryID": repo["id"],
                "revisions": [matched_project["revision"]]
            })
    
    if not repositories:
        return {"error": "Could not match any repositories with project URLs. Check URL formatting."}
    
    context_variables = {
        "input": {
            "name": context_name,
            "description": "",
            "public": True,
            "namespace": None,
            "query": ""
        },
        "repositories": repositories
    }
    
    context_result = execute_graphql_operation(endpoint, headers, "CreateSearchContext", context_variables)
    
    if "errors" in context_result:
        return {"error": f"Failed to create search context: {context_result['errors']}"}
    
    return context_result

def main():
    parser = argparse.ArgumentParser(description='Generate a Markdown file representing a Gerrit repo XML manifest for use in Sourcegraph.')
    parser.add_argument('file_path', type=str, help='Path to the Gerrit repo XML manifest file')
    parser.add_argument('remote_fetch', type=str, help='Default remote fetch URL')
    parser.add_argument('--create-context', action='store_true', default=False, 
                      help='(optional) Create a Search Context. Requires SRC_ENDPOINT and SRC_ACCESS_TOKEN environment variables to be set.')
    parser.add_argument('--context-name', type=str, 
                      help='(optional) Name for the Search Context (alphanumeric and ._/- characters only). Defaults to manifest filename without extension when not specified.')
    
    parser.epilog = """
Environment variables:
  SRC_ENDPOINT      Sourcegraph instance URL (required for Search Context creation)
  SRC_ACCESS_TOKEN  Sourcegraph access token (required for Search Context creation)
"""
    parser.formatter_class = argparse.RawDescriptionHelpFormatter

    args = parser.parse_args()

    # Generate output filename and default context name based on input filename
    input_filename = os.path.basename(args.file_path)
    base_name = os.path.splitext(input_filename)[0]
    output_filename = base_name + ".md"
    
    if args.context_name is None:
        args.context_name = base_name

    # Parse Gerrit repo XML file and build manifest structure tree
    projects = parse_manifest(args.file_path, args.remote_fetch)
    tree = build_tree(projects)

    # Track search context creation status
    context_created = False
    context_url = ""
    
    # Create search context
    if args.create_context:
        endpoint = os.environ.get('SRC_ENDPOINT')
        token = os.environ.get('SRC_ACCESS_TOKEN')
        
        if not endpoint or not token:
            print("Error: SRC_ENDPOINT and SRC_ACCESS_TOKEN environment variables must be set when using --create-context")
            return
            
        headers = {
            "Authorization": f"token {token}",
            "Content-Type": "application/json"
        }
        result = create_search_context(endpoint, headers, projects, args.context_name)
        if "error" in result:
            print(f"Error creating search context: {result['error']}")
            print("The markdown file will still be generated without a search context link.")
        else:
            context_created = True
            context_url = f"{endpoint}/search?q=context:{args.context_name}"
            print(f"Search context created with URL: {context_url}")

    # Generate markdown file from manifest structure
    markdown = generate_markdown(tree)
    with open(output_filename, "w") as md_file:
        md_file.write(f"# {base_name} project structure\n\n")
        
        # Add search context link if created successfully
        if context_created:
            md_file.write(f"[Search in Sourcegraph]({context_url})\n\n")
            
        md_file.write(markdown)

if __name__ == "__main__":
    main()
