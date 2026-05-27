from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
import xml.etree.ElementTree as ET
import json
import os
import gzip
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import urllib.request
import urllib.parse
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Israeli Supermarket Deal Finder")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
HISTORY_FILE = DATA_DIR / "price_history.json"
LAST_RUN_FILE = DATA_DIR / "last_run.json"

# Telegram config from environment variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8780786951")

# ─── Chain XML sources (Israeli food transparency law) ────────────────────────
# These are the official price transparency XML feeds required by Israeli law
CHAINS = {
    "shufersal": {
        "name": "שופרסל",
        "price_url": "https://prices.shufersal.co.il/FileObject/UpdateCategory?catID=5&storeId=0",
        "emoji": "🔵",
    },
    "rami_levy": {
        "name": "רמי לוי",
        "price_url": "https://url.retail.publishedprices.co.il/file/dl/gzgt",  # Rami Levy XML
        "emoji": "🟢",
    },
    "tiv_taam": {
        "name": "טיב טעם",
        "price_url": "https://tivtaam.retailprice.co.il/xml",
        "emoji": "🟡",
    },
}

# Popular Israeli grocery items (barcode-independent search by name)
POPULAR_ITEMS_KEYWORDS = [
    "חלב", "לחם", "ביצים", "גבינה", "קוטג",
    "עוף", "בקר", "סלמון", "טונה",
    "אורז", "פסטה", "קמח", "סוכר", "שמן",
    "עגבניות", "מלפפון", "גזר", "בצל", "תפוח אדמה",
    "תפוח", "בננה", "לימון",
    "מיץ", "קולה", "מים",
    "קפה", "תה", "שוקולד",
    "שמפו", "סבון", "נייר טואלט",
    "דטרגנט", "אקונומיקה",
    "יוגורט", "שמנת", "חמאה",
    "כנפיים", "חזה עוף", "שניצל",
]


def load_price_history() -> dict:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {}


def save_price_history(history: dict):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def load_last_run() -> dict:
    if LAST_RUN_FILE.exists():
        with open(LAST_RUN_FILE) as f:
            return json.load(f)
    return {}


def save_last_run(data: dict):
    with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def fetch_xml_prices(chain_id: str, url: str) -> list[dict]:
    """Fetch and parse XML price file from a chain."""
    products = []
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            # Try fetching the index/listing page first for chains that list files
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; PriceBot/1.0)",
                "Accept": "application/xml, text/xml, */*",
            }
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()

            content = resp.content

            # Handle gzip
            if content[:2] == b'\x1f\x8b':
                content = gzip.decompress(content)

            text = content.decode("utf-8", errors="replace")

            # Parse XML
            root = ET.fromstring(text)

            # Different chains use different XML schemas — handle both
            items = (
                root.findall(".//Item") or
                root.findall(".//Product") or
                root.findall(".//item")
            )

            for item in items[:500]:  # limit to 500 per chain for speed
                def get(tag):
                    el = item.find(tag)
                    return el.text.strip() if el is not None and el.text else ""

                name = get("ItemName") or get("ProductName") or get("itemName") or get("name")
                price_str = get("ItemPrice") or get("Price") or get("price") or get("UnitOfMeasurePrice")
                barcode = get("ItemCode") or get("Barcode") or get("barcode") or get("ManufacturerItemDescription")

                if not name or not price_str:
                    continue

                try:
                    price = float(price_str)
                except ValueError:
                    continue

                if price <= 0:
                    continue

                # Filter to popular items only
                name_lower = name.lower()
                is_popular = any(kw in name for kw in POPULAR_ITEMS_KEYWORDS)
                if not is_popular:
                    continue

                products.append({
                    "barcode": barcode,
                    "name": name,
                    "price": price,
                    "chain": chain_id,
                })

    except Exception as e:
        logger.error(f"Error fetching {chain_id}: {e}")

    return products


def find_deals(current_prices: dict, history: dict) -> list[dict]:
    """Find products with significant price drops vs 8-week average."""
    deals = []
    now = datetime.now().isoformat()
    eight_weeks_ago = (datetime.now() - timedelta(weeks=8)).isoformat()

    for key, current in current_prices.items():
        if key not in history:
            continue

        past_prices = [
            entry["price"]
            for entry in history[key]["entries"]
            if entry["date"] >= eight_weeks_ago
        ]

        if len(past_prices) < 2:
            continue

        avg = sum(past_prices) / len(past_prices)
        drop_pct = (avg - current["price"]) / avg * 100

        if drop_pct >= 10:  # 10%+ drop = deal
            deals.append({
                "name": current["name"],
                "chain": current["chain"],
                "current_price": current["price"],
                "avg_price": round(avg, 2),
                "drop_pct": round(drop_pct, 1),
            })

    deals.sort(key=lambda x: x["drop_pct"], reverse=True)

    # Top 10 per chain
    per_chain = {}
    result = []
    for deal in deals:
        chain = deal["chain"]
        if per_chain.get(chain, 0) < 10:
            result.append(deal)
            per_chain[chain] = per_chain.get(chain, 0) + 1

    return result


def update_history(history: dict, current_prices: dict) -> dict:
    today = datetime.now().isoformat()
    eight_weeks_ago = (datetime.now() - timedelta(weeks=8)).isoformat()

    for key, data in current_prices.items():
        if key not in history:
            history[key] = {"name": data["name"], "chain": data["chain"], "entries": []}

        history[key]["entries"].append({"date": today, "price": data["price"]})

        # Keep only last 8 weeks
        history[key]["entries"] = [
            e for e in history[key]["entries"] if e["date"] >= eight_weeks_ago
        ]

    return history


def send_telegram(deals: list[dict], chains_summary: dict):
    if not TELEGRAM_TOKEN:
        logger.warning("Telegram not configured, skipping message")
        return False

    try:
        chain_names = {"shufersal": "שופרסל 🔵", "rami_levy": "רמי לוי 🟢", "tiv_taam": "טיב טעם 🟡"}
        now = datetime.now().strftime('%d/%m/%Y %H:%M')

        if not deals:
            text = f"🛒 *בדיקת מחירים* | {now}\n\n😴 אין מבצעים משמעותיים כרגע.\nנבדוק שוב מחר!"
        else:
            lines = [f"🛒 *{len(deals)} מבצעים ברשתות המזון*\n_{now}_\n"]
            current_chain = None
            for deal in deals:
                if deal["chain"] != current_chain:
                    current_chain = deal["chain"]
                    lines.append(f"\n*{chain_names.get(current_chain, current_chain)}*")
                saving = deal["avg_price"] - deal["current_price"]
                lines.append(
                    f"• {deal['name']}\n"
                    f"  ₪{deal['current_price']:.2f} במקום ₪{deal['avg_price']:.2f} "
                    f"(↓{deal['drop_pct']}%, חיסכון ₪{saving:.2f})"
                )
            text = "\n".join(lines)

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }).encode()

        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        logger.info("Telegram message sent!")
        return True

    except Exception as e:
        logger.error(f"Failed to send Telegram: {e}")
        return False


# ─── API Endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "message": "Israeli Supermarket Deal Finder API"}


@app.get("/status")
async def get_status():
    last_run = load_last_run()
    history = load_price_history()
    total_products = sum(len(v.get("entries", [])) for v in history.values())
    return {
        "last_run": last_run.get("timestamp"),
        "total_tracked_products": len(history),
        "total_price_points": total_products,
        "chains": list(CHAINS.keys()),
        "telegram_configured": bool(TELEGRAM_TOKEN),
    }


@app.post("/run")
async def run_check(background_tasks: BackgroundTasks):
    """Trigger a price check. Runs in background and returns immediately."""
    background_tasks.add_task(do_price_check)
    return {"status": "started", "message": "בדיקת מחירים התחילה! תוצאות יגיעו תוך כמה דקות."}


@app.get("/deals")
async def get_deals():
    """Return current deals without re-fetching."""
    last_run = load_last_run()
    return {
        "timestamp": last_run.get("timestamp"),
        "deals": last_run.get("deals", []),
        "chains_fetched": last_run.get("chains_fetched", {}),
        "email_sent": last_run.get("email_sent", False),
    }


async def do_price_check():
    logger.info("Starting price check...")
    all_current = {}
    chains_fetched = {}

    for chain_id, chain_info in CHAINS.items():
        logger.info(f"Fetching {chain_info['name']}...")
        products = await fetch_xml_prices(chain_id, chain_info["price_url"])
        chains_fetched[chain_id] = len(products)
        logger.info(f"  Got {len(products)} products from {chain_info['name']}")

        for p in products:
            key = f"{chain_id}:{p['barcode'] or p['name']}"
            all_current[key] = p

    # Update history
    history = load_price_history()
    history = update_history(history, all_current)
    save_price_history(history)

    # Find deals
    deals = find_deals(all_current, history)
    logger.info(f"Found {len(deals)} deals")

    # Send Telegram message
    email_sent = send_telegram(deals, chains_fetched)

    # Save run results
    save_last_run({
        "timestamp": datetime.now().isoformat(),
        "deals": deals,
        "chains_fetched": chains_fetched,
        "total_products_checked": len(all_current),
        "email_sent": email_sent,
    })

    logger.info("Price check complete!")
