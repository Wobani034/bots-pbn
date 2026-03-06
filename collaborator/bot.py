"""
collaborator/bot.py
Bot collaborator.pro — Login + scraping des commandes + soumission URL
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from core import auth, lovable

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
CONFIG = {
    "login_url":        "https://collaborator.pro/creator/login",
    "dashboard_url":    "https://collaborator.pro/creator/dashboard/deals",
    "email":            os.getenv("EMAIL", ""),
    "password":         os.getenv("PASSWORD", ""),
    "cookies_file":     "cookies.json",
    "output_file":      "orders_output.json",
    "headless":         True,
    "timeout":          30_000,
    "lovable_endpoint": os.getenv("LOVABLE_ENDPOINT", ""),
    "import_api_key":   os.getenv("IMPORT_API_KEY", ""),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collaborator")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def new_browser(pw, headless=None):
    if headless is None:
        headless = CONFIG["headless"]
    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context(user_agent=UA)
    page    = context.new_page()
    page.set_default_timeout(CONFIG["timeout"])
    return browser, context, page


def ensure_logged_in(page, context):
    if auth.load_cookies(context, CONFIG["cookies_file"]):
        page.goto(CONFIG["dashboard_url"], wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5000)
        if auth.is_logged_in(page, CONFIG["login_url"]):
            log.info("Connecté via cookies")
            return
        log.warning("Cookies expirés")
    # Cloudflare bloque le login auto — on ne peut que signaler
    log.error("Cookies expirés ou absents — notification Lovable")
    lovable.notify_cookies_expired(
        "collaborator", CONFIG["lovable_endpoint"], CONFIG["import_api_key"]
    )
    raise RuntimeError("Cookies collaborator.pro expirés — mise à jour nécessaire via Lovable")


def interactive_login():
    """Ouvre un navigateur visible pour login manuel (Cloudflare)."""
    log.info("Login interactif — navigateur visible")
    log.info("Passe le Cloudflare, connecte-toi au dashboard...")
    with sync_playwright() as pw:
        browser, context, page = new_browser(pw, headless=False)
        try:
            page.goto(CONFIG["login_url"], wait_until="domcontentloaded", timeout=60_000)
            for _ in range(120):
                if "dashboard" in page.url:
                    break
                page.wait_for_timeout(1000)
            if "dashboard" not in page.url:
                log.error(f"Timeout — URL actuelle : {page.url}")
                return
            log.info(f"Dashboard atteint : {page.url}")
            page.wait_for_timeout(3000)
            auth.save_cookies(context, CONFIG["cookies_file"])
            log.info(f"Cookies sauvegardés dans {CONFIG['cookies_file']}")
        finally:
            browser.close()


def scrape_order_detail(page, order_id: str) -> dict:
    """Visite la page de détail d'une commande et extrait les infos."""
    url = f"https://collaborator.pro/deal/default/show-info-article?id={order_id}"
    log.info(f"  -> Détail commande #{order_id}")
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3000)

    page.screenshot(path=f"detail_{order_id}.png")
    with open(f"detail_{order_id}_dump.html", "w") as f:
        f.write(page.content())

    text = page.inner_text("body")

    # Nombre de mots
    m = re.search(r"(\d+)\s*words?", text, re.IGNORECASE)
    word_count_min = int(m.group(1)) if m else None

    # Thématique / sujet
    topic = ""
    m = re.search(r"(?:Topic|Theme|Subject)[:\s]*(.+)", text, re.IGNORECASE)
    if m:
        topic = m.group(1).strip()

    # Liens à insérer
    links_to_add = []
    # Cherche les liens dans le brief/requirements
    brief_el = page.query_selector(".task-description, .deal-requirements, .article-brief")
    if brief_el:
        dom_links = brief_el.evaluate("""el => Array.from(el.querySelectorAll('a')).map(a => ({
            href: a.href, anchor: a.innerText.trim()
        }))""")
        seen = set()
        for lnk in dom_links:
            if lnk.get("href") and lnk["href"] not in seen and "collaborator.pro" not in lnk["href"]:
                seen.add(lnk["href"])
                links_to_add.append({"href": lnk["href"], "anchor": lnk.get("anchor", "")})

    links_html = " ".join(
        f'<a href="{l["href"]}">{l["anchor"]}</a>' for l in links_to_add
    ) if links_to_add else ""

    return {
        "word_count_min": word_count_min,
        "topic":          topic,
        "links_to_add":   links_to_add,
        "links_html":     links_html,
    }


def scrape_orders(page, context) -> list:
    log.info(f"Navigation vers {CONFIG['dashboard_url']}")
    page.goto(CONFIG["dashboard_url"], wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(5000)

    rows = page.query_selector_all('[data-role="table-row"]')
    if not rows:
        log.info("Aucune commande trouvée")
        return []

    log.info(f"Tableau trouvé : {len(rows)} lignes")

    # Snapshot des données avant navigation
    snapshots = []
    for row in rows:
        # Order ID
        id_cell = row.query_selector('[data-cell-id="createdAt"]')
        id_text = id_cell.inner_text() if id_cell else ""
        m = re.search(r"ID\s*(\d+)", id_text)
        order_id = m.group(1) if m else ""

        # Date
        date_match = re.search(r"\d{2}\s+\w+\s+\d{4}", id_text)
        date = date_match.group() if date_match else ""

        # Site URL
        site_el = row.query_selector('[data-cell-id="site"] .column-creator__name-link')
        site_url = site_el.inner_text().strip() if site_el else ""

        # Article title
        article_el = row.query_selector('[data-cell-id="article"] a')
        article_title = article_el.inner_text().strip() if article_el else ""

        # Client
        client_el = row.query_selector('[data-cell-id="customer"] a')
        client = client_el.inner_text().strip() if client_el else ""

        # Status
        status_el = row.query_selector('[data-cell-id="status"] .status-block__content')
        status_text = status_el.inner_text().strip().split("\n")[0] if status_el else ""

        # Published URL
        pub_el = row.query_selector('[data-cell-id="status"] .status-block__link')
        published_url = pub_el.get_attribute("href") if pub_el else ""

        # Price
        price_el = row.query_selector('[data-cell-id="price"]')
        price_text = price_el.inner_text().strip() if price_el else "0"
        price_num = re.search(r"[\d.]+", price_text)
        gain = float(price_num.group()) if price_num else 0.0

        if not order_id:
            continue

        snapshots.append({
            "order_id":      order_id,
            "date":          date,
            "site_url":      site_url,
            "article_title": article_title,
            "client":        client,
            "status_text":   status_text,
            "published_url": published_url,
            "gain":          gain,
        })

    # Visite les pages de détail
    orders = []
    for snap in snapshots:
        status = _map_status(snap["status_text"])

        detail = scrape_order_detail(page, snap["order_id"])
        snap.update(detail)
        snap["task_type"] = "write_and_publish"
        snap["status"]    = status

        orders.append(snap)
        log.info(f"Commande #{snap['order_id']} ({status}) — {snap['site_url']} — {snap['gain']}€")

        # Retour au dashboard
        page.goto(CONFIG["dashboard_url"], wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)

    return orders


def _map_status(status_text: str) -> str:
    """Mappe le statut Collaborator vers notre statut interne."""
    s = status_text.lower()
    if "posted" in s or "completed" in s or "accepted" in s:
        return "completed"
    if "writing" in s or "in progress" in s:
        return "to_write"
    if "review" in s:
        return "in_review"
    if "waiting" in s:
        return "waiting_for_client"
    return "unknown"


def normalize_order(order: dict) -> dict:
    """Construit le payload Lovable."""
    return {
        "provider":        "collaborator",
        "order_id":        order["order_id"],
        "site_url":        order.get("site_url", ""),
        "gain":            order.get("gain", 0.0),
        "deadline_days":   order.get("deadline_days"),
        "task_type":       order.get("task_type", "write_and_publish"),
        "status":          order.get("status", "to_write"),
        "topic":           order.get("topic", ""),
        "word_count_min":  order.get("word_count_min"),
        "links_to_add":    order.get("links_to_add", []),
        "links_html":      order.get("links_html", ""),
        "article_title":   order.get("article_title"),
        "article_content": order.get("article_content"),
        "published_url":   order.get("published_url", ""),
        "client":          order.get("client", ""),
        "messages":        order.get("messages", []),
    }


def validate_order(order_id: str, published_url: str) -> dict:
    raise NotImplementedError("validate_order pas encore implémenté pour collaborator")


def _notify(order_id, published_url, success, message):
    lovable.notify_validation(order_id, published_url, success, message,
                              CONFIG["lovable_endpoint"], CONFIG["import_api_key"])


def run():
    if not CONFIG["email"] or not CONFIG["password"]:
        log.error("EMAIL ou PASSWORD manquant dans le fichier .env")
        sys.exit(1)

    log.info("Démarrage bot collaborator")
    with sync_playwright() as pw:
        browser, context, page = new_browser(pw)
        try:
            ensure_logged_in(page, context)
            orders = scrape_orders(page, context)
        finally:
            browser.close()

    result = {
        "bot":          "collaborator",
        "scraped_at":   datetime.now(timezone.utc).isoformat(),
        "orders_count": len(orders),
        "orders":       orders,
    }
    with open(CONFIG["output_file"], "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    log.info(f"Résultats sauvegardés dans {CONFIG['output_file']}")

    payloads = [normalize_order(o) for o in orders]
    lovable.send_orders(payloads, CONFIG["lovable_endpoint"], CONFIG["import_api_key"])

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def serve():
    from core.webhook import create_app
    app  = create_app(CONFIG, run, validate_order, _notify)
    port = int(os.getenv("PORT", 5004))
    log.info(f"Webhook collaborator démarré sur le port {port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--login":
        interactive_login()
    elif len(sys.argv) == 2 and sys.argv[1] == "--serve":
        serve()
    else:
        run()
