# Jarvis Telegram Bot

Sof matnli chat/AI-yordamchi Telegram bot. Original **Jarvis MK37** loyihasidan
shaxsiyat (system prompt) va doimiy xotira (`save_memory`) mantig'i olindi;
kompyuterni boshqarish (fayl, terminal, brauzer, ekran) funksiyalari
**qo'shilmadi** — bu faqat suhbat va shaxsiy ma'lumotlarni eslab qolish uchun.

## Fayllar
- `bot.py` — asosiy bot (python-telegram-bot + google-genai/Gemini)
- `memory_manager.py` — har bir Telegram chat uchun alohida doimiy xotira (JSON)
- `requirements.txt`
- `render.yaml` — Render.com uchun tayyor konfiguratsiya
- `.env.example` — kerakli environment variable'lar namunasi

## 1. Kerakli kalitlar
1. **Telegram bot token** — Telegram'da [@BotFather](https://t.me/BotFather)
   ga `/newbot` yuboring, tokenni oling.
2. **Gemini API key** — https://aistudio.google.com/apikey dan bepul oling.

   ⚠️ Eslatma: yuklagan zip faylingizdagi `config/api_keys.json` ichida eski
   Gemini kaliti ochiq holda yozilgan edi. Uni Google AI Studio'da darhol
   **bekor qiling (revoke)** va yangisini oling — bu yangi loyihada hech qanday
   kalit kodga yozilmagan, faqat environment variable orqali beriladi.

## 2. Mahalliy sinov (ixtiyoriy)
```bash
cd jarvis_bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # va ichiga tokenlaringizni yozing
export $(cat .env | xargs)  # yoki python-dotenv qo'shing
python bot.py
```
Botga Telegram'da `/start` yozib sinab ko'rasiz.

## 3. Render.com'ga deploy qilish

Render'da doimiy ishlab turadigan bot uchun eng to'g'ri xizmat turi —
**Background Worker** (chunki bot Telegram'ga polling qiladi, HTTP port
kerak emas). Bepul tarifda background worker yo'q — eng arzon **Starter**
tarif (~$7/oy) kifoya, doimiy diskka (`/data`) xotira fayllarini saqlash
uchun ham kichik disk qo'shilgan.

**Qadamlar:**
1. Ushbu `jarvis_bot/` papkani alohida GitHub repo qilib yuklang (yoki
   mavjud repo ichiga qo'shing).
2. Render dashboard → **New +** → **Blueprint** → repo'ni tanlang.
   Render `render.yaml` faylini avtomatik topib, xizmatni sozlaydi.
3. Deploy paytida so'raladigan environment variable'larni kiriting:
   `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, (ixtiyoriy) `ALLOWED_CHAT_IDS`.
4. Deploy tugagach, bot avtomatik ishga tushadi va Telegram'da javob bera
   boshlaydi.

Agar Blueprint ishlatmasangiz, qo'lda: **New +** → **Background Worker** →
repo'ni ulang → Build command: `pip install -r requirements.txt` →
Start command: `python bot.py` → yuqoridagi environment variable'larni
qo'lda kiriting.

### Bepul (Web Service) variant haqida eslatma
Render'ning bepul tarifi faqat HTTP port'ga ulanadigan **Web Service**'larni
qo'llab-quvvatlaydi va bo'sh turganda "uxlab qoladi". Buni ishlatish uchun
bot kodini polling o'rniga **Telegram webhook** rejimiga o'tkazish kerak
bo'ladi (kichik HTTP server qo'shib). Agar shu variant kerak bo'lsa, ayting —
`bot.py`ni webhook rejimiga moslab beraman.

## 4. Xavfsizlik va cheklovlar
- `ALLOWED_CHAT_IDS` ni to'ldirsangiz, faqat shu Telegram chat_id'lar botdan
  foydalana oladi (o'zingizning chat_id'ingizni bilish uchun Telegram'da
  [@userinfobot](https://t.me/userinfobot) ga yozing).
- Har bir chat_id uchun alohida, izolyatsiyalangan xotira fayli saqlanadi —
  foydalanuvchilar bir-birining ma'lumotlarini ko'ra olmaydi.
- Qisqa muddatli suhbat konteksti operativ xotirada (RAM) saqlanadi va bot
  qayta ishga tushganda yo'qoladi; uzoq muddatli faktlar (`save_memory`
  orqali saqlangan) diskda qoladi — Render'da bu uchun `render.yaml`dagi
  Persistent Disk (`/data`) ishlatilyapti.
- Ushbu bot **kompyuter/fayl/terminalni boshqarish imkoniyatiga ega emas** —
  faqat matnli suhbat va xotira. Agar keyinchalik shunday funksiyalarni
  qo'shmoqchi bo'lsangiz, buni faqat o'zingiz uchun, qattiq autentifikatsiya
  bilan (masalan, faqat `ALLOWED_CHAT_IDS`dagi ID) qilishni maslahat beraman.
