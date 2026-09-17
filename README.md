# brand-evidence

Local-first, tamper-evident record of trademark use, plus daily mention monitoring.
Two paths feed one append-only, hash-chained evidence log:

- **push** (`hook`): each published post is captured the moment it goes live: PNG, PDF, raw HTML,
  SHA-256 of each, and a submission to the Internet Archive for an independent timestamp.
- **pull** (`crawl`): a daily job searches Hacker News, Reddit, Google Alerts feeds and an optional
  web search API for the brand terms, dedupes, and captures new mentions.

A daily `digest` reports run status and facts. It never generates commentary.

## Setup (under 10 minutes)

```bash
git clone <this repo> brand-evidence && cd brand-evidence
uv sync --all-groups                       # Python 3.12 venv + deps
uv run playwright install chromium         # headless browser for capture
cp .env.example .env                       # fill in the required values
uv run brand-evidence init                 # creates the SQLite schema (alembic upgrade head)
uv run brand-evidence digest               # sanity check: an empty digest is a valid output
uv run brand-evidence schedule install     # launchd: crawl 07:00, archive-poll every 2h,
                                           # digest 07:30, timestamp 07:45
```

Required in `.env`: `BE_DB_PATH`, `BE_STORE_PATH`, `BE_DIGEST_PATH`, `BE_BRAND_TERMS`,
`BE_ARCHIVE_PROVIDER=wayback`, `BE_WAYBACK_ACCESS_KEY`, `BE_WAYBACK_SECRET_KEY`. The CLI refuses
to start when any is missing. The Wayback keys are free: create an archive.org account and copy
them from https://archive.org/account/s3.php. Anonymous Save Page Now no longer works. Optional source
credentials disable that source with a warning when absent.

Google Alerts: create alerts for each term at https://www.google.com/alerts, choose "RSS feed"
as the delivery, and paste the feed URLs comma-separated into `BE_GOOGLE_ALERTS_FEEDS`.

## Several projects on one machine

Each project has its own `.env`, database and store. Register them once:

```bash
brand-evidence projects add northwind --env-file ~/brands/northwind/.env
brand-evidence projects add acme      --env-file ~/brands/acme/.env
brand-evidence --project northwind digest
brand-evidence schedule install          # one set of launchd jobs per registered project
brand-evidence --project northwind report --json   # machine-readable status
```

The registry lives at `~/.config/brand-evidence/projects.yaml`. `BE_PROJECT` works as well.

## Posts published from a server

When publishing happens elsewhere (a posting pipeline on a server, say), point
`BE_OWN_PUBLICATIONS_URL` at a JSON feed of publications; `crawl` records new items as posts
and captures them. The feed returns `{platform, external_url, external_id, published_at, text}`.
`sync-posts` runs the same step on its own.

## Proving dates in the past

Evidence for posts published before this tool existed comes from sources that already carry a
date: the platform's own data export (LinkedIn: Settings → Data privacy → Get a copy of your
data; Threads: Accounts Center → Download your information), notification emails from the
platform in your mailbox, and snapshots the Wayback Machine may already hold.

```bash
brand-evidence backfill --file posts.csv          # captures now, archives now, records prior snapshots
brand-evidence archive-history                    # look up prior snapshots for every recorded post
brand-evidence attach --file linkedin-export.zip --kind platform_export --note "requested 2026-09-15"
brand-evidence attach --file post-notification.eml --kind email --post-id <id>
```

Backfilled posts carry two dates: `published_at` from the platform and `captured_at` from now.
Prior snapshots are stored with status `historical` and the archive's own timestamp.

## Commands

```
brand-evidence hook --platform threads --url <url> --body-file <path> [--published-at <iso>]
brand-evidence crawl [--since <iso>] [--source <name>] [--no-capture]
brand-evidence archive-poll
brand-evidence digest [--date YYYY-MM-DD] [--email]
brand-evidence verify
brand-evidence timestamp
brand-evidence export --format zip --out <path>
brand-evidence mentions list [--status new]
brand-evidence mentions set-status <id> <status> [--note <text>]
brand-evidence schedule install|uninstall
brand-evidence backfill --file <csv> [--no-capture] [--no-history]
brand-evidence archive-history [--url <url>]
brand-evidence attach --file <path> --kind <kind> [--post-id <id>] [--note <text>]
brand-evidence sync-posts [--no-capture]
brand-evidence report [--json] [--hours 24]
brand-evidence projects list|add|remove
brand-evidence init
```

### Wiring the publish hook

Whatever publishes a post should call, right after the platform returns the URL:

```bash
brand-evidence hook --platform linkedin --url "$POST_URL" --body-file post.txt \
  --published-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --external-id "$POST_ID"
```

LinkedIn usually shows a login wall to anonymous visitors. The capture records whatever the
anonymous visitor sees and sets `capture_meta.login_wall_suspected`. Authenticated capture is
deliberately out of scope for v1.

## Trusted timestamps

Two independent time sources back the record without publishing anything private:

- `brand-evidence timestamp` (scheduled daily at 07:45) sends the SHA-256 of the chain head
  (`<seq>:<entry_hash>`) to an RFC 3161 Time Stamping Authority and stores the token as an
  artifact. This proves the whole chain up to that point existed by that time.
- `export` stamps `MANIFEST.txt` and ships `MANIFEST.tsr`. A third party verifies with
  `openssl ts -verify -data MANIFEST.txt -in MANIFEST.tsr -CAfile <tsa-chain.pem>`.

`BE_TSA_URL` defaults to freetsa.org; any RFC 3161 endpoint works (DigiCert, Sectigo).
`verify` also re-checks every stored token against the chain entry it anchors.

## The evidence log

Every write to `posts`, `mentions`, `artifacts` and `archive_snapshots` appends a row to
`evidence_log` with `entry_hash = sha256(prev_hash + occurred_at + event_type + canonical_json(payload))`.
SQLite triggers refuse UPDATE and DELETE on that table. `brand-evidence verify` recomputes the
whole chain and names the first broken `seq`. `export` produces a zip a third party can check
with `shasum -a 256 -c MANIFEST.txt` and a ten-line script described in its `README.txt`.

## Scheduling without a Mac

`.github/workflows/crawl.yml` runs `crawl --no-capture` and `digest` daily on GitHub Actions.
It covers discovery only; capture and archive submission need the local machine.

## Tests

```bash
uv run pytest            # passes with networking disabled; HTTP is mocked with respx
uv run ruff check .
```

The Playwright integration test is skipped when the Chromium binary is not installed.

## Not in v1

Legal or tax advice, auto-posting, auto-responses, authenticated capture, non-English sources,
a dashboard, multi-user or hosted anything. See the requirements document §2 and §14.

## License

AGPL-3.0-or-later. You may run, study, modify and share this; if you offer it to
others as a network service, that service's users are entitled to your changes
under the same terms. Copyright remains with the author, so a separate
commercial licence is possible - open an issue.
