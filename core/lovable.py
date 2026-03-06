"""
core/lovable.py
Envoi des commandes et notifications vers Lovable (Edge Function import-orders).
"""

import json
import logging
import urllib.request
import urllib.error

log = logging.getLogger("core.lovable")


def send_orders(orders: list, endpoint: str, api_key: str):
    """Envoie une liste de payloads normalisés à Lovable, un par un."""
    if not endpoint or not api_key:
        log.warning("LOVABLE_ENDPOINT ou IMPORT_API_KEY manquant — envoi ignoré")
        return

    for payload in orders:
        _post(payload, endpoint, api_key, label=f"order #{payload.get('order_id')}")


def notify_validation(order_id: str, published_url: str, success: bool, message: str,
                      endpoint: str, api_key: str):
    """Notifie Lovable du résultat d'une validation (succès ou erreur)."""
    payload = {
        "order_id":      order_id,
        "published_url": published_url,
        "status":        "completed" if success else "error",
        "message":       message,
    }
    _post(payload, endpoint, api_key, label=f"validation #{order_id}")


def notify_cookies_expired(provider: str, endpoint: str, api_key: str):
    """Notifie Lovable que les cookies d'un provider sont expirés."""
    payload = {
        "provider": provider,
        "status":   "cookies_expired",
        "message":  f"Les cookies de connexion {provider} sont expirés. Veuillez les mettre à jour.",
    }
    _post(payload, endpoint, api_key, label=f"cookies_expired {provider}")


def _post(payload: dict, endpoint: str, api_key: str, label: str = ""):
    if not endpoint or not api_key:
        return
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req  = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = resp.read().decode("utf-8")
        log.info(f"Lovable {label} ← HTTP {resp.status} : {resp_body[:200]}")
    except urllib.error.HTTPError as e:
        log.error(f"Lovable {label} HTTP {e.code} : {e.read().decode('utf-8')[:300]}")
    except Exception as e:
        log.error(f"Lovable {label} échoué : {e}")
