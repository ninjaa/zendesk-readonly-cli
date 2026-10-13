# Zendesk Read-only CLI

A small Python standard-library client for reading Zendesk Support tickets. No packages, browser automation, or installation scripts. Requires Python 3.9 or newer.

## Use

Clone this repository and run `python3 zendesk.py --help`. All global options go **before** the command.

The subdomain is the label before `.zendesk.com`, such as `yourcompany`. Set it with `--subdomain` or `ZENDESK_SUBDOMAIN`. There is no default tenant; URLs, custom domains and malformed labels are rejected.

To enter an existing API token without putting it in shell history or a file:

```sh
python3 zendesk.py --subdomain yourcompany --email agent@example.com --prompt-token me
python3 zendesk.py --subdomain yourcompany --email agent@example.com --prompt-token open
python3 zendesk.py --subdomain yourcompany --email agent@example.com --prompt-token thread 123
```

The token prompt hides input, uses the token only in memory, and refuses to fall back to echoed input. Each invocation prompts again. `--prompt-token` requires an email and rejects an active OAuth token to avoid ambiguity.

Existing environment variables or a private env file also work:

```sh
python3 zendesk.py --env-file /path/to/private/zendesk.env me
python3 zendesk.py --env-file /path/to/private/zendesk.env unresolved
```

Recognized settings:

| Setting | Purpose |
| --- | --- |
| `ZENDESK_SUBDOMAIN` | Required tenant label unless `--subdomain` is provided |
| `ZENDESK_OAUTH_TOKEN` | OAuth bearer authentication; takes precedence over API-token settings |
| `ZENDESK_API_TOKEN` | API token, paired with an agent email |
| `ZENDESK_EMAIL` | Agent email; overridden by `--email` |

The env-file reader accepts simple `KEY=value` lines, optional `export ` prefixes and matching outer quotes. It never executes the file or expands shell syntax. Environment settings override file settings. Keep credential files private; the client does not create, save or manage tokens. Use an existing agent credential with permission to read the intended account.

Prefer OAuth for ongoing access. Zendesk is retiring API-token authentication on April 30, 2027; see its [migration notice](https://support.zendesk.com/hc/en-us/articles/10840968198042-Announcing-the-removal-of-API-tokens-as-an-authentication-method-for-API-requests). This client accepts an existing OAuth access token; it does not create or automatically refresh one. Official [authentication documentation](https://developer.zendesk.com/api-reference/introduction/security-and-auth/) explains the available methods.

## Commands and output

| Command | Result |
| --- | --- |
| `me` | Authenticated user ID, name, email and role |
| `open` | New and open tickets, newest update first |
| `unresolved` | Unresolved tickets, including pending and hold |
| `thread ID` | Ticket details, all comment pages, attachment metadata and available author names |

Successful output is JSON on stdout. Missing author records appear in `unresolved_author_ids`. Errors go to stderr, omit response bodies and credential values, and return exit code 1. Invalid command-line arguments return 2; interruption returns 130. The client never automatically writes output files.

## Boundaries and privacy

Only GET requests to a small set of `/api/v2` endpoints on the selected `https://SUBDOMAIN.zendesk.com` origin are allowed. Redirects and cross-tenant pagination links are refused. There are no ticket updates, replies, uploads or downloaded attachments.

Ticket subjects, descriptions, comments, author names and attachment metadata can contain private customer data. Treat stdout and any redirected files accordingly. Do not commit credentials or ticket exports. Ignore rules cover common credential and JSON-output files, but cannot identify every filename or secret. The client performs no telemetry; Python's standard HTTPS and proxy configuration still apply.

Pagination follows cursor or offset links and rejects missing completion markers, loops or more than 200 pages. Results are buffered in memory and are not emitted when a fetch fails. Requests time out after 30 seconds each. HTTP 429 stops with a retry-later message; there are no automatic retries. This is not a backup or point-in-time snapshot, and very large accounts can exceed the page limit. Zendesk API permissions and available author records constrain the result.

## Offline tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic data and mocked HTTP responses. They exercise credential precedence and redaction, hidden token entry, tenant and endpoint boundaries, redirects, pagination and author batching. Live account access is not part of the tests.
