import argparse
import xml.etree.ElementTree as ET
from collections import defaultdict
import os

def parse_manifest(xml_file, default_fetch):
    tree = ET.parse(xml_file)
    root = tree.getroot()

    default_remote = root.find("default").get("remote")

    remotes = {}
    for remote in root.findall("remote"):
        name = remote.get("name")
        fetch = default_fetch if remote.get("fetch") == ".." else remote.get("fetch")
        remotes[name] = fetch

    projects = []
    for project in root.findall("project"):
        path = project.get("path")
        name = project.get("name")
        groups = project.get("groups", "")
        remote = project.get("remote", default_remote)
        fetch = remotes.get(remote, "")

        linkfiles = [
            (linkfile.get("src"), linkfile.get("dest"))
            for linkfile in project.findall("linkfile")
        ]

        remote_url = f"{fetch}/{name}"
        print(f"git clone {remote_url}")

        projects.append({
            "path": path,
            "name": name,
            "groups": groups,
            "remote_url": f"{fetch}/{name}",
            "linkfiles": linkfiles
        })
    return projects

def build_tree(projects):
    tree = {}
    for project in projects:
        parts = project["path"].split("/")
        current = tree
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = {
            "name": project["name"],
            "groups": project["groups"],
            "remote_url": project["remote_url"],
            "linkfiles": project["linkfiles"]
        }
    return tree

def generate_markdown(tree, indent=0):
    markdown = ""
    for key, value in sorted(tree.items()):
        if isinstance(value, dict) and "name" in value:
            markdown += " " * indent + f"- [{key}/]({value['remote_url']})\n"
            for src, dest in value.get("linkfiles", []):
                markdown += " " * (indent + 2) + f"- [{src}]({value['remote_url']}/{src}) → [{dest}]({value['remote_url']}/{dest})\n"
        else:
            markdown += " " * indent + f"- {key}/\n"
            markdown += generate_markdown(value, indent + 2)
    return markdown

def main():
    parser = argparse.ArgumentParser(description='Process an XML manifest file.')
    parser.add_argument('file_path', type=str, help='Path to the XML manifest file')
    parser.add_argument('remote_fetch', type=str, help='Default remote fetch URL')

    args = parser.parse_args()

    # Parse and build manifest structure tree
    projects = parse_manifest(args.file_path, args.remote_fetch)
    tree = build_tree(projects)

    # Generate markdown file from manifest structure
    markdown = generate_markdown(tree)
    with open("project_structure.md", "w") as md_file:
        md_file.write("# Project Structure\n\n")
        md_file.write(markdown)

if __name__ == "__main__":
    main()
