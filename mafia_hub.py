"""
Mafia shaxsiy hisob markazi (bot ichida, /start → 🎭 Mafia → 👤 Hisobim).
Ekranlar: home, prof, top, shop, inv, ach, grp, hist. Hammasi mfs:<view>[:arg] callback bilan ishlaydi.
Ma'lumot mafia_stats orqali (db.py ga saqlanadi), o'yin mexanikasi mafia.py da.
"""
import html
import time

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup

import mafia
import mafia_stats as ms

MEDAL = ["🥇", "🥈", "🥉"]


def _e(s) -> str:
    return html.escape(str(s))


def _date(t) -> str:
    return time.strftime("%d.%m %H:%M", time.gmtime(float(t) + 5 * 3600))   # UTC+5


def _bar(xp: int, lo: int, hi: int, n: int = 10) -> str:
    f = max(0, min(n, round((xp - lo) / max(1, hi - lo) * n)))
    return "▰" * f + "▱" * (n - f)


def _role(r) -> str:
    ro = mafia.ROLES.get(r)
    return f"{ro['emoji']} {ro['name']}" if ro else str(r)


def _kb(rows: list) -> Markup:
    return Markup(rows + [[Btn("⬅️ Hisob", callback_data="mfs:home"), Btn("🎭 Mafia bo'limi", callback_data="st:s:mafia")]])


def _pos(i: int) -> str:
    return MEDAL[i] if i < 3 else f"{i + 1}."


def _home(uid, name, arg):
    p = ms.profile(uid, name)
    lv, cur = p["lvl"], ms.CURRENCY
    inv = " ".join(f"{ms.SHOP[k]['emoji']}×{n}" for k, n in p["inv"].items() if n and k in ms.SHOP) or "bo'sh"
    ready, left = ms.daily_info(uid)
    t = (f"🎭 <b>MAFIA — {_e(name)}</b>\n⭐ {lv['title']} · {lv['level']}-daraja\n"
         f"{_bar(lv['xp'], lv['lo'], lv['hi'])} {lv['xp']}/{lv['hi']} XP\n\n"
         f"💵 Balans: <b>{p['coins']} {cur}</b>\n"
         f"🎮 O'yinlar: {p['games']} · 🏆 G'alaba: {p['wins']} ({p['winrate']}%)\n"
         f"🔥 Seriya: {p['streak']} (eng yaxshi {p['best_streak']})"
         + (f" · 🏅 Reyting #{p['rank']}" if p.get("rank") else "") +
         f"\n🎖 Yutuqlar: {len(p['achievements'])}/{len(ms.ACHIEVEMENTS)}\n🎒 Inventar: {inv}\n\n"
         "O'ynash uchun guruhda /mafia deb yozing.")
    gift = f"🎁 Sovg'a (+{ms.DAILY_COINS})" if ready else f"🎁 {left // 3600 + 1} soatdan keyin"
    return t, _kb([
        [Btn("👤 Profil", callback_data="mfs:prof"), Btn("🏆 Reyting", callback_data="mfs:top:week")],
        [Btn("🛒 Do'kon", callback_data="mfs:shop"), Btn("🎒 Inventar", callback_data="mfs:inv")],
        [Btn("🎖 Yutuqlar", callback_data="mfs:ach"), Btn("🏘 Guruhlar", callback_data="mfs:grp")],
        [Btn("📜 Tarix", callback_data="mfs:hist"), Btn(gift, callback_data="mfs:daily")],
    ])


def _prof(uid, name, arg):
    p = ms.profile(uid, name)
    lv = p["lvl"]
    t = (f"👤 <b>{_e(name)}</b> — profil\n⭐ {lv['title']} · {lv['level']}-daraja ({lv['xp']} XP)\n\n"
         f"🎮 O'yinlar: {p['games']} · 🏆 G'alaba: {p['wins']} · ❌ Mag'lubiyat: {p['games'] - p['wins']}\n"
         f"📈 Winrate: {p['winrate']}% · 🛡 Tirik qolgan: {p.get('survived', 0)}\n"
         f"🔥 Joriy seriya: {p['streak']} · Eng yaxshi: {p['best_streak']}\n"
         f"💵 Hozir: {p['coins']} {ms.CURRENCY} · Jami topilgan: {p['earned']} {ms.CURRENCY}\n"
         + (f"🏅 Reyting: #{p['rank']}\n" if p.get("rank") else "")
         + (f"💰 Boylar orasida: #{ms.rank_of_coins(uid)}\n" if p["games"] or p["coins"] != ms.START_COINS else "")
         + (f"📅 Boshlagan: {_date(p['since'])[:5]}\n" if p.get("since") else ""))
    roles = p["roles"][:6]
    if roles:
        t += "\n<b>Rollar bo'yicha</b>\n" + "\n".join(f"{_role(r['role'])} — {r['wins']}/{r['games']} ({r['winrate']}%)" for r in roles)
        good = [r for r in p["roles"] if r["games"] >= 3]
        if good:
            b = max(good, key=lambda r: (r["winrate"], r["games"]))
            t += f"\n\n⭐ Eng kuchli rol: {_role(b['role'])} ({b['winrate']}%)"
    else:
        t += "\nHali o'yin yo'q. Birinchi g'alabangni qozon!"
    return t, _kb([])


def _top(uid, name, arg):
    mode = arg if arg in ("week", "all", "rich", "grp") else "week"
    if mode == "rich":
        rows = ms.richest(10)
        t = "💰 <b>Eng boylar</b>\n\n" + ("\n".join(f"{_pos(i)} {_e(r['name'])} — {r['coins']} {ms.CURRENCY}" for i, r in enumerate(rows)) or "Hali yo'q")
        r = ms.rank_of_coins(uid)
        t += f"\n\nSening o'ring: {'#' + str(r) if r else '—'}"
    elif mode == "grp":
        rows = ms.top_groups(10)
        t = "🏘 <b>Top guruhlar</b>\n\n" + ("\n".join(f"{_pos(i)} {_e(g['title'])} — {g['games']} o'yin · {g['players']} kishi" for i, g in enumerate(rows)) or "Hali yo'q")
    else:
        full = ms.leaderboard(mode, 100000)
        rows = full[:10]
        t = ("📅 <b>Haftalik top-10</b> (" + ms.week_key() + ")" if mode == "week" else "🌍 <b>Umumiy top-10</b>") + "\n\n"
        t += "\n".join(f"{_pos(i)} {_e(r['name'])} — {r['wins']} g'alaba / {r['games']} · {r['winrate']}%" for i, r in enumerate(rows)) or "Hali o'yin yo'q"
        own = next((i + 1 for i, r in enumerate(full) if r["uid"] == uid), None)
        t += f"\n\nSening o'ring: {'#' + str(own) if own else '—'}"
    tabs = [Btn(("• " if mode == k else "") + lab, callback_data=f"mfs:top:{k}")
            for k, lab in (("week", "📅 Hafta"), ("all", "🌍 Umumiy"), ("rich", "💰 Boylar"), ("grp", "🏘 Guruhlar"))]
    return t, _kb([tabs[:2], tabs[2:]])


def _shop(uid, name, arg):
    p = ms.profile(uid, name)
    cur = ms.CURRENCY
    ready, left = ms.daily_info(uid)
    t = f"🛒 <b>Mafia do'koni</b>\n💵 Balans: <b>{p['coins']} {cur}</b>\nSotib olingan narsa keyingi o'yin boshlanganda avtomatik ishlaydi (bittadan 3 tagacha saqlanadi).\n"
    rows = []
    for k, it in ms.SHOP.items():
        t += f"\n{it['emoji']} <b>{_e(it['name'])}</b> — {it['price']} {cur} · sende {p['inv'].get(k, 0)}\n<i>{_e(it['desc'])}</i>\n"
        rows.append([Btn(f"{it['emoji']} {it['name']} · {it['price']} {cur}", callback_data=f"mfs:buy:{k}")])
    rows.append([Btn(f"🎁 Kunlik sovg'a (+{ms.DAILY_COINS})" if ready else f"🎁 {left // 3600 + 1} soatdan keyin", callback_data="mfs:daily")])
    return t, _kb(rows)


def _inv(uid, name, arg):
    p = ms.profile(uid, name)
    own = [(k, n) for k, n in p["inv"].items() if n and k in ms.SHOP]
    t = "🎒 <b>Inventar</b>\n\n" + ("\n".join(f"{ms.SHOP[k]['emoji']} <b>{_e(ms.SHOP[k]['name'])}</b> ×{n}\n<i>{_e(ms.SHOP[k]['desc'])}</i>" for k, n in own)
                                   or "Hozircha bo'sh. Do'kondan biror narsa ol.")
    return t, _kb([[Btn("🛒 Do'kon", callback_data="mfs:shop")]])


def _ach(uid, name, arg):
    p = ms.profile(uid, name)
    have = {a["key"] for a in p["achievements"]}
    t = f"🎖 <b>Yutuqlar</b> ({len(have)}/{len(ms.ACHIEVEMENTS)})\n\n" + "\n".join(
        f"{'✅' if k in have else '🔒'} {a['emoji']} <b>{_e(a['name'])}</b> — {_e(a['desc'])}" for k, a in ms.ACHIEVEMENTS.items())
    return t, _kb([])


def _grp(uid, name, arg):
    gs = ms.top_groups(10)
    t = "🏘 <b>Misumi Mafia o'ynalgan guruhlar</b>\n\n"
    t += "\n".join(f"{_pos(i)} <b>{_e(g['title'])}</b>\n   🎮 {g['games']} o'yin · 👥 {g['players']} kishi · "
                   f"🏙 {g['wins'].get('town', 0)} / 🔫 {g['wins'].get('mafia', 0)} / 🔪 {g['wins'].get('maniac', 0) + g['wins'].get('vampire', 0)}"
                   for i, g in enumerate(gs)) or "Hali birorta guruh o'ynamagan. Botni guruhga qo'shib /mafia deb yoz!"
    mine = ms.groups_of(uid)
    if mine:
        t += "\n\n<b>Sen o'ynagan guruhlar</b>\n" + "\n".join(f"• {_e(g['title'])} — {g['games']} o'yin" for g in mine[:8])
    t += "\n\n🏙 shahar / 🔫 mafiya / 🔪 yakka g'alabalari"
    return t, _kb([[Btn("🏆 Reyting", callback_data="mfs:top:grp")]])


def _hist(uid, name, arg):
    p = ms.profile(uid, name)
    t = "📜 <b>Oxirgi o'yinlar</b>\n\n" + ("\n".join(
        f"{'✅' if h['won'] else '❌'} {_role(h['role'])} · +{h['gain']} {ms.CURRENCY}" + (f" · {_e(h['grp'])}" if h.get('grp') else "") + f"\n   <i>{_date(h['t'])}</i>"
        for h in p["hist"]) or "Hali o'yin yo'q.")
    return t, _kb([])


_VIEWS = {"home": _home, "prof": _prof, "top": _top, "shop": _shop, "inv": _inv, "ach": _ach, "grp": _grp, "hist": _hist}


def render(uid: int, name: str, view: str = "home", arg=None):
    return _VIEWS.get(view, _home)(uid, name or "", arg)
