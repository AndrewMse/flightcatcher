"""Discord bot: alerts with one-tap approval.

The approve button is the whole point of using a real bot rather than a
webhook. A Multipass window can open at 04:00 and the seat is contested, so the
gap between "the bot found it" and "a human said yes" has to be one tap on a
phone.

Approval is only ever *granted* here. The watcher holds the browser at the
confirm step and decides whether a decision is still valid; a tap that arrives
after the hold expired is refused, not honoured late.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .notify import describe
from .settings import DiscordSettings
from .store import Booking, Candidate, Store

log = logging.getLogger(__name__)
UTC = timezone.utc

try:
    import discord
    from discord import app_commands

    DISCORD_AVAILABLE = True
except ImportError:  # pragma: no cover
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]
    DISCORD_AVAILABLE = False


COLOUR_OK = 0x3BA55D
COLOUR_WARN = 0xFAA61A
COLOUR_BAD = 0xED4245
COLOUR_INFO = 0x5865F2


class DiscordUnavailable(RuntimeError):
    pass


def _countdown(target: datetime) -> str:
    seconds = int((target - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        return "now"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def candidate_lines(candidate: Candidate) -> str:
    lines = []
    for leg in candidate.legs:
        departs = leg["departs_local"][:16].replace("T", " ")
        lines.append(f"`{leg['origin']} → {leg['destination']}`  {departs}")
    return "\n".join(lines)


if DISCORD_AVAILABLE:

    class ApprovalView(discord.ui.View):
        """Approve / Skip buttons attached to one booking."""

        def __init__(
            self,
            booking_id: int,
            on_decision: Callable[[int, bool, str], bool],
            timeout: float,
            may_approve: Callable[[int], bool],
        ) -> None:
            super().__init__(timeout=max(10.0, timeout))
            self.booking_id = booking_id
            self.on_decision = on_decision
            self.may_approve = may_approve

        async def _decide(
            self, interaction: discord.Interaction, approved: bool
        ) -> None:
            # The button lives in a channel; whoever can see it can click it.
            # Only the configured approvers may actually spend anything.
            if not self.may_approve(interaction.user.id):
                await interaction.response.send_message(
                    "You are not on the approver list for this FlightCatcher.",
                    ephemeral=True,
                )
                return

            who = f"discord:{interaction.user}"
            accepted = self.on_decision(self.booking_id, approved, who)
            for child in self.children:
                child.disabled = True

            if not accepted:
                text = (
                    "Too late — this booking is no longer waiting. The held seat "
                    "expired or it was already decided."
                )
            elif approved:
                text = "Approved. Confirming now…"
            else:
                text = "Skipped. Nothing was booked."

            await interaction.response.edit_message(content=text, view=self)
            self.stop()

        @discord.ui.button(label="Approve & book", style=discord.ButtonStyle.success)
        async def approve(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            await self._decide(interaction, True)

        @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
        async def skip(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            await self._decide(interaction, False)


class DiscordNotifier:
    """Notifier backed by a Discord bot connection."""

    def __init__(
        self,
        settings: DiscordSettings,
        store: Store,
        on_decision: Callable[[int, bool, str], bool],
        status_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        if not DISCORD_AVAILABLE:
            raise DiscordUnavailable(
                "discord.py is not installed. Run: pip install 'flightcatcher[discord]'"
            )
        self.settings = settings
        self.store = store
        self.on_decision = on_decision
        self.status_provider = status_provider

        intents = discord.Intents.default()
        self.client = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.client)
        self._ready = asyncio.Event()
        self._register_events()
        self._register_commands()

    # --- lifecycle ----------------------------------------------------------

    def _register_events(self) -> None:
        @self.client.event
        async def on_ready() -> None:  # noqa: ANN202
            try:
                if self.settings.guild_id:
                    # Guild-scoped commands appear immediately; global ones can
                    # take up to an hour to propagate.
                    guild = discord.Object(id=self.settings.guild_id)
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                else:
                    await self.tree.sync()
            except Exception as exc:  # noqa: BLE001
                log.warning("slash command sync failed: %s", exc)
            self._ready.set()
            log.info("Discord connected as %s", self.client.user)
            self.store.log("discord", f"Connected as {self.client.user}")

    async def start(self) -> None:
        asyncio.create_task(self.client.start(self.settings.bot_token))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=60)
        except asyncio.TimeoutError:
            log.warning("Discord did not become ready within 60s; continuing")

    async def close(self) -> None:
        if not self.client.is_closed():
            await self.client.close()

    async def _channel(self):  # noqa: ANN202
        await self._ready.wait()
        channel = self.client.get_channel(self.settings.channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(self.settings.channel_id)
        return channel

    async def _send(self, **kwargs: Any) -> None:
        try:
            channel = await self._channel()
            await channel.send(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not send to Discord: %s", exc)

    # --- Notifier protocol --------------------------------------------------

    async def candidate_available(self, candidate: Candidate) -> None:
        embed = discord.Embed(
            title="Multipass seat available",
            description=candidate_lines(candidate),
            colour=COLOUR_OK,
        )
        embed.add_field(
            name="Trip",
            value=f"{'Direct' if candidate.stops == 0 else f'{candidate.stops} stop'} · "
            f"{candidate.total_minutes // 60}h{candidate.total_minutes % 60:02d}",
        )
        embed.add_field(
            name="Window closes in", value=_countdown(candidate.window_closes_utc)
        )
        embed.add_field(
            name="Cost",
            value=f"€{10 * len(candidate.legs)} · {len(candidate.legs)} trip credit(s)",
        )
        if candidate.stops:
            embed.add_field(
                name="⚠ Self-transfer",
                value="Separate bookings, no protection if the first leg is late.",
                inline=False,
            )
        await self._send(content=self.settings.mention or None, embed=embed)

    async def approval_needed(self, booking: Booking, candidate: Candidate) -> None:
        hold = booking.hold_expires_at
        embed = discord.Embed(
            title="Approve this booking?",
            description=candidate_lines(candidate),
            colour=COLOUR_WARN,
        )
        embed.add_field(
            name="Cost",
            value=f"€{booking.detail.get('cost_eur', 10 * len(candidate.legs)):.0f} · "
            f"{booking.detail.get('trip_credits', len(candidate.legs))} trip credit(s)",
        )
        if hold:
            embed.add_field(name="Decide within", value=_countdown(hold))
        if candidate.stops:
            embed.add_field(
                name="⚠ Self-transfer",
                value=f"{len(candidate.legs)} separate bookings confirmed back to back.",
                inline=False,
            )
        embed.set_footer(text=f"Booking #{booking.id} · prepared, not yet confirmed")

        timeout = 600.0
        if hold:
            timeout = max(10.0, (hold - datetime.now(UTC)).total_seconds())
        view = ApprovalView(
            booking.id, self.on_decision, timeout, self.settings.may_approve
        )

        files = []
        if booking.screenshot_path:
            try:
                files.append(discord.File(booking.screenshot_path))
            except Exception:  # noqa: BLE001
                pass

        await self._send(
            content=self.settings.mention or None,
            embed=embed,
            view=view,
            files=files or None,
        )

    async def booking_finished(self, booking: Booking, candidate: Candidate) -> None:
        colours = {"booked": COLOUR_OK, "failed": COLOUR_BAD}
        embed = discord.Embed(
            title=f"Booking {booking.status.replace('_', ' ')}",
            description=describe(candidate),
            colour=colours.get(booking.status, COLOUR_INFO),
        )
        if booking.confirmation:
            embed.add_field(name="Reference", value=booking.confirmation)
        if booking.error:
            embed.add_field(name="Detail", value=booking.error[:1000], inline=False)
        await self._send(embed=embed)

    async def problem(self, title: str, detail: str) -> None:
        embed = discord.Embed(title=title, description=detail[:3500], colour=COLOUR_BAD)
        await self._send(content=self.settings.mention or None, embed=embed)

    # --- slash commands -----------------------------------------------------

    def _register_commands(self) -> None:
        store = self.store

        @self.tree.command(name="status", description="FlightCatcher watcher status")
        async def status_cmd(interaction: discord.Interaction) -> None:  # noqa: ANN202
            data = self.status_provider() if self.status_provider else {}
            embed = discord.Embed(title="FlightCatcher", colour=COLOUR_INFO)
            session = data.get("session_ok")
            embed.add_field(
                name="Wizz session",
                value="ok" if session else ("unknown" if session is None else "NOT LOGGED IN"),
            )
            embed.add_field(
                name="Checks left this hour",
                value=str(data.get("checks_remaining_this_hour", "?")),
            )
            embed.add_field(name="Open bookings", value=str(data.get("open_bookings", 0)))
            embed.add_field(
                name="Last search", value=str(data.get("last_search_at") or "never")
            )
            if data.get("last_problem"):
                embed.add_field(
                    name="Last problem", value=str(data["last_problem"])[:1000], inline=False
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="wants", description="List active watches")
        async def wants_cmd(interaction: discord.Interaction) -> None:  # noqa: ANN202
            wants = store.list_wants(active_only=True)
            if not wants:
                await interaction.response.send_message(
                    "No active wants.", ephemeral=True
                )
                return
            lines = [
                f"**{w.name}** — {w.origin} → {w.destination}, "
                f"{w.date_from} to {w.date_to}, ≤{w.max_stops} stop(s)"
                + ("  · auto-book" if w.auto_request_booking else "")
                for w in wants
            ]
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(name="upcoming", description="Candidates unlocking soonest")
        async def upcoming_cmd(interaction: discord.Interaction) -> None:  # noqa: ANN202
            candidates = store.list_candidates(status="watching", limit=10)
            if not candidates:
                await interaction.response.send_message(
                    "Nothing being watched yet.", ephemeral=True
                )
                return
            lines = []
            for c in candidates[:10]:
                state = c.window_status()
                when = (
                    f"open, closes in {_countdown(c.window_closes_utc)}"
                    if state == "open"
                    else f"unlocks in {_countdown(c.window_opens_utc)}"
                )
                flag = " ⚠staggered" if c.staggered_hours > 0 else ""
                lines.append(f"`{' → '.join(c.path)}` — {when} — {c.availability}{flag}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
