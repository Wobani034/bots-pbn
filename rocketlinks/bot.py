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

DOCUMENT_URL_PATTERNS = [
    r"https?://1drv\.ms/\S+",
    r"https?://onedrive\.live\.com/\S+",
    r"https?://docs\.google\.com/\S+",
    r"https?://drive\.google\.com/\S+",
]


def fetch_document_content(context, url: str) -> tuple[str | None, str | None]:
    """
    Tente de récupérer le contenu d'un document partagé (OneDrive, Google Docs).
    Retourne (html, title) ou (None, None) si échec.
    """
    doc_page = None
    try:
        doc_page = context.new_page()
        doc_page.set_default_timeout(30_000)
        doc_page.goto(url, wait_until="networkidle")
        doc_page.wait_for_timeout(8_000)
        html, title = _extract_word_online(doc_page)
        if not html:
            html, title = _extract_google_docs(doc_page)
        return html, title
    except Exception as e:
        log.warning(f"fetch_document_content({url}) échoué : {e}")
        return None, None
    finally:
        if doc_page:
            doc_page.close()


def _extract_word_online(page) -> tuple[str | None, str | None]:
    """
    Extrait le contenu d'un document Word Online (OneDrive).
    Retourne (html, title) ou (None, None) si échec.
    """
    word_frame = next(
        (f for f in page.frames if "wordeditorframe" in f.url), None
    )
    if not word_frame:
        return None, None

    # Scroll pour déclencher le lazy-loading jusqu'à stabilisation
    # Word Online rend le contenu au fur et à mesure du scroll dans le conteneur interne
    prev_count = 0
    for _ in range(20):
        elements = word_frame.query_selector_all(".OutlineElement")
        count = len(elements)
        if count == prev_count and count > 0:
            break
        prev_count = count
        # Scroll le dernier élément visible dans le viewport pour forcer le rendu suivant
        word_frame.evaluate("""() => {
            const els = document.querySelectorAll('.OutlineElement');
            if (els.length) els[els.length - 1].scrollIntoView({behavior: 'instant', block: 'end'});
            // Fallback : scroll tous les conteneurs possibles
            const containers = document.querySelectorAll('#WACViewPanel, .PageContentContainer, .Pages, [class*="Canvas"]');
            containers.forEach(c => c.scrollTop = c.scrollHeight);
            window.scrollTo(0, document.body.scrollHeight);
        }""")
        page.wait_for_timeout(2000)

    elements = word_frame.query_selector_all(".OutlineElement")
    if not elements:
        return None, None

    # Titre = texte du premier OutlineElement avec role='heading' qui a du contenu
    # Fallback : page.title() nettoyé (nom du fichier)
    title = None
    for el in elements:
        is_h = el.evaluate("el => !!el.querySelector(\"[role='heading']\")")
        if is_h:
            t = el.inner_text().strip() or el.evaluate("el => el.textContent.trim()")
            if t:
                title = t
                break
    if not title:
        raw_title = page.title().strip()
        title = re.sub(r"\.(docx?|gdoc|pdf)$", "", raw_title, flags=re.IGNORECASE).strip() or None

    parts              = []
    list_open          = False
    list_ordered       = False
    title_skipped      = False

    for el in elements:
        children_classes = el.evaluate(
            "el => Array.from(el.querySelectorAll('[class]')).map(c => c.className).join(' ')"
        )
        is_list_item = "ListMarkerWrappingSpan" in children_classes
        is_heading   = el.evaluate("el => !!el.querySelector(\"[role='heading']\")")

        # Texte : supprime le marqueur de liste (\uf0b7 bullet Word ou "1." numéroté)
        raw_text = el.inner_text()
        text     = re.sub(r"^[\uf0b7\u2022•]+\s*", "", raw_text).strip()
        text     = re.sub(r"^\d+\.\s+", "", text)
        if not text:
            continue

        # Liens hypertexte
        links = el.evaluate("""el => Array.from(el.querySelectorAll('a')).map(a => ({
            text: a.innerText.trim(),
            href: a.href
        }))""")
        for lnk in links:
            if lnk.get("href") and lnk.get("text"):
                text = text.replace(lnk["text"], f'<a href="{lnk["href"]}">{lnk["text"]}</a>')

        # Skip le titre principal H1 (déjà dans le champ title)
        if is_heading and not title_skipped:
            title_skipped = True
            continue

        if is_list_item:
            # Détecte si liste numérotée via le texte du ListMarker
            marker_text = el.evaluate(
                "el => { const m = el.querySelector('.ListMarker'); return m ? m.innerText.trim() : ''; }"
            )
            is_ordered = bool(re.match(r'^\d+', marker_text))

            # Ferme la liste courante si le type change
            if list_open and list_ordered != is_ordered:
                parts.append("</ol>" if list_ordered else "</ul>")
                list_open = False

            if not list_open:
                parts.append("<ol>" if is_ordered else "<ul>")
                list_open    = True
                list_ordered = is_ordered

            parts.append(f"<li>{text}</li>")
        else:
            if list_open:
                parts.append("</ol>" if list_ordered else "</ul>")
                list_open = False
            parts.append(f"<h2>{text}</h2>" if is_heading else f"<p>{text}</p>")

    if list_open:
        parts.append("</ol>" if list_ordered else "</ul>")

    return ("\n".join(parts) if parts else None), title


def _extract_google_docs(page) -> tuple[str | None, str | None]:
    """Extrait le contenu d'un Google Doc."""
    url = page.url
    if "docs.google.com/document" in url:
        doc_id = re.search(r"/document/d/([^/]+)", url)
        if doc_id:
            export_url = f"https://docs.google.com/document/d/{doc_id.group(1)}/export?format=html"
            try:
                page.goto(export_url, wait_until="networkidle")
                title = page.title()
                return page.inner_html("body"), title
            except Exception:
                pass
    return None, None


AUTO_MESSAGE = (
    "Bonjour,\n\n"
    "Merci pour votre commande. Afin de publier votre article dans les meilleurs délais, "
    "pourriez-vous me l'envoyer directement dans ce message au format HTML "
    "(avec les balises h1, h2, p, listes, liens, etc.) "
    "ou via un lien consultable en ligne (Google Docs, etc.) ?\n\n"
    "Merci !"
)
AUTO_MESSAGE_SIGNATURE = "pourriez-vous me l'envoyer directement dans ce message"

THANK_YOU_MESSAGE = (
    "Bonjour,\n\n"
    "Votre article est bien en ligne, merci de votre confiance !\n\n"
    "Bonne continuation."
)


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


def scrape_messages(page) -> list:
    """Extrait tous les messages de la page de détail d'une offre (déjà chargée)."""
    messages = []
    for el in page.query_selector_all(".message"):
        classes    = el.get_attribute("class") or ""
        sender     = "advertiser" if "advertiserMessage" in classes else "publisher"
        text       = el.inner_text().strip()
        # Extrait la date (format "05/03/2026 | 20:36")
        date_match = re.search(r"(\d{2}/\d{2}/\d{4})\s*\|\s*(\d{2}:\d{2})", text)
        date       = f"{date_match.group(1)} {date_match.group(2)}" if date_match else ""
        # Supprime la ligne de date du corps du message
        body       = re.sub(r"\d{2}/\d{2}/\d{4}\s*\|\s*\d{2}:\d{2}", "", text).strip()
        body       = re.sub(r"^(Vu|Lu)\n?", "", body).strip()
        if body:
            messages.append({"sender": sender, "date": date, "content": body})
    return messages


def send_message(page, order_id: str, message: str) -> bool:
    """Envoie un message à l'annonceur sur une commande RocketLinks."""
    log.info(f"  -> Envoi message automatique sur #{order_id}")
    page.goto(
        f"https://www.rocketlinks.net/offers/{order_id}",
        wait_until="networkidle",
        timeout=CONFIG["timeout"],
    )
    try:
        # Ouvre le formulaire de message
        page.click("a:has-text('Envoyer un message')", timeout=10_000)
        page.wait_for_selector("#ConversationMessageConversationTypeId", state="visible", timeout=10_000)
        page.select_option("#ConversationMessageConversationTypeId", "1")
        page.fill("#ConversationMessageMessage", message)
        page.click("#submitButtonMessage")
        page.wait_for_load_state("networkidle", timeout=CONFIG["timeout"])
        log.info(f"  -> Message envoyé sur #{order_id}")
        return True
    except Exception as e:
        log.warning(f"  -> Impossible d'envoyer le message sur #{order_id} : {e}")
        return False


def scrape_order_detail(page, context, order_id: str, task_type: str) -> dict:
    """Visite la page de détail d'une offre et en extrait le brief + messages."""
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
    brief_el   = page.query_selector(".article-brief")
    brief_text = brief_el.inner_text() if brief_el else ""
    raw_links  = re.findall(r'<a\s+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>', brief_text)
    links_to_add = [{"href": href, "anchor": anchor.strip()} for href, anchor in raw_links]

    # Messages de la discussion
    messages = scrape_messages(page)

    # Contenu article : uniquement dans les messages annonceur après notre message auto
    article_content = None
    article_title   = None
    auto_sent = any(
        AUTO_MESSAGE_SIGNATURE in m["content"]
        for m in messages if m["sender"] == "publisher"
    )

    if task_type == "publish_only":
        if not auto_sent:
            # Pas encore demandé → envoyer le message automatique
            send_message(page, order_id, AUTO_MESSAGE)
            page.goto(url, wait_until="networkidle", timeout=CONFIG["timeout"])
            messages = scrape_messages(page)
            auto_sent = True

        # Cherche un lien de document dans tous les messages annonceur
        for msg in messages:
            if msg["sender"] != "advertiser":
                continue
            for pattern in DOCUMENT_URL_PATTERNS:
                doc_url_match = re.search(pattern, msg["content"])
                if doc_url_match:
                    log.info(f"  -> Lien document trouvé, fetch en cours : {doc_url_match.group()[:60]}")
                    article_content, article_title = fetch_document_content(context, doc_url_match.group())
                    if article_content:
                        log.info(f"  -> Contenu récupéré ({len(article_content)} chars), titre : {article_title}")
                    break
            if article_content:
                break

        # Si pas de lien document : cherche la réponse HTML après notre message auto
        if not article_content:
            after_auto = False
            for msg in messages:
                if msg["sender"] == "publisher" and AUTO_MESSAGE_SIGNATURE in msg["content"]:
                    after_auto = True
                    continue
                if after_auto and msg["sender"] == "advertiser" and msg["content"]:
                    article_content = msg["content"]
                    break

    return {
        "word_count_min":  word_count_min,
        "topic":           topic,
        "deadline_days":   deadline_days,
        "links_to_add":    links_to_add,
        "messages":        messages,
        "article_content": article_content,
        "article_title":   article_title,
        "auto_msg_sent":   auto_sent,
    }


def scrape_orders(page, context) -> list:
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

        detail = scrape_order_detail(page, context, order_id, task_type)
        order.update(detail)

        orders.append(order)
        log.info(f"Commande #{order_id} ({task_type}) — {site_url} — {gain}€ — {len(detail['messages'])} message(s)")

        page.goto(CONFIG["orders_url"], wait_until="networkidle", timeout=CONFIG["timeout"])

    return orders


def compute_status(task_type: str, article_content, messages: list) -> str:
    """
    Détermine le statut métier :
    - to_write          : on rédige + publie
    - waiting_for_client: publish_only, message auto envoyé, article pas encore reçu
    - to_publish        : article reçu du client, prêt à publier
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
    messages        = order.get("messages", [])
    return {
        "provider":        "rocketlinks",
        "order_id":        order["order_id"],
        "site_url":        order.get("site_url", ""),
        "gain":            order.get("gain", 0.0),
        "deadline_days":   order.get("deadline_days"),
        "task_type":       task_type,
        "status":          compute_status(task_type, article_content, messages),
        "topic":           order.get("topic", ""),
        "word_count_min":  order.get("word_count_min"),
        "links_to_add":    order.get("links_to_add", []),
        "article_title":   order.get("article_title"),
        "article_content": article_content,
        "messages":        messages,
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

            guarantee = page.locator('input[name="data[Deal][guarantee]"][type="checkbox"]')
            if guarantee.count() and not guarantee.is_checked():
                guarantee.check()

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

            # Message de remerciement après validation réussie
            if success:
                send_message(page, order_id, THANK_YOU_MESSAGE)

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
            orders = scrape_orders(page, context)
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
