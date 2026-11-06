# parser.py — парсер + рассылка + мини-апп в одном процессе (Render-ready)
import asyncio, json, os, time, logging, hmac, hashlib
from collections import defaultdict
from urllib.parse import parse_qsl

import uvicorn
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.tl.functions.payments import GetStarGiftsRequest, GetResaleStarGiftsRequest
from telethon.tl.types import StarGiftUnique
from telethon.errors import FloodWaitError
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# ==================== НАСТРОЙКИ ====================
API_ID = 30189344
API_HASH = "27655d73547e7db954ac5025596c9308"
BOT_TOKEN = "9115260417:AAHmPVGghG_T9ODQFyi-agEICtU5zHw_2OE"

MINIAPP_URL = os.environ.get("MINIAPP_URL", "https://твой-проект.onrender.com")

GROUP_CHEAP     = -1004453015273
GROUP_MID       = -1004311505503
GROUP_EXPENSIVE = -1004274993
GROUP_OVERPICE  = -1004369241832
GROUP_NEW       = -1003639073612

CHEAP_MAX, MID_MAX, OVERPICE_PCT, NEW_MAX_TON = 5.0, 20.0, 20.0, 10.0
MAX_PER_SELLER, SELLER_WINDOW = 3, 3600
STARS_TO_TON_RATE = 0.00315

CHECK_INTERVAL, SCAN_PER_CYCLE, LOTS_PER_GIFT = 5, 10, 50
REQUEST_PAUSE, CONCURRENT_REQUESTS, FLOOR_REFRESH_SEC = 1.5, 1, 14400

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

CACHE_FILE      = os.path.join(DATA_DIR, "active_gifts.json")
PROCESSED_FILE  = os.path.join(DATA_DIR, "processed.json")
FLOOR_FILE      = os.path.join(DATA_DIR, "floors.json")
SELLER_LOG_FILE = os.path.join(DATA_DIR, "seller_log.json")
OFFERS_FILE     = os.path.join(DATA_DIR, "offers.json")
SESSION_FILE    = os.path.join(DATA_DIR, "user_session")
# ===================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
processed_offers, seller_cache = set(), {}
seller_post_times = defaultdict(list)
active_gift_ids, scan_index, floors = [], 0, {}
semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

# ==================== МИНИ-АПП ====================
app = FastAPI()

def _load_offers():
    try:
        with open(OFFERS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def _save_offer(item):
    data = _load_offers()
    data[item["key"]] = {**item, "ts": time.time()}
    try:
        with open(OFFERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e: logging.warning(f"offers save: {e}")

def _validate_init(init_data):
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received: raise HTTPException(401, "no hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received): raise HTTPException(401, "bad signature")
    if time.time() - int(pairs.get("auth_date", 0)) > 86400: raise HTTPException(401, "expired")
    return pairs

class LotQuery(BaseModel):
    lot_id: str

@app.post("/api/lot")
def api_lot(q: LotQuery, x_init_data: str = Header(...)):
    _validate_init(x_init_data)
    lot = _load_offers().get(q.lot_id)
    if not lot: raise HTTPException(404, "lot not found")
    return lot

@app.get("/api/recent")
def api_recent(x_init_data: str = Header(...), limit: int = 10):
    _validate_init(x_init_data)
    items = list(_load_offers().values())
    items.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return items[:limit]

HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{margin:0;font-family:system-ui,sans-serif;background:#17181c;color:#e6e6e6;padding:16px}
h2{margin:0 0 8px;font-size:20px}.price{font-size:18px;color:#6ee7b7;margin:4px 0 12px}
.meta{font-size:13px;color:#9ca3af;margin:2px 0}
.buttons{display:flex;gap:8px;margin-top:16px}
.btn{flex:1;text-align:center;padding:12px;border-radius:10px;background:#2b2d36;color:#e6e6e6;text-decoration:none;font-size:14px}
.card{background:#22242b;padding:12px;border-radius:10px;margin:8px 0;font-size:14px;cursor:pointer}
</style></head><body><div id="app">Загрузка…</div><script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
const initData=tg.initData;
const lotId=tg.initDataUnsafe?.start_param||new URLSearchParams(location.search).get("lot");
async function api(path,body){
  const headers={"X-Init-Data":initData,"Content-Type":"application/json","ngrok-skip-browser-warning":"1"};
  const r=body?await fetch(path,{method:"POST",headers,body:JSON.stringify(body)}):await fetch(path,{headers});
  if(!r.ok)throw new Error(await r.text());return r.json();
}
function render(o){
  document.getElementById("app").innerHTML=`
    <h2>${o.title} #${o.num}</h2>
    <p class="price">${o.stars} ⭐ · ${o.price_ton} TON</p>
    <p class="meta">${o.model?"🎨 "+o.model:""}</p>
    <p class="meta">${o.background?"🖼 "+o.background:""}</p>
    <p class="meta">${o.pattern?"👑 "+o.pattern:""}</p>
    <p class="meta">${o.rarity?"📊 "+o.rarity:""}</p>
    <p class="meta">📍 ${o.seller_status}</p>
    <div class="buttons">
      <a class="btn" href="${o.seller_link}" target="_blank">👤 Профиль</a>
      <a class="btn" href="${o.gift_link}" target="_blank">🎁 Открыть</a>
    </div>`;
}
(async()=>{try{
  if(lotId){render(await api("/api/lot",{lot_id:lotId}));}
  else{
    const list=await api("/api/recent");
    const el=document.getElementById("app");
    el.innerHTML="<h2>Последние офферы</h2>";
    list.forEach(o=>{const d=document.createElement("div");d.className="card";
      d.innerHTML=`<b>${o.title} #${o.num}</b><br>${o.stars} ⭐ · ${o.price_ton} TON`;
      d.onclick=()=>render(o);el.appendChild(d);});
  }
}catch(e){document.getElementById("app").textContent="Ошибка: "+e.message;}})();
</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
def index(): return HTML

async def run_miniapp():
    port = int(os.environ.get("PORT", 8080))
    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")).serve()
# ===================================================

def convert_stars_to_ton(s): return round(s * STARS_TO_TON_RATE, 2)

def extract_stars_from_lot(lot):
    for item in getattr(lot, "resell_amount", None) or []:
        if type(item).__name__ == "StarsAmount" and getattr(item, "amount", 0):
            return int(item.amount)
    return 0

def extract_attributes(lot):
    r = {"model": None, "background": None, "pattern": None, "rarity": None, "opened_nft": None}
    for attr in getattr(lot, "attributes", []) or []:
        n = type(attr).__name__
        if n == "StarGiftAttributeModel":
            r["model"] = getattr(attr, "name", None); r["rarity"] = getattr(attr, "rarity_permille", None)
        elif n == "StarGiftAttributeBackdrop": r["background"] = getattr(attr, "name", None)
        elif n == "StarGiftAttributePattern": r["pattern"] = getattr(attr, "name", None)
    r["opened_nft"] = getattr(lot, "availability_issued", None)
    return r

def _load_json(p):
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: return None
    return None

def _save_json(p, d):
    try:
        with open(p, "w", encoding="utf-8") as f: json.dump(d, f)
    except Exception: pass

def save_floors(): _save_json(FLOOR_FILE, floors)
def save_seller_log(): _save_json(SELLER_LOG_FILE, {str(k): list(v) for k, v in seller_post_times.items()})

def load_floors():
    global floors
    raw = _load_json(FLOOR_FILE)
    if raw: floors = raw

def load_seller_log():
    global seller_post_times
    raw = _load_json(SELLER_LOG_FILE)
    if not raw: return
    res = defaultdict(list)
    for k, v in raw.items():
        try: key = int(k)
        except Exception: continue
        if isinstance(v, list): res[key] = [float(x) for x in v]
        elif isinstance(v, (int, float)): res[key] = [float(v)]
    seller_post_times = res
    logging.info(f"Продавцов с историей: {len(seller_post_times)}")

def save_processed():
    try:
        with open(PROCESSED_FILE, "w") as f: json.dump(list(processed_offers), f)
    except Exception as e: logging.warning(f"processed: {e}")

def load_processed():
    global processed_offers
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, "r") as f: processed_offers = set(json.load(f))
            logging.info(f"Обработанных: {len(processed_offers)}")
        except Exception as e: logging.warning(f"processed: {e}")

async def resolve_seller(sid):
    if sid in seller_cache: return seller_cache[sid]
    try:
        e = await client.get_entity(sid)
        un = getattr(e, "username", None)
        st = type(getattr(e, "status", None)).__name__ if getattr(e, "status", None) else "Unknown"
        if "Online" in st: stt = "В сети"
        elif "Recently" in st: stt = "Недавно"
        elif "LastWeek" in st: stt = "На этой неделе"
        elif "LastMonth" in st: stt = "В этом месяце"
        else: stt = "Давно"
        if un: r = (f"@{un}", f"https://t.me/{un}", stt)
        else: r = (getattr(e, "first_name", None) or "Продавец", f"tg://user?id={sid}", stt)
    except Exception:
        r = (f"Пользователь #{sid}", f"tg://user?id={sid}", "—")
    seller_cache[sid] = r
    return r

async def resolve_seller_from_lot(lot):
    sid = None
    host = getattr(lot, "host_id", None)
    if host and getattr(host, "user_id", None): sid = host.user_id
    if not sid:
        owner = getattr(lot, "owner_id", None)
        if owner and getattr(owner, "user_id", None): sid = owner.user_id
    if not sid and getattr(lot, "owner_name", None):
        ns = str(lot.owner_name).strip()
        cand = ns[1:] if ns.startswith("@") else (ns if ns.isascii() and ns.replace("_", "").isalnum() else None)
        if cand:
            try: sid = (await client.get_entity(cand)).id
            except Exception: pass
    if not sid and getattr(lot, "owner_address", None):
        return (0, "Владелец (TON)", f"https://tonviewer.com/{lot.owner_address}", "—")
    if not sid: return None
    m, l, s = await resolve_seller(sid)
    return (sid, m, l, s)

def seller_allowed(sid):
    if sid == 0: return True
    now = time.time()
    t = seller_post_times[sid]
    t[:] = [x for x in t if now - x < SELLER_WINDOW]
    return len(t) < MAX_PER_SELLER

def register_seller_post(sid):
    if sid == 0: return
    seller_post_times[sid].append(time.time()); save_seller_log()

def pick_groups(price, vs):
    t = []
    if price <= CHEAP_MAX: t.append(GROUP_CHEAP)
    elif price <= MID_MAX: t.append(GROUP_MID)
    else: t.append(GROUP_EXPENSIVE)
    if price <= NEW_MAX_TON: t.append(GROUP_NEW)
    if vs is not None and vs >= OVERPICE_PCT: t.append(GROUP_OVERPICE)
    return list(set(t))

async def send_offer(item):
    price = item["price_ton"]
    floor = floors.get(item["title"])
    vs = round((price - floor) / floor * 100, 1) if floor and floor > 0 else None
    targets = pick_groups(price, vs)
    if not targets: return

    _save_offer(item)

    lines = [f"🎁 <b>{item['title']} #{item['num']}</b>", "",
             f"💵 Цена: <b>{item['stars']}</b> ⭐ · <b>{price} TON</b>"]
    if floor:
        if vs <= -5: mark = f"⬇️ <b>{vs}%</b> ниже флора"
        elif vs >= 5: mark = f"📈 <b>+{vs}%</b> выше флора"
        else: mark = "≈ в рынке"
        lines.append(f"📊 {mark} · флор <b>{floor} TON</b>")
    lines.append("")
    if item.get("model"): lines.append(f"🎨 Модель: {item['model']}")
    if item.get("background"): lines.append(f"🖼 Фон: {item['background']}")
    if item.get("pattern"): lines.append(f"👑 Узор: {item['pattern']}")
    if item.get("rarity"): lines.append(f"📊 Рейтинг: {item['rarity']}")
    if item.get("opened_nft") is not None: lines.append(f"📦 Открытых NFT: {item['opened_nft']}")
    lines.append(f"📍 В сети: {item['seller_status']}")
    lines.append(f"🔗 {item['gift_link']}")
    text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль владельца", url=item["seller_link"]),
         InlineKeyboardButton(text="🎁 Открыть", url=item["gift_link"])],
        [InlineKeyboardButton(text="📱 Открыть в приложении",
                              web_app=WebAppInfo(url=f"{MINIAPP_URL}/?lot={item['key']}"))],
    ])

    async def _send(t):
        try:
            await bot.send_message(chat_id=t, text=text, parse_mode="HTML",
                                   disable_web_page_preview=True, reply_markup=kb)
            logging.info(f"✅ → {t} | {item['title']} #{item['num']} | {price} TON")
            return True
        except Exception as e:
            logging.error(f"❌ {t}: {type(e).__name__}: {e}"); return False

    results = await asyncio.gather(*[_send(t) for t in targets])
    if any(results): register_seller_post(item["seller_id"])

async def fetch_resale_gifts(gid, limit=50):
    async with semaphore:
        try:
            res = await client(GetResaleStarGiftsRequest(gift_id=gid, offset="", limit=limit))
            return getattr(res, "gifts", []) or []
        except FloodWaitError as e:
            logging.warning(f"FloodWait {e.seconds}s на {gid}")
            await asyncio.sleep(e.seconds + 1); return []
        except Exception as e:
            logging.debug(f"resale {gid}: {type(e).__name__}: {e}"); return []

async def discover_active_gifts():
    global active_gift_ids
    raw = _load_json(CACHE_FILE)
    if raw:
        active_gift_ids = raw
        logging.info(f"Кэш: {len(active_gift_ids)}"); return
    try:
        cat = await client(GetStarGiftsRequest(hash=0))
        active_gift_ids = [getattr(g, "id", None) for g in (getattr(cat, "gifts", []) or []) if getattr(g, "id", None)]
        _save_json(CACHE_FILE, active_gift_ids)
        logging.info(f"Загружено {len(active_gift_ids)} ID")
    except Exception as e: logging.error(f"Каталог: {e}")

async def update_floors():
    global floors
    tp = defaultdict(list)
    for gid in active_gift_ids[:40]:
        for lot in await fetch_resale_gifts(gid, limit=20):
            if isinstance(lot, StarGiftUnique):
                title, stars = getattr(lot, "title", None), extract_stars_from_lot(lot)
                if title and stars: tp[title].append(convert_stars_to_ton(stars))
        await asyncio.sleep(REQUEST_PAUSE)
    for title, prices in tp.items():
        if prices: floors[title] = round(min(prices), 2)
    save_floors()
    logging.info(f"Флор обновлён: {len(floors)}")

async def process_lot(lot):
    lot_id, slug = getattr(lot, "id", None), getattr(lot, "slug", None)
    num = getattr(lot, "num", "—"); title = getattr(lot, "title", None) or "NFT Подарок"
    stars = extract_stars_from_lot(lot)
    if not (lot_id and slug and stars): return None
    si = await resolve_seller_from_lot(lot)
    if not si: return None
    sid, mention, link, stt = si
    key = f"{lot_id}_{stars}_{sid}"
    if key in processed_offers or not seller_allowed(sid): return None
    a = extract_attributes(lot)
    return {"key": key, "seller_id": sid, "title": title, "num": num, "stars": stars,
            "price_ton": convert_stars_to_ton(stars), "model": a["model"], "background": a["background"],
            "pattern": a["pattern"], "rarity": a["rarity"], "opened_nft": a["opened_nft"],
            "seller_mention": mention, "seller_link": link, "seller_status": stt,
            "gift_link": f"https://t.me/nft/{slug}"}

async def scan_market():
    global scan_index
    if not active_gift_ids: return
    batch = active_gift_ids[scan_index:scan_index + SCAN_PER_CYCLE] or active_gift_ids[:SCAN_PER_CYCLE]
    scan_index = (scan_index + SCAN_PER_CYCLE) % len(active_gift_ids)
    fresh = []
    for gid in batch:
        for lot in (await fetch_resale_gifts(gid, limit=LOTS_PER_GIFT))[:LOTS_PER_GIFT]:
            if isinstance(lot, StarGiftUnique):
                item = await process_lot(lot)
                if item: fresh.append(item)
        await asyncio.sleep(REQUEST_PAUSE)
    logging.info(f"Цикл: проверено {len(batch)}, новых: {len(fresh)}")
    if fresh:
        await asyncio.gather(*[send_offer(i) for i in fresh])
        for i in fresh: processed_offers.add(i["key"])
        save_processed()

async def floors_loop():
    while True:
        try: await update_floors()
        except Exception as e: logging.error(f"floors: {type(e).__name__}: {e}")
        await asyncio.sleep(FLOOR_REFRESH_SEC)

async def main():
    await client.start()
    load_processed(); load_floors(); load_seller_log()
    await discover_active_gifts()
    asyncio.create_task(run_miniapp())
    logging.info(f"Мини-апп слушает, публичный {MINIAPP_URL}")
    asyncio.create_task(floors_loop())
    while True:
        t0 = time.time()
        try: await scan_market()
        except Exception as e: logging.error(f"Ошибка: {type(e).__name__}: {e}")
        logging.info(f"⏱ цикл {round(time.time()-t0,1)}s")
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: logging.info("Остановлено.")