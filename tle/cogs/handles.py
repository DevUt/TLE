import io
import asyncio
import contextlib
import logging
import math
import html
import cairo
import os
import time
import gi
import datetime
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Pango, PangoCairo

import discord
import random, string
from discord.ext import commands

from tle import constants
from tle.util import cache_system2
from tle.util import codeforces_api as cf
from tle.util import clist_api as clist
from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.util import events
from tle.util import paginator
from tle.util import table
from tle.util import tasks
from tle.util import db
from tle.util import scaper
from tle.util.codeforces_api import Rank, rating2rank
from tle import constants

from discord.ext import commands

from PIL import Image, ImageFont, ImageDraw

_HANDLES_PER_PAGE = 15
_NAME_MAX_LEN = 20
_PAGINATE_WAIT_TIME = 5 * 60  # 5 minutes
_PRETTY_HANDLES_PER_PAGE = 10
_TOP_DELTAS_COUNT = 10
_MAX_RATING_CHANGES_PER_EMBED = 15
_UPDATE_HANDLE_STATUS_INTERVAL = 6 * 60 * 60  # 6 hours
_UPDATE_CLIST_CACHE_INTERVAL = 3 * 60 * 60 # 3 hour

_GITGUD_SCORE_DISTRIB = (2, 3, 5, 8, 12, 17, 23, 23, 23)
_GITGUD_MAX_NEG_DELTA_VALUE = -300
_GITGUD_MAX_POS_DELTA_VALUE = 500

_DIVISION_RATING_LOW  = (2100, 1600, -1000)
_DIVISION_RATING_HIGH = (9999, 2099,  1599)
_SUPPORTED_CLIST_RESOURCES = ('codechef.com', 'atcoder.jp',
 'leetcode.com','codingcompetitions.withgoogle.com', 'facebook.com/hackercup', 'codedrills.io')
_CLIST_RESOURCE_SHORT_FORMS = {'cc':'codechef.com','codechef':'codechef.com', 'cf':'codeforces.com',
 'codeforces':'codeforces.com','ac':'atcoder.jp', 'atcoder':'atcoder.jp', 'lc':'leetcode.com', 
 'leetcode':'leetcode.com', 'google':'codingcompetitions.withgoogle.com', 'cd': 'codedrills.io', 'codedrills':'codedrills.io',
 'fb':'facebook.com/hackercup', 'facebook':'facebook.com/hackercup'}

CODECHEF_RATED_RANKS = (
    Rank(-10 ** 9, 1400, '1 Star', '1★', '#DADADA', 0x666666),
    Rank(1400, 1600, '2 Star', '2★', '#C9E0CA', 0x1e7d22),
    Rank(1600, 1800, '3 Star', '3★', '#CEDAF3', 0x3366cc),
    Rank(1800, 2000, '4 Star', '4★', '#DBD2DE', 0x684273),
    Rank(2000, 2200, '5 Star', '5★', '#FFF0C2', 0xffbf00),
    Rank(2200, 2500, '6 Star', '6★', '#FFE3C8', 0xff7f00),
    Rank(2500, 10**9, '7 Star', '7★', '#F1C1C8', 0xd0011b)
)

ATCODER_RATED_RANKS = (
    Rank(-10 ** 9, 400, 'Gray', 'Gray', '#DADADA', 0x808080),
    Rank(400, 800, 'Brown', 'Brown', '#D9C5B2', 0x7F3F00),
    Rank(800, 1200, 'Green', 'Green', '#B2D9B2', 0x007F00),
    Rank(1200, 1600, 'Cyan', 'Cyan', '#B2ECEC', 0x00C0C0),
    Rank(1600, 2000, 'Blue', 'Blue', '#B2B2FF', 0x0000FF),
    Rank(2000, 2400, 'Yellow', 'Yellow', '#ECECB2', 0xBFBF00),
    Rank(2400, 2800, 'Orange', 'Orange', '#FFD9B2', 0xF67B00),
    Rank(2800, 10**9, 'Red', 'Red', '#FFB2B2', 0xF70000)
)

class HandleCogError(commands.CommandError):
    pass

def ac_rating_to_color(rating):
    h = discord_color_to_hex(rating2acrank(rating).color_embed)
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def cc_rating_to_color(rating):
    h = discord_color_to_hex(rating2star(rating).color_embed)
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def discord_color_to_hex(color):
    h = str(hex(color))
    h = h[2:]
    return ('0'*(6-len(h)))+h

def rating_to_color(rating):
    """returns (r, g, b) pixels values corresponding to rating"""
    rank = rating2rank(rating)
    if rank is None or rank.color_embed is None:
        return None
    h = discord_color_to_hex(rank.color_embed)
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def rating2star(rating):
    for rank in CODECHEF_RATED_RANKS:
        if rank.low <= rating < rank.high:
            return rank

def rating2acrank(rating):
    for rank in ATCODER_RATED_RANKS:
        if rank.low <= rating < rank.high:
            return rank

def randomword(length):
   letters = string.ascii_lowercase
   return ''.join(random.choice(letters) for i in range(length))

FONTS = [
    'Noto Sans',
    'Noto Sans CJK JP',
    'Noto Sans CJK SC',
    'Noto Sans CJK TC',
    'Noto Sans CJK HK',
    'Noto Sans CJK KR',
]

def get_gudgitters_image(rankings):
    """return PIL image for rankings"""
    SMOKE_WHITE = (250, 250, 250)
    BLACK = (0, 0, 0)

    DISCORD_GRAY = (.212, .244, .247)

    ROW_COLORS = ((0.95, 0.95, 0.95), (0.9, 0.9, 0.9))

    WIDTH = 900
    #HEIGHT = 900
    BORDER_MARGIN = 20
    COLUMN_MARGIN = 10
    HEADER_SPACING = 1.25
    WIDTH_RANK = 0.08*WIDTH
    WIDTH_NAME = 0.38*WIDTH
    LINE_HEIGHT = 40#(HEIGHT - 2*BORDER_MARGIN)/(20 + HEADER_SPACING)
    HEIGHT = int((len(rankings) + HEADER_SPACING) * LINE_HEIGHT + 2*BORDER_MARGIN)
    # Cairo+Pango setup
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    context = cairo.Context(surface)
    context.set_line_width(1)
    context.set_source_rgb(*DISCORD_GRAY)
    context.rectangle(0, 0, WIDTH, HEIGHT)
    context.fill()
    layout = PangoCairo.create_layout(context)
    layout.set_font_description(Pango.font_description_from_string(','.join(FONTS) + ' 20'))
    layout.set_ellipsize(Pango.EllipsizeMode.END)

    def draw_bg(y, color_index):
        nxty = y + LINE_HEIGHT

        # Simple
        context.move_to(BORDER_MARGIN, y)
        context.line_to(WIDTH, y)
        context.line_to(WIDTH, nxty)
        context.line_to(0, nxty)
        context.set_source_rgb(*ROW_COLORS[color_index])
        context.fill()

    def draw_row(pos, username, handle, rating, color, y, bold=False):
        context.set_source_rgb(*[x/255.0 for x in color])

        context.move_to(BORDER_MARGIN, y)

        def draw(text, width=-1):
            text = html.escape(text)
            if bold:
                text = f'<b>{text}</b>'
            layout.set_width((width - COLUMN_MARGIN)*1000) # pixel = 1000 pango units
            layout.set_markup(text, -1)
            PangoCairo.show_layout(context, layout)
            context.rel_move_to(width, 0)

        draw(pos, WIDTH_RANK)
        draw(username, WIDTH_NAME)
        draw(handle, WIDTH_NAME)
        draw(rating)

    #

    y = BORDER_MARGIN

    # draw header
    draw_row('#', 'Name', 'Handle', 'Points', SMOKE_WHITE, y, bold=True)
    y += LINE_HEIGHT*HEADER_SPACING

    for i, (pos, name, handle, rating, score) in enumerate(rankings):
        color = rating_to_color(rating)
        draw_bg(y, i%2)
        draw_row(str(pos+1), f'{name} ({rating if rating else "N/A"})', handle, str(score), color, y)
        if rating and rating >= 3000:  # nutella
            draw_row('', name[0], handle[0], '', BLACK, y)
        y += LINE_HEIGHT

    image_data = io.BytesIO()
    surface.write_to_png(image_data)
    image_data.seek(0)
    discord_file = discord.File(image_data, filename='gudgitters.png')
    return discord_file

def get_prettyhandles_image(rows, font, color_converter=rating_to_color):
    """return PIL image for rankings"""
    SMOKE_WHITE = (250, 250, 250)
    BLACK = (0, 0, 0)
    img = Image.new('RGB', (900, 450), color=SMOKE_WHITE)
    draw = ImageDraw.Draw(img)

    START_X, START_Y = 20, 20
    Y_INC = 32
    WIDTH_RANK = 64
    WIDTH_NAME = 340

    def draw_row(pos, username, handle, rating, color, y):
        x = START_X
        draw.text((x, y), pos, fill=color, font=font)
        x += WIDTH_RANK
        draw.text((x, y), username, fill=color, font=font)
        x += WIDTH_NAME
        draw.text((x, y), handle, fill=color, font=font)
        x += WIDTH_NAME
        draw.text((x, y), rating, fill=color, font=font)

    y = START_Y
    # draw header
    draw_row('#', 'Username', 'Handle', 'Rating', BLACK, y)
    y += int(Y_INC * 1.5)

    # trim name to fit in the column width
    def _trim(name):
        width = WIDTH_NAME - 10
        while font.getsize(name)[0] > width:
            name = name[:-4] + '...'  # "…" is printed as floating dots
        return name

    for pos, name, handle, rating in rows:
        name = _trim(name)
        handle = _trim(handle)
        color = color_converter(rating)
        draw_row(str(pos), name, handle, str(rating) if rating else 'N/A', color or BLACK, y)
        if rating and rating >= 3000:  # nutella
            nutella_x = START_X + WIDTH_RANK
            draw.text((nutella_x, y), name[0], fill=BLACK, font=font)
            nutella_x += WIDTH_NAME
            draw.text((nutella_x, y), handle[0], fill=BLACK, font=font)
        y += Y_INC

    return img


def _make_profile_embed(member, user, handles={}, *, mode):
    assert mode in ('set', 'get')
    if user:
        if mode == 'set':
            desc = f'Handle for {member.mention} successfully set to **[{user.handle}]({user.url})**'
        else:
            desc = f'Handle for {member.mention} is currently set to **[{user.handle}]({user.url})**'
        if user.rating is None:
            embed = discord.Embed(description=desc)
            embed.add_field(name='Rating', value='Unrated', inline=True)
        else:
            embed = discord.Embed(description=desc, color=user.rank.color_embed)
            embed.add_field(name='Rating', value=user.rating, inline=True)
            embed.add_field(name='Rank', value=user.rank.title, inline=True)
    else:
        embed = discord.Embed(description="CodeForces handle is not set for this user")
    for key in handles:
        if key=='codeforces.com' or key=='codedrills.io': continue
        title = key
        if key=="codingcompetitions.withgoogle.com": title = "google"
        embed.add_field(name=title, value=handles[key], inline=True)
    if user:
        embed.set_thumbnail(url=f'{user.titlePhoto}')
    return embed


def _make_pages(users, title, resource='codeforces.com'):
    chunks = paginator.chunkify(users, _HANDLES_PER_PAGE)
    pages = []
    done = 1
    no_rating = resource in ['codingcompetitions.withgoogle.com', 'facebook.com/hackercup']
    no_rating_suffix = resource!='codeforces.com'
    style = table.Style('{:>}  {:<}  {:<}  {:<}')
    for chunk in chunks:
        t = table.Table(style)
        t += table.Header('#', 'Name', 'Handle', 'Contests' if no_rating else 'Rating')
        t += table.Line()
        for i, (member, handle, rating, n_contests) in enumerate(chunk):
            name = member.display_name if member else "unknown"
            if len(name) > _NAME_MAX_LEN:
                name = name[:_NAME_MAX_LEN - 1] + '…'
            rank = cf.rating2rank(rating)
            rating_str = 'N/A' if rating is None else str(rating)
            fourth = n_contests if no_rating else ((f'{rating_str}')+((f'({rank.title_abbr})') if not no_rating_suffix else ''))
            t += table.Data(i + done, name, handle, fourth)
        table_str = '```\n'+str(t)+'\n```'
        embed = discord_common.cf_color_embed(description=table_str)
        pages.append((title, embed))
        done += len(chunk)
    return pages


def parse_date(arg):
    try:
        if len(arg) == 6:
            fmt = '%m%Y'
        # elif len(arg) == 4:
            # fmt = '%Y'
        else:
            raise ValueError
        return datetime.datetime.strptime(arg, fmt)
    except ValueError:
        raise HandleCogError(f'{arg} is an invalid date argument')

class Handles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger(self.__class__.__name__)
        self.font = ImageFont.truetype(constants.NOTO_SANS_CJK_BOLD_FONT_PATH, size=26) # font for ;handle pretty
        self.converter = commands.MemberConverter()

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self):
        cf_common.event_sys.add_listener(self._on_rating_changes)
        self._set_ex_users_inactive_task.start()
        self._update_clist_users_cache.start()

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        cf_common.user_db.set_inactive([(member.guild.id, member.id)])

    @tasks.task_spec(name='RefreshClistUserCache',
                     waiter=tasks.Waiter.fixed_delay(_UPDATE_CLIST_CACHE_INTERVAL))
    async def _update_clist_users_cache(self, _):
        for guild in self.bot.guilds:
            try:
                await self._update_stars_all(guild)
            except:
                pass

    @commands.command(brief='update status, mark guild members as active')
    @commands.check_any(commands.has_role('Admin'), commands.is_owner())
    async def _updatestatus(self, ctx):
        gid = ctx.guild.id
        active_ids = [m.id for m in ctx.guild.members]
        cf_common.user_db.reset_status(gid)
        rc = sum(cf_common.user_db.update_status(gid, chunk) for chunk in paginator.chunkify(active_ids, 100))
        cf_common.user_db.update()
        await ctx.send(f'{rc} members active with handle')

    @commands.Cog.listener()
    async def on_member_join(self, member):
        rc = cf_common.user_db.update_status(member.guild.id, [member.id])
        if rc == 1:
            handle = cf_common.user_db.get_handle(member.id, member.guild.id)
            await self._update_ranks(member.guild, [(int(member.id), handle)])

    @tasks.task_spec(name='SetExUsersInactive',
                     waiter=tasks.Waiter.fixed_delay(_UPDATE_HANDLE_STATUS_INTERVAL))
    async def _set_ex_users_inactive_task(self, _):
        # To set users inactive in case the bot was dead when they left.
        to_set_inactive = []
        for guild in self.bot.guilds:
            user_id_handle_pairs = cf_common.user_db.get_handles_for_guild(guild.id)
            to_set_inactive += [(guild.id, user_id) for user_id, _ in user_id_handle_pairs
                                if guild.get_member(user_id) is None]
        cf_common.user_db.set_inactive(to_set_inactive)

    @events.listener_spec(name='RatingChangesListener',
                          event_cls=events.RatingChangesUpdate,
                          with_lock=True)
    async def _on_rating_changes(self, event):
        contest, changes = event.contest, event.rating_changes
        change_by_handle = {change.handle: change for change in changes}

        async def update_for_guild(guild):
            if cf_common.user_db.has_auto_role_update_enabled(guild.id):
                with contextlib.suppress(HandleCogError):
                    await self._update_ranks_all(guild)
            channel_id = cf_common.user_db.get_rankup_channel(guild.id)
            channel = guild.get_channel(channel_id)
            if channel is not None:
                with contextlib.suppress(HandleCogError):
                    embeds = self._make_rankup_embeds(guild, contest, change_by_handle)
                    for embed in embeds:
                        await channel.send(embed=embed)

        await asyncio.gather(*(update_for_guild(guild) for guild in self.bot.guilds),
                             return_exceptions=True)
        self.logger.info(f'All guilds updated for contest {contest.id}.')

    @commands.group(brief='Commands that have to do with handles', invoke_without_command=True)
    async def handle(self, ctx):
        """Change or collect information about specific handles on Codeforces"""
        await ctx.send_help(ctx.command)

    @staticmethod
    async def update_member_star_role(member, role_to_assign, *, reason):
        """Sets the `member` to only have the rank role of `role_to_assign`. All other rank roles
        on the member, if any, will be removed. If `role_to_assign` is None all existing rank roles
        on the member will be removed.
        """
        if member is None: return
        role_names_to_remove = {rank.title for rank in CODECHEF_RATED_RANKS}
        if role_to_assign is not None:
            role_names_to_remove.discard(role_to_assign.name)
        to_remove = [role for role in member.roles if role.name in role_names_to_remove]
        if to_remove:
            await member.remove_roles(*to_remove, reason=reason)
        if role_to_assign is not None and role_to_assign not in member.roles:
            await member.add_roles(role_to_assign, reason=reason)

    @staticmethod
    async def update_member_rank_role(member, role_to_assign, *, reason):
        """Sets the `member` to only have the rank role of `role_to_assign`. All other rank roles
        on the member, if any, will be removed. If `role_to_assign` is None all existing rank roles
        on the member will be removed.
        """
        role_names_to_remove = {rank.title for rank in cf.RATED_RANKS}
        if role_to_assign is not None:
            role_names_to_remove.discard(role_to_assign.name)
            role_names_to_remove.add('Unrated')
            if role_to_assign.name not in ['Newbie', 'Pupil', 'Specialist', 'Expert']:
                role_names_to_remove.add('Purgatory')
        to_remove = [role for role in member.roles if role.name in role_names_to_remove]
        if to_remove:
            await member.remove_roles(*to_remove, reason=reason)
        if role_to_assign is not None and role_to_assign not in member.roles:
            await member.add_roles(role_to_assign, reason=reason)

    @handle.command(brief='Set Codeforces handle of a user', usage="@member [website]:[handle]")
    @commands.check_any(commands.has_any_role('Admin', constants.TLE_MODERATOR), commands.is_owner())
    async def set(self, ctx, member: discord.Member, handle: str):
        """Set codeforces/codechef/atcoder/google handle of a user.

        Some examples are given below
        ;handle set @Benjamin Benq
        ;handle set @Kamil cf:Errichto
        ;handle set @Gennady codechef:gennady.korotkevich
        ;handle set @Paramjeet cc:thesupremeone
        ;handle set @Jatin atcoder:nagpaljatin1411
        ;handle set @Alex ac:Um_nik
        ;handle set @Priyansh google:Priyansh31dec
        """
        embed = None
        resource = 'codeforces.com'
        if ':' in handle:
            resource = handle[0: handle.index(':')]
            handle = handle[handle.index(':')+1:]
        if resource in _CLIST_RESOURCE_SHORT_FORMS:
            resource = _CLIST_RESOURCE_SHORT_FORMS[resource]
        if resource!='codeforces.com':
            if resource=='all':
                resource = None
            if resource!=None and resource not in _SUPPORTED_CLIST_RESOURCES:
                raise HandleCogError(f'The resource `{resource}` is not supported.')
            users = await clist.account(handle=handle, resource=resource)
            for user in users:
                if user['resource'] not in _SUPPORTED_CLIST_RESOURCES:
                    continue
                await self._set_account_id(member.id, ctx.guild, user)
        else:
            # CF API returns correct handle ignoring case, update to it
            user, = await cf.user.info(handles=[handle])
            await self._set(ctx, member, user)
        await self.get(ctx, member, settingHandle=True)
    
    @handle.command(brief='Resolve handles of other sites using codeforces handles')
    @commands.check_any(commands.has_any_role('Admin', constants.TLE_MODERATOR), commands.is_owner())
    async def sync_all(self, ctx):
        guild = ctx.guild
        for member in guild.members:
                handle = cf_common.user_db.get_handle(member.id, guild.id)
                if handle:
                    try:
                        await self.set(ctx, member, "all:"+str(handle))
                    except clist.HandleNotFoundError:
                        pass            

    async def _set_account_id(self, member_id, guild, user):
        try:
            guild_id = guild.id
            cf_common.user_db.set_account_id(member_id, guild_id, user['id'], user['resource'], user['handle'])
            if user['resource']=='codechef.com':
                roletitle = rating2star(user['rating']).title
                roles = [role for role in guild.roles if role.name == roletitle]
                if not roles:
                    raise HandleCogError(f'Handle Linked, but failed to assign role for `{roletitle}` as the required role is not present in the server')
                await self.update_member_star_role(guild.get_member(member_id),roles[0] ,reason='CodeChef Account Set')
        except db.UniqueConstraintFailed:
            raise HandleCogError(f'The handle `{user["handle"]}` is already associated with another user.')


    async def _set(self, ctx, member, user):
        handle = user.handle
        try:
            cf_common.user_db.set_handle(member.id, ctx.guild.id, handle)
        except db.UniqueConstraintFailed:
            raise HandleCogError(f'The handle `{handle}` is already associated with another user.')
        cf_common.user_db.cache_cf_user(user)

        if user.rank == cf.UNRATED_RANK:
            role_to_assign = None
        else:
            roles = [role for role in ctx.guild.roles if role.name == user.rank.title]
            if not roles:
                raise HandleCogError(f'Role for rank `{user.rank.title}` not present in the server')
            role_to_assign = roles[0]
        await self.update_member_rank_role(member, role_to_assign,
                                           reason='New handle set for user')

    @handle.command(brief='Identify yourself', usage='[[website]:[handle]]')
    @cf_common.user_guard(group='handle',
                          get_exception=lambda: HandleCogError('Identification is already running for you'))
    async def identify(self, ctx, handle: str):
        """Link a codeforces/codechef/atcoder account to discord account
        
        Some examples are given below
        ;handle identify Benq
        ;handle identify cf:Errichto
        ;handle identify codechef:gennady.korotkevich
        ;handle identify cc:thesupremeone
        ;handle identify atcoder:nagpaljatin1411
        ;handle identify ac:Um_nik

        For linking google/codedrills/leetcode handles, please contact a moderator  
        """
        invoker = str(ctx.author)
        resource = 'codeforces.com'
        if ':' in handle:
            resource = handle[0: handle.index(':')]
            handle = handle[handle.index(':')+1:]
        if resource in _CLIST_RESOURCE_SHORT_FORMS:
            resource = _CLIST_RESOURCE_SHORT_FORMS[resource]
        if resource!='codeforces.com':
            if resource=='all':
                return await ctx.send(f'Sorry `{invoker}`, all keyword can only be used with set command')
            if resource not in ['codechef.com','atcoder.jp']:
                raise HandleCogError(f'{ctx.author.mention}, you cannot identify handles of {resource} as of now ')
            wait_msg = await ctx.channel.send('Fetching account details, please wait...')
            users = await clist.account(handle, resource)
            if users is None or len(users)<0:
                raise HandleCogError(f'{ctx.author.mention}, I couldn\'t find your handle, don\'t tell me you haven\'t given any contest ')
            user = users[0]
            token = randomword(8)
            await wait_msg.delete()
            field = "name" 
            if resource=='atcoder.jp': field = 'affiliation'
            wait_msg = await ctx.send(f'`{invoker}`, change your {field} to `{token}` on {resource} within 60 seconds')
            await asyncio.sleep(60)
            await wait_msg.delete()
            wait_msg = await ctx.channel.send(f'Verifying {field} change...')
            if scaper.assert_display_name(handle, token, resource, ctx.author.mention):
                member = ctx.author
                await self._set_account_id(member.id, ctx.guild, user)
                await wait_msg.delete()
                await self.get(ctx, member, settingHandle=True)
            else:
                await wait_msg.delete()
                await ctx.send(f'Sorry `{invoker}`, can you try again?')
        else:
            if cf_common.user_db.get_handle(ctx.author.id, ctx.guild.id):
                raise HandleCogError(f'{ctx.author.mention}, you cannot identify when your handle is '
                                    'already set. Ask an Admin or Moderator if you wish to change it')

            if cf_common.user_db.get_user_id(handle, ctx.guild.id):
                raise HandleCogError(f'The handle `{handle}` is already associated with another user. Ask an Admin or Moderator in case of an inconsistency.')

            if handle in cf_common.HandleIsVjudgeError.HANDLES:
                raise cf_common.HandleIsVjudgeError(handle)

            users = await cf.user.info(handles=[handle])
            handle = users[0].handle
            problems = [prob for prob in cf_common.cache2.problem_cache.problems
                        if prob.rating <= 1200]
            problem = random.choice(problems)
            await ctx.send(f'`{invoker}`, submit a compile error to <{problem.url}> within 60 seconds')
            await asyncio.sleep(60)

            subs = await cf.user.status(handle=handle, count=5)
            if any(sub.problem.name == problem.name and sub.verdict == 'COMPILATION_ERROR' for sub in subs):
                user, = await cf.user.info(handles=[handle])
                await self._set(ctx, ctx.author, user)
                embed = _make_profile_embed(ctx.author, user, mode='set')
                await ctx.send(embed=embed)
            else:
                await ctx.send(f'Sorry `{invoker}`, can you try again?')

    @handle.command(brief='Get handle by Discord username')
    async def get(self, ctx, member: discord.Member, settingHandle = False):
        """Show Codeforces handle of a user."""
        handle = cf_common.user_db.get_handle(member.id, ctx.guild.id)
        handles = cf_common.user_db.get_account_id_by_user(member.id, ctx.guild.id)
        if not handle and handles is None:
            raise HandleCogError(f'Handle for {member.mention} not found in database')
        user = cf_common.user_db.fetch_cf_user(handle) if handle else None
        handles = cf_common.user_db.get_account_id_by_user(member.id, ctx.guild.id)
        embed = _make_profile_embed(member, user,handles=handles, mode='get' if not settingHandle else 'set')
        await ctx.send(embed=embed)

    @handle.command(brief='Get Discord username by cf handle')
    async def rget(self, ctx, handle: str):
        """Show Discord username of a cf handle."""
        user_id = cf_common.user_db.get_user_id(handle, ctx.guild.id)
        if not user_id:
            raise HandleCogError(f'Discord username for `{handle}` not found in database')
        user = cf_common.user_db.fetch_cf_user(handle)
        member = ctx.guild.get_member(user_id)
        embed = _make_profile_embed(member, user, mode='get')
        await ctx.send(embed=embed)

    @handle.command(brief='Remove handle for a user')
    @commands.check_any(commands.has_any_role('Admin', constants.TLE_MODERATOR), commands.is_owner())
    async def remove(self, ctx, member: discord.Member):
        """Remove Codeforces handle of a user."""
        rc = cf_common.user_db.remove_handle(member.id, ctx.guild.id)
        if not rc:
            raise HandleCogError(f'Handle for {member.mention} not found in database')
        await self.update_member_rank_role(member, role_to_assign=None,
                                           reason='Handle removed for user')
        await self.update_member_star_role(member, role_to_assign=None, reason='Handle removed for user')
        embed = discord_common.embed_success(f'Removed handle for {member.mention}')
        await ctx.send(embed=embed)

    @handle.command(brief='Remove handle for a user')
    @commands.check_any(commands.has_any_role('Admin', constants.TLE_MODERATOR), commands.is_owner())
    async def removebyid(self, ctx, member_id:int):
        """Remove Codeforces handle of a user."""
        rc = cf_common.user_db.remove_handle(member_id, ctx.guild.id)
        member = ctx.guild.get_member(member_id)
        mention = 'unknown' if not member else member.mention
        if not rc:
            raise HandleCogError(f'Handle for {mention} not found in database')
        if member:
            await self.update_member_rank_role(member, role_to_assign=None,
                                            reason='Handle removed for user')
            await self.update_member_star_role(member, role_to_assign=None, reason='Handle removed for user')
        embed = discord_common.embed_success(f'Removed handle for {mention}')
        await ctx.send(embed=embed)

    @handle.command(brief='Resolve redirect of a user\'s handle')
    async def unmagic(self, ctx):
        """Updates handle of the calling user if they have changed handles
        (typically new year's magic)"""
        member = ctx.author
        handle = cf_common.user_db.get_handle(member.id, ctx.guild.id)
        await self._unmagic_handles(ctx, [handle], {handle: member})

    @handle.command(brief='Resolve handles needing redirection')
    @commands.check_any(commands.has_any_role('Admin', constants.TLE_MODERATOR), commands.is_owner())
    async def unmagic_all(self, ctx):
        """Updates handles of all users that have changed handles
        (typically new year's magic)"""
        user_id_and_handles = cf_common.user_db.get_handles_for_guild(ctx.guild.id)

        handles = []
        rev_lookup = {}
        for user_id, handle in user_id_and_handles:
            member = ctx.guild.get_member(user_id)
            handles.append(handle)
            rev_lookup[handle] = member
        await self._unmagic_handles(ctx, handles, rev_lookup)

    async def _unmagic_handles(self, ctx, handles, rev_lookup):
        handle_cf_user_mapping = await cf.resolve_redirects(handles)
        mapping = {(rev_lookup[handle], handle): cf_user
                   for handle, cf_user in handle_cf_user_mapping.items()}
        summary_embed = await self._fix_and_report(ctx, mapping)
        await ctx.send(embed=summary_embed)

    async def _fix_and_report(self, ctx, redirections):
        fixed = []
        failed = []
        for (member, handle), cf_user in redirections.items():
            if not cf_user:
                failed.append(handle)
            else:
                await self._set(ctx, member, cf_user)
                fixed.append((handle, cf_user.handle))

        # Return summary embed
        lines = []
        if not fixed and not failed:
            return discord_common.embed_success('No handles updated')
        if fixed:
            lines.append('**Fixed**')
            lines += (f'{old} -> {new}' for old, new in fixed)
        if failed:
            lines.append('**Failed**')
            lines += failed
        return discord_common.embed_success('\n'.join(lines))

    @commands.command(brief="Show gudgitters of the last 30 days", aliases=["recentgitgudders"])
    async def recentgudgitters(self, ctx):
        """Show the list of users of gitgud with their scores."""
        minimal_finish_time = int(datetime.datetime.now().timestamp())-30*24*60*60
        results = cf_common.user_db.get_gudgitters_last(minimal_finish_time)
        res = {}
        for entry in results:
            res[entry[0]] = 0
        for entry in results:
            res[entry[0]] += _GITGUD_SCORE_DISTRIB[(int(entry[1])+300)//100]
        
        rankings = []
        index = 0
        for user_id, score in sorted(res.items(), key=lambda item: item[1], reverse=True):
            member = ctx.guild.get_member(int(user_id))
            if member is None:
                continue
            if score > 0:
                handle = cf_common.user_db.get_handle(user_id, ctx.guild.id)
                user = cf_common.user_db.fetch_cf_user(handle)
                if user is None:
                    continue
                discord_handle = member.display_name
                rating = user.rating
                rankings.append((index, discord_handle, handle, rating, score))
                index += 1
            if index == 20:
                break

        if not rankings:
            raise HandleCogError('No one has completed a gitgud challenge, send ;gitgud to request and ;gotgud to mark it as complete')
        discord_file = get_gudgitters_image(rankings)
        await ctx.send(file=discord_file)

    @commands.command(brief="Show gudgitters", aliases=["gitgudders"])
    async def gudgitters(self, ctx):
        """Show the list of users of gitgud with their scores."""
        res = cf_common.user_db.get_gudgitters()
        res.sort(key=lambda r: r[1], reverse=True)

        rankings = []
        index = 0
        for user_id, score in res:
            member = ctx.guild.get_member(int(user_id))
            if member is None:
                continue
            if score > 0:
                handle = cf_common.user_db.get_handle(user_id, ctx.guild.id)
                user = cf_common.user_db.fetch_cf_user(handle)
                if user is None:
                    continue
                discord_handle = member.display_name
                rating = user.rating
                rankings.append((index, discord_handle, handle, rating, score))
                index += 1
            if index == 20:
                break

        if not rankings:
            raise HandleCogError('No one has completed a gitgud challenge, send ;gitgud to request and ;gotgud to mark it as complete')
        discord_file = get_gudgitters_image(rankings)
        await ctx.send(file=discord_file)

    def filter_rating_changes(self, rating_changes):
        rating_changes = [change for change in rating_changes
                    if self.dlo <= change.ratingUpdateTimeSeconds < self.dhi]
        return rating_changes

    @commands.command(brief="Show gudgitters of the month", aliases=["monthlygitgudders"], usage="[div1|div2|div3] [d=mmyyyy]")
    async def monthlygudgitters(self, ctx, *args):
        """Show the list of users of gitgud with their scores."""
        
        # Calculate time range of given month (d=) or current month
        now_time = datetime.datetime.now()
        for arg in args:
            if arg[0:2] == 'd=':
                now_time = parse_date(arg[2:])
        now_time = now_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_time = int(now_time.timestamp())
        if now_time.month == 12:
            now_time = now_time.replace(month=1,year=now_time.year+1)
        else:
            now_time = now_time.replace(month=now_time.month+1)
        end_time = int(now_time.timestamp())
        
        division = None
        for arg in args:
            if arg[0:3] == 'div':
                try:
                    division = int(arg[3])
                    if division < 1 or division > 3: 
                        raise HandleCogError('Division number must be within range [1-3]')
                except ValueError:
                    raise HandleCogError(f'{arg} is an invalid div argument')
       
        # get gitgud of month and calculate scores
        results = cf_common.user_db.get_gudgitters_timerange(start_time, end_time)
        res = {}
        for entry in results:
            res[entry[0]] = 0
        for entry in results:
            res[entry[0]] += _GITGUD_SCORE_DISTRIB[(int(entry[1])+300)//100]
        
        rankings = []
        index = 0
        cache = cf_common.cache2.rating_changes_cache
        for user_id, score in sorted(res.items(), key=lambda item: item[1], reverse=True):
            member = ctx.guild.get_member(int(user_id))
            if member is None:
                continue
            if score > 0:
                handle = cf_common.user_db.get_handle(user_id, ctx.guild.id)
                user = cf_common.user_db.fetch_cf_user(handle)
                if user is None:
                    continue
                rating = user.rating
                
                #### Live checking of a rating is not working since we get rate limited
                # check if user is in a certain division
                #handle, = await cf_common.resolve_handles(ctx, self.converter, (handle,))
                #rating_changes = await cf.user.rating(handle=handle)
                #rating_changes = [change for change in rating_changes if change.ratingUpdateTimeSeconds < start_time]
                #### Taking stuff from cache instead
                rating_changes = cache.get_rating_changes_for_handle(handle)
                rating_changes = [change for change in rating_changes if change.ratingUpdateTimeSeconds < start_time]
                rating_changes.sort(key=lambda a: a.ratingUpdateTimeSeconds)
                if division is not None:
                    if len(rating_changes) < 6: 
                        continue
                    if rating_changes[-1] is None: continue
                    if rating_changes[-1].newRating < _DIVISION_RATING_LOW[division-1] or rating_changes[-1].newRating > _DIVISION_RATING_HIGH[division-1]:
                        continue
                    rating = rating_changes[-1].newRating
                discord_handle = member.display_name
                rankings.append((index, discord_handle, handle, rating, score))
                index += 1
            if index == 20:
                break

        if not rankings:
            raise HandleCogError('No one has completed a gitgud challenge, send ;gitgud to request and ;gotgud to mark it as complete')
        discord_file = get_gudgitters_image(rankings)
        await ctx.send(file=discord_file)

    @handle.command(brief="Show all handles", usage="[countries...] [website]")
    async def list(self, ctx, resource='codeforces.com'):
        """Shows members of the server who have registered their handles and
        their Codeforces ratings. You can additionally specify a list of countries
        if you wish to display only members from those countries. Country data is
        sourced from codeforces profiles. e.g. ;handle list Croatia Slovenia
        """
        if resource in _CLIST_RESOURCE_SHORT_FORMS:
            resource = _CLIST_RESOURCE_SHORT_FORMS[resource]
        if resource!='codeforces.com' and resource not in _SUPPORTED_CLIST_RESOURCES:
            raise HandleCogError(f'The resource `{resource}` is not supported.')
        countries = []
        users = None
        wait_msg = await ctx.channel.send('Fetching handle list, please wait...')
        if resource=='codeforces.com':
            res = cf_common.user_db.get_cf_users_for_guild(ctx.guild.id)
            users = [(ctx.guild.get_member(user_id), cf_user.handle, cf_user.rating)
                    for user_id, cf_user in res if not countries or cf_user.country in countries]
            users = [(member, handle, rating, 0) for member, handle, rating in users if member is not None]
        else:
            account_ids = cf_common.user_db.get_account_ids_for_resource(ctx.guild.id ,resource)
            members = {}
            ids = []
            for user_id, account_id, handle in account_ids:
                ids.append(account_id)
                members[account_id] = ctx.guild.get_member(user_id)
            clist_users = await clist.fetch_user_info(resource, ids)
            users = []
            for clist_user in clist_users:
                handle = clist_user['handle']
                if resource=='codedrills.io':
                    handle = clist_user['name'] or ' '
                rating = int(clist_user['rating']) if clist_user['rating']!=None else None
                member = members[int(clist_user['id'])]
                n_contests = clist_user['n_contests']
                users.append((member, handle, rating, n_contests))
        if not users:
            raise HandleCogError('No members with registered handles.')

        users.sort(key=lambda x: (1 if x[2] is None else -x[2], -x[3],x[1]))  # Sorting by (-rating,-contests, handle)
        title = 'Handles of server members '+(('('+resource+')') if resource!=None else '')
        if countries:
            title += ' from ' + ', '.join(f'`{country}`' for country in countries)
        pages = _make_pages(users, title, resource)
        await wait_msg.delete()
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=_PAGINATE_WAIT_TIME,
                           set_pagenum_footers=True)

    @handle.command(brief="Show handles, but prettier", usage="[website] [page no]")
    async def pretty(self, ctx, arg1:str = None, arg2:str=None):
        """Show members of the server who have registered their handles and their Codeforces
        ratings, in color.
        """
        page_no = None
        resource = None
        if arg1!=None and arg2!=None:
            resource = arg1
            if resource in _CLIST_RESOURCE_SHORT_FORMS:
                resource = _CLIST_RESOURCE_SHORT_FORMS[arg1]
            try:
                page_no = int(arg2)
            except:
                page_no = -1  
        elif arg1!=None:
            if arg1 in _CLIST_RESOURCE_SHORT_FORMS:
                resource = _CLIST_RESOURCE_SHORT_FORMS[arg1]
            elif arg1 in _SUPPORTED_CLIST_RESOURCES:
                resource = arg1
            else:
                try:
                    page_no = int(arg1)
                except:
                    page_no = -1    
        wait_msg = await ctx.channel.send("Getting handle list...")
        rows = []
        author_idx = None
        if resource is not None and resource!='codeforces.com':
            if resource not in ['codechef.com', 'atcoder.jp']:
                raise HandleCogError(resource+' is not supported for handle pretty command.')
            id_to_member = dict()
            account_ids = cf_common.user_db.get_account_ids_for_resource(ctx.guild.id ,resource)
            ids = []
            for user_id, account_id, handle in account_ids:
                ids.append(account_id)
                id_to_member[account_id] = ctx.guild.get_member(user_id)
            clist_users = await clist.fetch_user_info(resource, account_ids=ids)
            clist_users.sort(key=lambda user: int(user['rating']) if user['rating'] is not None else -1, reverse=True)
            for user in clist_users:
                if user['id'] not in id_to_member: continue
                member = id_to_member[user['id']]
                if member is None: continue
                idx = len(rows)
                if member==ctx.author:
                    author_idx = idx
                rows.append((idx, member.display_name, user['handle'], user['rating']))
        else:
            user_id_cf_user_pairs = cf_common.user_db.get_cf_users_for_guild(ctx.guild.id)
            user_id_cf_user_pairs.sort(key=lambda p: p[1].rating if p[1].rating is not None else -1,
                                    reverse=True)
            for user_id, cf_user in user_id_cf_user_pairs:
                member = ctx.guild.get_member(user_id)
                if member is None:
                    continue
                idx = len(rows)
                if member == ctx.author:
                    author_idx = idx
                rows.append((idx, member.display_name, cf_user.handle, cf_user.rating))

        if not rows:
            raise HandleCogError('No members with registered handles.')
        max_page = math.ceil(len(rows) / _PRETTY_HANDLES_PER_PAGE) - 1
        if author_idx is None and page_no is None:
            raise HandleCogError(f'Please specify a page number between 0 and {max_page}.')

        msg = None
        if page_no is not None:
            if page_no < 0 or max_page < page_no:
                msg_fmt = 'Page number must be between 0 and {}. Showing page {}.'
                if page_no < 0:
                    msg = msg_fmt.format(max_page, 0)
                    page_no = 0
                else:
                    msg = msg_fmt.format(max_page, max_page)
                    page_no = max_page
            start_idx = page_no * _PRETTY_HANDLES_PER_PAGE
        else:
            msg = f'Showing neighbourhood of user `{ctx.author.display_name}`.'
            num_before = (_PRETTY_HANDLES_PER_PAGE - 1) // 2
            start_idx = max(0, author_idx - num_before)
        rows_to_display = rows[start_idx : start_idx + _PRETTY_HANDLES_PER_PAGE]
        img = None
        if resource=='codechef.com':
            img = get_prettyhandles_image(rows_to_display, self.font, color_converter=cc_rating_to_color)
        elif resource=='atcoder.jp':
            img = get_prettyhandles_image(rows_to_display, self.font, color_converter=ac_rating_to_color)
        else:
            img = get_prettyhandles_image(rows_to_display, self.font)
        buffer = io.BytesIO()
        img.save(buffer, 'png')
        buffer.seek(0)
        await wait_msg.delete()
        await ctx.send(msg, file=discord.File(buffer, 'handles.png'))

    async def _update_ranks_all(self, guild):
        """For each member in the guild, fetches their current ratings and updates their role if
        required.
        """
        res = cf_common.user_db.get_handles_for_guild(guild.id)
        await self._update_ranks(guild, res)
    
    async def _update_stars_all(self, guild):
        res = cf_common.user_db.get_account_ids_for_resource(guild.id, "codechef.com")
        await self._update_stars(guild, res)    

    async def _update_stars(self, guild, res):
        if not res:
            raise HandleCogError('Handles not set for any user')
        id_to_member = {account_id: guild.get_member(user_id) for user_id, account_id, handle in res}
        account_ids = [account_id for user_id, account_id, handle in res]
        clist_users = await clist.fetch_user_info("codechef.com", account_ids=account_ids)
        required_roles = {rating2star(user['rating']).title for user in clist_users if user['rating']!=None}
        star2role = {role.name: role for role in guild.roles if role.name in required_roles}
        missing_roles = required_roles - star2role.keys()
        if missing_roles:
            roles_str = ', '.join(f'`{role}`' for role in missing_roles)
            plural = 's' if len(missing_roles) > 1 else ''
            raise HandleCogError(f'Role{plural} for rank{plural} {roles_str} not present in the server')
        for user in clist_users:
            if user['id'] in id_to_member:
                member = id_to_member[user['id']]
                role_to_assign = None if user['rating'] is None else star2role[rating2star(user['rating']).title]
                await self.update_member_star_role(member, role_to_assign, reason='CodeChef star updates')

    async def _update_ranks(self, guild, res):
        member_handles = [(guild.get_member(user_id), handle) for user_id, handle in res]
        member_handles = [(member, handle) for member, handle in member_handles if member is not None]
        if not member_handles:
            raise HandleCogError('Handles not set for any user')
        members, handles = zip(*member_handles)
        users = await cf.user.info(handles=handles)
        for user in users:
            cf_common.user_db.cache_cf_user(user)
        cf_common.user_db.update();
        required_roles = {user.rank.title for user in users if user.rank != cf.UNRATED_RANK}
        rank2role = {role.name: role for role in guild.roles if role.name in required_roles}
        missing_roles = required_roles - rank2role.keys()
        if missing_roles:
            roles_str = ', '.join(f'`{role}`' for role in missing_roles)
            plural = 's' if len(missing_roles) > 1 else ''
            raise HandleCogError(f'Role{plural} for rank{plural} {roles_str} not present in the server')

        for member, user in zip(members, users):
            role_to_assign = None if user.rank == cf.UNRATED_RANK else rank2role[user.rank.title]
            await self.update_member_rank_role(member, role_to_assign,
                                               reason='Codeforces rank update')

    @staticmethod
    def _make_rankup_embeds(guild, contest, change_by_handle):
        """Make an embed containing a list of rank changes and top rating increases for the members
        of this guild.
        """
        user_id_handle_pairs = cf_common.user_db.get_handles_for_guild(guild.id)
        member_handle_pairs = [(guild.get_member(user_id), handle)
                               for user_id, handle in user_id_handle_pairs]
        def ispurg(member):
            # TODO: temporary code, todo properly later
            return any(role.name == 'Purgatory' for role in member.roles)

        member_change_pairs = [(member, change_by_handle[handle])
                               for member, handle in member_handle_pairs
                               if member is not None and handle in change_by_handle and not ispurg(member)]
        if not member_change_pairs:
            raise HandleCogError(f'Contest `{contest.id} | {contest.name}` was not rated for any '
                                 'member of this server.')

        member_change_pairs.sort(key=lambda pair: pair[1].newRating, reverse=True)
        rank_to_role = {role.name: role for role in guild.roles}

        def rating_to_displayable_rank(rating):
            rank = cf.rating2rank(rating).title
            role = rank_to_role.get(rank)
            return role.mention if role else rank

        rank_changes_str = []
        for member, change in member_change_pairs:
            cache = cf_common.cache2.rating_changes_cache
            if (change.oldRating == 1500
                    and len(cache.get_rating_changes_for_handle(change.handle)) == 1):
                # If this is the user's first rated contest.
                old_role = 'Unrated'
            else:
                old_role = rating_to_displayable_rank(change.oldRating)
            new_role = rating_to_displayable_rank(change.newRating)
            if new_role != old_role:
                rank_change_str = (f'{member.mention} [{change.handle}]({cf.PROFILE_BASE_URL}{change.handle}): {old_role} '
                                   f'\N{LONG RIGHTWARDS ARROW} {new_role}')
                rank_changes_str.append(rank_change_str)

        member_change_pairs.sort(key=lambda pair: pair[1].newRating - pair[1].oldRating,
                                 reverse=True)
        top_increases_str = []
        for member, change in member_change_pairs[:_TOP_DELTAS_COUNT]:
            delta = change.newRating - change.oldRating
            if delta <= 0:
                break
            increase_str = (f'{member.mention} [{change.handle}]({cf.PROFILE_BASE_URL}{change.handle}): {change.oldRating} '
                            f'\N{HORIZONTAL BAR} **{delta:+}** \N{LONG RIGHTWARDS ARROW} '
                            f'{change.newRating}')
            top_increases_str.append(increase_str)

        rank_changes_str = rank_changes_str or ['No rank changes']

        embed_heading = discord.Embed(
            title=contest.name, url=contest.url, description="")
        embed_heading.set_author(name="Rank updates")
        embeds = [embed_heading]

        for rank_changes_chunk in paginator.chunkify(
                rank_changes_str, _MAX_RATING_CHANGES_PER_EMBED):
            desc = '\n'.join(rank_changes_chunk)
            embed = discord.Embed(description=desc)
            embeds.append(embed)

        top_rating_increases_embed = discord.Embed(description='\n'.join(
            top_increases_str) or 'Nobody got a positive delta :(')
        top_rating_increases_embed.set_author(name='Top rating increases')

        embeds.append(top_rating_increases_embed)
        discord_common.set_same_cf_color(embeds)

        return embeds

    @commands.group(brief='Commands for role updates',
                    invoke_without_command=True, hidden=True)
    async def roleupdate(self, ctx):
        """Group for commands involving role updates."""
        await ctx.send_help(ctx.command)
    
    @roleupdate.command(brief='Update CodeChef star roles')
    @commands.check_any(commands.has_any_role('Admin', constants.TLE_MODERATOR), commands.is_owner())
    async def codechef(self, ctx):
        wait_msg = await ctx.channel.send("Updating codechef stars...")
        await self._update_stars_all(ctx.guild)
        await wait_msg.delete()
        await ctx.send(embed=discord_common.embed_success('Roles updated successfully.'))

    @roleupdate.command(brief='Update Codeforces rank roles')
    @commands.check_any(commands.has_any_role('Admin', constants.TLE_MODERATOR), commands.is_owner())
    async def now(self, ctx):
        """Updates Codeforces rank roles for every member in this server."""
        await self._update_ranks_all(ctx.guild)
        await ctx.send(embed=discord_common.embed_success('Roles updated successfully.'))

    @roleupdate.command(brief='Enable or disable auto role updates',
                        usage='on|off')
    @commands.check_any(commands.has_any_role('Admin', constants.TLE_MODERATOR), commands.is_owner())
    async def auto(self, ctx, arg):
        """Auto role update refers to automatic updating of rank roles when rating
        changes are released on Codeforces. 'on'/'off' disables or enables auto role
        updates.
        """
        if arg == 'on':
            rc = cf_common.user_db.enable_auto_role_update(ctx.guild.id)
            if not rc:
                raise HandleCogError('Auto role update is already enabled.')
            await ctx.send(embed=discord_common.embed_success('Auto role updates enabled.'))
        elif arg == 'off':
            rc = cf_common.user_db.disable_auto_role_update(ctx.guild.id)
            if not rc:
                raise HandleCogError('Auto role update is already disabled.')
            await ctx.send(embed=discord_common.embed_success('Auto role updates disabled.'))
        else:
            raise ValueError(f"arg must be 'on' or 'off', got '{arg}' instead.")

    @roleupdate.command(brief='Publish a rank update for the given contest',
                        usage='here|off|contest_id')
    @commands.check_any(commands.has_any_role('Admin', constants.TLE_MODERATOR), commands.is_owner())
    async def publish(self, ctx, arg):
        """This is a feature to publish a summary of rank changes and top rating
        increases in a particular contest for members of this server. 'here' will
        automatically publish the summary to this channel whenever rating changes on
        Codeforces are released. 'off' will disable auto publishing. Specifying a
        contest id will publish the summary immediately.
        """
        if arg == 'here':
            cf_common.user_db.set_rankup_channel(ctx.guild.id, ctx.channel.id)
            await ctx.send(
                embed=discord_common.embed_success('Auto rank update publishing enabled.'))
        elif arg == 'off':
            rc = cf_common.user_db.clear_rankup_channel(ctx.guild.id)
            if not rc:
                raise HandleCogError('Rank update publishing is already disabled.')
            await ctx.send(embed=discord_common.embed_success('Rank update publishing disabled.'))
        else:
            try:
                contest_id = int(arg)
            except ValueError:
                raise ValueError(f"arg must be 'here', 'off' or a contest ID, got '{arg}' instead.")
            await self._publish_now(ctx, contest_id)

    async def _publish_now(self, ctx, contest_id):
        try:
            contest = cf_common.cache2.contest_cache.get_contest(contest_id)
        except cache_system2.ContestNotFound as e:
            raise HandleCogError(f'Contest with id `{e.contest_id}` not found.')
        if contest.phase != 'FINISHED':
            raise HandleCogError(f'Contest `{contest_id} | {contest.name}` has not finished.')
        try:
            changes = await cf.contest.ratingChanges(contest_id=contest_id)
        except cf.RatingChangesUnavailableError:
            changes = None
        if not changes:
            raise HandleCogError(f'Rating changes are not available for contest `{contest_id} | '
                                 f'{contest.name}`.')

        change_by_handle = {change.handle: change for change in changes}
        rankup_embeds = self._make_rankup_embeds(ctx.guild, contest, change_by_handle)
        for rankup_embed in rankup_embeds:
            await ctx.channel.send(embed=rankup_embed)

    async def _generic_remind(self, ctx, action, role_name, what):
        roles = [role for role in ctx.guild.roles if role.name == role_name]
        if not roles:
            raise HandleCogError(f'Role `{role_name}` not present in the server')
        role = roles[0]
        if action == 'give':
            if role in ctx.author.roles:
                await ctx.send(embed=discord_common.embed_neutral(f'You are already subscribed to {what} reminders'))
                return
            await ctx.author.add_roles(role, reason=f'User subscribed to {what} reminders')
            await ctx.send(embed=discord_common.embed_success(f'Successfully subscribed to {what} reminders'))
        elif action == 'remove':
            if role not in ctx.author.roles:
                await ctx.send(embed=discord_common.embed_neutral(f'You are not subscribed to {what} reminders'))
                return
            await ctx.author.remove_roles(role, reason=f'User unsubscribed from {what} reminders')
            await ctx.send(embed=discord_common.embed_success(f'Successfully unsubscribed from {what} reminders'))
        else:
            raise HandleCogError(f'Invalid action {action}')

    @commands.command(brief='Grants or removes the specified pingable role',
                      usage='[give/remove] [vc/duel]')
    async def role(self, ctx, action: str, which: str):
        """e.g. ;role remove duel"""
        if which == 'vc':
            await self._generic_remind(ctx, action, 'Virtual Contestant', 'vc')
        elif which == 'duel':
            await self._generic_remind(ctx, action, 'Duelist', 'duel')
        else:
            raise HandleCogError(f'Invalid role {which}')

    @discord_common.send_error_if(HandleCogError, cf_common.HandleIsVjudgeError)
    async def cog_command_error(self, ctx, error):
        pass


def setup(bot):
    bot.add_cog(Handles(bot))
