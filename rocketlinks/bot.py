"""
rocketlinks/bot.py
Bot rocketlinks.net — Login + scraping des commandes + soumission URL
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
    "login_url":        "https://www.rocketlinks.net/login",
    "orders_url":       "https://www.rocketlinks.net/deals/all",
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
log = logging.getLogger("rocketlinks")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

STATUS_MAP = {
    "Vous devez rédiger, publier et indiquer l'URL de l'article": "write_and_publish",
    "Vous devez publier et indiquer l'URL de l'article":          "publish_only",
}


def new_browser(pw):
    browser = pw.chromium.launch(headless=CONFIG["headless"])
    context = browser.new_context(user_agent=UA)
    page    = context.new_page()
    page.set_default_timeout(CONFIG["timeout"])
    return browser, context, page


def ensure_logged_in(page, context):
    if auth.load_cookies(context, CONFIG["cookies_file"]):
        page.goto(CONFIG["orders_url"], wait_until="networkidle", timeout=CONFIG["timeout"])
        if auth.is_logged_in(page, CONFIG["login_url"]):
            log.info("Connecté via cookies")
            return
        log.warning("Cookies expirés — fallback login")
    do_login(page)
    if not auth.is_logged_in(page, CONFIG["login_url"]):
        log.error("Echec du login")
        sys.exit(1)
    auth.save_cookies(context, CONFIG["cookies_file"])
    log.info("Login réussi")


def do_login(page):
    log.info("Login avec email/password...")
    page.goto(CONFIG["login_url"], wait_until="networkidle")
    page.fill('input[name="data[User][email]"]', CONFIG["email"])
    page.fill('input[name="data[User][password]"]', CONFIG["password"])
    page.click('input[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=CONFIG["timeout"])


def scrape_order_detail(page, order_id: str) -> dict:
    """Visite la page de détail d'une offre et en extrait le brief."""
    url = f"https://www.rocketlinks.net/offers/{order_id}"
    log.info(f"  -> Détail offre #{order_id}")
    page.goto(url, wait_until="networkidle", timeout=CONFIG["timeout"])

    text = page.inner_text("body")

    # Nombre de mots minimum
    m = re.search(r"Nombre de mots[^\d]*(\d+)", text)
    word_count_min = int(m.group(1)) if m else None

    # Thématique
    m = re.search(r"thématique suivante\s*:\s*(.+)", text)
    topic = m.group(1).strip() if m else ""

    # Délai restant
    m = re.search(r"(\d+)\s*jours?\s+et\s+\d+\s*heure", text)
    deadline_days = int(m.group(1)) if m else None

    # Liens à insérer (HTML encodé dans la balise <pre> du brief)
    brief_el = page.query_selector(".article-brief")
    brief_text = brief_el.inner_text() if brief_el else ""
    raw_links = re.findall(r'<a\s+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>', brief_text)
    links_to_add = [{"href": href, "anchor": anchor.strip()} for href, anchor in raw_links]

    # Contenu article fourni par le client (Type A, si disponible)
    article_content = None
    article_el = page.query_selector(".article-content, [class*='article-text'], .content-provided")
    if article_el:
        article_content = article_el.inner_text().strip()

    return {
        "word_count_min": word_count_min,
        "topic":          topic,
        "deadline_days":  deadline_days,
        "links_to_add":   links_to_add,
        "article_content": article_content,
    }


def scrape_orders(page) -> list:
    log.info(f"Navigation vers {CONFIG['orders_url']}")
    page.goto(CONFIG["orders_url"], wait_until="networkidle", timeout=CONFIG["timeout"])

    rows = page.query_selector_all("table tbody tr")
    if not rows:
        log.info("Aucune commande en cours")
        return []

    log.info(f"Tableau trouvé : {len(rows)} lignes")

    # Snapshot complet avant de naviguer
    snapshots = []
    for row in rows:
        cells = [td.inner_text().strip() for td in row.query_selector_all("td")]
        if not any(cells):
            continue
        links = [
            {"text": a.inner_text().strip(), "href": a.get_attribute("href")}
            for a in row.query_selector_all("a")
        ]
        snapshots.append({"cells": cells, "links": links})

    orders = []
    for snap in snapshots:
        # order_id depuis /offers/384254
        offer_link = next(
            (l for l in snap["links"] if l["href"] and l["href"].startswith("/offers/")),
            None,
        )
        if not offer_link:
            continue
        order_id = offer_link["href"].split("/")[-1]

        # Colonnes : Statut modifié le | Titre | Statut | URL | Prix | Gains totaux
        status_text = snap["cells"][2] if len(snap["cells"]) > 2 else ""
        task_type   = STATUS_MAP.get(status_text, "unknown")
        site_url    = snap["cells"][3] if len(snap["cells"]) > 3 else ""

        gain_raw = (snap["cells"][4] if len(snap["cells"]) > 4 else "0")
        gain_raw = gain_raw.replace("€", "").replace(",", ".").replace("\xa0", "").strip()
        try:
            gain = float(gain_raw)
        except ValueError:
            gain = 0.0

        order = {
            "order_id":    order_id,
            "site_url":    site_url,
            "task_type":   task_type,
            "status_text": status_text,
            "gain":        gain,
        }

        # Détails du brief
        detail = scrape_order_detail(page, order_id)
        order.update(detail)

        orders.append(order)
        log.info(f"Commande #{order_id} ({task_type}) — {site_url} — {gain}€")

        # Retour à la liste
        page.goto(CONFIG["orders_url"], wait_until="networkidle", timeout=CONFIG["timeout"])

    return orders


def compute_status(task_type: str, article_content) -> str:
    """
    Détermine le statut métier de la commande :
    - to_write          : on doit rédiger, publier et soumettre l'URL
    - waiting_for_client: publish_only mais l'article n'a pas encore été envoyé
    - to_publish        : article reçu du client, prêt à publier et soumettre l'URL
    """
    if task_type == "write_and_publish":
        return "to_write"
    if task_type == "publish_only":
        return "to_publish" if article_content else "waiting_for_client"
    return "unknown"


def normalize_order(order: dict) -> dict:
    """Construit le payload Lovable."""
    task_type       = order.get("task_type", "unknown")
    article_content = order.get("article_content")
    return {
        "provider":        "rocketlinks",
        "order_id":        order["order_id"],
        "site_url":        order.get("site_url", ""),
        "gain":            order.get("gain", 0.0),
        "deadline_days":   order.get("deadline_days"),
        "task_type":       task_type,
        "status":          compute_status(task_type, article_content),
        "topic":           order.get("topic", ""),
        "word_count_min":  order.get("word_count_min"),
        "links_to_add":    order.get("links_to_add", []),
        "article_content": article_content,
    }


def validate_order(order_id: str, published_url: str) -> dict:
    """Soumet l'URL publiée sur RocketLinks via le formulaire announce-article."""
    log.info(f"Validation commande #{order_id} → {published_url}")

    with sync_playwright() as pw:
        browser, context, page = new_browser(pw)
        try:
            ensure_logged_in(page, context)
            announce_url = f"https://www.rocketlinks.net/offers/{order_id}/announce-article"
            page.goto(announce_url, wait_until="networkidle", timeout=CONFIG["timeout"])

            if page.url != announce_url and "announce" not in page.url:
                raise RuntimeError(f"Impossible d'accéder au formulaire pour #{order_id}")

            page.fill('input[name="data[Deal][dedicated_page]"]', published_url)

            # Coche la garantie
            guarantee = page.locator('input[name="data[Deal][guarantee]"][type="checkbox"]')
            if guarantee.count() and not guarantee.is_checked():
                guarantee.check()

            # Coche les CGU
            terms = page.locator('input[name="data[Terms][accept]"]')
            if terms.count() and not terms.is_checked():
                terms.check()

            page.click('input[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=CONFIG["timeout"])

            text = page.inner_text("body").lower()
            if any(w in text for w in ("succès", "confirmé", "merci", "enregistré", "thank")):
                success = True
                message = "URL soumise avec succès"
            else:
                alert = page.query_selector(".alert-danger, .error, [class*='error']")
                msg   = alert.inner_text().strip() if alert else text[:300]
                raise RuntimeError(msg)

        finally:
            browser.close()

    return {"order_id": order_id, "success": success, "message": message}


def run():
    if not CONFIG["email"] or not CONFIG["password"]:
        log.error("EMAIL ou PASSWORD manquant dans le fichier .env")
        sys.exit(1)

    log.info("Démarrage bot rocketlinks")
    with sync_playwright() as pw:
        browser, context, page = new_browser(pw)
        try:
            ensure_logged_in(page, context)
            orders = scrape_orders(page)
        finally:
            browser.close()

    result = {
        "bot":          "rocketlinks",
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


def _notify(order_id, published_url, success, message):
    lovable.notify_validation(order_id, published_url, success, message,
                              CONFIG["lovable_endpoint"], CONFIG["import_api_key"])


def serve():
    from core.webhook import create_app
    app  = create_app(CONFIG, run, validate_order, _notify)
    port = int(os.getenv("PORT", 5003))
    log.info(f"Webhook rocketlinks démarré sur le port {port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--validate":
        print(json.dumps(validate_order(sys.argv[2], sys.argv[3]), indent=2))
    elif len(sys.argv) == 2 and sys.argv[1] == "--serve":
        serve()
    else:
        run()
