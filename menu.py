"""
Misumi AI — Telegram "/" buyruqlar menyusi (set_my_commands).
Bot ishga tushganda (post_init) bir marta o'rnatiladi. Faqat eng kerakli ~20 ta buyruq —
qolganlari /start dagi bo'limlarda.
"""
import logging

from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats

logger = logging.getLogger("misumi-menu")

_PRIVATE = {
    None: [  # uz (standart)
        ("start", "Bosh menyu"),
        ("imagine", "Rasm yaratish"),
        ("ovoz", "Matnni ovozga aylantirish"),
        ("yt", "YouTube qidiruv / musiqa"),
        ("yukla", "Havola orqali video yuklash"),
        ("anime", "Anime qidirish"),
        ("stiker", "Stiker yasash"),
        ("dars", "O'qish yordamchisi"),
        ("translate", "Tarjima"),
        ("weather", "Ob-havo"),
        ("referral", "Do'st taklif qil — Pro ol"),
        ("reset", "Suhbatni tozalash"),
        ("paysupport", "To'lov bo'yicha yordam"),
    ],
    "ru": [
        ("start", "Главное меню"),
        ("imagine", "Создать картинку"),
        ("ovoz", "Текст в голос"),
        ("yt", "YouTube / музыка"),
        ("yukla", "Скачать видео по ссылке"),
        ("anime", "Поиск аниме"),
        ("stiker", "Создать стикер"),
        ("dars", "Помощник в учёбе"),
        ("translate", "Перевод"),
        ("weather", "Погода"),
        ("referral", "Пригласи друга — получи Pro"),
        ("reset", "Очистить диалог"),
        ("paysupport", "Помощь по оплате"),
    ],
    "en": [
        ("start", "Main menu"),
        ("imagine", "Generate an image"),
        ("ovoz", "Text to speech"),
        ("yt", "YouTube / music"),
        ("yukla", "Download video from a link"),
        ("anime", "Search anime"),
        ("stiker", "Make a sticker"),
        ("dars", "Study helper"),
        ("translate", "Translate"),
        ("weather", "Weather"),
        ("referral", "Invite a friend — get Pro"),
        ("reset", "Reset conversation"),
        ("paysupport", "Payment help"),
    ],
}

_GROUP = [
    ("start", "Botni ishga tushirish"),
    ("mafia", "Mafia o'yinini boshlash"),
    ("ship", "Ship (juftlik)"),
    ("duel", "Duel"),
    ("quiz", "Viktorina"),
    ("tictactoe", "Krestik-nolik"),
    ("rps", "Tosh-qaychi-qog'oz"),
    ("reyting", "Reyting"),
]


async def setup(app) -> None:
    bot = app.bot
    try:
        for lang, cmds in _PRIVATE.items():
            kw = {"language_code": lang} if lang else {}
            await bot.set_my_commands(
                [BotCommand(c, d) for c, d in cmds], scope=BotCommandScopeAllPrivateChats(), **kw
            )
        await bot.set_my_commands(
            [BotCommand(c, d) for c, d in _GROUP], scope=BotCommandScopeAllGroupChats()
        )
        logger.info("[menu] buyruqlar menyusi o'rnatildi")
    except Exception:
        logger.exception("[menu] buyruqlar menyusini o'rnatib bo'lmadi")
