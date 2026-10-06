# Hopwatch

Finds and books Wizz Air itineraries on **Wizz Multipass** — including
self-transfer routings the airline will never sell you as one ticket, like
`OTP → WAW → EIN`.

Multipass lets you book any available seat for a flat €10, but only from 72
hours before departure. So the interesting question isn't "what's cheap", it's
**"what is about to become bookable, and when exactly"** — and then being awake
at that moment. This is the thing that's awake.

Under the hood: layer-2 searches run as jobs on a durable queue with
idempotent, crash-safe workers, either locally on SQLite or on AWS (SQS,
Lambda, DynamoDB, CloudWatch), with JSON logs, health alerts, CI/CD and a
benchmark suite. The numbers are under [Results](#results).

## What it does

```
┌── layer 1 ── route graph ────────── Wizz's whole network, 188 stations
│   layer 2 ── schedule feasibility ─ which paths fly, when they unlock
│   layer 3 ── Multipass availability ─ does a €10 seat actually exist (logged in)
└── layer 4 ── watch → alert → book ─ Discord push, one-tap approve
```

Layers 1–2 use Wizz's public endpoints and need no account. Layers 3–4 drive a
logged-in browser, which you seed by hand once.

## Architecture

```
          scheduler — every 15 min, one job per want, keyed by want + time slot
          local: inside `hopwatch serve`          AWS: EventBridge → planner Lambda
                                     │
                                     ▼
          job queue       local: SQLite + leases       AWS: SQS + dead-letter queue
                                     │
                                     ▼
          search workers  local: `hopwatch worker`     AWS: Lambda (arm64)
          (stateless)     ─── one shared Wizz rate limit, one shared timetable cache ───
                                     │
                                     ▼
          store           local: SQLite                AWS: DynamoDB (on-demand)
                                     ▲
                                     │ candidates whose window is open
   booker — one process, at home: Multipass checks · booking + approval · web UI · Discord
```

Two halves with different needs:

- **Searches** (layers 1–2) only read public endpoints and write results. They
  are stateless, so they go through a queue: any worker, any number of them,
  safely retried.
- **The booker** (layers 3–4) is deliberately *not* queued. A prepared booking
  is a live browser page parked at the confirm button, and an approval has to
  resolve against that exact page in that exact process. An approval on a queue
  could arrive late, or twice, and confirm something nobody meant to buy — the
  one thing this program must never do.

Everything above storage talks to four small interfaces — `Store`, `JobQueue`,
`RateLimiter`, `Cache` — each with a local and an AWS implementation, held to
the same contract tests. `backend.mode` picks one:

| | local | AWS |
|---|---|---|
| state | SQLite | DynamoDB, 8 on-demand tables |
| job queue | SQLite table with leases | SQS, dead-letter queue after 5 deliveries |
| search workers | inside `hopwatch serve`, plus `hopwatch worker` | Lambda on the SQS queue |
| scheduler | inside `hopwatch serve` | EventBridge rule → planner Lambda |
| shared rate limit and cache | the same SQLite file / disk | DynamoDB |
| booker | `hopwatch serve` at home | `hopwatch serve` at home |

## Results

Measured with `python -m bench.run` against a fake Wizz backend over real HTTP
and against moto for AWS; full tables and raw JSON are in
[bench/results](bench/results/latest.md). Wizz itself is never benchmarked. The
politeness limit is scaled from 1.5 s to 0.2 s so a run takes minutes; ratios
carry over, wall times don't.

**Recovery from worker failures.** Four workers on the SQLite backend, a
random one `SIGKILL`ed every 0.6 s, 4 s lease:

| kills | of which mid-job | jobs | lost | run twice | results vs an undisturbed run | recovery p50 / p95 |
|---:|---:|---:|---:|---:|---|---|
| 51 | 24 | 36 | 0 | 0 | identical, 965 of 965 itineraries | 4.2 s / 7.8 s |

Every job finished exactly once, and what it found matched a clean run
itinerary for itinerary. Recovery is the lease plus the redo — 4 s here; the
defaults are 120 s locally and 900 s on SQS. The same chaos on SQS and DynamoDB
(28 kills, 11 mid-job, 20 jobs): 0 lost, 0 run twice.

**Politeness under scale-out.** One sweep of 12 wants from a cold cache, the
shared limit (the real design) against each worker having its own (the naive
one), limit 5 req/s:

| workers | shared: sweep | shared: Wizz req/s | naive: sweep | naive: Wizz req/s (peak) |
|---:|---:|---:|---:|---:|
| 1 | 21.7 s | 5.0 | 22.1 s | 4.9 (5) |
| 2 | 22.1 s | 5.0 | 11.8 s | 9.7 (10) |
| 4 | 22.7 s | 5.0 | 7.1 s | 16.6 (20) |
| 8 | 23.3 s | 5.0 | 4.4 s | 28.8 (40) |

With the shared limit, eight workers put exactly the same load on Wizz as one.
The naive design is 5× faster precisely because it sends 6× the traffic, peaking
at 8× the limit — the pattern that gets a Multipass account noticed. Throughput
against Wizz is capped on purpose: extra workers buy isolation and crash
recovery, not speed. (The shared design's few extra calls are workers fetching
the route map at the same moment on start-up.)

**Pipeline throughput.** With every timetable cached, the SQLite backend
clears 128–149 jobs/s, flat from 1 to 8 workers: SQLite has one writer. Real
load is about one job per want per 15 minutes, four orders of magnitude below.

**Operating cost on AWS** (estimate). From counted API calls for 10 wants
swept every 15 minutes — 97 calls to Wizz, 668 DynamoDB write units and 209
read units per sweep — at eu-central-1 list prices:

| worker concurrency | Lambda GB-s / month | list price | with the always-free tier |
|---:|---:|---:|---:|
| 1 | 113,609 | $4.17 | $1.56 |
| 2 (deployed) | 218,369 | $5.57 | $1.56 |

What drives it: Lambda time is almost all waiting on the 1.5 s politeness
limit, so a second worker doubles compute for no extra throughput; it's kept so
one slow job can't stall the queue, and both fit in the free tier. DynamoDB
writes are the one cost the free tier doesn't cover, and most are `last_seen`
refreshes: every sweep re-touches each itinerary it already knows, about 570
here. Refreshing at most hourly instead of every 15 minutes would
remove three quarters of those writes — the next optimisation, now with a
number on it.

## Quick start

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[service,dev]'
.venv/bin/playwright install chromium
```

Try it without any of the booking machinery first:

```bash
.venv/bin/hopwatch search Bucharest Eindhoven --from today --to +6d
```

Then set up the service:

```bash
.venv/bin/hopwatch init-config
.venv/bin/hopwatch login
.venv/bin/hopwatch serve
```

`login` opens a real browser and waits while **you** sign in. Nothing in this
program reads, stores, or types your password. The session is saved to a
profile directory and reused headlessly afterwards.

The web UI is at <http://127.0.0.1:8765>. `serve` runs one search worker
itself; more can run alongside it with `hopwatch worker`, sharing the same
database, rate limit and cache.

## The interface

A local web UI (FastAPI + a no-build frontend) and a Discord bot, sharing one
process:

- **Watches** — standing "get me from A to B between these dates" requests.
- **Candidates** — every itinerary found, sorted by what unlocks next, with
  live countdowns and the staggered-window warning.
- **Waiting for you** — bookings prepared up to the confirm button, with a
  screenshot of exactly what's about to be bought.
- **Activity** — live event feed over SSE.

Discord gets the same alerts as push, with **Approve & book / Skip** buttons so
you can decide from your phone at 04:00. Slash commands: `/status`, `/wants`,
`/upcoming`. Setup is in [docs/discord-setup.md](docs/discord-setup.md).

Only Discord user IDs listed in `approver_ids` may press Approve — anyone who
can see the channel can see the button, so the list fails closed: empty means
nobody, not everybody.

## How booking works

Nothing is ever bought without you saying so, in that moment:

1. A candidate's window opens; an authenticated check finds a Multipass seat.
2. The bot drives the booking to the final confirm button and **stops**.
3. It screenshots that page and pings you on Discord and in the UI.
4. You tap Approve. Only then is confirm clicked.
5. If nobody answers within `approval_hold_min` (default 12 minutes), the hold
   lapses and the booking is abandoned.

An approval that arrives after the hold lapsed is **refused**, not honoured
late — the UI returns 409 and Discord says "too late". A stale tap must never
book a flight.

Two hard rules in the automation:

- **It never types payment credentials.** If the flow reaches a card number,
  CVC, or a payment iframe, it aborts and hands the session to you. Multipass
  shouldn't ask — and if it does, a human should be looking at the screen.
- **A multi-leg trip is fully prepared before any leg is confirmed**, so your
  approval covers the whole trip rather than committing you to leg 1 with leg 2
  still unknown.

Spending caps live in config: `max_open_bookings`, `max_bookings_per_day`.

## The staggered window problem

This is the thing the tool exists to get right.

Each leg is its **own** Multipass booking with its **own** 72-hour window. For a
connection departing the next morning, leg 2 unlocks up to a day after leg 1.
Buy leg 1 the moment it opens and you're holding a ticket to a connecting
airport with no guarantee of the onward seat.

So an itinerary is only safely committable at `max(departure − 72h)` across all
legs, not `min`. That's what the reported window opens at, and the gap is
flagged everywhere it's shown:

```
⚠ leg 1 unlocks 8h18 before the last leg does — its seats can sell out
  while you wait. Do not book leg 1 early.
```

A 1-stop trip also costs **two** trip credits and €20, and is a self-transfer:
separate bookings, no rebooking protection if leg 1 runs late.

## What layer 3 knows, and what it's guessing

Availability checking drives the logged-in booking page and reads the
`/Api/search/search` response the app fetches for itself, rather than scraping
fare cards. That's far more durable, and it yields real arrival times — which
the public timetable omits.

**The one genuinely unverified part** is the exact shape of a Multipass fare in
that response. I don't have a pass, so `MULTIPASS_MARKERS` in `multipass.py` is
an informed guess matched broadly across fare fields. Every check stores its raw
fare JSON, so the first real check tells you the truth:

```bash
.venv/bin/hopwatch probe --origin OTP --destination EIN --date 2026-09-20
```

That dumps the live API responses, selector hit counts, page HTML and a
screenshot. Correct `selectors.py` and `MULTIPASS_MARKERS` from what you see.
All site-specific strings are quarantined in `selectors.py` for exactly this
reason — when Wizz redesigns, that's the only file that should need editing.

The detection deliberately errs toward over-matching: a false positive surfaces
a booking for you to reject, a false negative silently loses the window.

## Staying logged in

The bot never stores or types your Wizz password. `hopwatch login` opens a
browser, you sign in yourself, and the session is reused after that. **Tick
"Remember me" when you log in** — without it Wizz issues a short-lived session
cookie and you'll be signing in constantly.

Three things keep that session alive:

- **Keepalive.** The watcher can go hours between authenticated checks, and an
  idle Wizz session expires in between — leaving it dead at the exact moment a
  booking window opens. A light touch every `keepalive_min` (default 20)
  prevents that.
- **Cookie snapshots.** Chromium only flushes cookies to the profile on a clean
  close, so a crash or `kill -9` loses a session that never actually expired.
  Cookies are snapshotted to `session-cookies.json` (mode 600 — a live session
  is a credential) and restored automatically if the profile comes up empty.
- **One loud alert.** When the session really has expired, you get one Discord
  message with the command to fix it, not one every twenty minutes.

If you're still being logged out constantly, check "Remember me" first, then
confirm the profile directory is writable and surviving restarts — a profile on
a tmpfs or inside a container without a volume loses everything on exit.

## Reliability

Delivery is at-least-once and workers die, so every step is safe to repeat. The
job record in the store, not the message, decides whether work happens.

- **Duplicate requests make one job.** A job's key is `search:{want}:{slot}`
  (`manual:{want}:{minute}` for "Sweep now"), claimed with a conditional insert
  before anything is sent. A scheduler firing twice, the booker and EventBridge
  both scheduling, a double-clicked button: one job.
- **Duplicate deliveries do no work.** A worker skips a job that is already
  done. Candidates are upserts on `(want, itinerary signature)`, so even a
  repeated sweep can't duplicate an itinerary — including two workers finding
  the same flight at the same moment.
- **Crashes become retries.** Claiming a job takes a lease
  (`queue.visibility_s`: 120 s locally, 900 s on AWS to match the SQS
  visibility timeout). If the worker dies, the lease lapses, the message comes
  back and another worker takes over. A live lease is never stolen: a second
  delivery while the first worker is still going is deferred, not run twice.
- **Transient and permanent failures are told apart.** A Wizz 5xx or 429 that
  outlasts the client's own retries, a timeout, a throttled write: retried
  after 30 s, 60 s, 120 s … up to 15 min. A deleted or paused want, or a place
  that can't be resolved: failed once, not retried.
- **Poison jobs are parked, not looped.** After 5 deliveries a job is
  dead-lettered (SQS's redrive policy; the SQLite queue does the same) and
  raises an alert.
- **Lost messages are re-sent.** A job recorded but never sent — a crash
  between the two — is sent again on the next tick. Harmless, because of all
  of the above.
- **One politeness budget.** Workers share one rate limit and one timetable
  cache, kept in the backend: each call reserves the next slot in a single
  transaction (SQLite) or conditional write (DynamoDB). Adding workers never
  adds traffic to Wizz.

Four health signals are checked by the booker every 5 minutes and alerted once
per incident on Discord; on AWS, CloudWatch alarms watch the same things:

| signal | fires when |
|---|---|
| dead letters | any job was dead-lettered |
| queue stalled | a job has waited more than twice the sweep interval — are workers running? |
| job failures | at least 4 jobs finished in the last hour and more than half did not succeed |
| booker silent (AWS) | no heartbeat from the booker for 15 minutes — nothing can be booked |

## Running at home

The booker always runs at home, or on any small box you control: it holds the
logged-in browser. Docker is the easy way — the Playwright base image carries
Chromium's system libraries, which are otherwise the fiddly part on a Pi.

```bash
cd deploy
docker compose up -d      # ghcr.io/andrewmse/hopwatch:latest, built by CI
```

The database, browser profile and screenshots live on the `./state` volume and
the config in `./config/config.toml`, so restarts lose nothing.

**Automatic updates.** CI publishes an image on every push to main.
`hopwatch-update.timer` pulls it every 15 minutes and restarts onto it **only
when the booker reports `safe_to_restart`** — nothing being prepared, held for
approval or confirmed. A restart between two confirmed legs is the one way an
update could strand someone, so a booker that is running but can't be asked is
left alone too. With the repository at `/opt/hopwatch`:

```bash
sudo cp deploy/hopwatch-update.service deploy/hopwatch-update.timer /etc/systemd/system/
sudo systemctl enable --now hopwatch-update.timer
```

Without Docker, `deploy/hopwatch.service` runs it from a virtualenv:

```bash
sudo cp deploy/hopwatch.service /etc/systemd/system/
sudo systemctl enable --now hopwatch
```

Two things to be careful with:

- **The browser profile is a credential.** It holds a logged-in session that can
  spend money. `StateDirectoryMode=0700`, and it's gitignored.
- **The web UI has no authentication** and binds to localhost. The approve
  endpoint spends money — put a reverse proxy with auth in front before exposing
  it anywhere.

Keep the Discord token in the environment (`HOPWATCH_DISCORD_TOKEN`), not
in the config file.

## Running on AWS

Nothing is deployed by default, and nothing costs money until you opt in.
`infra/` is a CDK app in Python, synthesized and checked by tests in CI on
every push.

It creates eight on-demand DynamoDB tables with TTLs, the `hopwatch-searches`
SQS queue and its dead-letter queue, two arm64 Lambdas (the planner every 15
minutes, the worker on the queue with reserved concurrency 2), log groups kept
14 days, five CloudWatch alarms to an SNS topic (optionally email), and a
`hopwatch-booker` IAM policy. There is no VPC, and so no NAT gateway, which
alone would cost more than everything else here combined.

One-time setup:

```bash
pip install -e '.[aws,infra]'
scripts/build_lambda.sh
cd infra
npx aws-cdk@2 bootstrap                      # once per account and region
npx aws-cdk@2 deploy HopwatchGithubOidc      # lets GitHub deploy without stored keys
```

Then set the repository variable `AWS_DEPLOY_ROLE_ARN` to that stack's
`DeployRoleArn` output (optionally `AWS_REGION` and `ALERT_EMAIL` too). From
then on every green CI run on main deploys `HopwatchStack`.

Point the booker at it with `HopwatchStack`'s outputs:

```toml
[backend]
mode = "aws"

[aws]
region = "eu-central-1"
queue_url = "https://sqs.eu-central-1.amazonaws.com/…/hopwatch-searches"
dlq_url = "https://sqs.eu-central-1.amazonaws.com/…/hopwatch-searches-dlq"
```

and give its machine credentials with the `hopwatch-booker` policy attached.
The stack publishes the policy but deliberately creates no IAM user or access
key: a long-lived key minted by a template would sit in CloudFormation's hands.

**Not yet verified: Wizz from AWS.** The workers call Wizz's public timetable
endpoint from AWS address space, and its WAF may treat datacenter traffic
differently from a home connection. If it does, keep the queue and tables on
AWS and run the workers at home instead: `hopwatch worker` with
`backend.mode = "aws"` consumes the same SQS queue.

## Operations

- **CI** — `.github/workflows/ci.yml`: tests on Python 3.11 and 3.12, including
  the moto-backed AWS tests and the CDK assertions; then the Lambda bundle and
  `cdk synth`; then, on main, the Docker image to GHCR.
- **CD** — `deploy-aws.yml` deploys the stack through GitHub's OIDC (skipped
  until `AWS_DEPLOY_ROLE_ARN` is set); the update timer deploys at home.
- **Logs** — `--log-format json` (or `HOPWATCH_LOG_FORMAT=json`, the default in
  Docker and on Lambda) writes one JSON object per line, carrying whatever is in
  scope: `job`, `attempt`, `worker`, `want_id`, `booking_id`. "Why did this
  sweep fail" is one filter on `job`.
- **Alerts** — the health signals above: Discord locally, CloudWatch alarms to
  SNS on AWS. The planner publishes the booker's heartbeat age as an
  embedded-format metric, so a booker that goes quiet at home still raises an
  alarm on AWS.
- **Looking inside** — `hopwatch status` shows queue depth; the web UI's status
  strip shows waiting, running and dead-lettered jobs; `/api/jobs`,
  `/api/search-runs` and `/api/health` have the detail. Every sweep records what
  it cost: Wizz calls, cache hits, routes, itineraries, new candidates.

## CLI

| Command | What it does |
|---|---|
| `search FROM TO` | Dated, timing-feasible itineraries |
| `routes FROM TO` | Route options, ignoring dates |
| `places QUERY` | Resolve a code, metro code, city or country |
| `login` | Open a browser and sign in yourself |
| `probe` | Dump what the live site serves, to calibrate selectors |
| `serve` | Run the booker, web UI, Discord bot and a search worker |
| `worker` | Run search workers only: no browser, web UI or Discord |
| `status` | Summarise stored state without starting anything |
| `init-config` | Write an example config file |
| `refresh` | Force-refresh cached API version and network |

Search flags: `--min-layover`, `--max-stops`, `--max-detour`, `--max-trip-hours`,
`--after-hour` / `--before-hour`, `--allow-ground-transfer`,
`--sort window|departure|duration`, `--bookable-now`, `--json`, `--offline`.
Global: `-v`, `--log-format text|json`.

## Notes on the Wizz backend

Findings that cost some time to work out:

- **The API version rotates.** Endpoints live at `be.wizzair.com/{version}/Api/…`
  and `{version}` changes every few weeks. It's scraped from the homepage and
  cached; a 404 forces a re-scrape.
- **There's an anti-forgery handshake.** The first response sets a
  `RequestVerificationToken` cookie; every later request must echo it in an
  `X-RequestVerificationToken` header or you get
  `400 {"handlerError":"InvalidProtocol"}`. One-shot `curl` never sees this;
  anything with a cookie jar breaks after exactly one request.
- **`timetable` takes one route per call.** A second `flightList` entry is read
  as the *return* leg, not a second route.
- **Metro codes leak through.** A query for `OTP` can return `BBU` results. The
  station in the *response* is the real one. Same for `WSW` (WAW/WMI), `LON`,
  `PAR`, `MIL`, `ROM`, `STO`.
- **`priceType: "checkPrice"` means `amount: 0`**, a placeholder, not a free
  flight. Fall back to `originalPrice`.
- **`search/search` is gated** — 429 to anything without a browser session.
  That's why layer 3 needs Playwright rather than more HTTP.

## Failure honesty

The recurring principle: *"I could not check this"* and *"there is nothing
here"* must never look alike to something that decides whether to spend money.

- A leg whose timetable can't be fetched produces `!! INCOMPLETE`, not an empty
  result.
- A search response with no flight-list key is an error to retry; one with an
  empty list is a real "nothing on sale".
- A partially-confirmed multi-leg booking is reported as **PARTIAL BOOKING**
  with an explicit "you may be stranded, act now", never as a plain failure.
- Availability that can't be classified is `unknown`, not `sold_out`.
- A sweep where some legs could not be checked finishes as `incomplete` and
  says which, rather than as a clean "nothing found".
- A job that keeps failing is dead-lettered and alerted on, never quietly
  dropped.

## Politeness

Unauthenticated requests are spaced 1.5 s apart **across every worker
combined** — the limit lives in the shared backend, not in each process —
capped at 3 concurrent per worker, and cached in a cache all workers share.
Authenticated checks are the scarce resource and run under a hard budget
(`max_checks_per_hour`, default 30, with a minimum gap), spent only on
candidates whose window is actually open.

Automated access is against Wizz's terms of service. The account attached to
your Multipass is what's at risk, so the defaults are deliberately slow.

## Upgrading from FlightCatcher

The project was renamed. On first start Hopwatch moves FlightCatcher-era files
it finds beside its default paths — the config, the database with its
`-wal`/`-shm`, the browser profile, screenshots and cookie backup — and never
overwrites anything. A config that names the old paths explicitly keeps using
them, and `FLIGHTCATCHER_*` environment variables still work, with a warning.

Under systemd, `ProtectSystem=strict` keeps the old state directory read-only,
so move it by hand before switching units: stop `flightcatcher`, move
`/var/lib/flightcatcher` to `/var/lib/hopwatch` and `/etc/flightcatcher` to
`/etc/hopwatch`, update any paths in `config.toml`, then start `hopwatch`.

## Tests

```bash
.venv/bin/pytest
```

343 tests, with no network, no browser and no AWS account:

- the route map is a synthetic fixture and HTTP goes through a mock transport;
- **contract tests** run one suite against both implementations of the store,
  queue, rate limit and cache — SQLite, and DynamoDB/SQS on moto — so the
  laptop and the cloud agree on every rule, concurrent claims and upserts
  included;
- the job runner is tested through every way a job can arrive: duplicated,
  crashed mid-flight, leased elsewhere, failing transiently or permanently,
  dead-lettered, malformed;
- the CDK stack is checked on its synthesized template: dead-letter redrive,
  alarms, log retention, no NAT gateway;
- the parts that need a real Multipass account are tested either side of the
  browser boundary.

The benchmarks are separate: `python -m bench.run` (see [bench/](bench/)).
