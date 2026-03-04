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
    "login_url":        "https://app.linksgarden.com/login",   # à ajuster
    "orders_url":       "https://app.linksgarden.com/orders",  # à ajuster
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
    """À adapter selon le formulaire de linksgarden."""
    log.info("Login avec email/password...")
    page.goto(CONFIG["login_url"], wait_until="networkidle")
    # TODO : inspecter les vrais sélecteurs de linksgarden
    page.fill('input[type="email"]', CONFIG["email"])
    page.fill('input[type="password"]', CONFIG["password"])
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=CONFIG["timeout"])


def scrape_orders(page) -> list:
    """À adapter selon la structure de la page commandes de linksgarden."""
    log.info(f"Navigation vers {CONFIG['orders_url']}")
    page.goto(CONFIG["orders_url"], wait_until="networkidle", timeout=CONFIG["timeout"])
    page.screenshot(path="orders_page.png")

    # TODO : implémenter le scraping une fois les sélecteurs connus
    orders = [{"status": "todo", "page_url": page.url}]
    log.warning("Scraping linksgarden non encore implémenté — dump HTML")
    with open("page_dump.html", "w", encoding="utf-8") as f:
        f.write(page.content())
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


def serve():
    app = Flask(__name__)

    @app.route("/webhook/validate", methods=["POST"])
    def webhook_validate():
        auth_header = request.headers.get("Authorization", "")
        if not CONFIG["import_api_key"] or auth_header != f"Bearer {CONFIG['import_api_key']}":
            return jsonify({"error": "Unauthorized"}), 401

        data          = request.get_json(silent=True) or {}
        order_id      = data.get("order_id", "")
        published_url = data.get("published_url", "")

        if not order_id or not published_url:
            return jsonify({"error": "order_id et published_url requis"}), 400

        try:
            result = validate_order(order_id, published_url)
        except NotImplementedError as e:
            return jsonify({"error": str(e)}), 501
        except RuntimeError as e:
            msg = str(e)
            log.error(f"Validation #{order_id} : {msg}")
            lovable.notify_validation(order_id, published_url, False, msg,
                                      CONFIG["lovable_endpoint"], CONFIG["import_api_key"])
            return jsonify({"order_id": order_id, "success": False, "message": msg}), 404
        except Exception as e:
            msg = f"Erreur inattendue : {e}"
            log.error(msg)
            lovable.notify_validation(order_id, published_url, False, msg,
                                      CONFIG["lovable_endpoint"], CONFIG["import_api_key"])
            return jsonify({"order_id": order_id, "success": False, "message": msg}), 500

        status_code = 200 if result["success"] else 422
        return jsonify(result), status_code

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
