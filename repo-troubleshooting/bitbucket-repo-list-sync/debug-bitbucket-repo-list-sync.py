#!/usr/bin/env python3
"""Replay Sourcegraph's Bitbucket Server repo-list sync requests.

This script fetches one Bitbucket Server external service config from
Sourcegraph, then issues the same `GET rest/api/1.0/repos` requests that the
Sourcegraph repo-list sync job sends. For each Bitbucket request, it prints the
request headers and body, then the response headers and body.

Required inputs can come from flags, environment variables, or a local `.env`
file:

  SRC_ENDPOINT=https://sourcegraph.example.com
  SRC_ACCESS_TOKEN=sgp_...
  BBS_EXTERNAL_SERVICE_ID=RXh0ZXJuYWxTZXJ2aWNlOjE=

Examples:

  uv run python debug-bitbucket-repo-list-sync.py

  uv run python debug-bitbucket-repo-list-sync.py \
    --src-endpoint https://sourcegraph.example.com \
    --src-access-token sgp_... \
    --bbs-external-service-id RXh0ZXJuYWxTZXJ2aWNlOjE= \
    --max-pages 1

By default, secret-bearing headers are redacted in the printed output. Pass
`--show-secrets` only when you are writing to a safe terminal or file.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import sys
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

PAGE_LIMIT = 1000
REQUEST_TIMEOUT_SECONDS = 60
BITBUCKET_REQUEST_CONTENT_TYPE = "application/json; charset=utf-8"
SOURCEGRAPH_USER_AGENT = "debug-bitbucket-repo-list-sync/0.1"
SECTION_WIDTH = 80

EXTERNAL_SERVICE_QUERY = """
query BitbucketServerExternalService($externalServiceId: ID!) {
  node(id: $externalServiceId) {
    __typename
    ... on ExternalService {
      id
      kind
      displayName
      config
    }
  }
}
"""


@dataclass(frozen=True)
class Arguments:
    sourcegraph_endpoint: str
    sourcegraph_access_token: str
    bitbucket_external_service_id: str
    timeout_seconds: int
    max_pages: int | None
    body_limit_bytes: int
    pretty_json: bool
    show_secrets: bool


@dataclass(frozen=True)
class HttpResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes


@dataclass(frozen=True)
class BitbucketAuth:
    method: str
    headers: dict[str, str]


def main() -> None:
    load_environment_file(Path(".env"))
    arguments = parse_arguments()

    banner("STEP 1 - Fetch Sourcegraph external service config")
    external_service = fetch_external_service(arguments)
    bitbucket_config = parse_external_service_config(str(external_service.get("config") or "{}"))

    bitbucket_url = string_config_value(bitbucket_config, "url")
    if not bitbucket_url:
        exit_with_error("External service config has no non-empty 'url' field.")

    print(f"External service: {external_service.get('displayName', '')}")
    print(f"Kind:             {external_service.get('kind', '')}")
    print(f"Bitbucket URL:    {bitbucket_url}")

    warn_if_config_contains_redacted_secrets(bitbucket_config)
    bitbucket_auth = build_bitbucket_auth(bitbucket_config)
    print(f"Auth method:      {bitbucket_auth.method}")

    repository_queries = repository_query_entries(bitbucket_config)
    print("repositoryQuery:  " + json.dumps(repository_queries))
    print(f"Page limit:       {PAGE_LIMIT}")

    banner("STEP 2 - Replay Bitbucket Server repo-list sync requests")
    replay_repository_queries(
        arguments=arguments,
        bitbucket_url=bitbucket_url,
        bitbucket_auth=bitbucket_auth,
        repository_queries=repository_queries,
    )

    banner("DONE")


def parse_arguments() -> Arguments:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--src-endpoint",
        default=os.environ.get("SRC_ENDPOINT", ""),
        help="Sourcegraph endpoint. Defaults to SRC_ENDPOINT.",
    )
    parser.add_argument(
        "--src-access-token",
        default=os.environ.get("SRC_ACCESS_TOKEN", ""),
        help="Sourcegraph access token. Defaults to SRC_ACCESS_TOKEN.",
    )
    parser.add_argument(
        "--bbs-external-service-id",
        default=os.environ.get("BBS_EXTERNAL_SERVICE_ID", ""),
        help="GraphQL node ID for the Bitbucket Server external service. "
        "Defaults to BBS_EXTERNAL_SERVICE_ID.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=positive_integer,
        default=REQUEST_TIMEOUT_SECONDS,
        help=f"HTTP request timeout in seconds. Default: {REQUEST_TIMEOUT_SECONDS}.",
    )
    parser.add_argument(
        "--max-pages",
        type=positive_integer,
        default=None,
        help="Maximum Bitbucket pages to request for each repositoryQuery entry. "
        "Default: no limit.",
    )
    parser.add_argument(
        "--body-limit-bytes",
        type=non_negative_integer,
        default=0,
        help="Maximum response-body bytes to print. Use 0 for no limit. Default: 0.",
    )
    parser.add_argument(
        "--pretty-json",
        action="store_true",
        help="Pretty-print JSON bodies instead of showing the raw response text.",
    )
    parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="Print Authorization, Cookie, token, password, and secret header values.",
    )

    namespace = parser.parse_args()
    missing_names = [
        name
        for name, value in (
            ("--src-endpoint or SRC_ENDPOINT", namespace.src_endpoint),
            ("--src-access-token or SRC_ACCESS_TOKEN", namespace.src_access_token),
            (
                "--bbs-external-service-id or BBS_EXTERNAL_SERVICE_ID",
                namespace.bbs_external_service_id,
            ),
        )
        if not value
    ]
    if missing_names:
        parser.error("Missing required input(s): " + ", ".join(missing_names))

    return Arguments(
        sourcegraph_endpoint=namespace.src_endpoint,
        sourcegraph_access_token=namespace.src_access_token,
        bitbucket_external_service_id=namespace.bbs_external_service_id,
        timeout_seconds=namespace.timeout_seconds,
        max_pages=namespace.max_pages,
        body_limit_bytes=namespace.body_limit_bytes,
        pretty_json=namespace.pretty_json,
        show_secrets=namespace.show_secrets,
    )


def positive_integer(value: str) -> int:
    parsed_value = int(value)
    if parsed_value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed_value


def non_negative_integer(value: str) -> int:
    parsed_value = int(value)
    if parsed_value < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed_value


def load_environment_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
            continue
        key_text, value_text = stripped_line.split("=", 1)
        key_text = key_text.strip()
        if key_text.startswith("export "):
            key_text = key_text.removeprefix("export ").strip()
        if key_text and key_text not in os.environ:
            os.environ[key_text] = clean_environment_value(value_text.strip())


def clean_environment_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def fetch_external_service(arguments: Arguments) -> dict[str, Any]:
    data = sourcegraph_graphql_request(
        endpoint=arguments.sourcegraph_endpoint,
        access_token=arguments.sourcegraph_access_token,
        query=EXTERNAL_SERVICE_QUERY,
        variables={"externalServiceId": arguments.bitbucket_external_service_id},
        timeout_seconds=arguments.timeout_seconds,
    )
    node = data.get("node")
    if not isinstance(node, dict) or node.get("__typename") != "ExternalService":
        exit_with_error(
            "Sourcegraph node lookup did not return an ExternalService. "
            "Check the external service ID and token permissions."
        )
    if node.get("kind") != "BITBUCKET_SERVER":
        exit_with_error(
            "External service kind is "
            f"{node.get('kind')!r}; expected 'BITBUCKET_SERVER'."
        )
    return node


def sourcegraph_graphql_request(
    endpoint: str,
    access_token: str,
    query: str,
    variables: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    url = endpoint.rstrip("/") + "/.api/graphql"
    request_body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    headers = {
        "Authorization": f"token {access_token}",
        "Content-Type": "application/json",
        "User-Agent": SOURCEGRAPH_USER_AGENT,
    }

    response = send_http_request(
        method="POST",
        url=url,
        headers=headers,
        body=request_body,
        timeout_seconds=timeout_seconds,
    )
    if response.status >= http.client.BAD_REQUEST:
        exit_with_error(
            f"Sourcegraph GraphQL request failed with HTTP {response.status} "
            f"{response.reason}:\n{decoded_body(response.body)}"
        )

    try:
        payload = json.loads(response.body)
    except json.JSONDecodeError as error:
        exit_with_error(f"Sourcegraph GraphQL response was not JSON: {error}")

    if not isinstance(payload, dict):
        exit_with_error("Sourcegraph GraphQL response was not a JSON object.")
    errors = payload.get("errors")
    if errors and not payload.get("data"):
        exit_with_error("Sourcegraph GraphQL errors:\n" + json.dumps(errors, indent=2))
    if errors:
        print("WARNING: Sourcegraph GraphQL returned partial errors:", file=sys.stderr)
        print(json.dumps(errors, indent=2), file=sys.stderr)

    data = payload.get("data")
    if not isinstance(data, dict):
        exit_with_error("Sourcegraph GraphQL response did not include a data object.")
    return data


def send_http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout_seconds: int,
) -> HttpResponse:
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in {"http", "https"}:
        exit_with_error(f"Unsupported URL scheme in {url!r}; expected http or https.")
    if not parsed_url.hostname:
        exit_with_error(f"URL has no hostname: {url!r}")

    connection_class = (
        http.client.HTTPSConnection if parsed_url.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(
        parsed_url.hostname,
        parsed_url.port,
        timeout=timeout_seconds,
    )
    request_target = urlunsplit(("", "", parsed_url.path or "/", parsed_url.query, ""))
    try:
        connection.request(method, request_target, body=body or None, headers=headers)
        response = connection.getresponse()
        return HttpResponse(
            status=response.status,
            reason=response.reason,
            headers=response.getheaders(),
            body=response.read(),
        )
    except OSError as error:
        exit_with_error(f"HTTP request to {url!r} failed: {error}")
    finally:
        connection.close()


def parse_external_service_config(config_text: str) -> dict[str, Any]:
    cleaned_config = strip_json_comments(config_text)
    cleaned_config = remove_trailing_commas(cleaned_config)
    try:
        parsed_config = json.loads(cleaned_config)
    except json.JSONDecodeError as error:
        exit_with_error(f"External service config was not valid JSON/JSONC: {error}")
    if not isinstance(parsed_config, dict):
        exit_with_error("External service config JSON must be an object.")
    return parsed_config


def strip_json_comments(text: str) -> str:
    result: list[str] = []
    inside_string = False
    escaped = False
    inside_line_comment = False
    inside_block_comment = False
    index = 0

    while index < len(text):
        character = text[index]
        next_character = text[index + 1] if index + 1 < len(text) else ""

        if inside_line_comment:
            if character == "\n":
                inside_line_comment = False
                result.append(character)
            index += 1
            continue

        if inside_block_comment:
            if character == "*" and next_character == "/":
                inside_block_comment = False
                index += 2
            else:
                if character == "\n":
                    result.append(character)
                index += 1
            continue

        if inside_string:
            result.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                inside_string = False
            index += 1
            continue

        if character == '"':
            inside_string = True
            result.append(character)
            index += 1
            continue
        if character == "/" and next_character == "/":
            inside_line_comment = True
            index += 2
            continue
        if character == "/" and next_character == "*":
            inside_block_comment = True
            index += 2
            continue

        result.append(character)
        index += 1

    return "".join(result)


def remove_trailing_commas(text: str) -> str:
    result: list[str] = []
    inside_string = False
    escaped = False
    index = 0

    while index < len(text):
        character = text[index]
        if inside_string:
            result.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                inside_string = False
            index += 1
            continue

        if character == '"':
            inside_string = True
            result.append(character)
            index += 1
            continue

        if character == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue

        result.append(character)
        index += 1

    return "".join(result)


def string_config_value(config: dict[str, Any], name: str) -> str:
    value = config.get(name)
    if isinstance(value, str):
        return value
    return ""


def warn_if_config_contains_redacted_secrets(config: dict[str, Any]) -> None:
    redacted_names: list[str] = []
    for field_name in ("token", "password"):
        field_value = string_config_value(config, field_name)
        if "REDACTED" in field_value.upper():
            redacted_names.append(field_name)
    if redacted_names:
        print(
            "WARNING: external service config includes redacted secret field(s): "
            + ", ".join(redacted_names),
            file=sys.stderr,
        )
        print(
            "Use a Sourcegraph site-admin token that can read unredacted external "
            "service config, or the Bitbucket replay request will authenticate "
            "with the redacted placeholder.",
            file=sys.stderr,
        )


def build_bitbucket_auth(config: dict[str, Any]) -> BitbucketAuth:
    token = string_config_value(config, "token")
    if token:
        return BitbucketAuth(method="Bearer token", headers={"Authorization": f"Bearer {token}"})

    username = string_config_value(config, "username")
    password = string_config_value(config, "password")
    if username:
        encoded_credentials = base64.b64encode(f"{username}:{password}".encode("utf-8"))
        authorization_value = "Basic " + encoded_credentials.decode("ascii")
        return BitbucketAuth(method="Basic auth", headers={"Authorization": authorization_value})

    authorization_config = config.get("authorization")
    if authorization_config:
        print(
            "WARNING: this Bitbucket Server config uses the 'authorization' block. "
            "The Go client can OAuth-sign those requests, but this standalone "
            "debug script only replays token and basic-auth connections.",
            file=sys.stderr,
        )
        return BitbucketAuth(method="unsupported authorization block", headers={})

    return BitbucketAuth(method="none", headers={})


def repository_query_entries(config: dict[str, Any]) -> list[str]:
    raw_value = config.get("repositoryQuery", ["all"])
    if raw_value is None:
        return ["all"]
    if isinstance(raw_value, str):
        return [raw_value]
    if not isinstance(raw_value, list):
        exit_with_error("External service config field 'repositoryQuery' must be a list of strings.")
    entries: list[str] = []
    for index, entry in enumerate(raw_value):
        if not isinstance(entry, str):
            exit_with_error(f"repositoryQuery[{index}] is not a string: {entry!r}")
        entries.append(entry)
    return entries


def replay_repository_queries(
    arguments: Arguments,
    bitbucket_url: str,
    bitbucket_auth: BitbucketAuth,
    repository_queries: Iterable[str],
) -> None:
    replayed_any_query = False
    for repository_query in repository_queries:
        if repository_query == "none":
            print("Skipping repositoryQuery entry 'none'; Sourcegraph does not list repos for it.")
            continue

        effective_query = "" if repository_query == "all" else repository_query
        replayed_any_query = True
        replay_single_repository_query(
            arguments=arguments,
            bitbucket_url=bitbucket_url,
            bitbucket_auth=bitbucket_auth,
            displayed_query=repository_query,
            effective_query=effective_query,
        )

    if not replayed_any_query:
        print("No Bitbucket repo-list requests to replay; every repositoryQuery entry was 'none'.")


def replay_single_repository_query(
    arguments: Arguments,
    bitbucket_url: str,
    bitbucket_auth: BitbucketAuth,
    displayed_query: str,
    effective_query: str,
) -> None:
    start = 0
    page_number = 0
    print()
    print(f">>> repositoryQuery entry: {displayed_query!r}")
    print(f">>> effective query string: {effective_query!r}")

    while True:
        if arguments.max_pages is not None and page_number >= arguments.max_pages:
            print(f"Reached --max-pages={arguments.max_pages}; stopping this query.")
            return

        page_number += 1
        response = replay_repos_page(
            arguments=arguments,
            bitbucket_url=bitbucket_url,
            bitbucket_auth=bitbucket_auth,
            repository_query=effective_query,
            start=start,
            page_number=page_number,
        )

        if response.status >= http.client.BAD_REQUEST:
            print(f"Stopping pagination after HTTP {response.status} {response.reason}.")
            return

        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError as error:
            print(f"Stopping pagination because the response body was not JSON: {error}")
            return
        if not isinstance(payload, dict):
            print("Stopping pagination because the response JSON was not an object.")
            return

        values = payload.get("values", [])
        value_count = len(values) if isinstance(values, list) else 0
        is_last_page = bool(payload.get("isLastPage", True))
        next_page_start = payload.get("nextPageStart")

        print()
        print(
            "Page summary: "
            f"values={value_count}, isLastPage={is_last_page}, "
            f"nextPageStart={next_page_start!r}"
        )

        if is_last_page:
            return
        if not isinstance(next_page_start, int):
            print("Stopping pagination because nextPageStart was missing or not an integer.")
            return
        start = next_page_start


def replay_repos_page(
    arguments: Arguments,
    bitbucket_url: str,
    bitbucket_auth: BitbucketAuth,
    repository_query: str,
    start: int,
    page_number: int,
) -> HttpResponse:
    url = build_bitbucket_repos_url(bitbucket_url, repository_query, start)
    request_headers = {
        "Content-Type": BITBUCKET_REQUEST_CONTENT_TYPE,
        **bitbucket_auth.headers,
    }

    banner(f"PAGE {page_number} REQUEST")
    print(f"GET {url}")
    print()
    print("Headers:")
    print_headers(request_headers.items(), show_secrets=arguments.show_secrets)
    print()
    print("Body:")
    print_body(
        body=b"",
        body_limit_bytes=arguments.body_limit_bytes,
        pretty_json=arguments.pretty_json,
    )

    response = send_http_request(
        method="GET",
        url=url,
        headers=request_headers,
        body=b"",
        timeout_seconds=arguments.timeout_seconds,
    )

    banner(f"PAGE {page_number} RESPONSE - HTTP {response.status} {response.reason}")
    print("Headers:")
    print_headers(response.headers, show_secrets=arguments.show_secrets)
    print()
    print("Body:")
    print_body(
        body=response.body,
        body_limit_bytes=arguments.body_limit_bytes,
        pretty_json=arguments.pretty_json,
    )
    return response


def build_bitbucket_repos_url(bitbucket_url: str, repository_query: str, start: int) -> str:
    page_parameters = [("limit", str(PAGE_LIMIT)), ("start", str(start))]
    query_parameters = parse_qsl(repository_query.lstrip("?"), keep_blank_values=True)
    query_string = urlencode(page_parameters + query_parameters)
    base_url = bitbucket_url.rstrip("/") + "/"
    return urljoin(base_url, "rest/api/1.0/repos?" + query_string)


def print_headers(headers: Iterable[tuple[str, str]], show_secrets: bool) -> None:
    for name, value in headers:
        print(f"  {name}: {display_header_value(name, value, show_secrets)}")


def display_header_value(name: str, value: str, show_secrets: bool) -> str:
    if show_secrets:
        return value
    lowercase_name = name.lower()
    sensitive_fragments = ("authorization", "cookie", "password", "secret", "token")
    if any(fragment in lowercase_name for fragment in sensitive_fragments):
        return "[redacted]"
    return value


def print_body(body: bytes, body_limit_bytes: int, pretty_json: bool) -> None:
    if not body:
        print("  (empty body)")
        return

    displayed_body = body
    omitted_bytes = 0
    if body_limit_bytes and len(body) > body_limit_bytes:
        displayed_body = body[:body_limit_bytes]
        omitted_bytes = len(body) - body_limit_bytes

    if pretty_json:
        try:
            parsed_body = json.loads(displayed_body)
            text = json.dumps(parsed_body, indent=2, sort_keys=True)
        except json.JSONDecodeError:
            text = decoded_body(displayed_body)
    else:
        text = decoded_body(displayed_body)

    print(textwrap.indent(text, "  "))
    if omitted_bytes:
        print(f"  ... omitted {omitted_bytes} byte(s); increase --body-limit-bytes to print more")


def decoded_body(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def banner(title: str) -> None:
    print()
    print("=" * SECTION_WIDTH)
    print(title)
    print("=" * SECTION_WIDTH)


def exit_with_error(message: str) -> NoReturn:
    print("ERROR: " + message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
