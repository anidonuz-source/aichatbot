"""
Gemini API kalitlarini aylantirish (rotatsiya).

Bir nechta kalitni `GEMINI_API_KEYS` ga vergul (yoki yangi qator) bilan yozing:
    GEMINI_API_KEYS=AIza...1,AIza...2,AIza...3
Eski `GEMINI_API_KEY` va `GEMINI_API_KEY_2`, `GEMINI_API_KEY_3` ... ham o'qiladi.

Qoida:
  * Har doim tartib bo'yicha birinchi "tirik" kalit ishlatiladi (1 -> 2 -> 3 ...).
  * Kalit limitga tegsa (429 / RESOURCE_EXHAUSTED) "o'lik" deb belgilanadi va
    keyingisiga o'tiladi.
      - kunlik limit  -> Google'da limit tiklanadigan vaqtgacha (yarim tun, Tinch okean vaqti)
      - daqiqalik limit -> ~65 soniya
      - noto'g'ri/bloklangan kalit -> 6 soat
  * Hammasi o'lik bo'lsa RuntimeError ko'tariladi (bot boshqa provayderga o'tadi).
    Birinchi kalit limiti tiklangach, u yana birinchi bo'lib ishlatiladi.

`pool.models.generate_content(...)` — genai.Client bilan bir xil interfeys, shuning
uchun eski kodni o'zgartirish shart emas.
"""
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from google import genai

log = logging.getLogger("gemini_pool")

MINUTE_COOLDOWN = 65          # soniya
BAD_KEY_COOLDOWN = 6 * 3600   # soniya


def load_keys() -> list[str]:
    raw = [os.environ.get("GEMINI_API_KEYS", "")]
    raw.append(os.environ.get("GEMINI_API_KEY", ""))
    raw += [os.environ.get(f"GEMINI_API_KEY_{i}", "") for i in range(2, 21)]
    keys: list[str] = []
    for chunk in raw:
        for k in re.split(r"[,\s;]+", chunk or ""):
            k = k.strip().strip("\"'")
            if k and k not in keys:
                keys.append(k)
    return keys


def _next_quota_reset() -> float:
    """Gemini kunlik limiti yarim tunda (Tinch okean vaqti) tiklanadi."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
        now = datetime.now(tz)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return nxt.timestamp()
    except Exception:
        now = datetime.now(timezone(timedelta(hours=-8)))
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return nxt.timestamp()


def _classify(exc: Exception) -> str:
    """'daily' | 'minute' | 'bad' | 'other'"""
    s = str(exc)
    low = s.lower()
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if "API_KEY_INVALID" in s or "api key not valid" in low or "api key expired" in low or code == 403 or "PERMISSION_DENIED" in s:
        return "bad"
    if code == 429 or "RESOURCE_EXHAUSTED" in s or "quota" in low or "rate limit" in low:
        if "perday" in low.replace(" ", "").replace("_", "") or "per day" in low or "daily" in low:
            return "daily"
        return "minute"
    return "other"


class GeminiPool:
    def __init__(self, keys: list[str]):
        if not keys:
            raise RuntimeError("GEMINI_API_KEYS (yoki GEMINI_API_KEY) kiritilmagan")
        self.keys = keys
        self.clients = [genai.Client(api_key=k) for k in keys]
        self.dead_until = [0.0] * len(keys)
        self._lock = threading.Lock()
        self.models = self          # pool.models.generate_content(...)
        log.warning("[gemini_pool] %d ta kalit yuklandi", len(keys))

    @staticmethod
    def _tag(i: int) -> str:
        return f"#{i + 1}"

    def _mark(self, i: int, kind: str) -> None:
        now = time.time()
        until = {"daily": _next_quota_reset(), "minute": now + MINUTE_COOLDOWN, "bad": now + BAD_KEY_COOLDOWN}[kind]
        with self._lock:
            self.dead_until[i] = until
        left = int(until - now)
        log.warning("[gemini_pool] kalit %s limitda (%s), ~%d daq. dam oladi", self._tag(i), kind, left // 60)

    def _alive(self) -> list[int]:
        now = time.time()
        with self._lock:
            return [i for i in range(len(self.keys)) if self.dead_until[i] <= now]

    def generate_content(self, **kwargs):
        last_exc: Exception | None = None
        tried: set[int] = set()
        while True:
            order = [i for i in self._alive() if i not in tried]
            if not order:
                break
            i = order[0]            # har doim tartib bo'yicha birinchi tirik kalit
            tried.add(i)
            try:
                return self.clients[i].models.generate_content(**kwargs)
            except Exception as e:  # noqa: BLE001
                kind = _classify(e)
                if kind == "other":
                    raise           # 5xx, xavfsizlik filtri va h.k.: kalitga aloqasi yo'q
                self._mark(i, kind)
                last_exc = e
        if last_exc is not None:
            raise RuntimeError(f"Gemini: barcha kalitlar limitda ({last_exc})") from last_exc
        raise RuntimeError("Gemini: barcha kalitlar limitda, tiklanishini kuting")

    def status(self) -> list[dict]:
        now = time.time()
        with self._lock:
            return [{"key": self._tag(i), "tail": k[-4:], "alive": self.dead_until[i] <= now,
                     "resets_in_min": max(0, int((self.dead_until[i] - now) // 60))}
                    for i, k in enumerate(self.keys)]
