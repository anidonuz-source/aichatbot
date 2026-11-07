"""
Misumi AI — Mafia o'yini mexanikasi
===================================
Telegram'dan mustaqil sof Python: rollar, fazalar (lobbi -> rol ochilishi -> tun -> kun -> ovoz),
tun natijasini hisoblash, g'alaba shartlari va Misumi'ning o'yinchi sifatidagi "miyasi".

Bot (mafia_bot.py) va Mini App API (mafia_api.py) shu modulning ustida ishlaydi.
Ikkalasi ham bitta jarayonda, shuning uchun holat xotirada, bitta RLock bilan himoyalangan.

Fazalar:  lobby -> reveal -> night -> day -> vote -> night -> ... -> ended
Rollar:   Fuqaro, Komissar, Doktor, Qo'riqchi, Kelin (bloklovchi) | Mafiya, Don | Maniak (yakka)
"""
import os
import random
import secrets
import threading
import time
from collections import Counter

LOCK = threading.RLock()

BOT_UID = 0                       # Misumi AI (gapiradigan bot). Qolgan botlar: -1, -2, ...
BOT_NAME = "Misumi AI"
BOT_NAMES = ["Misumi AI", "Bot Akmal", "Bot Dilya", "Bot Sardor", "Bot Malika", "Bot Jasur"]
MAX_BOTS = len(BOT_NAMES)
MIN_PLAYERS, MAX_PLAYERS = 4, 16

REVEAL_SEC = int(os.environ.get("MAFIA_REVEAL_SEC", "12"))
NIGHT_SEC = int(os.environ.get("MAFIA_NIGHT_SEC", "50"))
NIGHT_MIN_SEC = int(os.environ.get("MAFIA_NIGHT_MIN_SEC", "20"))   # tez tugasa rollar fosh bo'lmasin
DAY_SEC = int(os.environ.get("MAFIA_DAY_SEC", "90"))
VOTE_SEC = int(os.environ.get("MAFIA_VOTE_SEC", "45"))
LOBBY_TTL = 30 * 60
KEEP_ENDED_SEC = 30 * 60
AFK_SEC = 75                       # shuncha vaqt Mini App ochilmasa, o'yinchi "AFK" deb belgilanadi
EMOJIS = ["👍", "👎", "😂", "😱", "🔥", "🤔", "🙏", "😈"]
REACTION_TTL = 10
OPTIONAL_ROLES = ["commissar", "doctor", "lucky", "escort", "journalist", "bodyguard", "sniper",
                  "avenger", "judge", "maniac", "vampire", "don", "lawyer"]
DEFAULT_SETTINGS = {
    "night_sec": NIGHT_SEC, "day_sec": DAY_SEC, "vote_sec": VOTE_SEC,
    "safe_first_night": False,     # birinchi tunda hujumlar o'tmaydi
    "last_words": True,            # o'lgan o'yinchi bitta oxirgi so'z qoldiradi
    "lovers": True,                # 8+ o'yinchida ikki sevishgan
    "disabled": [],                # o'chirilgan rollar
}
SETTING_LIMITS = {"night_sec": (25, 120), "day_sec": (30, 240), "vote_sec": (20, 120)}

ROLES = {
    "citizen": {
        "name": "Fuqaro", "emoji": "👤", "team": "town", "action": None,
        "desc": "Oddiy fuqarosan. Qurolsan, lekin ovozing kuchli: kunduzi mafiyani top va osib qo'y.",
    },
    "commissar": {
        "name": "Komissar", "emoji": "🕵️", "team": "town", "action": "check",
        "desc": "Har tun bitta o'yinchini tekshirasan: u mafiyami yoki yo'q. Don va Maniak senga «tinch» ko'rinadi.",
    },
    "doctor": {
        "name": "Doktor", "emoji": "🩺", "team": "town", "action": "heal",
        "desc": "Har tun bitta o'yinchini davolaysan, o'sha tun o'lmaydi. Ketma-ket bir odamni emas; o'zingni butun o'yinda 1 marta.",
    },
    "bodyguard": {
        "name": "Qo'riqchi", "emoji": "🛡", "team": "town", "action": "guard",
        "desc": "Har tun bitta o'yinchini qo'riqlaysan. Unga hujum bo'lsa, uning o'rniga sen o'lasan.",
    },
    "escort": {
        "name": "Kelin", "emoji": "💃", "team": "town", "action": "block",
        "desc": "Har tun bitta o'yinchini band qilasan, u o'sha tun harakat qila olmaydi. Ketma-ket bir odamni emas.",
    },
    "mafia": {
        "name": "Mafiya", "emoji": "🔫", "team": "mafia", "action": "kill",
        "desc": "Mafiya jamoasidasan. Tunda jamoa bilan kelishib bitta o'yinchini o'ldirasan. Mafiya chatidan foydalan.",
    },
    "don": {
        "name": "Don", "emoji": "🎩", "team": "mafia", "action": "kill",
        "desc": "Mafiya boshlig'i. Ovozlar teng bo'lsa, qaror seniki. Komissar senga «tinch» deb ko'radi.",
    },
    "maniac": {
        "name": "Maniak", "emoji": "🔪", "team": "maniac", "action": "slay",
        "desc": "Yakkasan: hamma senga qarshi. Har tun bitta odamni o'ldirasan. Oxirgi tirik qolsang yutasan.",
    },
    "journalist": {
        "name": "Jurnalist", "emoji": "📰", "team": "town", "action": "compare",
        "desc": "Har tun ikki o'yinchini tanlaysan va ular bir jamoadami yoki yo'qligini bilasan.",
    },
    "sniper": {
        "name": "Snayper", "emoji": "🎯", "team": "town", "action": "snipe",
        "desc": "Butun o'yinda bitta o'qing bor. Tunda otasan. Begunohni (shahar) o'ldirsang, o'zing ham o'lasan.",
    },
    "lucky": {
        "name": "Omadli", "emoji": "🍀", "team": "town", "action": None,
        "desc": "Kechasi bo'lgan birinchi hujumdan omon qolasan. Ikkinchisi seni olib ketadi.",
    },
    "lawyer": {
        "name": "Advokat", "emoji": "⚖️", "team": "mafia", "action": "defend",
        "desc": "Mafiya jamoasidasan. Har tun bitta mafiyachini himoya qilasan: Komissar uni «tinch» deb ko'radi.",
    },
    "avenger": {
        "name": "Qasoskor", "emoji": "🗡", "team": "town", "action": "vow",
        "desc": "Har tun bitta o'yinchiga qasam ichasan. Kunduzi seni osib qo'yishsa, oxirgi qasam ichgan odaming sen bilan birga ketadi.",
    },
    "judge": {
        "name": "Sudya", "emoji": "👨‍⚖️", "team": "town", "action": None, "day_action": "veto",
        "desc": "Butun o'yinda bir marta ovoz berish paytida hukmni bekor qilasan: o'sha kuni hech kim osilmaydi. Sudya ekaningni hamma biladi.",
    },
    "vampire": {
        "name": "Vampir", "emoji": "🧛", "team": "vampire", "action": "bite",
        "desc": "Yakkasan. Har tun bitta odamni tishlaysan: birinchi tishlash belgi qoldiradi, ikkinchisi o'ldiradi. Oxirgi tirik qolsang yutasan.",
    },
}
TEAM_NAMES = {"town": "Shahar", "mafia": "Mafiya", "maniac": "Maniak", "vampire": "Vampir"}
ROLES_PUBLIC = {k: {"name": v["name"], "emoji": v["emoji"], "team": v["team"], "desc": v["desc"], "night": bool(v["action"]), "day": bool(v.get("day_action"))} for k, v in ROLES.items()}
ACTION_LABELS = {
    "kill": "O'ldirish", "slay": "O'ldirish", "check": "Tekshirish",
    "heal": "Davolash", "guard": "Qo'riqlash", "block": "Band qilish",
    "compare": "Solishtirish", "snipe": "O'q uzish", "defend": "Himoya qilish",
    "vow": "Qasam ichish", "bite": "Tishlash",
}

DEATH_LABELS = {
    "mafia": "mafiya o'ldirdi", "maniac": "maniak o'ldirdi", "sniper": "snayper otdi", "guard": "himoya qilib o'ldi",
    "guilt": "begunohni otdi", "lynch": "osildi", "heartbreak": "sevgilisi ketdi", "revenge": "qasoskor olib ketdi",
    "vampire": "vampir tishladi",
}
NOTES = {
    "guard": " (o'rniga o'zini tikdi)", "guilt": " (begunohni otib, o'zini ham yo'qotdi)",
    "heartbreak": " (sevgilisi ketgani uchun)", "revenge": " (Qasoskor o'zi bilan olib ketdi)",
    "vampire": " (Vampir tishladi)",
}

GAMES: dict[str, "Game"] = {}      # gid -> Game
CHAT_GAME: dict[int, str] = {}     # chat_id -> faol gid


def mafia_count(n: int) -> int:
    return {4: 1, 5: 1, 6: 1, 7: 1, 8: 1, 9: 2, 10: 2, 11: 2, 12: 2, 13: 3, 14: 3, 15: 3, 16: 3}[n]


def role_set(n: int, disabled=()) -> list[str]:
    """n ta o'yinchi uchun rollar ro'yxati (aralashtirilmagan). disabled — host o'chirgan rollar."""
    dis = set(disabled)
    specials = ["commissar", "doctor"]
    for need, r in ((6, "lucky"), (8, "escort"), (9, "journalist"), (11, "bodyguard"), (12, "sniper")):
        if n >= need:
            specials.append(r)
    solos = ["maniac"] if n >= 9 else []
    m = mafia_count(n)
    mafia = []
    if n >= 7:
        mafia.append("don")
    if n >= 10:
        mafia.append("lawyer")
    mafia = [r for r in mafia if r not in dis]
    mafia += ["mafia"] * (m - len(mafia))
    roles = mafia + [r for r in specials + solos if r not in dis]
    min_citizens = max(1, n // 8)
    for need, r in ((7, "avenger"), (10, "judge"), (13, "vampire")):
        if n >= need and r not in dis and len(roles) + 1 <= n - min_citizens:
            roles.append(r)
    while len(roles) > n:          # xavfsizlik: ortiqcha maxsus rollarni olib tashlaymiz
        roles.pop()
    roles += ["citizen"] * (n - len(roles))
    return roles


class P:
    def __init__(self, uid: int, name: str, username: str | None = None, is_bot: bool = False):
        self.uid = uid
        self.name = (name or str(uid)).strip()[:24] or str(uid)
        self.username = username
        self.photo: str | None = None
        self.is_bot = is_bot
        self.alive = True
        self.role: str | None = None
        self.last_heal: int | None = None
        self.self_healed = False
        self.last_block: int | None = None
        self.checked: dict[int, str] = {}      # komissar: uid -> "mafia" | "tinch"
        self.death: dict | None = None
        self.last_chat = 0.0
        self.shots = 1                          # snayper
        self.lucky_used = False                 # omadli
        self.lover: int | None = None           # sevishgan sherigi uid
        self.bitten = False                     # vampir tishlagan
        self.vow: int | None = None             # qasoskor: oxirgi qasam nishoni
        self.judge_used = False
        self.vetoed = False                     # sudya vetosini ishlatgan (yutuq uchun)
        self.sniper_hit_mafia = False
        self.lw_until = -1                      # oxirgi so'z shu faza raqamigacha ochiq
        self.last_word_used = False
        self.last_seen = time.time()

    @property
    def team(self) -> str | None:
        return ROLES[self.role]["team"] if self.role else None

    @property
    def action(self) -> str | None:
        return ROLES[self.role]["action"] if self.role else None


class Game:
    def __init__(self, chat_id: int, host_id: int, chat_title: str = ""):
        self.gid = secrets.token_hex(3)
        self.chat_id = chat_id
        self.chat_title = chat_title
        self.host_id = host_id
        self.created = time.time()
        self.last_activity = self.created
        self.players: dict[int, P] = {}
        self.phase = "lobby"
        self.day = 0
        self.phase_start = self.created
        self.deadline = 0.0
        self.duration = 0
        self.night_actions: dict[int, dict] = {}
        self.votes: dict[int, object] = {}       # voter -> target uid | "skip"
        self.ready: set[int] = set()
        self.chat: list[dict] = []
        self.feed: list[dict] = []
        self.private: dict[int, list[dict]] = {}
        self.outbox: list[dict] = []
        self.winner: str | None = None
        self.cancelled = False
        self.ended_at = 0.0
        self.version = 1
        self.lobby_msg_id: int | None = None
        self._eid = 0
        # Misumi "miyasi"
        self.bot_acted: set[int] = set()
        self.bot_due: dict[int, float] = {}
        self.bot_say_times: dict[int, list[float]] = {}
        self.accuse: Counter = Counter()
        # sozlamalar, reaksiyalar, tomoshabinlar, yakuniy jadval
        self.settings: dict = {**DEFAULT_SETTINGS, "disabled": []}
        self.phase_no = 0
        self.veto_by: int | None = None
        self.reactions: list[dict] = []
        self.spectators: dict[int, float] = {}
        self.log: list[dict] = []
        self.stats_recorded = False
        self.earned: dict[int, list[str]] = {}

    # ── ichki yordamchilar ─────────────────────────────────────────────
    def _id(self) -> int:
        self._eid += 1
        return self._eid

    def _bump(self) -> None:
        self.version += 1
        self.last_activity = time.time()

    def _out(self, kind: str, **data) -> None:
        self.outbox.append({"kind": kind, "gid": self.gid, **data})

    def _feed(self, text: str, kind: str = "info") -> None:
        self.feed.append({"id": self._id(), "kind": kind, "text": text, "day": self.day, "t": time.time()})
        self.feed = self.feed[-80:]

    def _notify(self, uid: int, text: str, kind: str = "info") -> None:
        if uid <= 0:
            return
        self.private.setdefault(uid, []).append({"id": self._id(), "kind": kind, "text": text, "t": time.time()})

    def _set_phase(self, phase: str, seconds: int) -> None:
        now = time.time()
        self.phase = phase
        self.phase_no += 1
        self.phase_start = now
        self.duration = seconds
        self.deadline = now + seconds
        self.bot_acted = set()
        hi = max(8, min(seconds - 8, 24))
        self.bot_due = {u: now + random.uniform(4, hi) for u in self.players if u <= 0}
        self._bump()

    def alive_players(self) -> list[P]:
        return [p for p in self.players.values() if p.alive]

    def _name(self, uid: int) -> str:
        p = self.players.get(uid)
        return p.name if p else "?"

    # ── lobbi ──────────────────────────────────────────────────────────
    def add_player(self, uid: int, name: str, username: str | None = None, is_bot: bool = False, photo: str | None = None) -> tuple[bool, str]:
        with LOCK:
            if self.phase != "lobby":
                return False, "O'yin allaqachon boshlangan"
            if uid in self.players:
                return True, "Sen allaqachon o'yindasan"
            if len(self.players) >= MAX_PLAYERS:
                return False, f"Joy qolmadi (maksimum {MAX_PLAYERS})"
            self.players[uid] = P(uid, name, username, is_bot)
            self.players[uid].photo = photo
            self._bump()
            self._out("lobby")
            return True, "Qo'shildi"

    def remove_player(self, uid: int) -> tuple[bool, str]:
        with LOCK:
            if self.phase != "lobby":
                return False, "O'yin boshlangan, chiqib bo'lmaydi"
            if uid not in self.players:
                return False, "Sen o'yinda emassan"
            del self.players[uid]
            humans = [u for u in self.players if u > 0]
            if not humans:
                self.cancel("Hamma chiqib ketdi, lobbi yopildi")
                return True, "Chiqding"
            if uid == self.host_id:
                self.host_id = humans[0]
            self._bump()
            self._out("lobby")
            return True, "Chiqding"

    def add_bot(self) -> tuple[bool, str]:
        with LOCK:
            slot = next((i for i in range(MAX_BOTS) if -i not in self.players), None)
            if slot is None:
                return False, f"Ko'pi bilan {MAX_BOTS} ta bot"
            return self.add_player(-slot, BOT_NAMES[slot], None, True)

    def remove_bot(self) -> tuple[bool, str]:
        with LOCK:
            if self.phase != "lobby":
                return False, "O'yin boshlangan"
            bots = [u for u in self.players if u <= 0]
            if not bots:
                return False, "Lobbida bot yo'q"
            del self.players[min(bots)]
            self._bump()
            self._out("lobby")
            return True, "Bot olib tashlandi"

    def bot_count(self) -> int:
        return sum(1 for u in self.players if u <= 0)

    def human_count(self) -> int:
        return sum(1 for p in self.players.values() if not p.is_bot)

    def start(self, by_uid: int, force: bool = False) -> tuple[bool, str]:
        with LOCK:
            if self.phase != "lobby":
                return False, "O'yin allaqachon boshlangan"
            if not force and by_uid != self.host_id:
                return False, "Faqat o'yin egasi boshlay oladi"
            n = len(self.players)
            if n < MIN_PLAYERS:
                return False, f"Kamida {MIN_PLAYERS} ta o'yinchi kerak (hozir {n})"
            if self.human_count() < 1:
                return False, "Kamida bitta odam kerak"
            roles = role_set(n, self.settings.get("disabled", []))
            random.shuffle(roles)
            now = time.time()
            for p, r in zip(self.players.values(), roles):
                p.role = r
                p.lover = None
                p.last_seen = now
            if self.settings.get("lovers", True) and n >= 8:
                a, b = random.sample(list(self.players.values()), 2)
                a.lover, b.lover = b.uid, a.uid
            self.day = 0
            self._feed("O'yin boshlandi. Rollar tarqatildi.", "start")
            self._set_phase("reveal", REVEAL_SEC)
            self._out("start")
            return True, "Boshlandi"

    def cancel(self, reason: str = "O'yin bekor qilindi") -> None:
        with LOCK:
            if self.phase == "ended":
                return
            self.cancelled = True
            self.phase = "ended"
            self.ended_at = time.time()
            self._feed(reason, "end")
            self._bump()
            self._out("cancel", reason=reason)

    # ── tun harakatlari ────────────────────────────────────────────────
    def targets_for(self, p: P) -> list[int]:
        """p hozirgi fazada tanlay oladigan nishonlar (uid ro'yxati)."""
        if not p.alive:
            return []
        if self.phase == "night" and p.action:
            a = p.action
            if a == "snipe" and p.shots <= 0:
                return []
            out = []
            for q in self.alive_players():
                if a == "kill" and q.team == "mafia":
                    continue
                if a == "defend" and q.team != "mafia":
                    continue
                if a in ("slay", "check", "guard", "block", "compare", "snipe", "vow", "bite") and q.uid == p.uid:
                    continue
                if a == "heal":
                    if q.uid == p.uid and p.self_healed:
                        continue
                    if q.uid == p.last_heal:
                        continue
                if a == "block" and q.uid == p.last_block:
                    continue
                out.append(q.uid)
            return out
        if self.phase == "vote":
            return [q.uid for q in self.alive_players() if q.uid != p.uid]
        return []

    def set_action(self, uid: int, target: int, target2: int | None = None) -> tuple[bool, str]:
        with LOCK:
            p = self.players.get(uid)
            if not p or not p.alive:
                return False, "Sen o'yinda emassan yoki o'lgansan"
            if self.phase != "night":
                return False, "Hozir tun emas"
            if not p.action:
                return False, "Sening rolingda tungi harakat yo'q"
            allowed = self.targets_for(p)
            rec = {"type": p.action, "target": target}
            if p.action == "snipe" and target == -1 and allowed:
                rec["target"] = None
            elif target not in allowed:
                return False, "Bu o'yinchini tanlab bo'lmaydi"
            if p.action == "compare":
                if target2 not in allowed or target2 == target:
                    return False, "Ikkita turli o'yinchi tanla"
                rec["target2"] = target2
            self.night_actions[uid] = rec
            self._bump()
            return True, "Qabul qilindi"

    def set_vote(self, uid: int, target) -> tuple[bool, str]:
        with LOCK:
            p = self.players.get(uid)
            if not p or not p.alive:
                return False, "O'lganlar ovoz bera olmaydi"
            if self.phase != "vote":
                return False, "Hozir ovoz berish vaqti emas"
            if target != "skip":
                try:
                    target = int(target)
                except (TypeError, ValueError):
                    return False, "Noto'g'ri nishon"
                if target not in self.targets_for(p):
                    return False, "Bu o'yinchiga ovoz berib bo'lmaydi"
            self.votes[uid] = target
            self._bump()
            return True, "Ovoz qabul qilindi"

    def set_ready(self, uid: int) -> tuple[bool, str]:
        with LOCK:
            p = self.players.get(uid)
            if not p or not p.alive or self.phase != "day":
                return False, "Hozir mumkin emas"
            self.ready.add(uid)
            self._bump()
            return True, "Tayyor"

    # ── host sozlamalari ───────────────────────────────────────────────
    def set_settings(self, uid: int, patch: dict) -> tuple[bool, str]:
        with LOCK:
            if self.phase != "lobby":
                return False, "Sozlamalar faqat lobbida o'zgaradi"
            if uid != self.host_id:
                return False, "Faqat o'yin egasi sozlay oladi"
            st = dict(self.settings)
            for k, (lo, hi) in SETTING_LIMITS.items():
                if k in patch:
                    try:
                        st[k] = max(lo, min(hi, int(patch[k])))
                    except (TypeError, ValueError):
                        return False, "Vaqt son bo'lishi kerak"
            for k in ("safe_first_night", "last_words", "lovers"):
                if k in patch:
                    st[k] = bool(patch[k])
            if "disabled" in patch:
                st["disabled"] = [r for r in dict.fromkeys(patch["disabled"] or []) if r in OPTIONAL_ROLES]
            self.settings = st
            self._bump()
            self._out("lobby")
            return True, "Sozlamalar saqlandi"

    # ── oxirgi so'z, reaksiya, sudya veto, AFK ─────────────────────────
    def can_last_word(self, p: P) -> bool:
        return (not p.alive and not p.is_bot and not p.last_word_used and self.settings.get("last_words", True)
                and p.death is not None and self.phase not in ("lobby", "ended") and self.phase_no <= p.lw_until)

    def last_word(self, uid: int, text: str) -> tuple[bool, str]:
        with LOCK:
            p = self.players.get(uid)
            if not p or not self.can_last_word(p):
                return False, "Oxirgi so'z vaqti o'tgan yoki ishlatilgan"
            text = " ".join((text or "").split())[:160]
            if not text:
                return False, "Bo'sh xabar"
            p.last_word_used = True
            self.chat.append({"id": self._id(), "ch": "last", "uid": uid, "name": p.name, "text": text, "t": time.time()})
            self.chat = self.chat[-200:]
            self._bump()
            self._out("last_word", name=p.name, role=p.role, text=text)
            return True, "OK"

    def react(self, uid: int, emoji: str) -> tuple[bool, str]:
        with LOCK:
            p = self.players.get(uid)
            if not p:
                return False, "Sen o'yinchi emassan"
            if emoji not in EMOJIS:
                return False, "Bunday reaksiya yo'q"
            if self.phase == "lobby":
                return False, "O'yin boshlanmagan"
            now = time.time()
            if now - getattr(p, "last_react", 0.0) < 1.5:
                return False, "Sekinroq"
            p.last_react = now
            self.reactions.append({"id": self._id(), "uid": uid, "name": p.name, "emoji": emoji, "t": now})
            self.reactions = self.reactions[-30:]
            self._bump()
            return True, "OK"

    def use_veto(self, uid: int) -> tuple[bool, str]:
        with LOCK:
            p = self.players.get(uid)
            if not p or not p.alive or p.role != "judge":
                return False, "Sen Sudya emassan"
            if self.phase != "vote":
                return False, "Veto faqat ovoz berish paytida"
            if p.judge_used:
                return False, "Vetoni allaqachon ishlatgansan"
            p.judge_used = True
            p.vetoed = True
            self.veto_by = uid
            self._feed(f"⚖️ Sudya {p.name} hukmni bekor qildi.", "veto")
            self._bump()
            return True, "Veto qo'llandi"

    def touch(self, uid: int) -> None:
        now = time.time()
        p = self.players.get(uid)
        if p:
            p.last_seen = now
        elif uid > 0:
            self.spectators[uid] = now
            if len(self.spectators) > 200:
                for u in sorted(self.spectators, key=self.spectators.get)[:50]:
                    del self.spectators[u]

    def is_afk(self, p: P, now: float | None = None) -> bool:
        now = now or time.time()
        return (not p.is_bot and p.alive and self.phase in ("night", "day", "vote")
                and now - p.last_seen > AFK_SEC)

    def _log(self, text: str) -> None:
        self.log.append({"day": self.day, "text": text})
        self.log = self.log[-300:]

    def _finalize_deaths(self, died: list[tuple[int, str]], phase: str) -> list[dict]:
        """O'limlarni qo'llaydi; sevishgan va qasoskor zanjirlarini ham hisoblaydi."""
        out: list[dict] = []
        queue = list(died)
        while queue:
            uid, cause = queue.pop(0)
            p = self.players.get(uid)
            if p is None or not p.alive:
                continue
            self._kill(uid, cause, phase)
            out.append({"uid": uid, "name": p.name, "role": p.role, "cause": cause})
            lov = self.players.get(p.lover) if p.lover is not None else None
            if lov is not None and lov.alive:
                queue.append((lov.uid, "heartbreak"))
            if cause == "lynch" and p.role == "avenger" and p.vow is not None:
                tv = self.players.get(p.vow)
                if tv is not None and tv.alive:
                    queue.append((tv.uid, "revenge"))
        if out and not any(q.alive and q.role in ("mafia", "don") for q in self.players.values()):
            for q in self.alive_players():          # oxirgi qotil Advokat bo'lsa, u Mafiyaga aylanadi
                if q.role == "lawyer":
                    q.role = "mafia"
                    self._notify(q.uid, "🔫 Jamoang ketdi. Endi sen oddiy Mafiyasan: tunda o'ldira olasan.", "promote")
        for d in out:
            self._log(f"☠️ {d['name']} — {ROLES[d['role']]['emoji']} {ROLES[d['role']]['name']} ({DEATH_LABELS.get(d['cause'], d['cause'])})")
        return out

    # ── chat ───────────────────────────────────────────────────────────
    def post_chat(self, uid: int, text: str) -> tuple[bool, str]:
        with LOCK:
            p = self.players.get(uid)
            if not p:
                return False, "Sen o'yinchi emassan"
            text = " ".join((text or "").split())[:220]
            if not text:
                return False, "Bo'sh xabar"
            now = time.time()
            if not p.is_bot and now - p.last_chat < 1.0:
                return False, "Sekinroq"
            if self.phase == "ended" or self.phase in ("lobby", "reveal"):
                ch = "day"
            elif not p.alive:
                ch = "dead"
            elif self.phase == "night":
                if p.team != "mafia":
                    return False, "Tunda shahar uxlaydi, gapirib bo'lmaydi"
                ch = "mafia"
            else:
                ch = "day"
            p.last_chat = now
            self.chat.append({"id": self._id(), "ch": ch, "uid": uid, "name": p.name, "text": text, "t": now})
            self.chat = self.chat[-200:]
            self._bump()
            return True, "OK"

    # ── fazalar ────────────────────────────────────────────────────────
    def _enter_night(self) -> None:
        self.day += 1
        self.night_actions = {}
        self.votes = {}
        self.veto_by = None
        self._set_phase("night", self.settings["night_sec"])
        self._feed(f"{self.day}-tun boshlandi. Shahar uxladi.", "night")
        self._out("night", day=self.day)

    def _enter_day(self) -> None:
        self.ready = set()
        self._set_phase("day", self.settings["day_sec"])
        self.bot_say_times = {}
        self.accuse = Counter()
        for p in self.alive_players():
            if p.is_bot:
                k = random.choice([2, 2, 3]) if p.uid == BOT_UID else random.choice([1, 1, 2])
                lo = time.time() + 5
                hi = max(lo + 1, self.deadline - 8)
                self.bot_say_times[p.uid] = sorted(random.uniform(lo, hi) for _ in range(k))

    def _enter_vote(self) -> None:
        self.votes = {}
        self._set_phase("vote", self.settings["vote_sec"])
        self._feed("Ovoz berish boshlandi.", "vote")
        self._out("vote_start")

    def _night_complete(self) -> bool:
        now = time.time()
        for p in self.alive_players():
            if p.action and p.uid not in self.night_actions and self.targets_for(p) and not self.is_afk(p, now):
                return False
        return True

    def _kill(self, uid: int, cause: str, phase: str) -> None:
        p = self.players[uid]
        if not p.alive:
            return
        p.alive = False
        p.death = {"day": self.day, "cause": cause, "phase": phase}
        p.lw_until = self.phase_no + 1
        self._notify(uid, "Sen o'ldirilding. Endi kuzatuvchisan va hamma rollarni ko'rasan.", "death")

    def _resolve_night(self) -> None:
        acts = self.night_actions
        alive_ids = {p.uid for p in self.alive_players()}

        # 1) Kelin band qiladi
        blocked: set[int] = set()
        for uid, a in acts.items():
            if a["type"] == "block" and uid in alive_ids:
                blocked.add(a["target"])
                self.players[uid].last_block = a["target"]
        for uid in blocked:
            if uid in self.players and self.players[uid].action:
                self._notify(uid, "💃 Bu tun seni band qilishdi. Harakating bajarilmadi.", "block")

        for uid, a in acts.items():
            p = self.players[uid]
            if uid in alive_ids and p.role:
                tn = self._name(a["target"]) if a["target"] is not None else "o'q uzmadi"
                if a.get("target2") is not None:
                    tn += f" va {self._name(a['target2'])}"
                self._log(f"{ROLES[p.role]['emoji']} {p.name} ({ROLES[p.role]['name']}) → {tn}" +
                          (" [band qilingan]" if uid in blocked else ""))

        def act_of(role: str):
            for uid, a in acts.items():
                p = self.players[uid]
                if p.role == role and uid in alive_ids and uid not in blocked:
                    return p, a
            return None, None

        def tgt(a):
            return a["target"] if a else None

        # 2) Advokat himoyasi
        defended: set[int] = set()
        lawyer, la = act_of("lawyer")
        if lawyer and la:
            defended.add(la["target"])

        # 3) Mafiya ovozi (Don teng ovozni hal qiladi)
        mv: Counter = Counter()
        don_vote = None
        for p in self.alive_players():
            if p.role in ("mafia", "don") and p.uid not in blocked and p.uid in acts:
                t = acts[p.uid]["target"]
                mv[t] += 1
                if p.role == "don":
                    don_vote = t
        kill_target = None
        if mv:
            top = max(mv.values())
            cands = [t for t, c in mv.items() if c == top]
            kill_target = don_vote if don_vote in cands else random.choice(cands)

        maniac, ma = act_of("maniac")
        doctor, da = act_of("doctor")
        guard, ga = act_of("bodyguard")
        commissar, ca = act_of("commissar")
        journalist, ja = act_of("journalist")
        sniper, sa = act_of("sniper")
        slay_target, heal_target, guard_target, check_target = tgt(ma), tgt(da), tgt(ga), tgt(ca)
        snipe_target = tgt(sa)
        avenger, va = act_of("avenger")
        if avenger and va:
            avenger.vow = va["target"]
        vampire, vb = act_of("vampire")
        bite_target = tgt(vb)
        safe = bool(self.settings.get("safe_first_night")) and self.day == 1
        if doctor:
            doctor.last_heal = heal_target
            if heal_target == doctor.uid:
                doctor.self_healed = True

        # 4) Komissar tekshiruvi
        if commissar and check_target is not None:
            t = self.players[check_target]
            res = "mafia" if t.role in ("mafia", "lawyer") and check_target not in defended else "tinch"
            commissar.checked[check_target] = res
            if res == "mafia":
                self._notify(commissar.uid, f"🔎 Tekshiruv: {t.name} — MAFIYA!", "check")
            else:
                self._notify(commissar.uid, f"🔎 Tekshiruv: {t.name} — tinch (mafiya emas).", "check")

        # 5) Jurnalist
        if journalist and ja and ja.get("target2") is not None:
            a_, b_ = self.players[ja["target"]], self.players[ja["target2"]]
            same = a_.team == b_.team
            self._notify(journalist.uid, f"📰 {a_.name} va {b_.name}: " +
                         ("bir jamoada." if same else "turli jamoalarda."), "compare")

        # 6) Hujumlar
        attacks: list[tuple[int, str]] = []
        if safe:
            self._feed("Birinchi tun xavfsiz: hujumlar o'tmadi.", "info")
        else:
            if kill_target is not None:
                attacks.append((kill_target, "mafia"))
            if slay_target is not None:
                attacks.append((slay_target, "maniac"))
            if sniper and snipe_target is not None:
                sniper.shots -= 1
                attacks.append((snipe_target, "sniper"))
        if vampire and bite_target is not None:
            bp = self.players[bite_target]
            if bp.bitten and not safe:
                attacks.append((bite_target, "vampire"))
            else:
                bp.bitten = True
        died: list[tuple[int, str]] = []
        guard_used = False
        dead_now: set[int] = set()
        for t_uid, cause in attacks:
            if t_uid in dead_now:
                continue
            tp = self.players[t_uid]
            if guard and not guard_used and guard_target == t_uid and guard.uid not in dead_now and guard.uid != t_uid:
                guard_used = True
                dead_now.add(guard.uid)
                died.append((guard.uid, "guard"))
                self._notify(t_uid, "🛡 Bu tun senga hujum bo'ldi, lekin Qo'riqchi seni qutqardi.", "saved")
                continue
            if doctor and heal_target == t_uid:
                self._notify(t_uid, "🩺 Bu tun senga hujum bo'ldi, lekin Doktor seni davoladi.", "saved")
                self._notify(doctor.uid, f"🩺 {self._name(t_uid)} ga hujum bo'ldi, sen uni qutqarding!", "saved")
                continue
            if tp.role == "lucky" and not tp.lucky_used:
                tp.lucky_used = True
                self._notify(t_uid, "🍀 Bu tun senga hujum bo'ldi, lekin omading keldi. Ikkinchisidan omon qolmaysan.", "saved")
                continue
            dead_now.add(t_uid)
            died.append((t_uid, cause))
        if sniper and (snipe_target, "sniper") in died and self.players[snipe_target].team == "mafia":
            sniper.sniper_hit_mafia = True
        if sniper and (snipe_target, "sniper") in died and self.players[snipe_target].team == "town" \
                and sniper.uid not in dead_now:
            dead_now.add(sniper.uid)
            died.append((sniper.uid, "guilt"))

        deaths = self._finalize_deaths(died, "night")
        for d in deaths:
            self._feed(f"{d['name']} tunda o'ldi{NOTES.get(d['cause'], '')} — {ROLES[d['role']]['emoji']} {ROLES[d['role']]['name']}.", "death")
        if not deaths:
            self._feed("Bu tun hech kim o'lmadi.", "info")
        self._out("morning", day=self.day, deaths=deaths)
        self._bump()
        if not self._check_win():
            self._enter_day()

    def _resolve_vote(self) -> None:
        tally: Counter = Counter()
        skips = 0
        for voter, t in self.votes.items():
            if voter not in self.players or not self.players[voter].alive:
                continue
            if t == "skip":
                skips += 1
            else:
                tally[t] += 1
        summary = ", ".join(f"{self._name(u)}: {c}" for u, c in tally.most_common()) or "ovoz yo'q"
        lynched = None
        if tally:
            top = max(tally.values())
            leaders = [u for u, c in tally.items() if c == top]
            if len(leaders) == 1 and top > skips:
                lynched = leaders[0]
        who = ", ".join(f"{self._name(v)}→{self._name(t) if t != 'skip' else 'o`tkazdi'}"
                        for v, t in self.votes.items() if v in self.players)
        if who:
            self._log(f"🗳 Ovozlar: {who}")
        if lynched is not None and self.veto_by is not None:
            vname = self._name(self.veto_by)
            self._feed(f"Ovozlar: {summary}. Sudya {vname} hukmni bekor qildi, hech kim osilmadi.", "info")
            self._log(f"⚖️ Sudya {vname} {self._name(lynched)} hukmini bekor qildi")
            self._out("no_lynch", day=self.day, summary=summary, veto=vname)
        elif lynched is not None:
            deaths = self._finalize_deaths([(lynched, "lynch")], "day")
            p = self.players[lynched]
            extra = deaths[1:]
            info = {"uid": lynched, "name": p.name, "role": p.role, "summary": summary, "extra": extra}
            self._feed(f"Ovozlar: {summary}. {p.name} osildi — {ROLES[p.role]['emoji']} {ROLES[p.role]['name']}.", "lynch")
            for d in extra:
                self._feed(f"{d['name']} ham ketdi{NOTES.get(d['cause'], '')} — {ROLES[d['role']]['emoji']} {ROLES[d['role']]['name']}.", "death")
            self._out("lynch", day=self.day, **info)
        else:
            self._feed(f"Ovozlar: {summary}. Hech kim osilmadi.", "info")
            self._out("no_lynch", day=self.day, summary=summary)
        self._bump()
        if not self._check_win():
            self._enter_night()

    def _check_win(self) -> bool:
        alive = self.alive_players()
        m = sum(1 for p in alive if p.team == "mafia")
        solos = Counter(p.team for p in alive if p.team in ("maniac", "vampire"))
        sn = sum(solos.values())
        t = len(alive) - m - sn
        winner = None
        if m == 0 and sn == 0:
            winner = "town"
        elif m == 0 and len(solos) == 1 and t <= 1:
            winner = next(iter(solos))
        elif m > 0 and m >= t + sn:
            winner = "mafia"
        if winner:
            self._finish(winner)
            return True
        return False

    def _finish(self, winner: str) -> None:
        self.winner = winner
        self.phase = "ended"
        self.ended_at = time.time()
        self._feed(f"O'yin tugadi. G'olib: {TEAM_NAMES[winner]}.", "end")
        self._bump()
        self._out("end", winner=winner)
        if not self.stats_recorded:
            self.stats_recorded = True
            threading.Thread(target=self._record_stats, daemon=True).start()

    def _record_stats(self) -> None:
        try:
            import mafia_stats
            earned = mafia_stats.record_game(self)
        except Exception as e:
            print(f"[mafia] statistika yozilmadi: {e}")
            return
        if earned:
            with LOCK:
                self.earned = earned
                self._out("achievements", earned={str(k): v for k, v in earned.items()})
                self._bump()

    # ── tick ───────────────────────────────────────────────────────────
    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        with LOCK:
            if self.phase == "lobby":
                if now - self.last_activity > LOBBY_TTL:
                    self.cancel("Lobbi vaqti tugadi, o'yin bekor qilindi")
                return
            if self.phase == "ended":
                return
            self._bot_tick(now)
            if self.phase == "reveal":
                if now >= self.deadline:
                    self._enter_night()
            elif self.phase == "night":
                if now >= self.deadline or (now - self.phase_start >= min(NIGHT_MIN_SEC, self.settings["night_sec"] - 5)
                                            and self._night_complete()):
                    self._resolve_night()
            elif self.phase == "day":
                alive = len(self.alive_players())
                if now >= self.deadline or len(self.ready) * 2 > alive:
                    self._enter_vote()
            elif self.phase == "vote":
                alive_ids = [p.uid for p in self.alive_players()]
                all_in = all(u in self.votes or self.is_afk(self.players[u], now) for u in alive_ids)
                judge_wait = any(p.role == "judge" and not p.judge_used and not p.is_bot for p in self.alive_players()) \
                    and now - self.last_activity < 8
                if now >= self.deadline or (all_in and not judge_wait):
                    self._resolve_vote()

    # ── botlar ─────────────────────────────────────────────────────────
    def _bot_tick(self, now: float) -> None:
        for bot in [p for p in self.alive_players() if p.is_bot]:
            u = bot.uid
            if self.phase == "night" and u not in self.bot_acted and now >= self.bot_due.get(u, 0):
                self.bot_acted.add(u)
                self._bot_night(bot)
            elif self.phase == "day":
                ts = self.bot_say_times.get(u)
                while ts and now >= ts[0]:
                    ts.pop(0)
                    self._bot_say(bot)
            elif self.phase == "vote" and u not in self.bot_acted and now >= self.bot_due.get(u, 0):
                self.bot_acted.add(u)
                self._bot_vote(bot)

    def _bot_night(self, bot: P) -> None:
        opts = self.targets_for(bot)
        a = bot.action
        if not a or not opts:
            return
        pool, t2 = opts, None
        if a == "check":
            pool = [u for u in opts if u not in bot.checked] or opts
        elif a == "heal":
            pool = [u for u in opts if u != bot.uid] or opts
            if bot.uid in opts and random.random() < 0.25:
                pool = [bot.uid]
        elif a == "kill":
            mates = Counter(x["target"] for x in self.night_actions.values()
                            if x["type"] == "kill" and x["target"] in opts)
            if mates and random.random() < 0.85:
                pool = [mates.most_common(1)[0][0]]
        elif a == "compare":
            if len(opts) < 2:
                return
            pool, t2 = random.sample(opts, 2)[:1], None
            t2 = random.choice([u for u in opts if u != pool[0]])
        elif a == "snipe":
            pool = [random.choice(opts)] if random.random() < 0.12 else [-1]
        self.set_action(bot.uid, random.choice(pool), t2)

    def _bot_vote(self, bot: P) -> None:
        opts = self.targets_for(bot)
        if not opts:
            return
        if bot.role == "judge" and not bot.judge_used and random.random() < 0.2:
            self.use_veto(bot.uid)
        known = [u for u, r in bot.checked.items() if r == "mafia" and u in opts]
        if known:
            self.set_vote(bot.uid, known[0])
            return
        is_mafia = bot.team == "mafia"
        if is_mafia:
            opts = [u for u in opts if self.players[u].team != "mafia"] or opts
        acc = [u for u, _ in self.accuse.most_common() if u in opts]
        if acc and random.random() < (0.5 if is_mafia else 0.8):
            self.set_vote(bot.uid, acc[0])
            return
        tally = Counter(t for t in self.votes.values() if t != "skip" and t in opts)
        if tally and random.random() < 0.7:
            self.set_vote(bot.uid, tally.most_common(1)[0][0])
        elif random.random() < 0.1:
            self.set_vote(bot.uid, "skip")
        else:
            self.set_vote(bot.uid, random.choice(opts))

    def _bot_say(self, bot: P) -> None:
        others = [q for q in self.alive_players() if q.uid != bot.uid]
        if not others:
            return
        if bot.role == "commissar":
            known = [u for u, r in bot.checked.items() if r == "mafia" and self.players[u].alive]
            if known:
                t = self.players[known[0]]
                self.accuse[t.uid] += 2
                self.post_chat(bot.uid, random.choice([
                    f"{t.name} mafiya, men aniq bilaman. Unga ovoz bering",
                    f"Menda ma'lumot bor: {t.name} toza emas. Osamiz",
                    f"{t.name} ni tekshirdim, mafiya chiqdi. Ishoninglar",
                ]))
                return
        if bot.uid == BOT_UID and random.random() < 0.8:
            self._out("bot_say")      # mafia_bot.py AI bilan gapirtiradi
            return
        town = [q for q in others if q.team != "mafia"] if bot.team == "mafia" else others
        t = random.choice(town or others)
        if self.accuse and random.random() < 0.5:
            lead = self.accuse.most_common(1)[0][0]
            if lead in self.players and self.players[lead].alive and lead != bot.uid:
                self.post_chat(bot.uid, f"{self.players[lead].name} ga qo'shilaman, ovoz beraman")
                return
        self.post_chat(bot.uid, random.choice([
            f"{t.name} nimadir yashiryapti, ko'zimni olmayman",
            f"{t.name}, sen nega jimsan?",
            "shoshmay ovoz beraylik, keyin pushaymon bo'lamiz",
            f"{t.name} ning kechagi gapi menga yoqmadi",
            "bu tunni kim qilganini bilaman deb o'ylayman",
            f"men begunohman, {t.name} ni kuzataylik",
        ]))

    # ── Mini App uchun ko'rinish ───────────────────────────────────────
    def _can_see_role(self, viewer: P | None, p: P) -> bool:
        if self.phase == "ended" or not p.role:
            return self.phase == "ended"
        if not p.alive:
            return True
        if viewer is None:
            return False
        if viewer.uid == p.uid:
            return True
        if not viewer.alive:
            return True
        return viewer.team == "mafia" and p.team == "mafia"

    def _msg_visible(self, viewer: P | None, m: dict) -> bool:
        if self.phase == "ended" or m["ch"] in ("day", "last"):
            return True
        if viewer is None:
            return False
        if m["ch"] == "mafia":
            return viewer.team == "mafia"
        if m["ch"] == "dead":
            return not viewer.alive
        return False

    def view_for(self, uid: int) -> dict:
        with LOCK:
            viewer = self.players.get(uid)
            now = time.time()
            self.touch(uid)
            started = self.phase != "lobby"
            players = []
            for p in self.players.values():
                role_visible = self._can_see_role(viewer, p)
                players.append({
                    "uid": p.uid, "name": p.name, "alive": p.alive, "is_bot": p.is_bot, "photo": getattr(p, "photo", None),
                    "host": p.uid == self.host_id,
                    "role": p.role if role_visible else None,
                    "role_name": ROLES[p.role]["name"] if role_visible and p.role else None,
                    "role_emoji": ROLES[p.role]["emoji"] if role_visible and p.role else None,
                    "team": p.team if role_visible else None,
                    "death": p.death if not p.alive else None,
                    "afk": self.is_afk(p, now),
                })
            me = None
            if viewer:
                r = viewer.role
                allowed = self.targets_for(viewer)
                act = self.night_actions.get(uid)
                me = {
                    "uid": uid, "name": viewer.name, "alive": viewer.alive,
                    "role": r, "role_name": ROLES[r]["name"] if r else None,
                    "role_emoji": ROLES[r]["emoji"] if r else None,
                    "role_desc": ROLES[r]["desc"] if r else None,
                    "team": viewer.team,
                    "team_name": TEAM_NAMES.get(viewer.team) if viewer.team else None,
                    "action": viewer.action if self.phase == "night" and viewer.alive else None,
                    "action_label": ACTION_LABELS.get(viewer.action) if viewer.action else None,
                    "targets": allowed,
                    "my_target": act["target"] if act and self.phase == "night" else None,
                    "my_target2": act.get("target2") if act and self.phase == "night" else None,
                    "my_pass": bool(act and self.phase == "night" and act["target"] is None),
                    "shots": viewer.shots,
                    "can_skip": viewer.action == "snipe" and viewer.shots > 0,
                    "pick": 2 if viewer.action == "compare" else 1,
                    "my_vote": self.votes.get(uid) if self.phase == "vote" else None,
                    "ready": uid in self.ready,
                    "can_chat": self._can_chat(viewer),
                    "chat_channel": self._chat_channel(viewer),
                    "lover": ({"uid": viewer.lover, "name": self._name(viewer.lover)}
                              if viewer.lover is not None else None),
                    "vow": viewer.vow if viewer.action == "vow" else None,
                    "can_veto": bool(viewer.alive and viewer.role == "judge" and not viewer.judge_used
                                     and self.phase == "vote" and self.veto_by is None),
                    "can_last_word": self.can_last_word(viewer),
                }
            votes = None
            if self.phase == "vote":
                votes = {str(k): v for k, v in self.votes.items()}
            return {
                "gid": self.gid, "version": self.version, "now": now,
                "phase": self.phase, "day": self.day, "deadline": self.deadline,
                "duration": self.duration, "phase_start": self.phase_start,
                "host_id": self.host_id, "is_host": uid == self.host_id,
                "min_players": MIN_PLAYERS, "max_players": MAX_PLAYERS,
                "can_start": started is False and len(self.players) >= MIN_PLAYERS and uid == self.host_id,
                "has_bot": self.bot_count() > 0, "bot_count": self.bot_count(), "max_bots": MAX_BOTS,
                "chat_title": self.chat_title,
                "roles_info": ROLES_PUBLIC, "team_names": TEAM_NAMES,
                "players": players, "me": me, "votes": votes,
                "ready_count": len(self.ready), "alive_count": len(self.alive_players()),
                "winner": self.winner, "winner_name": TEAM_NAMES.get(self.winner) if self.winner else None,
                "cancelled": self.cancelled,
                "settings": self.settings,
                "optional_roles": [{"role": r, "name": ROLES[r]["name"], "emoji": ROLES[r]["emoji"],
                                    "disabled": r in self.settings.get("disabled", [])} for r in OPTIONAL_ROLES],
                "limits": SETTING_LIMITS,
                "spectator": viewer is None,
                "watching": sum(1 for t in self.spectators.values() if now - t < 30),
                "emojis": EMOJIS,
                "reactions": [r for r in self.reactions if now - r["t"] <= REACTION_TTL][-12:],
                "veto_by": self._name(self.veto_by) if self.veto_by is not None else None,
                "recap": self._recap() if self.phase == "ended" and not self.cancelled else None,
                "chat": [m for m in self.chat if self._msg_visible(viewer, m)][-80:],
                "feed": self.feed[-40:],
                "private": (self.private.get(uid) or [])[-12:],
            }

    def _recap(self) -> dict:
        """O'yin tugagach: kim kim edi va kim nima qildi."""
        roster = [{"uid": p.uid, "name": p.name, "is_bot": p.is_bot, "alive": p.alive,
                   "role": p.role, "role_name": ROLES[p.role]["name"], "role_emoji": ROLES[p.role]["emoji"],
                   "team": p.team, "won": p.team == self.winner,
                   "lover": self._name(p.lover) if p.lover is not None else None,
                   "death": DEATH_LABELS.get(p.death["cause"], p.death["cause"]) if p.death else None,
                   "death_day": p.death["day"] if p.death else None}
                  for p in self.players.values()]
        return {"roster": roster, "log": self.log[-120:]}

    def _chat_channel(self, viewer: P) -> str:
        if self.phase in ("lobby", "reveal", "ended"):
            return "day"
        if not viewer.alive:
            return "dead"
        if self.phase == "night":
            return "mafia" if viewer.team == "mafia" else "none"
        return "day"

    def _can_chat(self, viewer: P) -> bool:
        return self._chat_channel(viewer) != "none"


# ── modul darajasidagi yordamchilar ────────────────────────────────────
def create_game(chat_id: int, host_id: int, host_name: str, chat_title: str = "", host_username: str | None = None) -> Game:
    with LOCK:
        g = Game(chat_id, host_id, chat_title)
        g.add_player(host_id, host_name, host_username)
        GAMES[g.gid] = g
        CHAT_GAME[chat_id] = g.gid
        return g


def get_game(gid: str) -> Game | None:
    return GAMES.get(gid)


def active_game(chat_id: int) -> Game | None:
    gid = CHAT_GAME.get(chat_id)
    g = GAMES.get(gid) if gid else None
    if g and g.phase != "ended":
        return g
    return None


def find_game_for(uid: int) -> Game | None:
    """Foydalanuvchi qatnashayotgan eng yangi tugamagan o'yin (Mini App'ga qayta kirish uchun)."""
    with LOCK:
        mine = [g for g in GAMES.values() if g.phase != "ended" and uid in g.players]
        return max(mine, key=lambda g: g.created) if mine else None


def tick_all() -> None:
    now = time.time()
    with LOCK:
        for g in list(GAMES.values()):
            g.tick(now)
        for gid, g in list(GAMES.items()):
            if g.phase == "ended" and g.ended_at and now - g.ended_at > KEEP_ENDED_SEC and not g.outbox:
                del GAMES[gid]
                if CHAT_GAME.get(g.chat_id) == gid:
                    del CHAT_GAME[g.chat_id]
    _maybe_save(now)


def drain_outbox() -> list[tuple["Game", dict]]:
    out = []
    with LOCK:
        for g in GAMES.values():
            if g.outbox:
                for ev in g.outbox:
                    out.append((g, ev))
                g.outbox = []
    return out


# ── saqlash (Postgres, db.py orqali) ───────────────────────────────────
STATE_STORE, STATE_KEY = "mafia", "games"
_SCALARS = ("gid", "chat_id", "chat_title", "host_id", "created", "last_activity", "phase", "day",
            "phase_start", "deadline", "duration", "winner", "cancelled", "ended_at", "version",
            "lobby_msg_id", "_eid", "settings", "phase_no", "veto_by", "log", "stats_recorded")
_PFIELDS = ("uid", "name", "username", "is_bot", "alive", "role", "last_heal", "self_healed",
            "last_block", "death", "shots", "lucky_used", "lover", "bitten", "vow", "judge_used", "vetoed",
            "sniper_hit_mafia", "lw_until", "last_word_used", "photo")
_saved: dict[str, int] = {}
_last_save = 0.0


def _game_to_dict(g: "Game") -> dict:
    d = {k: getattr(g, k) for k in _SCALARS}
    d["players"] = [dict({k: getattr(p, k) for k in _PFIELDS}, checked={str(u): r for u, r in p.checked.items()})
                    for p in g.players.values()]
    d["night_actions"] = {str(k): v for k, v in g.night_actions.items()}
    d["votes"] = {str(k): v for k, v in g.votes.items()}
    d["ready"] = list(g.ready)
    d["chat"], d["feed"] = g.chat, g.feed
    d["private"] = {str(k): v for k, v in g.private.items()}
    return d


def _game_from_dict(d: dict) -> "Game":
    g = Game(d["chat_id"], d["host_id"], d.get("chat_title", ""))
    for k in _SCALARS:
        if k in d:
            setattr(g, k, d[k])
    for pd in d.get("players", []):
        p = P(pd["uid"], pd["name"], pd.get("username"), pd.get("is_bot", False))
        for k in _PFIELDS:
            if k in pd:
                setattr(p, k, pd[k])
        p.name = pd["name"]
        p.checked = {int(u): r for u, r in (pd.get("checked") or {}).items()}
        g.players[p.uid] = p
    g.night_actions = {int(k): v for k, v in d.get("night_actions", {}).items()}
    g.votes = {int(k): v for k, v in d.get("votes", {}).items()}
    g.ready = set(d.get("ready", []))
    g.chat, g.feed = d.get("chat", []), d.get("feed", [])
    g.private = {int(k): v for k, v in d.get("private", {}).items()}
    g.settings = {**DEFAULT_SETTINGS, **(g.settings or {})}
    for p in g.players.values():
        p.last_seen = time.time()          # qayta ishga tushgandan keyin hamma "faol"
    if g.phase not in ("lobby", "ended"):
        g.deadline = max(g.deadline, time.time() + 30)   # qayta ishga tushgandan keyin vaqt beramiz
    return g


def save_all() -> None:
    import db
    with LOCK:
        data = {"games": [_game_to_dict(g) for g in GAMES.values()]}
    db.save(STATE_STORE, STATE_KEY, data)


def load_all() -> int:
    """Server qayta ishga tushganda o'yinlarni tiklaydi. Tiklangan o'yinlar soni."""
    try:
        import db
        data = db.load(STATE_STORE, STATE_KEY, None)
    except Exception as e:
        print(f"[mafia] yuklab bo'lmadi: {e}")
        return 0
    n = 0
    with LOCK:
        for d in (data or {}).get("games", []):
            try:
                g = _game_from_dict(d)
            except Exception as e:
                print(f"[mafia] o'yin tiklanmadi: {e}")
                continue
            if g.gid in GAMES:
                continue
            GAMES[g.gid] = g
            _saved[g.gid] = g.version
            if g.phase != "ended":
                CHAT_GAME[g.chat_id] = g.gid
                n += 1
    return n


def _maybe_save(now: float) -> None:
    global _last_save
    if now - _last_save < 3:
        return
    cur = {gid: g.version for gid, g in GAMES.items()}
    if cur == _saved:
        return
    _last_save = now
    try:
        save_all()
        _saved.clear()
        _saved.update(cur)
    except Exception as e:
        print(f"[mafia] saqlab bo'lmadi: {e}")
