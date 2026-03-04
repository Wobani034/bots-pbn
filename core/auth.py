"""
core/auth.py
Gestion login + cookies commune à tous les bots.
"""

import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("core.auth")


def save_cookies(context, cookies_file: str):
    cookies = context.cookies()
    with open(cookies_file, "w") as f:
        json.dump(cookies, f, indent=2)
    log.info(f"{len(cookies)} cookies sauvegardés")


def load_cookies(context, cookies_file: str) -> bool:
    p = Path(cookies_file)
    if not p.exists() or p.stat().st_size < 10:
        return False
    with open(p) as f:
        cookies = json.load(f)
    context.add_cookies(cookies)
    log.info(f"{len(cookies)} cookies chargés")
    return True


def is_logged_in(page, login_url: str) -> bool:
    return "/login" not in page.url and "connexion" not in page.url.lower() and login_url not in page.url
