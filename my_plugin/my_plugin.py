from discord.ext import commands


class RedTicketNames(commands.Cog):
    """Renames new Modmail thread channels to start with 🔴."""

    PREFIX = "🔴"

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        channel = thread.channel
        if channel and not channel.name.startswith(self.PREFIX):
            try:
                await channel.edit(name=f"{self.PREFIX}{channel.name}")
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(RedTicketNames(bot))