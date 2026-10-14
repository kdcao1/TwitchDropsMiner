from __future__ import annotations

import asyncio
import logging
from time import time
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from twitch import Twitch
    from inventory import TimedDrop


logger = logging.getLogger("TwitchDrops")

API_URL = "https://discord.com/api/v10"
MIN_INTERVAL = 30  # seconds, to stay well clear of Discord's rate limits
STALL_SECONDS = 15 * 60  # no drop progress for this long while watching = stalled
COLOR_ACTIVE = 0x57F287
COLOR_IDLE = 0x95A5A6
COLOR_STALLED = 0xED4245


def _bar(progress: float, length: int = 14) -> str:
    filled = round(max(0.0, min(1.0, progress)) * length)
    return f"{'▰' * filled}{'▱' * (length - filled)} {progress:.1%}"


def _duration(minutes: int) -> str:
    hours, minutes = divmod(max(0, minutes), 60)
    return f"{hours}h {minutes:02}m" if hours else f"{minutes}m"


class DiscordStatus:
    """
    Posts a single status message to a Discord channel through a bot, and keeps editing
    that same message with the current mining progress. The message ID is stored in the
    settings, so restarting the miner continues editing the same message.
    A separate message is posted if the current drop stops progressing for too long.
    """

    def __init__(self, twitch: Twitch):
        self._twitch = twitch
        self._task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        # stall detection
        self._tracked: tuple[str, int] | None = None  # (drop id, current minutes)
        self._tracked_since: float = 0.0
        self._stall_alerted: bool = False
        # drops we already warned about not finishing in time
        self._deadline_alerted: set[str] = set()

    @property
    def configured(self) -> bool:
        settings = self._twitch.settings
        return bool(
            settings.discord_enabled
            and settings.discord_token.strip()
            and settings.discord_channel_id.strip()
        )

    def start(self) -> None:
        if self._task is None and self.configured:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def restart(self) -> None:
        await self.stop()
        self.start()

    # --- message building ---

    def _current_drop(self) -> TimedDrop | None:
        return self._twitch.gui.progress.current_drop

    def _priority_text(self, campaign: Any) -> str:
        settings = self._twitch.settings
        if campaign.id in settings.priority_campaigns:
            return "★ Manually prioritized"
        if campaign.game.name in settings.priority:
            return f"Game priority #{settings.priority.index(campaign.game.name) + 1}"
        return "Not prioritized"

    async def send_test(self) -> str:
        """Sends a one-off test message and returns a human readable result."""
        settings = self._twitch.settings
        if not settings.discord_token.strip() or not settings.discord_channel_id.strip():
            return "Enter a bot token and a channel ID first."
        try:
            status, data = await self._request(
                "POST",
                f"/channels/{settings.discord_channel_id.strip()}/messages",
                {"content": "✅ Twitch Drops Miner test message. Discord status is working."},
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return f"Could not reach Discord: {exc}"
        if status in (200, 201):
            return "Test message sent. Check the channel."
        hints = {
            401: "the bot token is invalid",
            403: "the bot lacks Send Messages permission in that channel",
            404: "the channel ID is wrong, or the bot is not in that server",
        }
        reason = hints.get(status, data.get("message", "unknown error"))
        return f"Test failed (HTTP {status}): {reason}."

    def _build_embed(self) -> dict[str, Any]:
        drop = self._current_drop()
        channel = self._twitch.watching_channel.get_with_default(None)
        embed: dict[str, Any] = {
            "title": "Twitch Drops Miner",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if drop is None or channel is None:
            embed["color"] = COLOR_IDLE
            embed["description"] = "Idle: not mining anything right now."
            return embed
        campaign = drop.campaign
        embed["color"] = COLOR_STALLED if self._stall_alerted else COLOR_ACTIVE
        embed["fields"] = [
            {"name": "Game", "value": campaign.game.name, "inline": True},
            {"name": "Campaign", "value": campaign.name, "inline": True},
            {
                "name": "Channel",
                "value": f"[{channel.name}]({channel.url})",
                "inline": True,
            },
            {"name": "Priority", "value": self._priority_text(campaign), "inline": True},
            {
                "name": f"Drop: {drop.rewards_text()}",
                "value": f"{_bar(drop.progress)}\n{_duration(drop.remaining_minutes)} remaining",
                "inline": False,
            },
            {
                "name": (
                    f"Campaign ({campaign.claimed_drops}/{campaign.total_drops} claimed)"
                ),
                "value": (
                    f"{_bar(campaign.progress)}\n"
                    f"{_duration(campaign.remaining_minutes)} remaining"
                ),
                "inline": False,
            },
        ]
        return embed

    # --- Discord API ---

    async def _request(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        settings = self._twitch.settings
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bot {settings.discord_token.strip()}"},
                timeout=aiohttp.ClientTimeout(total=20),
            )
        kwargs: dict[str, Any] = {}
        if settings.proxy:
            kwargs["proxy"] = settings.proxy
        async with self._session.request(
            method, f"{API_URL}{path}", json=payload, **kwargs
        ) as response:
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                data = {}
            return response.status, data if isinstance(data, dict) else {}

    async def _send_or_edit(self, embed: dict[str, Any]) -> bool:
        """Returns False if the configuration is unusable and the loop should stop."""
        settings = self._twitch.settings
        channel_id = settings.discord_channel_id.strip()
        payload = {"embeds": [embed]}
        message_id = settings.discord_message_id
        if message_id:
            status, data = await self._request(
                "PATCH", f"/channels/{channel_id}/messages/{message_id}", payload
            )
            if status == 404:
                # the message was deleted, post a new one
                settings.discord_message_id = message_id = ''
            else:
                return self._check_status(status, data)
        status, data = await self._request("POST", f"/channels/{channel_id}/messages", payload)
        if status in (200, 201) and "id" in data:
            settings.discord_message_id = str(data["id"])
            settings.save()
            return True
        return self._check_status(status, data)

    def _check_status(self, status: int, data: dict[str, Any]) -> bool:
        if status in (200, 201):
            return True
        if status == 429:
            self._rate_limited_for = float(data.get("retry_after", 5))
            return True
        if status in (401, 403, 404):
            # bad token, missing permissions or wrong channel: retrying will not help
            self._twitch.gui.print(
                f"Discord status disabled: HTTP {status} {data.get('message', '')}. "
                "Check the bot token, channel ID and the bot's permissions."
            )
            return False
        logger.warning(f"Discord status update failed: HTTP {status} {data}")
        return True

    _rate_limited_for: float = 0.0

    # --- stall detection ---

    async def _check_deadline(self) -> None:
        """Warns once per drop if it cannot finish before it (or its campaign) ends."""
        drop = self._current_drop()
        channel = self._twitch.watching_channel.get_with_default(None)
        if drop is None or channel is None or drop.id in self._deadline_alerted:
            return
        ends_at = min(drop.ends_at, drop.campaign.ends_at)
        minutes_left = (ends_at - datetime.now(timezone.utc)).total_seconds() / 60
        # watching earns one drop minute per real minute, so remaining > time left = too late
        if drop.remaining_minutes <= minutes_left:
            return
        self._deadline_alerted.add(drop.id)
        settings = self._twitch.settings
        await self._request(
            "POST",
            f"/channels/{settings.discord_channel_id.strip()}/messages",
            {
                "embeds": [{
                    "title": "⏰ Drop will not finish in time",
                    "color": COLOR_STALLED,
                    "description": (
                        f"**{drop.rewards_text()}** ({drop.campaign.game.name}: "
                        f"{drop.campaign.name}) needs {_duration(drop.remaining_minutes)} more "
                        f"but ends in {_duration(int(minutes_left))}."
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }],
            },
        )

    async def _check_stall(self) -> None:
        drop = self._current_drop()
        channel = self._twitch.watching_channel.get_with_default(None)
        if drop is None or channel is None:
            self._tracked = None
            self._stall_alerted = False
            return
        state = (drop.id, drop.current_minutes)
        now = time()
        if state != self._tracked:
            self._tracked = state
            self._tracked_since = now
            self._stall_alerted = False
            return
        if self._stall_alerted or now - self._tracked_since < STALL_SECONDS:
            return
        self._stall_alerted = True
        minutes = int((now - self._tracked_since) // 60)
        settings = self._twitch.settings
        await self._request(
            "POST",
            f"/channels/{settings.discord_channel_id.strip()}/messages",
            {
                "embeds": [{
                    "title": "⚠️ Drop progress stalled",
                    "color": COLOR_STALLED,
                    "description": (
                        f"No progress on **{drop.rewards_text()}** "
                        f"({drop.campaign.game.name}: {drop.campaign.name}) "
                        f"for {minutes} minutes while watching "
                        f"[{channel.name}]({channel.url})."
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }],
            },
        )

    async def _loop(self) -> None:
        while True:
            try:
                await self._check_stall()
                await self._check_deadline()
                if not await self._send_or_edit(self._build_embed()):
                    self._task = None  # configuration is unusable, stop for good
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(f"Discord status update failed: {exc}")
            interval = max(MIN_INTERVAL, int(self._twitch.settings.discord_interval))
            delay = max(interval, self._rate_limited_for)
            self._rate_limited_for = 0.0
            await asyncio.sleep(delay)
