"""
core/webhook.py
Serveur Flask partagé entre tous les bots.
Expose /webhook/validate, /webhook/refresh et /webhook/cookies.
"""

import json
import logging
import threading
import time
from flask import Flask, request, jsonify

log = logging.getLogger("core.webhook")

REFRESH_LOCK    = threading.Lock()
REFRESH_TIMEOUT = 120  # secondes max d'attente si un refresh est déjà en cours


def create_app(config: dict, run_fn, validate_fn, notify_fn) -> Flask:
    """
    Crée l'app Flask avec les endpoints.

    Params:
        config       : CONFIG du bot (import_api_key, lovable_endpoint, cookies_file...)
        run_fn       : fonction run() du bot (scraping + envoi Lovable)
        validate_fn  : fonction validate_order(order_id, published_url)
        notify_fn    : fonction notify_lovable_validation(...)
    """
    app = Flask(__name__)

    def check_auth() -> bool:
        auth_header = request.headers.get("Authorization", "")
        return bool(config["import_api_key"]) and auth_header == f"Bearer {config['import_api_key']}"

    @app.route("/webhook/validate", methods=["POST"])
    def webhook_validate():
        if not check_auth():
            return jsonify({"error": "Unauthorized"}), 401

        data          = request.get_json(silent=True) or {}
        order_id      = data.get("order_id", "")
        published_url = data.get("published_url", "")

        if not order_id or not published_url:
            return jsonify({"error": "order_id et published_url requis"}), 400

        try:
            result = validate_fn(order_id, published_url)
        except NotImplementedError as e:
            return jsonify({"error": str(e)}), 501
        except RuntimeError as e:
            msg = str(e)
            log.error(f"Validation #{order_id} : {msg}")
            notify_fn(order_id, published_url, False, msg)
            return jsonify({"order_id": order_id, "success": False, "message": msg}), 404
        except Exception as e:
            msg = f"Erreur inattendue : {e}"
            log.error(msg)
            notify_fn(order_id, published_url, False, msg)
            return jsonify({"order_id": order_id, "success": False, "message": msg}), 500

        # Notifie Lovable du succès de la validation
        if result.get("success"):
            notify_fn(order_id, published_url, True, result.get("message", ""))

        return jsonify(result), 200 if result["success"] else 422

    @app.route("/webhook/refresh", methods=["POST"])
    def webhook_refresh():
        if not check_auth():
            return jsonify({"error": "Unauthorized"}), 401

        def run_with_lock():
            acquired = REFRESH_LOCK.acquire(timeout=REFRESH_TIMEOUT)
            if not acquired:
                log.warning("Refresh ignoré : un refresh tourne déjà depuis plus de 2 minutes")
                return
            try:
                log.info("Refresh démarré")
                run_fn()
            finally:
                REFRESH_LOCK.release()
                log.info("Refresh terminé")

        if REFRESH_LOCK.locked():
            log.info("Refresh déjà en cours — nouvelle demande mise en attente (max 2 min)")

        threading.Thread(target=run_with_lock, daemon=True).start()
        return jsonify({"status": "started"}), 200

    @app.route("/webhook/cookies", methods=["POST"])
    def webhook_cookies():
        """Reçoit des cookies (format browser extension JSON) et les sauvegarde."""
        if not check_auth():
            return jsonify({"error": "Unauthorized"}), 401

        data = request.get_json(silent=True)
        if not data or not isinstance(data, list):
            return jsonify({"error": "Corps JSON attendu : liste de cookies"}), 400

        # Convertit du format browser extension vers Playwright
        pw_cookies = []
        for c in data:
            pc = {
                "name":     c.get("name", ""),
                "value":    c.get("value", ""),
                "domain":   c.get("domain", ""),
                "path":     c.get("path", "/"),
                "httpOnly": c.get("httpOnly", False),
                "secure":   c.get("secure", False),
            }
            if "expirationDate" in c:
                pc["expires"] = c["expirationDate"]
            elif "expires" in c:
                pc["expires"] = c["expires"]
            ss = c.get("sameSite", "None")
            ss_map = {"unspecified": "None", "no_restriction": "None",
                      "lax": "Lax", "strict": "Strict"}
            pc["sameSite"] = ss_map.get(ss.lower(), "None") if isinstance(ss, str) else "None"
            pw_cookies.append(pc)

        cookies_file = config.get("cookies_file", "cookies.json")
        with open(cookies_file, "w") as f:
            json.dump(pw_cookies, f, indent=2)

        log.info(f"{len(pw_cookies)} cookies reçus et sauvegardés dans {cookies_file}")
        return jsonify({"status": "ok", "cookies_count": len(pw_cookies)}), 200

    @app.route("/webhook/status", methods=["GET"])
    def webhook_status():
        """Endpoint de santé / statut."""
        return jsonify({"status": "ok"}), 200

    return app
