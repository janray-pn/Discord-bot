import asyncio
import functools
import itertools
import math
import random
import os
import time
# import pandas as pd

import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands
from keep_alive import keep_alive


youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(
            cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError(
                'Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError(
                    'Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(
            cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError(
                        'Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} days'.format(days))
        if hours > 0:
            duration.append('{} hours'.format(hours))
        if minutes > 0:
            duration.append('{} minutes'.format(minutes))
        if seconds > 0:
            duration.append('{} seconds'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Now Playing!',
                               description='```css\n{0.source.title}\n```'.format(
                                   self),
                               color=discord.Color.blurple())
                 .add_field(name='Duration', value=self.source.duration)
                 .add_field(name='Requested-by', value=self.requester.mention)
                 .add_field(name='Uploader', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='URL', value='[Click]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:

                try:
                    async with timeout(180):
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage(
                'This command can\'t be used in DM channels.')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send('An error occurred: {}'.format(str(error)))

    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):

        if not channel and not ctx.author.voice:
            raise VoiceError(
                'You are neither connected to a voice channel nor specified a channel to join.')

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='leave', aliases=['disconnect'])
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):

        if not ctx.voice_state.voice:
            return await ctx.send('Ara But you are not connected to any voice channel.')

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command(name='volume')
    async def _volume(self, ctx: commands.Context, *, volume: int):

        if not ctx.voice_state.is_playing:
            return await ctx.send('Im not playing at the moment.')

        if 0 > volume > 100:
            return await ctx.send('Indicate the volume between 0 and 100')

        ctx.voice_state.volume = volume / 100
        await ctx.send('Volume of the player set to {}%'.format(volume))

    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    @commands.command(name='pause')
    @commands.has_permissions(manage_guild=True)
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):

        if ctx.voice_state.is_playing and ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='stop')
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):

        ctx.voice_state.songs.clear()

        if ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):

        if not ctx.voice_state.is_playing:
            return await ctx.send('Ara But im not playing any music now')

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send('Okay vote added, currently at **{}/3**'.format(total_votes))

        else:
            await ctx.send('Ara ara But you have already voted to skip this song.')

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Empty queue.')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(
                i + 1, song)

        embed = (discord.Embed(description='**{} tracks:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Viewing page {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Ara ara The queue is empty.')

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('👍')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Ara ara The queue is empty.')

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('👍')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):

        if not ctx.voice_state.is_playing:
            return await ctx.send('Ara ara Nothing i can play at the moment.')

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('👍')

    @commands.command(name='play')
    async def _play(self, ctx: commands.Context, *, search: str):

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send('Ara ara error occurred while processing this request: {}'.format(str(e)))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send('Ara ara its Enqueued {}'.format(str(source)))

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError(
                'Ara But you are not connected to any voice channel.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError(
                    'Ara Im already in a voice channel.')


bot = commands.Bot(command_prefix='@')
bot.remove_command("help")
bot.add_cog(Music(bot))


@bot.event
async def on_ready():
    print('Logged in as: {0.user.name}'.format(bot))

# WordSets and Images
# url_swearwords = 'https://raw.githubusercontent.com/janray-pn/datasets/main/krulcifer-bot-datasets/swearwords.csv'
# swearwords_data = pd.read_csv(url_swearwords)
# swearwords_data.astype(str)

# url_nicknames = 'https://raw.githubusercontent.com/janray-pn/datasets/main/krulcifer-bot-datasets/nicknames.csv'
# nicknames_data = pd.read_csv(url_nicknames)
# nicknames_data.astype(str)

# restricted_words_list = []
# restricted_words_list.append(swearwords_data['Swear_Word'].tolist())
# restricted_words =[]
# restricted_words = ','.join(str(i) for i in restricted_words_list)
# print('swear', restricted_words)

# restricted_names_list = []
# restricted_names_list.append(nicknames_data['Nicknames'].tolist())
# restricted_names = []
# restricted_names = ','.join(str(j) for j in restricted_names_list)
# print('names', restricted_names)


restricted_words = [
    'fuck',
    'shit',
    'piss',
    'dick',
    'asshole',
    'ass',
    'bitch',
    'bastard',
    'damn',
    'cunt',
]
restricted_names = [
    'chugg',
    'chuggy',
    'boss',
    'janray',
    'chugray',
    'tamago',
]

tester = ["janray", "junray"]

Krulcifer_angry = "https://cdn.discordapp.com/attachments/848340726499901472/848778012605808660/458fbbe02e499c369eba4827579c2477.png"
Krulcifer_profile = "https://cdn.discordapp.com/attachments/848340726499901472/848341528723062824/mainphoto.jpg"
Krulcifer_janray = "https://cdn.discordapp.com/attachments/848340726499901472/848778029861699624/2538f38cccba720f7466143dbd6095cc.png"

# Message Reader & help command


@bot.command(pass_context=True)
async def help(ctx):
    embed = discord.Embed(
        title="Krulcifer Music Bot!",
        url="https://Krulcifer-Bot.rafaellachica1.repl.co",
        description="All Available Commands",
        color=discord.Color.purple())
    embed.set_author(name="Sebastian Lachicus",
                     url="https://Krulcifer-Bot.rafaellachica.repl.co", icon_url=Krulcifer_profile)
    embed.set_thumbnail(url=Krulcifer_profile)
    embed.add_field(
        name="@help", value="Returns a list of all commands", inline=False)
    embed.add_field(
        name="@join", value="Krulcifer will join your voice channel", inline=False)
    embed.add_field(
        name="@ping", value="Krulcifer will return its internet connection latency", inline=False)
    embed.add_field(
        name="@play", value="<@play music-name>Krulcifer will play music", inline=False)
    embed.add_field(
        name="@pause", value="Krulcifer will pause the music", inline=False)
    embed.add_field(
        name="@stop", value="Krulcifer will stop the music", inline=False)
    embed.add_field(
        name="@leave", value="Krulcifer clears the queue and leaves the voice channel", inline=False)
    embed.add_field(
        name="@loop", value="Loops the currently playing music", inline=False)
    embed.add_field(
        name="@now", value="Displays the current playing music", inline=False)
    embed.add_field(
        name="@queue", value="Display the list of music queued", inline=False)
    embed.add_field(
        name="@remove", value="Removes a song from the queue at a given index.", inline=False)
    embed.add_field(
        name="@resume", value="Resume the paused music", inline=False)
    embed.add_field(name="@shuffle",
                    value="Shuffles the music queue", inline=False)
    embed.add_field(
        name="@skip", value="Vote to skip the song. The Requester can automatically skip", inline=False)
    embed.add_field(
        name="@volume", value="Sets the volume of a player 0-100", inline=False)
    embed.set_footer(text="Learn more by typing .help")
    await ctx.send(embed=embed)


@bot.command()
async def ping(ctx, index=5):
    for i in range(index):
        await ctx.send(f'`Krulcifer Latency: {round(bot.latency * 1000)}ms`')
    await ctx.send('`Krulcifer is nominal`')


@bot.command()
async def chat(ctx, *, messages):
    admin = "Tamago#3912"
    author_temp = str(ctx.author)

    if author_temp == admin:
        await ctx.channel.send(messages)
    else:
        await ctx.channel.send("`Sorry you are not allowed to use me`")


@bot.event
async def on_message(message):
    sentence = message.content.lower()
    global cmdcounter

    if message.author == bot.user:
        return

    if str(message.author) == "Tamago#3912":
        if any(word in sentence for word in restricted_words):
            await message.channel.send('Ara ara.. `Janray` 何をしたんだ？ ')
            await message.channel.send(Krulcifer_angry)

    if str(message.author) == "Tsukasa#9908":
        if message.content.startswith("Krulcifer: command ping"):
            await message.channel.send("```yaml\nMusic-Cogs:(Working) Passed```")
            await message.channel.send("```yaml\nObserver-Cogs:(Working) Passed```")
            await message.channel.send("```yaml\nFlask-Cogs:(Working) Passed```")
            await message.channel.send("```yaml\nUptimeRobot-Cogs:(Working) Passed```")
            await message.channel.send(f'```yaml\nKrulcifer Latency: {round(bot.latency * 1000)}ms```')

    time.sleep(0.8)
    if str(message.author) != "Tamagod#3912":
        if str(message.author) != "Bronya#9911":
            if str(message.author) != "Translator#2653":
                if str(message.author) != "Tsukasa#9908":
                    if any(word in sentence for word in tester):

                        if message.channel.name == '💬-jp-chat':
                            await message.channel.send(str(message.author.nick) + '-san.. それは何ですか、あなたは私の夫が必要ですか')
                            await message.channel.send(Krulcifer_janray)
                        else:
                            await message.channel.send(str(message.author.nick) + ' Hmm？ Do you need my darling?')
                            await message.channel.send(Krulcifer_janray)

    if "@chat" in message.content:
        await message.delete()

    await bot.process_commands(message)


keep_alive()
bot.run(os.getenv("TOKEN"))
