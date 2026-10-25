"""Published slash-command menus; authorization remains in the router."""
import logging

from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from jmemby.bot.access import GROUP_USER_COMMANDS

PRIVATE_COMMANDS = (
    ('help', '使用帮助'), ('jm', '直接下载漫画：/jm ID'),
    ('register', '注册账号'), ('my', '阅读账号和服务器地址'),
    ('account', '领取或补开阅读账号'), ('report', '举报不合适内容：/report ID'),
    ('like', '给漫画点赞：/like ID'), ('random', '随机推荐一部漫画'),
    ('rank', '查看人气前十'),
)


async def configure_commands(bot):
    commands = [BotCommand(command=command, description=description) for command, description in PRIVATE_COMMANDS]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands([c for c in commands if '/' + c.command in GROUP_USER_COMMANDS],
                                  scope=BotCommandScopeAllGroupChats())
    except Exception as exc:
        logging.getLogger(__name__).warning('command_menu_update_failed type=%s', type(exc).__name__)
