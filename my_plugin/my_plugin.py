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
    """Simple ticket status and snippet commands."""

    def __init__(self, bot):
        self.bot = bot

        self._rename_tasks = {}
        self._rename_targets = {}
        self._rename_lock = asyncio.Lock()

        self._last_rename = 0.0
        self._rename_interval = 2.0

    @staticmethod
    def _remove_status_emoji(name):
        if name.startswith(RED):
            return name[len(RED) :]

        if name.startswith(GREEN):
            return name[len(GREEN) :]

        return name

    def _queue_rename(self, channel, emoji):
        channel_id = channel.id

        self._rename_targets[channel_id] = emoji

        existing_task = self._rename_tasks.get(channel_id)

        if existing_task is not None and not existing_task.done():
            return

        task = self.bot.loop.create_task(
            self._process_rename(channel)
        )

        self._rename_tasks[channel_id] = task

    async def _process_rename(self, channel):
        channel_id = channel.id

        try:
            await asyncio.sleep(0.25)

            while True:
                emoji = self._rename_targets.pop(
                    channel_id,
                    None,
                )

                if emoji is None:
                    return

                try:
                    base_name = self._remove_status_emoji(
                        channel.name
                    )

                    new_name = f"{emoji}{base_name}"

                    if channel.name == new_name:
                        continue

                    async with self._rename_lock:
                        elapsed = (
                            time.monotonic()
                            - self._last_rename
                        )

                        if elapsed < self._rename_interval:
                            await asyncio.sleep(
                                self._rename_interval - elapsed
                            )

                        emoji = self._rename_targets.pop(
                            channel_id,
                            emoji,
                        )

                        base_name = self._remove_status_emoji(
                            channel.name
                        )

                        new_name = f"{emoji}{base_name}"

                        if channel.name != new_name:
                            await channel.edit(
                                name=new_name,
                                reason="Ticket status update",
                            )

                        self._last_rename = time.monotonic()

                except discord.NotFound:
                    return

                except discord.Forbidden:
                    print(
                        "[TicketClaimSystem] "
                        f"No permission to rename channel {channel_id}."
                    )
                    return

                except discord.HTTPException as error:
                    print(
                        "[TicketClaimSystem] "
                        f"Failed to rename channel {channel_id}: "
                        f"{error!r}"
                    )
                    return

                if channel_id not in self._rename_targets:
                    return

                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            raise

        finally:
            current_task = self._rename_tasks.get(channel_id)

            if current_task is asyncio.current_task():
                self._rename_tasks.pop(
                    channel_id,
                    None,
                )

    async def _send_message(
        self,
        channel,
        title,
        body,
        colour,
    ):
        embed = discord.Embed(
            title=title,
            description=body,
            colour=colour,
        )

        try:
            await channel.send(
                embed=embed
            )

        except discord.NotFound:
            return

        except discord.Forbidden:
            return

        except discord.HTTPException as error:
            print(
                "[TicketClaimSystem] "
                f"Failed to send message in channel "
                f"{getattr(channel, 'id', 'unknown')}: "
                f"{error!r}"
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
            await self._send_message(
                ctx.channel,
                "Permission Denied",
                "You don't have permission to use this command.",
                RED_COLOUR,
            )
            return

        if isinstance(
            error,
            commands.MissingRequiredArgument,
        ):
            await self._send_message(
                ctx.channel,
                "Missing Argument",
                f"Missing required argument: `{error.param.name}`.",
                RED_COLOUR,
            )
            return

        if isinstance(
            error,
            commands.BadArgument,
        ):
            await self._send_message(
                ctx.channel,
                "Invalid Argument",
                str(error),
                RED_COLOUR,
            )
            return

        print(
            "[TicketClaimSystem] "
            f"Unexpected command error: {error!r}"
        )

        await self._send_message(
            ctx.channel,
            "Something Went Wrong",
            "An unexpected error occurred while handling "
            "this ticket. Please close this ticket and "
            "have the user open a new one if the issue "
            "continues.",
            RED_COLOUR,
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
                self._queue_rename(
                    channel,
                    RED,
                )

        except Exception as error:
            print(
                "[TicketClaimSystem] "
                f"Failed to set new ticket status: {error!r}"
            )

    async def _get_ticket_thread(self, ctx):
        try:
            thread = await self.bot.threads.find(
                channel=ctx.channel
            )

        except discord.NotFound:
            return None

        except discord.HTTPException as error:
            print(
                "[TicketClaimSystem] "
                f"Ticket lookup failed: {error!r}"
            )
            return None

        if thread is None:
            await self._send_message(
                ctx.channel,
                "Not a Ticket",
                "This isn't a ticket channel.",
                RED_COLOUR,
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
            await self._send_message(
                ctx.channel,
                "Snippet Not Found",
                f"The snippet `{snippet_name}` does not exist.",
                RED_COLOUR,
            )

            return False

        snippet = self.bot.snippets.get(
            resolved_name
        )

        if snippet is None:
            await self._send_message(
                ctx.channel,
                "Snippet Not Found",
                f"The snippet `{snippet_name}` does not exist.",
                RED_COLOUR,
            )

            return False

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
                await self._send_message(
                    ctx.channel,
                    "Snippet Failed",
                    "I couldn't send that snippet to the "
                    "recipient. The ticket has not been closed.",
                    RED_COLOUR,
                )

                return False

        except discord.NotFound:
            await self._send_message(
                ctx.channel,
                "Snippet Failed",
                "The ticket channel no longer exists.",
                RED_COLOUR,
            )

            return False

        except discord.Forbidden:
            await self._send_message(
                ctx.channel,
                "Snippet Failed",
                "I don't have permission to send the snippet.",
                RED_COLOUR,
            )

            return False

        except discord.HTTPException as error:
            print(
                "[TicketClaimSystem] "
                f"Failed to send snippet "
                f"{resolved_name!r}: {error!r}"
            )

            await self._send_message(
                ctx.channel,
                "Snippet Failed",
                "I couldn't send that snippet to the "
                "recipient. The ticket has not been closed.",
                RED_COLOUR,
            )

            return False

        except Exception as error:
            print(
                "[TicketClaimSystem] "
                f"Unexpected snippet error: {error!r}"
            )

            await self._send_message(
                ctx.channel,
                "Snippet Failed",
                "I couldn't send that snippet to the "
                "recipient. The ticket has not been closed.",
                RED_COLOUR,
            )

            return False

        return True

    @commands.command(name="opensnippet")
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

        thread = await self._get_ticket_thread(ctx)

        if thread is None:
            return

        await self._send_snippet(
            ctx,
            thread,
            snippet_name,
        )

    @commands.command(name="closesnippet")
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

        thread = await self._get_ticket_thread(ctx)

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

        except discord.NotFound:
            return

        except discord.Forbidden:
            await self._send_message(
                ctx.channel,
                "Snippet Sent",
                "The snippet was sent successfully, "
                "but I couldn't close the ticket.",
                GREEN_COLOUR,
            )

        except discord.HTTPException as error:
            print(
                "[TicketClaimSystem] "
                f"Failed to close ticket: {error!r}"
            )

            await self._send_message(
                ctx.channel,
                "Snippet Sent",
                "The snippet was sent successfully, "
                "but I couldn't close the ticket.",
                GREEN_COLOUR,
            )

        except Exception as error:
            print(
                "[TicketClaimSystem] "
                f"Unexpected close error: {error!r}"
            )

            await self._send_message(
                ctx.channel,
                "Snippet Sent",
                "The snippet was sent successfully, "
                "but I couldn't close the ticket.",
                GREEN_COLOUR,
            )

    @commands.command(name="claim")
    @checks.has_permissions(
        PermissionLevel.SUPPORTER
    )
    @commands.guild_only()
    async def claim(self, ctx):
        """Mark a ticket as claimed."""

        thread = await self._get_ticket_thread(ctx)

        if thread is None:
            return

        self._queue_rename(
            ctx.channel,
            GREEN,
        )

        await self._send_message(
            ctx.channel,
            "Ticket Claimed",
            f"{ctx.author.mention} has claimed this ticket.",
            GREEN_COLOUR,
        )

    @commands.command(name="unclaim")
    @checks.has_permissions(
        PermissionLevel.SUPPORTER
    )
    @commands.guild_only()
    async def unclaim(self, ctx):
        """Mark a ticket as unclaimed."""

        thread = await self._get_ticket_thread(ctx)

        if thread is None:
            return

        self._queue_rename(
            ctx.channel,
            RED,
        )

        await self._send_message(
            ctx.channel,
            "Ticket Unclaimed",
            "This ticket is now unclaimed and available to staff.",
            RED_COLOUR,
        )


async def setup(bot):
    await bot.add_cog(
        TicketClaimSystem(bot)
    )
