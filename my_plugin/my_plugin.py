import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


RED = "🔴"
GREEN = "🟢"
YELLOW = "🟡"


class TicketClaimSystem(commands.Cog):
    """Ticket claiming, subs, hold status, and claim-bypass roles."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)

    # ---------- storage helpers ----------

    async def _get_state(self, channel_id):
        doc = await self.db.find_one({"_id": f"ticket:{channel_id}"})
        if doc is None:
            doc = {
                "_id": f"ticket:{channel_id}",
                "claimed_by": None,
                "subs": [],
                "hold": False,
                "locked_entities": [],
            }
        return doc

    async def _save_state(self, channel_id, state):
        state["_id"] = f"ticket:{channel_id}"
        await self.db.replace_one({"_id": state["_id"]}, state, upsert=True)

    async def _get_config(self):
        doc = await self.db.find_one({"_id": "config"})
        if doc is None:
            doc = {"_id": "config", "bypass_roles": []}
        return doc

    async def _save_config(self, config):
        config["_id"] = "config"
        await self.db.replace_one({"_id": "config"}, config, upsert=True)

    async def _has_bypass(self, member):
        config = await self._get_config()
        member_role_ids = {r.id for r in member.roles}
        return any(rid in member_role_ids for rid in config["bypass_roles"])

    # ---------- channel name / emoji helpers ----------

    @staticmethod
    def _strip_emoji(name):
        for e in (RED, GREEN, YELLOW):
            if name.startswith(e):
                return name[len(e):]
        return name

    async def _set_status_emoji(self, channel, emoji):
        base = self._strip_emoji(channel.name)
        try:
            await channel.edit(name=f"{emoji}{base}")
        except discord.HTTPException:
            pass

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
                    await channel.set_permissions(target, send_messages=False)
                    kind = "role" if isinstance(target, discord.Role) else "member"
                    denied.append((kind, target.id))

        await channel.set_permissions(claimer, send_messages=True)
        return denied

    async def _unlock_channel(self, channel, state):
        for kind, entity_id in state.get("locked_entities", []):
            entity = (
                channel.guild.get_role(entity_id)
                if kind == "role"
                else channel.guild.get_member(entity_id)
            )
            if entity is not None:
                await channel.set_permissions(entity, overwrite=None)

        if state.get("claimed_by"):
            claimer = channel.guild.get_member(state["claimed_by"])
            if claimer:
                await channel.set_permissions(claimer, overwrite=None)

        for sub_id in state.get("subs", []):
            member = channel.guild.get_member(sub_id)
            if member:
                await channel.set_permissions(member, overwrite=None)

    # ---------- Components V2 announcement ----------

    async def _send_layout(self, channel, *, title, body):
        layout = discord.ui.LayoutView()
        container = discord.ui.Container(discord.ui.TextDisplay(f"## {title}\n{body}"))
        layout.add_item(container)
        await channel.send(view=layout)

    # ---------- new ticket -> red ----------

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        channel = thread.channel
        if channel and not channel.name.startswith((RED, GREEN, YELLOW)):
            await self._set_status_emoji(channel, RED)

    # ---------- commands ----------

    @commands.command(name="claim")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.guild_only()
    async def claim(self, ctx):
        thread = await self.bot.threads.find(channel=ctx.channel)
        if thread is None:
            return await ctx.send("This isn't a ticket channel.")

        state = await self._get_state(ctx.channel.id)
        if state["claimed_by"]:
            return await ctx.send(f"This ticket is already claimed by <@{state['claimed_by']}>.")

        locked = await self._lock_channel(ctx.channel, ctx.author)
        state.update(claimed_by=ctx.author.id, subs=[], hold=False, locked_entities=locked)
        await self._save_state(ctx.channel.id, state)

        await self._set_status_emoji(ctx.channel, GREEN)
        await self._send_layout(
            ctx.channel,
            title="Ticket Claimed by " + ctx.author.display_name,
            body=f"Ticket is now claimed by {ctx.author.mention}",
        )

    @commands.command(name="unclaim")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.guild_only()
    async def unclaim(self, ctx):
        thread = await self.bot.threads.find(channel=ctx.channel)
        if thread is None:
            return await ctx.send("This isn't a ticket channel.")

        state = await self._get_state(ctx.channel.id)
        if not state["claimed_by"]:
            return await ctx.send("This ticket isn't claimed.")

        if state["claimed_by"] != ctx.author.id and not await self._has_bypass(ctx.author):
            return await ctx.send("Only the person who claimed this ticket can unclaim it.")

        await self._unlock_channel(ctx.channel, state)
        state.update(claimed_by=None, subs=[], hold=False, locked_entities=[])
        await self._save_state(ctx.channel.id, state)

        await self._set_status_emoji(ctx.channel, RED)
        await self._send_layout(
            ctx.channel,
            title="Ticket Unclaimed",
            body="The ticket is now unclaimed and can be claimed",
        )

    @commands.command(name="sub")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.guild_only()
    async def sub(self, ctx, member: discord.Member):
        state = await self._get_state(ctx.channel.id)
        if not state["claimed_by"]:
            return await ctx.send("This ticket isn't claimed yet.")
        if ctx.author.id != state["claimed_by"] and not await self._has_bypass(ctx.author):
            return await ctx.send("Only the person who claimed this ticket can add subs.")
        if member.id in state["subs"]:
            return await ctx.send(f"{member.mention} is already subbed on this ticket.")

        await ctx.channel.set_permissions(member, send_messages=True)
        state["subs"].append(member.id)
        await self._save_state(ctx.channel.id, state)

        await self._send_layout(
            ctx.channel,
            title="Ticket Sub Success",
            body=f"{member.mention} is now subbed onto {ctx.channel.mention} ticket "
                 f"they can now respond to the user.",
        )

    @commands.command(name="unsub")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.guild_only()
    async def unsub(self, ctx, member: discord.Member):
        state = await self._get_state(ctx.channel.id)
        if member.id not in state["subs"]:
            return await ctx.send(f"{member.mention} isn't subbed on this ticket.")
        if ctx.author.id != state["claimed_by"] and not await self._has_bypass(ctx.author):
            return await ctx.send("Only the person who claimed this ticket can remove subs.")

        await ctx.channel.set_permissions(member, overwrite=None)
        state["subs"].remove(member.id)
        await self._save_state(ctx.channel.id, state)

        await self._send_layout(
            ctx.channel,
            title="Ticket Sub Remove",
            body=f"{member.mention} has been removed from sub permissions",
        )

    @commands.command(name="hold")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.guild_only()
    async def hold(self, ctx):
        state = await self._get_state(ctx.channel.id)
        if not state["claimed_by"]:
            return await ctx.send("This ticket needs to be claimed before it can go on hold.")

        state["hold"] = not state["hold"]
        await self._save_state(ctx.channel.id, state)

        if state["hold"]:
            await self._set_status_emoji(ctx.channel, YELLOW)
            await self._send_layout(ctx.channel, title="Ticket On Hold", body="This ticket has been put on hold.")
        else:
            await self._set_status_emoji(ctx.channel, GREEN)
            await self._send_layout(ctx.channel, title="Ticket Resumed", body="This ticket is no longer on hold.")

    @commands.command(name="claimbypass")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @commands.guild_only()
    async def claimbypass(self, ctx, role: discord.Role):
        config = await self._get_config()
        if role.id in config["bypass_roles"]:
            config["bypass_roles"].remove(role.id)
            await self._save_config(config)
            return await ctx.send(f"{role.mention} removed from claim-bypass roles.")

        config["bypass_roles"].append(role.id)
        await self._save_config(config)
        await ctx.send(
            f"{role.mention} added to claim-bypass roles — members with this role can always "
            f"talk in tickets and manage claims regardless of claim status."
        )


async def setup(bot):
    await bot.add_cog(TicketClaimSystem(bot))