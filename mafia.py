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

BOT_UID = 0                       # Misumi o'yinchi sifatida qatnashganda shu id
BOT_NAME = "Misumi AI"
MIN_PLAYERS, MAX_PLAYERS = 4, 16

REVEAL_SEC = int(os.environ.get("MAFIA_REVEAL_SEC", "12"))
NIGHT_SEC = int(os.environ.get("MAFIA_NIGHT_SEC", "50"))
NIGHT_MIN_SEC = int(os.environ.get("MAFIA_NIGHT_MIN_SEC", "20"))   # tez tugasa rollar fosh bo'lmasin
DAY_SEC = int(os.environ.get("MAFIA_DAY_SEC", "90"))
VOTE_SEC = int(os.environ.get("MAFIA_VOTE_SEC", "45"))
LOBBY_TTL = 30 * 60
KEEP_ENDED_SEC = 30 * 60

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
}
TEAM_NAMES = {"town": "Shahar", "mafia": "Mafiya", "maniac": "Maniak"}
ROLES_PUBLIC = {k: {"name": v["name"], "emoji": v["emoji"], "team": v["team"], "desc": v["desc"]} for k, v in ROLES.items()}
ACTION_LABELS = {
    "kill": "O'ldirish", "slay": "O'ldirish", "check": "Tekshirish",
    "heal": "Davolash", "guard": "Qo'riqlash", "block": "Band qilish",
}

GAMES: dict[str, "Game"] = {}      # gid -> Game
CHAT_GAME: dict[int, str] = {}     # chat_id -> faol gid


def mafia_count(n: int) -> int:
    return {4: 1, 5: 1, 6: 2, 7: 2, 8: 2, 9: 3, 10: 3, 11: 3, 12: 4, 13: 4, 14: 4, 15: 5, 16: 5}[n]


def role_set(n: int) -> list[str]:
    """n ta o'yinchi uchun rollar ro'yxati (aralashtirilmagan)."""
    specials = ["commissar", "doctor"]
    if n >= 8:
        specials.append("maniac")
    if n >= 9:
        specials.append("escort")
    if n >= 10:
        specials.append("bodyguard")
    m = mafia_count(n)
    mafia = (["don"] if n >= 6 else []) + ["mafia"] * (m - (1 if n >= 6 else 0))
    roles = mafia + specials
    roles += ["citizen"] * (n - len(roles))
    return roles


class P:
    def __init__(self, uid: int, name: str, username: str | None = None, is_bot: bool = False):
        self.uid = uid
        self.name = (name or str(uid)).strip()[:24] or str(uid)
        self.username = username
        self.is_bot = is_bot
        self.alive = True
        self.role: str | None = None
        self.last_heal: int | None = None
        self.self_healed = False
        self.last_block: int | None = None
        self.checked: dict[int, str] = {}      # komissar: uid -> "mafia" | "tinch"
        self.death: dict | None = None
        self.last_chat = 0.0

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
        self.bot_acted = False
        self.bot_due = 0.0
        self.bot_say_times: list[float] = []

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
        if uid == BOT_UID:
            return
        self.private.setdefault(uid, []).append({"id": self._id(), "kind": kind, "text": text, "t": time.time()})

    def _set_phase(self, phase: str, seconds: int) -> None:
        now = time.time()
        self.phase = phase
        self.phase_start = now
        self.duration = seconds
        self.deadline = now + seconds
        self.bot_acted = False
        self.bot_due = now + random.uniform(6, max(8, min(seconds - 8, 24)))
        self._bump()

    def alive_players(self) -> list[P]:
        return [p for p in self.players.values() if p.alive]

    def _name(self, uid: int) -> str:
        p = self.players.get(uid)
        return p.name if p else "?"

    # ── lobbi ──────────────────────────────────────────────────────────
    def add_player(self, uid: int, name: str, username: str | None = None, is_bot: bool = False) -> tuple[bool, str]:
        with LOCK:
            if self.phase != "lobby":
                return False, "O'yin allaqachon boshlangan"
            if uid in self.players:
                return True, "Sen allaqachon o'yindasan"
            if len(self.players) >= MAX_PLAYERS:
                return False, f"Joy qolmadi (maksimum {MAX_PLAYERS})"
            self.players[uid] = P(uid, name, username, is_bot)
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
            if uid == self.host_id and self.players:
                humans = [u for u in self.players if u != BOT_UID]
                self.host_id = humans[0] if humans else self.host_id
            self._bump()
            self._out("lobby")
            return True, "Chiqding"

    def add_bot(self) -> tuple[bool, str]:
        return self.add_player(BOT_UID, BOT_NAME, None, True)

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
            roles = role_set(n)
            random.shuffle(roles)
            for p, r in zip(self.players.values(), roles):
                p.role = r
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
            out = []
            for q in self.alive_players():
                if p.action in ("kill",) and q.team == "mafia":
                    continue
                if p.action in ("slay", "check", "guard", "block") and q.uid == p.uid:
                    continue
                if p.action == "heal":
                    if q.uid == p.uid and p.self_healed:
                        continue
                    if q.uid == p.last_heal:
                        continue
                if p.action == "block" and q.uid == p.last_block:
                    continue
                out.append(q.uid)
            return out
        if self.phase == "vote":
            return [q.uid for q in self.alive_players() if q.uid != p.uid]
        return []

    def set_action(self, uid: int, target: int) -> tuple[bool, str]:
        with LOCK:
            p = self.players.get(uid)
            if not p or not p.alive:
                return False, "Sen o'yinda emassan yoki o'lgansan"
            if self.phase != "night":
                return False, "Hozir tun emas"
            if not p.action:
                return False, "Sening rolingda tungi harakat yo'q"
            if target not in self.targets_for(p):
                return False, "Bu o'yinchini tanlab bo'lmaydi"
            self.night_actions[uid] = {"type": p.action, "target": target}
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
        self._set_phase("night", NIGHT_SEC)
        self._feed(f"{self.day}-tun boshlandi. Shahar uxladi.", "night")
        self._out("night", day=self.day)

    def _enter_day(self) -> None:
        self.ready = set()
        self._set_phase("day", DAY_SEC)
        bot = self.players.get(BOT_UID)
        self.bot_say_times = []
        if bot and bot.alive:
            k = random.choice([2, 2, 3])
            lo, hi = time.time() + 6, self.deadline - 10
            self.bot_say_times = sorted(random.uniform(lo, hi) for _ in range(k))

    def _enter_vote(self) -> None:
        self.votes = {}
        self._set_phase("vote", VOTE_SEC)
        self._feed("Ovoz berish boshlandi.", "vote")
        self._out("vote_start")

    def _night_complete(self) -> bool:
        for p in self.alive_players():
            if p.action and p.uid not in self.night_actions:
                return False
        return True

    def _kill(self, uid: int, cause: str, phase: str) -> None:
        p = self.players[uid]
        if not p.alive:
            return
        p.alive = False
        p.death = {"day": self.day, "cause": cause, "phase": phase}
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
            if self.players[uid].action:
                self._notify(uid, "💃 Bu tun seni band qilishdi. Harakating bajarilmadi.", "block")

        def act_of(role: str):
            for uid, a in acts.items():
                p = self.players[uid]
                if p.role == role and uid in alive_ids and uid not in blocked:
                    return p, a["target"]
            return None, None

        # 2) Mafiya ovozi (Don teng ovozni hal qiladi)
        mv: Counter = Counter()
        don_vote = None
        for p in self.alive_players():
            if p.team == "mafia" and p.uid not in blocked and p.uid in acts:
                t = acts[p.uid]["target"]
                mv[t] += 1
                if p.role == "don":
                    don_vote = t
        kill_target = None
        if mv:
            top = max(mv.values())
            cands = [t for t, c in mv.items() if c == top]
            kill_target = don_vote if don_vote in cands else random.choice(cands)

        maniac, slay_target = act_of("maniac")
        doctor, heal_target = act_of("doctor")
        guard, guard_target = act_of("bodyguard")
        commissar, check_target = act_of("commissar")
        # Doktor/Qo'riqchi/Kelin cheklovlari uchun harakat (bloklangan bo'lsa ham) yozib qo'yiladi
        for uid, a in acts.items():
            p = self.players[uid]
            if p.role == "doctor" and uid in alive_ids and uid not in blocked:
                p.last_heal = a["target"]
                if a["target"] == uid:
                    p.self_healed = True

        # 3) Komissar tekshiruvi
        if commissar and check_target is not None:
            t = self.players[check_target]
            res = "mafia" if t.role == "mafia" else "tinch"
            commissar.checked[check_target] = res
            if res == "mafia":
                self._notify(commissar.uid, f"🔎 Tekshiruv: {t.name} — MAFIYA!", "check")
            else:
                self._notify(commissar.uid, f"🔎 Tekshiruv: {t.name} — tinch (mafiya emas).", "check")

        # 4) Hujumlar
        attacks = []
        if kill_target is not None:
            attacks.append(kill_target)
        if slay_target is not None:
            attacks.append(slay_target)
        died: list[tuple[int, str]] = []
        guard_used = False
        dead_now: set[int] = set()
        for tgt in attacks:
            if tgt in dead_now:
                continue
            if guard and not guard_used and guard_target == tgt and guard.uid not in dead_now and guard.uid != tgt:
                guard_used = True
                dead_now.add(guard.uid)
                died.append((guard.uid, "guard"))
                self._notify(tgt, "🛡 Bu tun senga hujum bo'ldi, lekin Qo'riqchi seni qutqardi.", "saved")
                continue
            if doctor and heal_target == tgt:
                self._notify(tgt, "🩺 Bu tun senga hujum bo'ldi, lekin Doktor seni davoladi.", "saved")
                if doctor:
                    self._notify(doctor.uid, f"🩺 {self._name(tgt)} ga hujum bo'ldi — sen uni qutqarding!", "saved")
                continue
            dead_now.add(tgt)
            died.append((tgt, "mafia" if tgt == kill_target else "maniac"))

        deaths = []
        for uid, cause in died:
            self._kill(uid, cause, "night")
            p = self.players[uid]
            deaths.append({"uid": uid, "name": p.name, "role": p.role, "cause": cause})
            self._feed(f"{p.name} tunda o'ldirildi — {ROLES[p.role]['emoji']} {ROLES[p.role]['name']}.", "death")
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
        if lynched is not None:
            self._kill(lynched, "lynch", "day")
            p = self.players[lynched]
            info = {"uid": lynched, "name": p.name, "role": p.role, "summary": summary}
            self._feed(f"Ovozlar: {summary}. {p.name} osildi — {ROLES[p.role]['emoji']} {ROLES[p.role]['name']}.", "lynch")
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
        mn = sum(1 for p in alive if p.team == "maniac")
        t = len(alive) - m - mn
        winner = None
        if m == 0 and mn == 0:
            winner = "town"
        elif m == 0 and mn > 0 and t <= 1:
            winner = "maniac"
        elif m > 0 and m >= t + mn:
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
                if now >= self.deadline or (now - self.phase_start >= NIGHT_MIN_SEC and self._night_complete()):
                    self._resolve_night()
            elif self.phase == "day":
                alive = len(self.alive_players())
                if now >= self.deadline or len(self.ready) * 2 > alive:
                    self._enter_vote()
            elif self.phase == "vote":
                alive_ids = [p.uid for p in self.alive_players()]
                if now >= self.deadline or all(u in self.votes for u in alive_ids):
                    self._resolve_vote()

    # ── Misumi o'yinchi sifatida ───────────────────────────────────────
    def _bot_tick(self, now: float) -> None:
        bot = self.players.get(BOT_UID)
        if not bot or not bot.alive:
            return
        if self.phase == "night" and not self.bot_acted and now >= self.bot_due:
            self.bot_acted = True
            self._bot_night(bot)
        elif self.phase == "day":
            while self.bot_say_times and now >= self.bot_say_times[0]:
                self.bot_say_times.pop(0)
                self._out("bot_say")
        elif self.phase == "vote" and not self.bot_acted and now >= self.bot_due:
            self.bot_acted = True
            self._bot_vote(bot)

    def _bot_night(self, bot: P) -> None:
        opts = self.targets_for(bot)
        if not bot.action or not opts:
            return
        pool = opts
        if bot.action == "check":
            fresh = [u for u in opts if u not in bot.checked]
            pool = fresh or opts
        elif bot.action == "heal":
            others = [u for u in opts if u != bot.uid]
            pool = others or opts
            if bot.uid in opts and random.random() < 0.25:
                pool = [bot.uid]
        elif bot.action in ("kill", "slay"):
            # Kuzatilgan komissar/doktor kabi xavfli odamlarni bilmaydi: tasodifiy, lekin jonli odamlardan
            pool = opts
        self.set_action(bot.uid, random.choice(pool))

    def _bot_vote(self, bot: P) -> None:
        opts = self.targets_for(bot)
        if not opts:
            return
        known = [u for u, r in bot.checked.items() if r == "mafia" and u in opts]
        if bot.role == "commissar" and known:
            self.set_vote(bot.uid, known[0])
            return
        tally = Counter(t for v, t in self.votes.items() if t != "skip" and t in opts)
        if bot.team == "mafia":
            tally = Counter({u: c for u, c in tally.items() if self.players[u].team != "mafia"})
            opts = [u for u in opts if self.players[u].team != "mafia"] or opts
        if tally and random.random() < 0.75:
            self.set_vote(bot.uid, tally.most_common(1)[0][0])
        elif random.random() < 0.12:
            self.set_vote(bot.uid, "skip")
        else:
            self.set_vote(bot.uid, random.choice(opts))

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
        if self.phase == "ended" or m["ch"] == "day":
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
            started = self.phase != "lobby"
            players = []
            for p in self.players.values():
                role_visible = self._can_see_role(viewer, p)
                players.append({
                    "uid": p.uid, "name": p.name, "alive": p.alive, "is_bot": p.is_bot,
                    "host": p.uid == self.host_id,
                    "role": p.role if role_visible else None,
                    "role_name": ROLES[p.role]["name"] if role_visible and p.role else None,
                    "role_emoji": ROLES[p.role]["emoji"] if role_visible and p.role else None,
                    "team": p.team if role_visible else None,
                    "death": p.death if not p.alive else None,
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
                    "my_vote": self.votes.get(uid) if self.phase == "vote" else None,
                    "ready": uid in self.ready,
                    "can_chat": self._can_chat(viewer),
                    "chat_channel": self._chat_channel(viewer),
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
                "has_bot": BOT_UID in self.players,
                "chat_title": self.chat_title,
                "roles_info": ROLES_PUBLIC, "team_names": TEAM_NAMES,
                "players": players, "me": me, "votes": votes,
                "ready_count": len(self.ready), "alive_count": len(self.alive_players()),
                "winner": self.winner, "winner_name": TEAM_NAMES.get(self.winner) if self.winner else None,
                "cancelled": self.cancelled,
                "chat": [m for m in self.chat if self._msg_visible(viewer, m)][-80:],
                "feed": self.feed[-40:],
                "private": (self.private.get(uid) or [])[-12:],
            }

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


def drain_outbox() -> list[tuple["Game", dict]]:
    out = []
    with LOCK:
        for g in GAMES.values():
            if g.outbox:
                for ev in g.outbox:
                    out.append((g, ev))
                g.outbox = []
    return out
