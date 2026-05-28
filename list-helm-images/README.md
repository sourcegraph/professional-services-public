# Sourcegraph Images Tool

A bash script that extracts and lists all Docker images from  Sourcegraph Helm charts

## Prerequisites

- `helm` CLI
- `jq` for JSON parsing

## Usage

```bash
# Latest version, sourcegraph/sourcegraph chart only
./list-helm-images.sh

# Specific version, sourcegraph/sourcegraph chart only
./list-helm-images.sh 6.5.2654

# Latest version, include native k8s executor images
./list-helm-images.sh --include-executor

# Specific version, include native k8s executor images
./list-helm-images.sh --include-executor 6.5.2654
```
