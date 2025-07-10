#!/bin/bash

set -e

INCLUDE_EXECUTOR=false
VERSION=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --include-executor)
            INCLUDE_EXECUTOR=true
            shift
            ;;
        *)
            VERSION="$1"
            shift
            ;;
    esac
done

# Check for helm CLI
if ! command -v helm &> /dev/null; then
    echo "Error: helm CLI not found. Please install helm first" >&2
    exit 1
fi

# Check if sourcegraph repo is added and update helm repo
if ! helm repo list | grep -q "sourcegraph"; then
    helm repo add sourcegraph https://helm.sourcegraph.com/release
fi

helm repo update > /dev/null

# Set VERSION to latest if not provided
if [ -z "$VERSION" ]; then
    VERSION=$(helm search repo sourcegraph/sourcegraph --output json | jq -r '.[0].version')
fi

# Template sourcegraph/sourcegraph helm chart and extract image list
echo "Using version: $VERSION"
IMAGES=$(helm template sourcegraph/sourcegraph --version="$VERSION" --set sgTestConnection.enabled=false | grep -oE 'image: [^[:space:]]+' | sed 's/image: //')

# Template sourcegraph/sourcegraph-executor-k8s and extract image list if flag is set
if [ "$INCLUDE_EXECUTOR" = true ]; then
    echo "Including executor images..."
    EXECUTOR_IMAGES=$(helm template sourcegraph/sourcegraph-executor-k8s --version="$VERSION" | grep -oE 'image: [^[:space:]]+' | sed 's/image: //')
    IMAGES=$(echo -e "$IMAGES\n$EXECUTOR_IMAGES")
fi

# Sort and deduplicate
echo "Full image list for version $VERSION:"
echo "---------------------------------------"
echo "$IMAGES" | sort -u
