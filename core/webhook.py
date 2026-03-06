"""
core/webhook.py
Serveur Flask partagé entre tous les bots.
Expose /webhook/validate et /webhook/refresh.
"""

import logging
import threading
import time
from flask import Flask, request, jsonify

log = logging.getLogger("core.webhook")

REFRESH_LOCK    = threading.Lock()
REFRESH_TIMEOUT = 120  # secondes max d'attente si un refresh est déjà en cours


def create_app(config: dict, run_fn, validate_fn, notify_fn) -> Flask:
    """
    Crée l'app Flask avec les deux endpoints.

    Params:
        config       : CONFIG du bot (import_api_key, lovable_endpoint...)
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

    return app
