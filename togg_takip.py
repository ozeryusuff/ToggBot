import email.utils
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Error as PWError, sync_playwright

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = "togg_state.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA}
MAX_SEEN = 200
MAX_AGE = timedelta(minutes=120)

CONFIGURATOR_URL = "https://configurator.togg.com.tr"
NO_VERSION = "Bu model için uygun versiyon bulunamadı"
STOCK_RE = re.compile(r"\b(?:V1|V2|RWD|Uzun Menzil|Standart Menzil)\b|\d[\d.,]*\s*(?:₺|TL\b)|₺\s*\d")

URLS = [
    "https://www.togg.com.tr/t10f",
    "https://www.togg.com.tr/t10f-price-list",
    "https://mediacenter.togg.com.tr/tr/t10f",
]
KEYWORDS = ["sipariş", "ön sipariş", "hemen başvur", "stok", "satın al", "çekiliş"]

NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=Togg+T10F+(sipari%C5%9F+OR+stok+OR+sat%C4%B1%C5%9F+OR+kampanya)+when:1h&hl=tr&gl=TR&ceid=TR:tr",
    "https://www.bing.com/news/search?q=Togg+T10F+(sipari%C5%9F+OR+stok+OR+sat%C4%B1%C5%9F)&format=rss",
]
ACCOUNTS = ["Togg2022", "Trumore"]
SOCIAL_RSS = [
    "https://nitter.privacydev.net/{}/rss",
    "https://nitter.poast.org/{}/rss",
    "https://rsshub.app/twitter/user/{}",
]
SYNDICATION = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{}"


def turkish_lower(text):
    return text.replace("İ", "i").replace("I", "ı").lower()


def send(msg):
    if not TOKEN or not CHAT_ID:
        print("[telegram yok]", msg)
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": msg[:4000], "disable_web_page_preview": True}, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print("Telegram hatası:", e)


def stock_status(text):
    """None: sayfa tam yüklenmedi, True: stok/versiyon açık, False: kapalı."""
    if "Versiyon" not in text and "Anahtar Teslim Fiyat" not in text:
        return None
    return ("Versiyon" in text and NO_VERSION not in text) or bool(STOCK_RE.search(text))


def check_configurator_stock(page, state):
    try:
        page.goto(CONFIGURATOR_URL, wait_until="domcontentloaded", timeout=60000)
    except PWError as e:
        print("Konfigüratör açılamadı, togg.com.tr üzerinden deneniyor:", e)
        page.goto(URLS[0], wait_until="domcontentloaded", timeout=60000)
        href = page.locator("a", has_text="T10F'i Konfigüre Et").first.get_attribute("href", timeout=15000)
        page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    try:
        page.locator("text=T10F").first.click(timeout=15000)
    except PWError as e:
        print("T10F sekmesine tıklanamadı:", e)
    page.wait_for_timeout(6000)
    text = page.inner_text("body")

    status = stock_status(text)
    if status is None:
        print("Konfigüratör sayfası tam yüklenemedi")
        return
    print("Konfigüratör stok durumu:", "AÇIK" if status else "kapalı")
    if status and not state.get("configurator_stock_open"):
        i = text.find("Versiyon")
        section = text[i:i + 800] if i >= 0 else text[:800]
        for _ in range(2):
            send(f"🚨🚨🚨 KIRMIZI ALARM: TOGG T10F KONFİGÜRATÖRDE STOK / VERSİYON AÇILDI! 🚨🚨🚨\n\n{section}\n\n{page.url}")
    state["configurator_stock_open"] = status


def check_websites(page, state, first_run):
    for url in URLS:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            text = page.inner_text("body")
        except PWError as e:
            print(f"{url} alınamadı: {e}")
            continue
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        low = turkish_lower(text)
        found = [k for k in KEYWORDS if k in low]
        old_hash = state["page_hashes"].get(url)
        old_found = state["found_keywords"].get(url, [])
        state["page_hashes"][url] = h
        state["found_keywords"][url] = found
        if first_run or old_hash is None:
            continue
        new_kw = [k for k in found if k not in old_found]
        if new_kw:
            for _ in range(2):
                send(f"🚨 KIRMIZI ALARM 🚨\nYeni kritik kelime: {', '.join(new_kw)}\n{url}")
        elif h != old_hash:
            send(f"ℹ️ Sayfa değişti:\n{url}")


def rss_items(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    items = []
    for item in ET.fromstring(r.content).iter("item"):
        link = item.findtext("link")
        try:
            dt = email.utils.parsedate_to_datetime(item.findtext("pubDate"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            dt = None
        items.append((item.findtext("guid") or link, item.findtext("title"), link, dt))
    return items


def notify_new(items, seen, prefix, first_run):
    now = datetime.now(timezone.utc)
    for item_id, title, link, dt in items:
        if not item_id or item_id in seen:
            continue
        seen.append(item_id)
        # Tarihi olmayan veya 2 saatten eski kayıt sadece "görüldü" olarak işaretlenir.
        if not first_run and dt is not None and now - dt <= MAX_AGE:
            send(f"{prefix} {title}\n{link}")


def check_news(state, first_run):
    for url in NEWS_FEEDS:
        try:
            notify_new(rss_items(url), state["seen_news"], "📰", first_run)
        except (requests.RequestException, ET.ParseError) as e:
            print(f"Haber kaynağı alınamadı ({url}): {e}")


def syndication_items(url, user):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = json.loads(BeautifulSoup(r.text, "html.parser").find("script", id="__NEXT_DATA__").string)
    items = []
    for entry in data["props"]["pageProps"]["timeline"]["entries"]:
        t = entry.get("content", {}).get("tweet")
        if t:
            dt = datetime.strptime(t["created_at"], "%a %b %d %H:%M:%S %z %Y")
            items.append((t["id_str"], t.get("full_text") or t.get("text"), f"https://x.com/{user}/status/{t['id_str']}", dt))
    return items


def check_social_media(state, first_run):
    for user in ACCOUNTS:
        for src in SOCIAL_RSS + [SYNDICATION]:
            url = src.format(user)
            try:
                if src == SYNDICATION:
                    items = syndication_items(url, user)
                else:
                    # Farklı köprüler aynı tweet'e farklı guid verir; tweet numarasıyla tekilleştir.
                    items = [(m.group(1) if (m := re.search(r"status/(\d+)", link or "")) else item_id, title, link, dt)
                             for item_id, title, link, dt in rss_items(url)]
            except Exception as e:  # köprüler sık sık bozuk döner; hiçbiri botu durdurmamalı
                print(f"{url} yanıt vermedi: {e}")
                continue
            if items:
                notify_new(items, state["seen_tweets"], f"🐦 @{user}:", first_run)
                break
        else:
            print(f"@{user} için hiçbir sosyal medya kaynağı yanıt vermedi")


def main():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    first_run = "page_hashes" not in state
    state.pop("pages", None)  # eski sürümün anahtarları
    state.pop("news", None)
    state.setdefault("configurator_stock_open", False)
    state.setdefault("page_hashes", {})
    state.setdefault("found_keywords", {})
    state.setdefault("seen_news", [])
    state.setdefault("seen_tweets", [])

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=UA, viewport={"width": 1920, "height": 1080})
            try:
                check_configurator_stock(page, state)
            except PWError as e:
                print("Konfigüratör kontrolü başarısız:", e)
            check_websites(page, state, first_run)
            browser.close()
    except PWError as e:
        print("Tarayıcı başlatılamadı:", e)

    check_news(state, first_run)
    check_social_media(state, first_run)
    state["seen_news"] = state["seen_news"][-MAX_SEEN:]
    state["seen_tweets"] = state["seen_tweets"][-MAX_SEEN:]

    if first_run:
        send("✅ Togg T10F takip botu başlatıldı.")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    assert turkish_lower("İSTANBUL SATIN AL") == "istanbul satın al"
    assert stock_status("Ana sayfa") is None
    assert stock_status(f"Versiyon {NO_VERSION} Bugünkü Anahtar Teslim Fiyat -") is False
    assert stock_status("Versiyon V1 RWD Standart Menzil") is True
    assert stock_status(f"Versiyon {NO_VERSION} Bugünkü Anahtar Teslim Fiyat 1.850.000 TL") is True
    main()
