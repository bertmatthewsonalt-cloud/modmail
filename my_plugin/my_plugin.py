import asyncio
import time

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


RED = "🔴"
GREEN = "🟢"

RED_COLOUR = discord.Colour.red()
GREEN_COLOUR = discord.Colour.green()


class TicketClaimSystem(commands.Cog):
    """Ticket status, claiming, and snippet commands."""

    def __init__(self, bot):
        self.bot = bot

        self._status_tasks = {}
        self._status_targets = {}
        self._status_lock = asyncio.Lock()

        self._last_status_edit = 0.0
        self._status_min_interval = 2.0

    @staticmethod
    def _strip_emoji(name):
        for emoji in (RED, GREEN):
            if name.startswith(emoji):
                return name[len(emoji):]

        return name

    def _queue_status_emoji(self, channel, emoji):
        channel_id = channel.id

        self._status_targets[channel_id] = emoji

        existing_task = self._status_tasks.get(channel_id)

        if existing_task is not None and not existing_task.done():
            return

        task = self.bot.loop.create_task(
            self._process_status_emoji(channel)
        )

        self._status_tasks[channel_id] = task

    async def _process_status_emoji(self, channel):
        channel_id = channel.id

        try:
            await asyncio.sleep(0.25)

            while True:
                emoji = self._status_targets.pop(
                    channel_id,
                    None,
                )

                if emoji is None:
                    return

                try:
                    base = self._strip_emoji(
                        channel.name
                    )

                    new_name = f"{emoji}{base}"

                    if channel.name == new_name:
                        if channel_id not in self._status_targets:
                            return

                        continue

                    async with self._status_lock:
                        elapsed = (
                            time.monotonic()
                            - self._last_status_edit
                        )

                        if elapsed < self._status_min_interval:
                            await asyncio.sleep(
                                self._status_min_interval
                                - elapsed
                            )

                        emoji = self._status_targets.pop(
                            channel_id,
                            emoji,
                        )

                        base = self._strip_emoji(
                            channel.name
                        )

                        new_name = f"{emoji}{base}"

                        if channel.name != new_name:
                            await channel.edit(
                                name=new_name
                            )

                        self._last_status_edit = (
                            time.monotonic()
                        )

                except discord.NotFound:
                    return

                except discord.Forbidden:
                    print(
                        f"[TicketClaimSystem] No permission to "
                        f"rename channel {channel_id}."
                    )
                    return

                except discord.HTTPException as e:
                    print(
                        f"[TicketClaimSystem] Failed to rename "
                        f"channel {channel_id}: {e!r}"
                    )
                    return

                if channel_id not in self._status_targets:
                    return

                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            raise

        finally:
            current_task = self._status_tasks.get(
                channel_id
            )

            if current_task is asyncio.current_task():
                self._status_tasks.pop(
                    channel_id,
                    None,
                )

    async def _send_layout(
        self,
        channel,
        *,
        title,
        body,
        colour=None,
    ):
        layout = discord.ui.LayoutView()

        container = discord.ui.Container(
            discord.ui.TextDisplay(
                f"## {title}\n{body}"
            ),
            accent_colour=colour,
        )

        layout.add_item(container)

        try:
            await channel.send(
                view=layout
            )

        except discord.NotFound:
            return

        except discord.Forbidden:
            return

        except discord.HTTPException as e:
            print(
                f"[TicketClaimSystem] Failed to send layout "
                f"in channel "
                f"{getattr(channel, 'id', 'unknown')}: "
                f"{e!r}"
            )

    async def _send_error(self, channel):
        await self._send_layout(
            channel,
            title="Something Went Wrong",
            body=(
                "An unexpected error occurred while handling "
                "this ticket. Please **close this ticket** and "
                "have the user open a new one if the issue "
                "continues."
            ),
            colour=RED_COLOUR,
        )

    async def cog_command_error(self, ctx, error):
        error = getattr(
            error,
            "original",
            error,
        )

        if isinstance(
            error,
            commands.CheckFailure,
        ):
            return await self._send_layout(
                ctx.channel,
                title="Permission Denied",
                body=(
                    "You don't have permission to use "
                    "this command."
                ),
                colour=RED_COLOUR,
            )

        if isinstance(
            error,
            commands.MissingRequiredArgument,
        ):
            return await self._send_layout(
                ctx.channel,
                title="Missing Argument",
                body=(
                    f"Missing required argument: "
                    f"{error.param.name}."
                ),
                colour=RED_COLOUR,
            )

        if isinstance(
            error,
            commands.BadArgument,
        ):
            return await self._send_layout(
                ctx.channel,
                title="Invalid Argument",
                body=str(error),
                colour=RED_COLOUR,
            )

        print(
            f"[TicketClaimSystem] Unexpected error in "
            f"{ctx.command}: {error!r}"
        )

        await self._send_error(
            ctx.channel
        )

    @commands.Cog.listener()
    async def on_thread_ready(
        self,
        thread,
        creator,
        category,
        initial_message,
    ):
        try:
            channel = thread.channel

            if channel is not None:
                self._queue_status_emoji(
                    channel,
                    RED,
                )

        except Exception as e:
            print(
                f"[TicketClaimSystem] on_thread_ready failed: "
                f"{e!r}"
            )

    async def _get_ticket_thread(self, ctx):
        try:
            thread = await self.bot.threads.find(
                channel=ctx.channel
            )

        except discord.NotFound:
            return None

        except discord.HTTPException as e:
            print(
                f"[TicketClaimSystem] Ticket lookup failed: "
                f"{e!r}"
            )
            return None

        if thread is None:
            await self._send_layout(
                ctx.channel,
                title="Not a Ticket",
                body="This isn't a ticket channel.",
                colour=RED_COLOUR,
            )

            return None

        return thread

    async def _send_snippet(
        self,
        ctx,
        thread,
        snippet_name,
    ):
        resolved_name = self.bot._resolve_snippet(
            snippet_name
        )

        if resolved_name is None:
            await self._send_layout(
                ctx.channel,
                title="Snippet Not Found",
                body=(
                    f"The snippet `{snippet_name}` "
                    f"does not exist."
                ),
                colour=RED_COLOUR,
            )

            return False

        snippet = self.bot.snippets[
            resolved_name
        ]

        try:
            result = await thread.reply(
                ctx.message,
                content=snippet,
            )

            if (
                isinstance(result, tuple)
                and len(result) > 0
                and result[0] is None
            ):
                await self._send_layout(
                    ctx.channel,
                    title="Snippet Failed",
                    body=(
                        "I couldn't send that snippet to the "
                        "recipient. The ticket has **not** "
                        "been closed."
                    ),
                    colour=RED_COLOUR,
                )

                return False

        except Exception as e:
            print(
                f"[TicketClaimSystem] Failed to send snippet "
                f"{resolved_name!r}: {e!r}"
            )

            await self._send_layout(
                ctx.channel,
                title="Snippet Failed",
                body=(
                    "I couldn't send that snippet to the "
                    "recipient. The ticket has **not** been "
                    "closed."
                ),
                colour=RED_COLOUR,
            )

            return False

        return True

    @commands.command(
        name="opensnippet"
    )
    @checks.has_permissions(
        PermissionLevel.SUPPORTER
    )
    @commands.guild_only()
    async def opensnippet(
        self,
        ctx,
        *,
        snippet_name: str,
    ):
        """Send a snippet without closing the ticket."""

        thread = await self._get_ticket_thread(
            ctx
        )

        if thread is None:
            return

        await self._send_snippet(
            ctx,
            thread,
            snippet_name,
        )

    @commands.command(
        name="closesnippet"
    )
    @checks.has_permissions(
        PermissionLevel.SUPPORTER
    )
    @commands.guild_only()
    async def closesnippet(
        self,
        ctx,
        *,
        snippet_name: str,
    ):
        """Send a snippet and then close the ticket."""

        thread = await self._get_ticket_thread(
            ctx
        )

        if thread is None:
            return

        sent = await self._send_snippet(
            ctx,
            thread,
            snippet_name,
        )

        if not sent:
            return

        try:
            await thread.close(
                closer=ctx.author,
                silent=True,
            )

        except Exception as e:
            print(
                f"[TicketClaimSystem] Failed to close ticket "
                f"after snippet {snippet_name!r}: {e!r}"
            )

            await self._send_layout(
                ctx.channel,
                title="Snippet Sent",
                body=(
                    "The snippet was sent successfully, but "
                    "I couldn't close the ticket automatically."
                ),
                colour=GREEN_COLOUR,
            )

    @commands.command(
        name="claim"
    )
    @checks.has_permissions(
        PermissionLevel.SUPPORTER
    )
    @commands.guild_only()
    async def claim(self, ctx):
        """Mark the ticket as claimed and turn it green."""

        thread = await self._get_ticket_thread(
            ctx
        )

        if thread is None:
            return

        self._queue_status_emoji(
            ctx.channel,
            GREEN,
        )

        await self._send_layout(
            ctx.channel,
            title="Ticket Claimed",
            body=(
                f"{ctx.author.mention} has claimed "
                f"this ticket."
            ),
            colour=GREEN_COLOUR,
        )

    @commands.command(
        name="unclaim"
    )
    @checks.has_permissions(
        PermissionLevel.SUPPORTER
    )
    @commands.guild_only()
    async def unclaim(self, ctx):
        """Mark the ticket as unclaimed and turn it red."""

        thread = await self._get_ticket_thread(
            ctx
        )

        if thread is None:
            return

        self._queue_status_emoji(
            ctx.channel,
            RED,
        )

        await self._send_layout(
            ctx.channel,
            title="Ticket Unclaimed",
            body=(
                "This ticket is now unclaimed and "
                "available to staff."
            ),
            colour=RED_COLOUR,
        )


async def setup(bot):
    await bot.add_cog(
        TicketClaimSystem(bot)
    )