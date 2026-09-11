import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

RED = "🔴"
GREEN = "🟢"
YELLOW = "🟡"

RED_COLOUR = discord.Colour.red()
GREEN_COLOUR = discord.Colour.green()
YELLOW_COLOUR = discord.Colour.gold()

class TicketClaimSystem(commands.Cog):
"""Ticket claiming, subs, hold status, no-claim lockdown, claim-bypass roles, and snippets."""


def __init__(self, bot):
    self.bot = bot
    self.db = bot.api.get_plugin_partition(self)

# ---------- storage helpers ----------

async def _get_state(self, channel_id):
    doc = await self.db.find_one({"_id": f"ticket:{channel_id}"})

    if doc is None:
        doc = {}

    doc.setdefault("_id", f"ticket:{channel_id}")
    doc.setdefault("claimed_by", None)
    doc.setdefault("subs", [])
    doc.setdefault("hold", False)
    doc.setdefault("noclaim", False)
    doc.setdefault("locked_entities", [])

    return doc

async def _save_state(self, channel_id, state):
    state["_id"] = f"ticket:{channel_id}"

    await self.db.replace_one(
        {"_id": state["_id"]},
        state,
        upsert=True,
    )

async def _get_config(self):
    doc = await self.db.find_one({"_id": "config"})

    if doc is None:
        doc = {}

    doc.setdefault("_id", "config")
    doc.setdefault("bypass_roles", [])

    return doc

async def _save_config(self, config):
    config["_id"] = "config"

    await self.db.replace_one(
        {"_id": "config"},
        config,
        upsert=True,
    )

async def _has_bypass(self, member):
    config = await self._get_config()

    member_role_ids = {role.id for role in member.roles}

    return any(
        role_id in member_role_ids
        for role_id in config["bypass_roles"]
    )

# ---------- channel name / emoji helpers ----------

@staticmethod
def _strip_emoji(name):
    for emoji in (RED, GREEN, YELLOW):
        if name.startswith(emoji):
            return name[len(emoji):]

    return name

async def _set_status_emoji(self, channel, emoji):
    base = self._strip_emoji(channel.name)
    new_name = f"{emoji}{base}"

    if channel.name == new_name:
        return

    try:
        await channel.edit(name=new_name)
    except discord.HTTPException:
        pass

def _queue_status_emoji(self, channel, emoji):
    self.bot.loop.create_task(
        self._set_status_emoji(channel, emoji)
    )

# ---------- locking / unlocking ----------

async def _lock_channel(self, channel, claimer):
    config = await self._get_config()

    bypass_role_ids = set(config["bypass_roles"])
    denied = []

    category = channel.category

    if category is not None:
        for target, overwrite in category.overwrites.items():
            if isinstance(target, discord.Role):
                if target.is_default() or target.id in bypass_role_ids:
                    continue

            if overwrite.send_messages:
                await channel.set_permissions(
                    target,
                    send_messages=False,
                )

                kind = (
                    "role"
                    if isinstance(target, discord.Role)
                    else "member"
                )

                denied.append((kind, target.id))

    await channel.set_permissions(
        claimer,
        send_messages=True,
    )

    return denied

async def _unlock_channel(self, channel, state):
    for kind, entity_id in state.get("locked_entities", []):
        entity = (
            channel.guild.get_role(entity_id)
            if kind == "role"
            else channel.guild.get_member(entity_id)
        )

        if entity is not None:
            await channel.set_permissions(
                entity,
                overwrite=None,
            )

    if state.get("claimed_by"):
        claimer = channel.guild.get_member(
            state["claimed_by"]
        )

        if claimer:
            await channel.set_permissions(
                claimer,
                overwrite=None,
            )

    for sub_id in state.get("subs", []):
        member = channel.guild.get_member(sub_id)

        if member:
            await channel.set_permissions(
                member,
                overwrite=None,
            )

# ---------- Components V2 announcements ----------

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
        await channel.send(view=layout)
    except discord.HTTPException:
        pass

async def _send_error(self, channel):
    await self._send_layout(
        channel,
        title="Something Went Wrong",
        body=(
            "An unexpected error occurred while handling this ticket. "
            "Please **close this ticket** and have the user open a new one "
            "if the issue continues."
        ),
        colour=RED_COLOUR,
    )

# ---------- global error handling ----------

async def cog_command_error(self, ctx, error):
    error = getattr(error, "original", error)

    if isinstance(error, commands.CheckFailure):
        return await self._send_layout(
            ctx.channel,
            title="Permission Denied",
            body="You don't have permission to use this command.",
            colour=RED_COLOUR,
        )

    if isinstance(error, commands.MissingRequiredArgument):
        return await self._send_layout(
            ctx.channel,
            title="Missing Argument",
            body=f"Missing required argument: {error.param.name}.",
            colour=RED_COLOUR,
        )

    if isinstance(error, commands.BadArgument):
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

    await self._send_error(ctx.channel)

# ---------- new ticket -> red ----------

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

        if channel and not channel.name.startswith(
            (RED, GREEN, YELLOW)
        ):
            self._queue_status_emoji(
                channel,
                RED,
            )

    except Exception as e:
        print(
            f"[TicketClaimSystem] on_thread_ready failed: {e!r}"
        )

# ---------- snippet helpers ----------

async def _get_ticket_thread(self, ctx):
    thread = await self.bot.threads.find(
        channel=ctx.channel
    )

    if thread is None:
        await self._send_layout(
            ctx.channel,
            title="Not a Ticket",
            body="This isn't a ticket channel.",
            colour=RED_COLOUR,
        )

        return None

    return thread

async def _send_snippet(self, ctx, thread, snippet_name):
    resolved_name = self.bot._resolve_snippet(
        snippet_name
    )

    if resolved_name is None:
        await self._send_layout(
            ctx.channel,
            title="Snippet Not Found",
            body=(
                f"The snippet `{snippet_name}` does not exist."
            ),
            colour=RED_COLOUR,
        )

        return False

    snippet = self.bot.snippets[resolved_name]

    try:
        await thread.reply(
            ctx.message,
            content=snippet,
        )

    except Exception as e:
        print(
            f"[TicketClaimSystem] Failed to send snippet "
            f"{resolved_name!r}: {e!r}"
        )

        await self._send_layout(
            ctx.channel,
            title="Snippet Failed",
            body=(
                "I couldn't send that snippet to the recipient. "
                "The ticket has **not** been closed."
            ),
            colour=RED_COLOUR,
        )

        return False

    return True

# ---------- opensnippet ----------

@commands.command(name="opensnippet")
@checks.has_permissions(PermissionLevel.SUPPORTER)
@commands.guild_only()
async def opensnippet(self, ctx, *, snippet_name: str):
    """Send a snippet to the recipient without closing the ticket."""

    thread = await self._get_ticket_thread(ctx)

    if thread is None:
        return

    await self._send_snippet(
        ctx,
        thread,
        snippet_name,
    )

# ---------- closesnippet ----------

@commands.command(name="closesnippet")
@checks.has_permissions(PermissionLevel.SUPPORTER)
@commands.guild_only()
async def closesnippet(self, ctx, *, snippet_name: str):
    """Send a snippet to the recipient and then close the ticket."""

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

    except Exception as e:
        print(
            f"[TicketClaimSystem] Failed to close ticket "
            f"after snippet {snippet_name!r}: {e!r}"
        )

        await self._send_layout(
            ctx.channel,
            title="Snippet Sent",
            body=(
                "The snippet was sent successfully, but I couldn't "
                "close the ticket automatically."
            ),
            colour=YELLOW_COLOUR,
        )

# ---------- claim ----------

@commands.command(name="claim")
@checks.has_permissions(PermissionLevel.SUPPORTER)
@commands.guild_only()
async def claim(self, ctx, target: discord.Member = None):
    thread = await self.bot.threads.find(
        channel=ctx.channel
    )

    if thread is None:
        return await self._send_layout(
            ctx.channel,
            title="Not a Ticket",
            body="This isn't a ticket channel.",
            colour=RED_COLOUR,
        )

    state = await self._get_state(
        ctx.channel.id
    )

    if state.get("claimed_by"):
        return await self._send_layout(
            ctx.channel,
            title="Already Claimed",
            body=(
                f"This ticket is already claimed by "
                f"<@{state['claimed_by']}>."
            ),
            colour=RED_COLOUR,
        )

    is_bypass = await self._has_bypass(
        ctx.author
    )

    if state.get("noclaim") and not is_bypass:
        return await self._send_layout(
            ctx.channel,
            title="Claim Restricted",
            body=(
                "🚫 This ticket is restricted — only a "
                "claim-bypass role holder can claim it."
            ),
            colour=RED_COLOUR,
        )

    if (
        target is not None
        and target.id != ctx.author.id
        and not is_bypass
    ):
        return await self._send_layout(
            ctx.channel,
            title="Permission Denied",
            body=(
                "Only a claim-bypass role holder can assign "
                "this ticket to someone else."
            ),
            colour=RED_COLOUR,
        )

    claimer = target or ctx.author
    lock_warning = None
    locked = []

    try:
        locked = await self._lock_channel(
            ctx.channel,
            claimer,
        )

    except discord.Forbidden:
        lock_warning = (
            "I don't have permission to fully lock this channel "
            "(check my Manage Roles permission and make sure my "
            "role sits above the staff role), but the ticket is "
            "still marked as claimed."
        )

    except discord.HTTPException as e:
        lock_warning = (
            f"Something went wrong locking the channel ({e}), "
            "but the ticket is still marked as claimed."
        )

    state.update(
        claimed_by=claimer.id,
        subs=[],
        hold=False,
        locked_entities=locked,
    )

    await self._save_state(
        ctx.channel.id,
        state,
    )

    self._queue_status_emoji(
        ctx.channel,
        GREEN,
    )

    await self._send_layout(
        ctx.channel,
        title="Ticket Claimed by " + claimer.display_name,
        body=f"Ticket is now claimed by {claimer.mention}",
        colour=GREEN_COLOUR,
    )

    if lock_warning:
        await self._send_layout(
            ctx.channel,
            title="Warning",
            body=f"⚠️ {lock_warning}",
            colour=YELLOW_COLOUR,
        )

# ---------- unclaim ----------

@commands.command(name="unclaim")
@checks.has_permissions(PermissionLevel.SUPPORTER)
@commands.guild_only()
async def unclaim(self, ctx, target: discord.Member = None):
    thread = await self.bot.threads.find(
        channel=ctx.channel
    )

    if thread is None:
        return await self._send_layout(
            ctx.channel,
            title="Not a Ticket",
            body="This isn't a ticket channel.",
            colour=RED_COLOUR,
        )

    state = await self._get_state(
        ctx.channel.id
    )

    if not state.get("claimed_by"):
        return await self._send_layout(
            ctx.channel,
            title="Not Claimed",
            body="This ticket isn't claimed.",
            colour=RED_COLOUR,
        )

    is_bypass = await self._has_bypass(
        ctx.author
    )

    if (
        state["claimed_by"] != ctx.author.id
        and not is_bypass
    ):
        return await self._send_layout(
            ctx.channel,
            title="Permission Denied",
            body=(
                "Only the person who claimed this ticket "
                "(or a claim-bypass role holder) can unclaim it."
            ),
            colour=RED_COLOUR,
        )

    if (
        target is not None
        and target.id != state["claimed_by"]
    ):
        return await self._send_layout(
            ctx.channel,
            title="Mismatch",
            body=(
                f"This ticket isn't claimed by "
                f"{target.mention}."
            ),
            colour=RED_COLOUR,
        )

    try:
        await self._unlock_channel(
            ctx.channel,
            state,
        )
    except discord.HTTPException:
        pass

    state.update(
        claimed_by=None,
        subs=[],
        hold=False,
        locked_entities=[],
    )

    await self._save_state(
        ctx.channel.id,
        state,
    )

    self._queue_status_emoji(
        ctx.channel,
        RED,
    )

    await self._send_layout(
        ctx.channel,
        title="Ticket Unclaimed",
        body="The ticket is now unclaimed and can be claimed",
        colour=RED_COLOUR,
    )

# ---------- subuser ----------

@commands.command(name="subuser")
@checks.has_permissions(PermissionLevel.SUPPORTER)
@commands.guild_only()
async def subuser(self, ctx, member: discord.Member):
    state = await self._get_state(
        ctx.channel.id
    )

    if not state.get("claimed_by"):
        return await self._send_layout(
            ctx.channel,
            title="Not Claimed",
            body="This ticket isn't claimed yet.",
            colour=RED_COLOUR,
        )

    is_bypass = await self._has_bypass(
        ctx.author
    )

    if (
        ctx.author.id != state["claimed_by"]
        and not is_bypass
    ):
        return await self._send_layout(
            ctx.channel,
            title="Permission Denied",
            body=(
                "Only the person who claimed this ticket "
                "(or a claim-bypass role holder) can add subs."
            ),
            colour=RED_COLOUR,
        )

    if member.id in state.get("subs", []):
        return await self._send_layout(
            ctx.channel,
            title="Already Subbed",
            body=(
                f"{member.mention} is already subbed "
                "on this ticket."
            ),
            colour=RED_COLOUR,
        )

    await ctx.channel.set_permissions(
        member,
        send_messages=True,
    )

    state.setdefault(
        "subs",
        []
    ).append(member.id)

    await self._save_state(
        ctx.channel.id,
        state,
    )

    await self._send_layout(
        ctx.channel,
        title="Ticket Sub Success",
        body=(
            f"{member.mention} is now subbed onto "
            f"{ctx.channel.mention} ticket "
            "they can now respond to the user."
        ),
        colour=GREEN_COLOUR,
    )

# ---------- unsubuser ----------

@commands.command(name="unsubuser")
@checks.has_permissions(PermissionLevel.SUPPORTER)
@commands.guild_only()
async def unsubuser(self, ctx, member: discord.Member):
    state = await self._get_state(
        ctx.channel.id
    )

    if member.id not in state.get("subs", []):
        return await self._send_layout(
            ctx.channel,
            title="Not Subbed",
            body=(
                f"{member.mention} isn't subbed "
                "on this ticket."
            ),
            colour=RED_COLOUR,
        )

    is_bypass = await self._has_bypass(
        ctx.author
    )

    if (
        ctx.author.id != state["claimed_by"]
        and not is_bypass
    ):
        return await self._send_layout(
            ctx.channel,
            title="Permission Denied",
            body=(
                "Only the person who claimed this ticket "
                "(or a claim-bypass role holder) can remove subs."
            ),
            colour=RED_COLOUR,
        )

    await ctx.channel.set_permissions(
        member,
        overwrite=None,
    )

    state["subs"].remove(member.id)

    await self._save_state(
        ctx.channel.id,
        state,
    )

    await self._send_layout(
        ctx.channel,
        title="Ticket Sub Removed",
        body=(
            f"{member.mention} has been removed "
            "from sub permissions"
        ),
        colour=RED_COLOUR,
    )

# ---------- hold ----------

@commands.command(name="hold")
@checks.has_permissions(PermissionLevel.SUPPORTER)
@commands.guild_only()
async def hold(self, ctx):
    state = await self._get_state(
        ctx.channel.id
    )

    if not state.get("claimed_by"):
        return await self._send_layout(
            ctx.channel,
            title="Not Claimed",
            body=(
                "This ticket needs to be claimed "
                "before it can go on hold."
            ),
            colour=RED_COLOUR,
        )

    is_bypass = await self._has_bypass(
        ctx.author
    )

    if (
        ctx.author.id != state["claimed_by"]
        and not is_bypass
    ):
        return await self._send_layout(
            ctx.channel,
            title="Permission Denied",
            body=(
                "Only the person who claimed this ticket "
                "(or a claim-bypass role holder) can toggle hold."
            ),
            colour=RED_COLOUR,
        )

    state["hold"] = not state.get(
        "hold",
        False,
    )

    await self._save_state(
        ctx.channel.id,
        state,
    )

    if state["hold"]:
        self._queue_status_emoji(
            ctx.channel,
            YELLOW,
        )

        await self._send_layout(
            ctx.channel,
            title="Ticket On Hold",
            body="This ticket has been put on hold.",
            colour=YELLOW_COLOUR,
        )

    else:
        self._queue_status_emoji(
            ctx.channel,
            GREEN,
        )

        await self._send_layout(
            ctx.channel,
            title="Ticket Resumed",
            body="This ticket is no longer on hold.",
            colour=GREEN_COLOUR,
        )

# ---------- noclaim ----------

@commands.command(name="noclaim")
@checks.has_permissions(PermissionLevel.SUPPORTER)
@commands.guild_only()
async def noclaim(self, ctx):
    state = await self._get_state(
        ctx.channel.id
    )

    state["noclaim"] = not state.get(
        "noclaim",
        False,
    )

    await self._save_state(
        ctx.channel.id,
        state,
    )

    if state["noclaim"]:
        await self._send_layout(
            ctx.channel,
            title="Claim Restricted",
            body=(
                "🚫 This ticket can now only be claimed "
                "by users with a claim-bypass role."
            ),
            colour=YELLOW_COLOUR,
        )

    else:
        await self._send_layout(
            ctx.channel,
            title="Claim Restriction Lifted",
            body=(
                "This ticket can now be claimed by "
                "any staff member again."
            ),
            colour=GREEN_COLOUR,
        )

# ---------- claim bypass ----------

@commands.command(name="claimbypass")
@checks.has_permissions(PermissionLevel.ADMINISTRATOR)
@commands.guild_only()
async def claimbypass(self, ctx, role: discord.Role):
    config = await self._get_config()

    if role.id in config["bypass_roles"]:
        config["bypass_roles"].remove(
            role.id
        )

        await self._save_config(
            config
        )

        return await self._send_layout(
            ctx.channel,
            title="Bypass Role Removed",
            body=(
                f"{role.mention} removed from "
                "claim-bypass roles."
            ),
            colour=RED_COLOUR,
        )

    config["bypass_roles"].append(
        role.id
    )

    await self._save_config(
        config
    )

    await self._send_layout(
        ctx.channel,
        title="Bypass Role Added",
        body=(
            f"{role.mention} added to claim-bypass roles — "
            "members with this role have full ownership of "
            "every ticket (claim, unclaim, sub, unsub) "
            "regardless of who claimed it."
        ),
        colour=GREEN_COLOUR,
    )


async def setup(bot):
await bot.add_cog(
TicketClaimSystem(bot)
)
