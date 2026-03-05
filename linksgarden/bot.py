"""
linksgarden/bot.py
Bot linksgarden.com — Login + scraping des commandes + webhook validation
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
from flask import Flask, request, jsonify

sys.path.insert(0, str(Path(__file__).parent.parent))
from core import auth, lovable

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
CONFIG = {
    "login_url":        "https://app.linksgarden.com/login.php",
    "orders_url":       "https://app.linksgarden.com/publisher-dashboard.php",
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
log = logging.getLogger("linksgarden")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


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
    page.fill('input[name="email"]', CONFIG["email"])
    page.fill('input[name="password"]', CONFIG["password"])
    page.click('button[name="login"]')
    page.wait_for_load_state("networkidle", timeout=CONFIG["timeout"])


def scrape_orders(page) -> list:
    log.info(f"Navigation vers {CONFIG['orders_url']}")
    page.goto(CONFIG["orders_url"], wait_until="networkidle", timeout=CONFIG["timeout"])
    page.screenshot(path="orders_page.png")

    orders = []
    # Headers : Id commande | Référence | Date commande | Votre site | Site client | Type | Rédacteur | Gain | État | Infos
    headers = [th.inner_text().strip() for th in page.query_selector_all("table thead th")]
    rows    = page.query_selector_all("table tbody tr")

    if rows:
        log.info(f"Tableau trouvé : {len(rows)} lignes")
        snapshots = []
        for i, row in enumerate(rows):
            cells = [td.inner_text().strip() for td in row.query_selector_all("td")]
            if not any(cells):
                continue
            links = [
                {"text": a.inner_text().strip(), "href": a.get_attribute("href")}
                for a in row.query_selector_all("a")
            ]
            snapshots.append({"index": i, "cells": cells, "links": links})

        for snap in snapshots:
            order = {"row_index": snap["index"]}
            for j, h in enumerate(headers):
                order[h] = snap["cells"][j] if j < len(snap["cells"]) else ""
            order["links"] = snap["links"]
            orders.append(order)
    else:
        log.info("Aucune commande en cours")

    return orders


def validate_order(order_id: str, published_url: str) -> dict:
    """À adapter selon le flux de validation de linksgarden."""
    log.info(f"Validation commande #{order_id} → {published_url}")
    # TODO : implémenter la validation une fois le back-office inspecté
    raise NotImplementedError("validate_order non encore implémenté pour linksgarden")


def run():
    if not CONFIG["email"] or not CONFIG["password"]:
        log.error("EMAIL ou PASSWORD manquant dans le fichier .env")
        sys.exit(1)

    log.info("Démarrage bot linksgarden")
    with sync_playwright() as pw:
        browser, context, page = new_browser(pw)
        try:
            ensure_logged_in(page, context)
            orders = scrape_orders(page)
        finally:
            browser.close()

    result = {
        "bot":          "linksgarden",
        "scraped_at":   datetime.now(timezone.utc).isoformat(),
        "orders_count": len(orders),
        "orders":       orders,
    }
    with open(CONFIG["output_file"], "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    log.info(f"Résultats sauvegardés dans {CONFIG['output_file']}")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def _notify(order_id, published_url, success, message):
    lovable.notify_validation(order_id, published_url, success, message,
                              CONFIG["lovable_endpoint"], CONFIG["import_api_key"])


def serve():
    from core.webhook import create_app
    app  = create_app(CONFIG, run, validate_order, _notify)
    port = int(os.getenv("PORT", 5002))
    log.info(f"Webhook linksgarden démarré sur le port {port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--validate":
        print(json.dumps(validate_order(sys.argv[2], sys.argv[3]), indent=2))
    elif len(sys.argv) == 2 and sys.argv[1] == "--serve":
        serve()
    else:
        run()
