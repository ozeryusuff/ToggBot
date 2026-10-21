import hashlib
import json
import os
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = "togg_state.json"
URLS = [
    "https://www.togg.com.tr/t10f",
    "https://www.togg.com.tr/t10f-price-list",
    "https://mediacenter.togg.com.tr/tr/t10f",
]
RSS_URL = "https://news.google.com/rss/search?q=Togg+T10F+(sipari%C5%9F+OR+stok+OR+sat%C4%B1%C5%9F+OR+kampanya)+when:1d&hl=tr&gl=TR&ceid=TR:tr"
KEYWORDS = ["sipariş", "ön sipariş", "konfigüratör", "hemen başvur", "stok", "satın al", "çekiliş"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
MAX_NEWS = 200


def tr_lower(text):
    return text.replace("İ", "i").replace("I", "ı").lower()


def send(msg):
    if not TOKEN or not CHAT_ID:
        print("[telegram yok]", msg)
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": True}, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print("Telegram hatası:", e)


def page_text(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "meta", "noscript", "link"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def main():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    first_run = not state
    pages = state.setdefault("pages", {})
    news = state.setdefault("news", [])

    for url in URLS:
        try:
            text = page_text(url)
        except requests.RequestException as e:
            print(f"{url} alınamadı: {e}")
            continue
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        low = tr_lower(text)
        found = [k for k in KEYWORDS if k in low]
        old = pages.get(url)
        pages[url] = {"hash": h, "keywords": found}
        if first_run or old is None:
            continue
        new_kw = [k for k in found if k not in old["keywords"]]
        if new_kw:
            for _ in range(2):
                send(f"🚨 KIRMIZI ALARM 🚨\nYeni kritik kelime: {', '.join(new_kw)}\n{url}")
        elif h != old["hash"]:
            send(f"ℹ️ Sayfa değişti:\n{url}")

    try:
        r = requests.get(RSS_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        items = ET.fromstring(r.content).iter("item")
        for item in items:
            guid = item.findtext("guid") or item.findtext("link")
            if not guid or guid in news:
                continue
            news.append(guid)
            if not first_run:
                send(f"📰 {item.findtext('title')}\n{item.findtext('link')}")
    except (requests.RequestException, ET.ParseError) as e:
        print("RSS alınamadı:", e)
    state["news"] = news[-MAX_NEWS:]

    if first_run:
        send("✅ Togg T10F takip botu başlatıldı.")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    assert tr_lower("İSTANBUL SATIN AL") == "istanbul satın al"
    main()
