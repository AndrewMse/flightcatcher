"""Command line interface for Hopwatch layers 1-2."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .client import WizzClient, WizzError
from .models import Itinerary
from .network import RouteNetwork
from .search import SearchOptions, search

_RELATIVE = re.compile(r"^([+-]?\d+)\s*([dw])$", re.I)


def parse_day(value: str, base: date | None = None) -> date:
    """Accept 2026-09-21, today, tomorrow, +3d, +2w."""
    base = base or date.today()
    token = value.strip().lower()
    if token in {"today", "now"}:
        return base
    if token == "tomorrow":
        return base + timedelta(days=1)
    match = _RELATIVE.match(token)
    if match:
        count, unit = int(match.group(1)), match.group(2).lower()
        return base + timedelta(days=count * (7 if unit == "w" else 1))
    try:
        return date.fromisoformat(token)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"cannot read {value!r} as a date (try 2026-09-21, tomorrow, or +3d)"
        )


def fmt_duration(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60}h{minutes % 60:02d}"


def fmt_countdown(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 0:
        return "now"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def load_network(client: WizzClient, refresh: bool = False) -> RouteNetwork:
    return RouteNetwork(client.route_map(refresh=refresh))


# --- rendering --------------------------------------------------------------


def render_itinerary(index: int, itinerary: Itinerary, net: RouteNetwork) -> str:
    window = itinerary.window
    assert window is not None

    head_bits = [
        " → ".join(itinerary.path),
        "direct" if itinerary.stops == 0 else f"{itinerary.stops} stop",
        fmt_duration(itinerary.total_minutes),
    ]
    for connection in itinerary.connections:
        label = f"{fmt_duration(connection.layover_min)} at {connection.from_station}"
        if connection.ground_transfer:
            label += f" → {connection.to_station} (ground transfer)"
        head_bits.append(label)

    lines = [f"[{index}] " + "  ·  ".join(head_bits)]

    for leg in itinerary.legs:
        arrival_ap = net.airport(leg.destination)
        from .timezones import tz_for

        arrives_local = leg.arrives_utc.astimezone(
            tz_for(leg.destination, arrival_ap.country_code if arrival_ap else None)
        )
        same_day = "" if arrives_local.date() == leg.departs_local.date() else " +1d"
        lines.append(
            f"      {leg.departs_local:%a %d %b}  "
            f"{leg.origin} {leg.departs_local:%H:%M} → "
            f"{leg.destination} {arrives_local:%H:%M}~{same_day}"
        )

    now = datetime.now(timezone.utc)
    opens_local = window.opens_utc.astimezone()
    closes_local = window.closes_utc.astimezone()

    if window.status == "open":
        state = f"BOOKABLE NOW, closes in {fmt_countdown(window.closes_utc - now)}"
    else:
        state = (
            f"opens {opens_local:%a %d %b %H:%M} local "
            f"(in {fmt_countdown(window.opens_utc - now)})"
        )
    lines.append(
        f"      Multipass: {state}, window ends {closes_local:%a %d %b %H:%M}"
    )

    cost = config.MULTIPASS_FARE_EUR * len(itinerary.legs)
    lines.append(
        f"      Cost: €{cost:.0f} over {len(itinerary.legs)} booking(s) "
        f"= {len(itinerary.legs)} trip credit(s)"
    )

    if window.staggered_hours > 0:
        lines.append(
            f"      ⚠ leg 1 unlocks {fmt_duration(int(window.staggered_hours * 60))} "
            f"before the last leg does — its seats can sell out while you wait. "
            f"Do not book leg 1 early."
        )
    if itinerary.has_ground_transfer:
        lines.append(
            "      ⚠ requires changing airports on the ground, with your own bags"
        )
    if itinerary.stops > 0:
        lines.append(
            "      ⚠ self-transfer: separate bookings, no protection if leg 1 is late"
        )

    return "\n".join(lines)


def itinerary_to_dict(itinerary: Itinerary) -> dict:
    window = itinerary.window
    return {
        "path": itinerary.path,
        "stops": itinerary.stops,
        "total_minutes": itinerary.total_minutes,
        "ground_transfer": itinerary.has_ground_transfer,
        "cost_eur": config.MULTIPASS_FARE_EUR * len(itinerary.legs),
        "trip_credits": len(itinerary.legs),
        "legs": [
            {
                "origin": leg.origin,
                "destination": leg.destination,
                "departs_local": leg.departs_local.isoformat(),
                "departs_utc": leg.departs_utc.isoformat(),
                "arrives_utc_estimated": leg.arrives_utc.isoformat(),
                "duration_min_estimated": leg.duration_min,
                "cash_price": leg.price_amount,
                "cash_currency": leg.price_currency,
            }
            for leg in itinerary.legs
        ],
        "connections": [
            {
                "layover_min": c.layover_min,
                "ground_transfer": c.ground_transfer,
                "from": c.from_station,
                "to": c.to_station,
            }
            for c in itinerary.connections
        ],
        "booking_window": None
        if window is None
        else {
            "status": window.status,
            "opens_utc": window.opens_utc.isoformat(),
            "closes_utc": window.closes_utc.isoformat(),
            "first_leg_opens_utc": window.first_leg_opens_utc.isoformat(),
            "staggered_hours": window.staggered_hours,
        },
    }


# --- commands ---------------------------------------------------------------


def cmd_places(args: argparse.Namespace, client: WizzClient) -> int:
    net = load_network(client)
    codes = net.resolve(args.query)
    if not codes:
        print(f"No station matches {args.query!r}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([itinerary_dict_place(net, c) for c in codes], indent=2))
        return 0
    print(f"{args.query!r} resolves to {len(codes)} station(s):")
    for code in codes:
        ap = net.airport(code)
        metro = f"  [metro {ap.mac}]" if ap.mac else ""
        print(f"  {code}  {ap.name} — {ap.country_name}{metro}")
    return 0


def itinerary_dict_place(net: RouteNetwork, code: str) -> dict:
    ap = net.airport(code)
    return {
        "iata": ap.iata,
        "name": ap.name,
        "country": ap.country_name,
        "country_code": ap.country_code,
        "mac": ap.mac,
    }


def cmd_routes(args: argparse.Namespace, client: WizzClient) -> int:
    net = load_network(client, refresh=args.refresh)
    origins = net.resolve(args.origin)
    dests = net.resolve(args.destination)
    if not origins or not dests:
        print("Could not resolve origin or destination", file=sys.stderr)
        return 1

    paths = net.find_paths(
        origins, dests, max_stops=args.max_stops, max_detour=args.max_detour
    )
    if args.json:
        print(json.dumps(
            [
                {"path": p, "distance_km": round(net.path_distance_km(p))}
                for p in paths
            ],
            indent=2,
        ))
        return 0

    print(f"{args.origin} ({', '.join(origins)}) → {args.destination} ({', '.join(dests)})")
    print(f"{len(paths)} route option(s), up to {args.max_stops} stop(s):\n")
    for path in paths:
        km = net.path_distance_km(path)
        kind = "direct" if len(path) == 2 else f"{len(path) - 2} stop"
        print(f"  {' → '.join(path):<24} {kind:<8} {km:>6.0f} km")
    if not paths:
        print("  (none — try --max-stops 2 or a wider --max-detour)")
    return 0


def cmd_search(args: argparse.Namespace, client: WizzClient) -> int:
    net = load_network(client, refresh=args.refresh)
    origins = net.resolve(args.origin)
    dests = net.resolve(args.destination)
    if not origins or not dests:
        print("Could not resolve origin or destination", file=sys.stderr)
        return 1

    opts = SearchOptions(
        date_from=args.date_from,
        date_to=args.date_to,
        max_stops=args.max_stops,
        min_layover_min=args.min_layover,
        max_layover_min=args.max_layover,
        max_detour=args.max_detour,
        max_trip_hours=args.max_trip_hours,
        allow_ground_transfer=args.allow_ground_transfer,
        only_bookable_now=args.bookable_now,
        include_closed=args.include_closed,
        earliest_departure_hour=args.after_hour,
        latest_departure_hour=args.before_hour,
        limit=args.limit,
        refresh=args.refresh,
        sort_by=args.sort,
    )

    result = search(client, net, origins, dests, opts)

    if args.json:
        print(json.dumps(
            {
                "query": {
                    "origins": origins,
                    "destinations": dests,
                    "date_from": opts.date_from.isoformat(),
                    "date_to": opts.date_to.isoformat(),
                },
                "stats": {
                    "paths_considered": result.paths_considered,
                    "routes_queried": result.routes_queried,
                    "departures_found": result.departures_found,
                    "complete": result.is_complete,
                    "failed_routes": [f"{a}-{b}" for a, b in result.failed_routes],
                },
                "itineraries": [itinerary_to_dict(i) for i in result.itineraries],
            },
            indent=2,
        ))
        return 0

    print(
        f"{args.origin} → {args.destination}, departing "
        f"{opts.date_from:%d %b} to {opts.date_to:%d %b}"
    )
    print(
        f"Checked {result.paths_considered} route option(s) across "
        f"{result.routes_queried} leg(s), {result.departures_found} departures."
    )
    if result.failed_routes:
        legs = ", ".join(f"{a}→{b}" for a, b in result.failed_routes)
        print(
            f"\n!! INCOMPLETE — {len(result.failed_routes)} leg(s) could not be "
            f"checked: {legs}\n"
            f"   Itineraries using them are missing from these results. Re-run before\n"
            f"   concluding a route does not exist."
        )
    print()

    if not result.itineraries:
        if result.failed_routes:
            print("No itineraries found, but the search was incomplete (see above).")
        else:
            print("No timing-feasible itineraries. Try widening the dates, raising")
            print("--max-stops, or lowering --min-layover.")
        return 0

    for index, itinerary in enumerate(result.itineraries, start=1):
        print(render_itinerary(index, itinerary, net))
        print()

    print(
        "Schedule feasibility only — this does not mean a Multipass seat exists.\n"
        "Arrival times marked ~ are estimated from distance; the API does not publish them."
    )
    return 0


def cmd_init_config(args: argparse.Namespace) -> int:
    from .settings import DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG

    path = Path(args.path).expanduser() if args.path else DEFAULT_CONFIG_PATH
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG)
    path.chmod(0o600)
    print(f"Wrote {path}")
    print("Fill in [passenger] before enabling automatic booking prep.")
    print("Prefer HOPWATCH_DISCORD_TOKEN in the environment over the config file.")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from .browser import BrowserUnavailable, interactive_login
    from .settings import Settings

    settings = Settings.load(Path(args.config).expanduser() if args.config else None)
    try:
        ok = asyncio.run(interactive_login(settings.browser))
    except BrowserUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0 if ok else 1


def cmd_probe(args: argparse.Namespace) -> int:
    from .browser import BrowserUnavailable, probe
    from .settings import Settings

    settings = Settings.load(Path(args.config).expanduser() if args.config else None)
    try:
        asyncio.run(
            probe(
                settings.browser,
                origin=args.origin,
                destination=args.destination,
                departure=args.date,
                out_dir=Path(args.out) if args.out else None,
            )
        )
    except BrowserUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .service import run_service
    from .settings import Settings

    # A long-running service that prints nothing on success is impossible to
    # trust. Unlike the one-shot commands, this one talks at INFO by default.
    if not args.verbose:
        logging.getLogger().setLevel(logging.INFO)
    # discord.py warns about missing voice codecs on every start. We never use
    # voice, and the noise buries the line that actually matters.
    logging.getLogger("discord.client").setLevel(logging.ERROR)

    settings = Settings.load(Path(args.config).expanduser() if args.config else None)
    if args.port:
        settings.web.port = args.port
    if args.host:
        settings.web.host = args.host
    if args.no_web:
        settings.web.enabled = False

    for problem in settings.describe_problems():
        print(f"warning: {problem}", file=sys.stderr)

    try:
        asyncio.run(run_service(settings))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Read-only peek at the database, without starting the service."""
    from .settings import Settings
    from .store import PENDING_APPROVAL, Store

    settings = Settings.load(Path(args.config).expanduser() if args.config else None)
    if not settings.database.exists():
        print(f"No database at {settings.database} — nothing has run yet.")
        return 0

    store = Store(settings.database)
    try:
        wants = store.list_wants()
        candidates = store.list_candidates(status="watching", limit=500)
        pending = store.list_bookings(statuses=[PENDING_APPROVAL])
        now = datetime.now(timezone.utc)
        open_now = [c for c in candidates if c.window_status(now) == "open"]

        print(f"Database: {settings.database}")
        print(f"Watches:  {sum(w.active for w in wants)} active / {len(wants)} total")
        print(f"Candidates: {len(candidates)} watched, {len(open_now)} inside their window")
        available = [c for c in open_now if c.availability == "available"]
        if available:
            print(f"\n{len(available)} with a Multipass seat right now:")
            for c in available[:10]:
                print(f"  {' → '.join(c.path)}  closes in "
                      f"{fmt_countdown(c.window_closes_utc - now)}")
        if pending:
            print(f"\n{len(pending)} booking(s) waiting for approval:")
            for b in pending:
                print(f"  #{b.id}  {b.summary}")
        for event in store.list_events(limit=args.events):
            print(f"  {event['ts'][11:16]}  {event['level']:<8} {event['message']}")
        return 0
    finally:
        store.close()


def cmd_refresh(args: argparse.Namespace, client: WizzClient) -> int:
    version = client.api_version(force=True)
    net = load_network(client, refresh=True)
    print(f"API version: {version}")
    print(f"Network: {len(net.stations)} stations, {len(net.edges)} routes")
    print(f"Cache: {client.cache.root}")
    return 0


# --- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hopwatch",
        description="Find Wizz Air itineraries bookable on Multipass, including self-transfers.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--offline", action="store_true", help="use cached data only")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    places = sub.add_parser("places", help="resolve a name or code to stations")
    places.add_argument("query")
    places.set_defaults(func=cmd_places)

    routes = sub.add_parser("routes", help="show route options ignoring dates")
    routes.add_argument("origin")
    routes.add_argument("destination")
    routes.add_argument("--max-stops", type=int, default=config.DEFAULT_MAX_STOPS)
    routes.add_argument("--max-detour", type=float, default=config.DEFAULT_MAX_DETOUR)
    routes.add_argument("--refresh", action="store_true")
    routes.set_defaults(func=cmd_routes)

    search_cmd = sub.add_parser("search", help="find dated, timing-feasible itineraries")
    search_cmd.add_argument("origin")
    search_cmd.add_argument("destination")
    search_cmd.add_argument("--from", dest="date_from", type=parse_day, default="today")
    search_cmd.add_argument("--to", dest="date_to", type=parse_day, default="+7d")
    search_cmd.add_argument("--max-stops", type=int, default=config.DEFAULT_MAX_STOPS)
    search_cmd.add_argument("--min-layover", type=int, default=config.DEFAULT_MIN_LAYOVER_MIN,
                            help="minutes; self-transfer means this is your only buffer")
    search_cmd.add_argument("--max-layover", type=int, default=config.DEFAULT_MAX_LAYOVER_MIN)
    search_cmd.add_argument("--max-detour", type=float, default=config.DEFAULT_MAX_DETOUR)
    search_cmd.add_argument("--max-trip-hours", type=float, default=config.DEFAULT_MAX_TRIP_HOURS)
    search_cmd.add_argument("--allow-ground-transfer", action="store_true",
                            help="permit connections that change airports within a city")
    search_cmd.add_argument("--bookable-now", action="store_true",
                            help="only itineraries whose 72h window is already open")
    search_cmd.add_argument("--include-closed", action="store_true")
    search_cmd.add_argument("--after-hour", type=int, default=None,
                            help="earliest first-leg departure hour, local")
    search_cmd.add_argument("--before-hour", type=int, default=None,
                            help="latest first-leg departure hour, local")
    search_cmd.add_argument("--sort", choices=["window", "departure", "duration"],
                            default="window",
                            help="window: what unlocks next (default); "
                                 "departure: what leaves soonest; duration: shortest trip")
    search_cmd.add_argument("--limit", type=int, default=50)
    search_cmd.add_argument("--refresh", action="store_true")
    search_cmd.set_defaults(func=cmd_search)

    refresh = sub.add_parser("refresh", help="force-refresh cached API version and network")
    refresh.set_defaults(func=cmd_refresh)

    # --- service commands (no WizzClient needed) ---------------------------

    init_cfg = sub.add_parser("init-config", help="write an example config file")
    init_cfg.add_argument("--path", default=None)
    init_cfg.add_argument("--force", action="store_true")
    init_cfg.set_defaults(func=cmd_init_config, standalone=True)

    login = sub.add_parser(
        "login", help="open a browser and sign in to Wizz Air yourself"
    )
    login.add_argument("--config", default=None)
    login.set_defaults(func=cmd_login, standalone=True)

    probe_cmd = sub.add_parser(
        "probe", help="dump what the live site serves, to calibrate selectors"
    )
    probe_cmd.add_argument("--config", default=None)
    probe_cmd.add_argument("--origin", default=None)
    probe_cmd.add_argument("--destination", default=None)
    probe_cmd.add_argument("--date", default=None, help="YYYY-MM-DD")
    probe_cmd.add_argument("--out", default=None)
    probe_cmd.set_defaults(func=cmd_probe, standalone=True)

    serve = sub.add_parser("serve", help="run the watcher, web UI and Discord bot")
    serve.add_argument("--config", default=None)
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--no-web", action="store_true")
    serve.set_defaults(func=cmd_serve, standalone=True)

    status = sub.add_parser("status", help="summarise stored state without running")
    status.add_argument("--config", default=None)
    status.add_argument("--events", type=int, default=10)
    status.set_defaults(func=cmd_status, standalone=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if isinstance(getattr(args, "date_from", None), str):
        args.date_from = parse_day(args.date_from)
    if isinstance(getattr(args, "date_to", None), str):
        args.date_to = parse_day(args.date_to)
    if getattr(args, "date_from", None) and args.date_to < args.date_from:
        print("--to is before --from", file=sys.stderr)
        return 2

    if getattr(args, "standalone", False):
        return args.func(args)

    try:
        with WizzClient(offline=args.offline) as client:
            return args.func(args, client)
    except WizzError as exc:
        print(f"Wizz Air backend error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
