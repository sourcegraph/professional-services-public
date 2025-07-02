# Sourcegraph Bootstrap Tool

A Python script that automates the initial setup and configuration of a new Sourcegraph instance.

## What It Does

- Creates the initial admin user account
- Generates an admin access token
- Configures the external URL
- Applies license key (if provided)

## Prerequisites

- Python 3.13+
- Network access to your Sourcegraph instance
- A Sourcegraph instance that hasn't been initialized yet

## Installation

### With pip

```bash
pip install -e .
```

### With uv

```bash
uv sync
```

## Environment Variables

The following environment variables can be used to configure the script for non-interactive use.

| Variable | Required | Description |
|----------|----------|-------------|
| `SRC_ENDPOINT` | Optional* | Sourcegraph instance URL (e.g., `https://sourcegraph.example.com`) |
| `SRC_ADMIN_USER` | Optional* | Admin username |
| `SRC_ADMIN_EMAIL` | Optional* | Admin email address |
| `SRC_ADMIN_PASS` | Optional* | Admin password |
| `SRC_LICENSE_KEY` | Optional | Sourcegraph license key |

*If not provided, the script will prompt for these values.

## Usage

```bash
python main.py
```
