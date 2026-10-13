#!/usr/bin/env python3
"""Read Zendesk tickets using only the Python standard library."""

import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import re
import sys
import warnings
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CONFIG_KEYS = {"ZENDESK_OAUTH_TOKEN", "ZENDESK_API_TOKEN", "ZENDESK_EMAIL", "ZENDESK_SUBDOMAIN"}
SUBDOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
ENDPOINT = re.compile(
    r"/api/v2/(?:users/(?:me|show_many)|search/export|tickets/[1-9][0-9]*(?:/comments)?)(?:\.json)?$"
)


class ClientError(Exception):
    """An error that is safe to print without response bodies or credentials."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ClientError("Zendesk returned a redirect; credentials were not forwarded.")


def configuration(env_file=None):
    values = {}
    if env_file:
        for line in Path(env_file).read_text().splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() in CONFIG_KEYS:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    values.update({key: os.environ[key] for key in CONFIG_KEYS if key in os.environ})
    return values


def credentials(values, email=None, prompt_token=False):
    token = values.get("ZENDESK_OAUTH_TOKEN")
    if token:
        if prompt_token:
            raise ClientError("--prompt-token cannot be combined with an active ZENDESK_OAUTH_TOKEN; select one authentication mode.")
        header = "Bearer " + token
    else:
        token = values.get("ZENDESK_API_TOKEN")
        email = email or values.get("ZENDESK_EMAIL")
        if not email or (not token and not prompt_token):
            raise ClientError("Set ZENDESK_OAUTH_TOKEN, or ZENDESK_API_TOKEN and ZENDESK_EMAIL (or --email).")
        if ":" in email or any(c.isspace() for c in email):
            raise ClientError("The Zendesk agent email is invalid.")
        if prompt_token:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    token = getpass.getpass("Zendesk API token (hidden): ")
            except (EOFError, getpass.GetPassWarning):
                raise ClientError("Could not read a hidden token from the terminal; no credential was sent.") from None
        if not token:
            raise ClientError("The Zendesk API token is empty.")
        header = "Basic " + base64.b64encode(f"{email}/token:{token}".encode()).decode()
    if any(c.isspace() for c in token):
        raise ClientError("The Zendesk credential contains whitespace; check its local configuration.")
    return header


def validate_subdomain(subdomain):
    if not isinstance(subdomain, str) or not SUBDOMAIN.fullmatch(subdomain):
        raise ClientError("Set --subdomain or ZENDESK_SUBDOMAIN to a lowercase Zendesk subdomain label (1–63 letters, digits or internal hyphens).")
    return subdomain


class Zendesk:
    def __init__(self, subdomain, authorization):
        self.hostname = validate_subdomain(subdomain) + ".zendesk.com"
        self.origin = "https://" + self.hostname
        self.authorization = authorization
        self.opener = build_opener(NoRedirects())

    def get(self, path):
        url = urljoin(self.origin + "/", path)
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc != self.hostname
                or parts.fragment or not ENDPOINT.fullmatch(parts.path)):
            raise ClientError("Refusing a request outside the allowed Zendesk API endpoints for the selected subdomain.")
        req = Request(url, headers={"Authorization": self.authorization, "Accept": "application/json"}, method="GET")
        try:
            with self.opener.open(req, timeout=30) as response:
                data = json.load(response)
        except HTTPError as exc:
            exc.close()
            if exc.code == 401:
                raise ClientError("HTTP 401: Zendesk rejected authentication; verify the credential and agent identity.") from None
            if exc.code == 429:
                raise ClientError("HTTP 429: Zendesk rate limit reached; retry later.") from None
            raise ClientError(f"Zendesk request failed with HTTP {exc.code}; response body omitted.") from None
        except (URLError, OSError, ValueError):
            raise ClientError("Zendesk network or JSON response error; details omitted to protect credentials.") from None
        if not isinstance(data, dict):
            raise ClientError("Zendesk returned an unexpected response shape.")
        return data

    def pages(self, path, key):
        items, visited = [], set()
        while path:
            canonical = urljoin(self.origin + "/", path)
            if canonical in visited or len(visited) >= 200:
                raise ClientError("Pagination repeated or exceeded 200 pages; no complete result is claimed.")
            visited.add(canonical)
            page = self.get(path)
            rows = page.get(key)
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ClientError("Zendesk returned an unexpected paginated response.")
            items.extend(rows)
            if "meta" in page:
                meta = page["meta"]
                if not isinstance(meta, dict) or not isinstance(meta.get("has_more"), bool):
                    raise ClientError("Zendesk omitted a valid cursor completion marker; result is incomplete.")
                links = page.get("links")
                path = links.get("next") if meta["has_more"] and isinstance(links, dict) else None
                if meta["has_more"] and not path:
                    raise ClientError("Zendesk omitted the next cursor page; result is incomplete.")
            else:
                if "next_page" not in page:
                    raise ClientError("Zendesk omitted a pagination completion marker; result is incomplete.")
                path = page["next_page"]
            if path is not None and (not isinstance(path, str) or not path):
                raise ClientError("Zendesk returned an invalid pagination link.")
        return items

    def tickets(self, unresolved=False):
        query = "status<solved" if unresolved else "status<=open"
        params = urlencode({"query": "type:ticket " + query, "filter[type]": "ticket", "page[size]": 100})
        tickets = self.pages("/api/v2/search/export.json?" + params, "results")
        return sorted(
            [pick(row, ("id", "subject", "status", "priority", "updated_at", "assignee_id", "requester_id")) for row in tickets],
            key=lambda row: row.get("updated_at") or "", reverse=True,
        )

    def thread(self, ticket_id):
        ticket = self.get(f"/api/v2/tickets/{ticket_id}.json")["ticket"]
        comments = self.pages(f"/api/v2/tickets/{ticket_id}/comments.json?per_page=100&sort_order=asc", "comments")
        authors = sorted({row["author_id"] for row in comments})
        users = []
        for start in range(0, len(authors), 100):
            params = urlencode({"ids": ",".join(str(i) for i in authors[start:start + 100])})
            batch = self.get("/api/v2/users/show_many.json?" + params).get("users")
            if not isinstance(batch, list) or not all(isinstance(row, dict) for row in batch):
                raise ClientError("Zendesk returned an unexpected author response.")
            users.extend(batch)
        return {
            "ticket": pick(ticket, ("id", "subject", "description", "status", "priority", "updated_at", "requester_id", "assignee_id", "tags")),
            "comments": [pick(row, ("id", "created_at", "author_id", "public", "body", "attachments")) for row in comments],
            "authors": [pick(row, ("id", "name", "role")) for row in users],
            "unresolved_author_ids": sorted(set(authors) - {row.get("id") for row in users}),
        }


def pick(row, fields):
    return {key: row.get(key) for key in fields}


def positive_id(value):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("ticket ID must be a positive integer")
    return int(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subdomain", help="Zendesk subdomain label, or set ZENDESK_SUBDOMAIN; no default tenant.")
    parser.add_argument("--prompt-token", action="store_true", help="Prompt for an API token without echoing or saving it; requires an agent email and no active OAuth token.")
    parser.add_argument("--env-file", help="Read Zendesk credentials in place from an existing env file; never execute it.")
    parser.add_argument("--email", help="Zendesk agent email for existing API-token authentication.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("me", help="Verify the authenticated identity.")
    commands.add_parser("open", help="List new/open tickets, including custom statuses in those categories.")
    commands.add_parser("unresolved", help="List all unresolved tickets, including pending and hold.")
    commands.add_parser("thread", help="Read a ticket, all comments and author names.").add_argument("ticket_id", type=positive_id)
    args = parser.parse_args()
    try:
        values = configuration(args.env_file)
        subdomain = args.subdomain or values.get("ZENDESK_SUBDOMAIN")
        client = Zendesk(validate_subdomain(subdomain), credentials(values, args.email, args.prompt_token))
        if args.command == "me":
            result = pick(client.get("/api/v2/users/me.json")["user"], ("id", "name", "email", "role"))
        elif args.command == "thread":
            result = client.thread(args.ticket_id)
        else:
            result = client.tickets(args.command == "unresolved")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Interrupted; no result is claimed.", file=sys.stderr)
        return 130
    except (ClientError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(str(exc) if isinstance(exc, ClientError) else "Local credential file or Zendesk response is invalid; details omitted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
