import discord
from discord.ext import commands


class AIRelay(commands.Cog):
    """
    Modmail ignores command messages sent by other bot accounts by default
    (this lives in discord.py's process_commands, which bails out early on
    message.author.bot). That's why a bot-sent "?r ..." or "?aiconnect" just
    sits in the channel doing nothing.

    This cog re-dispatches messages from ONE trusted bot (your automation
    bot) through Modmail's normal command pipeline directly, instead of
    going through process_commands. That skips the bot-author check while
    still running every normal permission check a command has
    (checks.has_permissions, checks.thread_only, etc.) exactly as if a human
    staff member had typed it.

    IMPORTANT: for commands like ?reply/?r (and snippets, which resolve to
    ?reply under the hood) to actually succeed, the trusted bot's Discord
    account still needs whatever permission level Modmail requires for that
    command (e.g. SUPPORTER). Grant that once via Modmail's own permission
    command, e.g.:
        ?permissions add level <automation_bot_user_id> SUPPORTER

    NOTE: this uses self.bot.get_contexts() (plural), Modmail's own
    context-resolution method, NOT discord.py's vanilla get_context()
    (singular). Snippets (like ?aiconnect) are stored in self.bot.snippets
    and only get resolved into a real command inside get_contexts() — the
    vanilla get_context() has no idea snippets exist and will always return
    ctx.command = None for them, silently no-oping the invoke. Real
    registered commands (?reply/?r, ?close, etc.) resolve fine either way,
    which is why only the snippet stopped working here.
    """

    # Replace with your automation bot's actual Discord user (client) ID.
    TRUSTED_BOT_ID = 1434985220506914897  # <-- SET THIS

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

        # Modmail's get_prefix() also accepts bot mentions as valid
        # prefixes, so we check against all of them rather than just
        # self.bot.prefix directly, to stay in sync with how Modmail
        # itself decides what counts as a command invocation.
        prefixes = await self.bot.get_prefix()
        if not any(content.startswith(p) for p in prefixes):
            return

        # get_contexts (plural) is Modmail's own context-resolution method.
        # It checks self.bot.snippets/self.bot.aliases BEFORE falling back
        # to a normal command lookup, and can return multiple contexts
        # (e.g. a multi-step alias). Vanilla get_context() (singular) skips
        # all of that and would leave ctx.command as None for any snippet.
        ctxs = await self.bot.get_contexts(message)
        if not ctxs:
            return

        for ctx in ctxs:
            if ctx.command is None:
                # Not a recognised Modmail command or snippet — ignore silently.
                continue

            try:
                await self.bot.invoke(ctx)
            except Exception as e:
                self.bot.logger.error(f"[AIRelay] Failed to invoke relayed command: {e}")


async def setup(bot):
    await bot.add_cog(AIRelay(bot))
