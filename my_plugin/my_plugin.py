import discord
from discord.ext import commands


class AIRelay(commands.Cog):
    """
    Modmail ignores command messages sent by other bot accounts by default
    (this lives in discord.py's process_commands, which bails out early on
    message.author.bot). That's why a bot-sent "?r ..." or "?aiconnect" just
    sits in the channel doing nothing.

    This cog re-dispatches messages from ONE trusted bot (your automation
    bot) through Modmail's normal get_context/invoke pipeline directly,
    instead of going through process_commands. That skips the bot-author
    check while still running every normal permission check a command has
    (checks.has_permissions, checks.thread_only, etc.) exactly as if a human
    staff member had typed it.

    IMPORTANT: for commands like ?reply/?r to actually succeed, the trusted
    bot's Discord account still needs whatever permission level Modmail
    requires for that command (e.g. SUPPORTER). Grant that once via
    Modmail's own permission command, e.g.:
        ?permissions add level <automation_bot_user_id> SUPPORTER
    """

    # Replace with your automation bot's actual Discord user (client) ID.
    TRUSTED_BOT_ID = 1506346483191119982  # <-- SET THIS

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Only ever act on the one trusted automation bot.
        if message.author.id != self.TRUSTED_BOT_ID:
            return

        # Never act on Modmail's own messages (paranoia guard against loops).
        if message.author.id == self.bot.user.id:
            return

        content = message.content or ""
        prefix = self.bot.prefix  # Modmail's configured command prefix, usually "?"
        if not content.startswith(prefix):
            return

        ctx = await self.bot.get_context(message)
        if ctx.command is None:
            # Not a recognised Modmail command — ignore silently.
            return

        try:
            await self.bot.invoke(ctx)
        except Exception as e:
            self.bot.logger.error(f"[AIRelay] Failed to invoke relayed command: {e}")


async def setup(bot):
    await bot.add_cog(AIRelay(bot))
