import inspect
from discord.ext import commands


class RedTicketNames(commands.Cog):
    """Prefixes new Modmail thread channel names with 🔴."""

    PREFIX = "🔴"

    def __init__(self, bot):
        self.bot = bot
        # Keep a reference to Modmail's original naming logic
        self._original_format_channel_name = bot.format_channel_name

        if inspect.iscoroutinefunction(self._original_format_channel_name):
            async def patched(author, exclude_channel=None, force_null=False):
                name = await self._original_format_channel_name(
                    author, exclude_channel=exclude_channel, force_null=force_null
                )
                return self.PREFIX + name
        else:
            def patched(author, exclude_channel=None, force_null=False):
                name = self._original_format_channel_name(
                    author, exclude_channel=exclude_channel, force_null=force_null
                )
                return self.PREFIX + name

        bot.format_channel_name = patched

    def cog_unload(self):
        # Restore original behaviour if the plugin is ever unloaded
        self.bot.format_channel_name = self._original_format_channel_name


async def setup(bot):
    await bot.add_cog(RedTicketNames(bot))