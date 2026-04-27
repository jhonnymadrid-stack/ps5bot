import asyncio
import os
import re
import json
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
from telethon import TelegramClient, events
import requests
import curl_cffi.requests as cf_requests
from bs4 import BeautifulSoup

load_dotenv()

API_ID   = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID  = int(os.getenv("CHAT_ID"))

# ---------------------------------------------------------------------------
# Clasificación PS5 — orden: más específico primero
# ---------------------------------------------------------------------------
# Cada tier: (lista de keywords, precio_máximo, etiqueta legible)
PS5_TIERS = [
    # --- Pro ---
    (["ps5 pro", "playstation 5 pro", "playstation5 pro"],
     550, "PS5 Pro"),

    # --- Disc Edition (con lector) ---
    # EN: disc edition / disk edition
    # DE: mit Laufwerk, Slim Disc, Slim Disk, Disc Slim
    (["disc edition", "disk edition",
      "mit laufwerk", "mit disk",
      "slim disc", "slim disk", "disc slim", "disk slim"],
     300, "PS5 Disc Edition"),

    # --- Digital Edition (sin lector) ---
    # EN: slim digital, digital edition, digital slim
    # DE: ohne Laufwerk, ohne Disk, Digital Slim
    (["slim digital", "digital edition", "digital slim",
      "ohne laufwerk", "ohne disk"],
     280, "PS5 Slim Digital"),

    # --- Genérico (cualquier PS5 sin especificar) ---
    (["ps5", "playstation 5", "playstation5"],
     250, "PS5"),
]

# Palabras que indican que el listing es un accesorio/juego, no una consola.
# Si alguna aparece ANTES de la keyword PS5 en el título → se descarta.
_ACCESORIOS = [
    "dualsense", "dualshock", "controller", "mando", "control ",
    "headset", "kopfhörer", "auricular",
    "spiel ", "spiele", "game ", "games", "juego", "juegos",
    "ladestation", "charging", "kühler", "cooling stand",
    "kabel ", "cable ", "cover ", "hülle ", "case ", "skin ",
]

def clasificar_ps5(titulo: str, descripcion: str = "") -> tuple[str | None, int | None]:
    """
    Devuelve (etiqueta, precio_max) según el tier más específico que coincida.
    Descarta listings de accesorios o juegos donde el keyword PS5 actúa solo
    como calificador del artículo principal (ej. 'DualSense PS5').
    """
    titulo_lower = titulo.lower()
    texto_completo = f"{titulo} {descripcion}".lower()

    # 1. Confirma que hay alguna mención PS5
    ps5_keywords = ["ps5", "playstation 5", "playstation5"]
    if not any(k in texto_completo for k in ps5_keywords):
        return None, None

    # 2. Posición del primer keyword PS5 en el TÍTULO
    ps5_pos = min(
        (titulo_lower.find(k) for k in ps5_keywords if k in titulo_lower),
        default=len(titulo_lower),
    )

    # 3. Descarta si un keyword de accesorio aparece ANTES de PS5
    for acc in _ACCESORIOS:
        acc_pos = titulo_lower.find(acc)
        if acc_pos != -1 and acc_pos < ps5_pos:
            return None, None

    # 4. Descarta packs de juegos: "PS5 Spiele..." / "PS5 Game X" sin indicador de consola
    _JUEGOS = ["spiele", "games", "jeux", "giochi"]
    _CONSOLA = ["konsole", "console", "slim", "pro", "disc", "disk", "digital",
                "laufwerk", "edition", "1tb", "825gb"]
    if any(j in titulo_lower for j in _JUEGOS):
        if not any(c in titulo_lower for c in _CONSOLA):
            return None, None

    # 4. Aplica el tier más específico que aparezca en el título
    for keywords, precio_max, etiqueta in PS5_TIERS:
        if any(k in titulo_lower for k in keywords):
            return etiqueta, precio_max

    return None, None

# ---------------------------------------------------------------------------
# Filtro geográfico Tutti — Zürich +20 km (el servidor ignora lat/lng/radius)
# ---------------------------------------------------------------------------
def _en_radio_zurich(postcode_str: str) -> bool:
    """Cubre ~45 min en tren desde Zürich HB (Winterthur, Schaffhausen, Zug, Baden, Frauenfeld)."""
    try:
        plz = int(postcode_str)
    except (ValueError, TypeError):
        return True  # sin código postal → no filtrar
    return (
        8000 <= plz <= 8199 or   # ZH: Zurich ciudad y suburbs cercanos
        8300 <= plz <= 8499 or   # ZH: Kloten, Winterthur (sin Schaffhausen 82xx)
        8500 <= plz <= 8510 or   # TG: Frauenfeld solamente
        8600 <= plz <= 8999 or   # ZH: lago, Uster, Dietikon, Schlieren
        6300 <= plz <= 6349 or   # ZG: Zug, Cham, Baar
        5000 <= plz <= 5116 or   # AG: Aarau, Brugg
        5200 <= plz <= 5246 or   # AG: Windisch
        5300 <= plz <= 5316 or   # AG: Zurzach
        5400 <= plz <= 5470 or   # AG: Baden, Wettingen
        5500 <= plz <= 5620      # AG: Mellingen, Lenzburg, Wohlen
    )

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
_SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_ads.json")
_next_scrape: float = 0.0
_force_scrape: bool = False

def _load_seen() -> set[str]:
    try:
        import json as _json
        with open(_SEEN_FILE) as f:
            return set(_json.load(f))
    except Exception:
        return set()

seen_ads: set[str] = _load_seen()

def ya_visto(id_anuncio: str) -> bool:
    return id_anuncio in seen_ads

def marcar_visto(id_anuncio: str) -> None:
    seen_ads.add(id_anuncio)
    try:
        import json as _json
        with open(_SEEN_FILE, "w") as f:
            _json.dump(list(seen_ads), f)
    except Exception:
        pass

def extraer_precio(texto: str) -> float | None:
    patrones = [
        r"CHF\s*(\d[\d']*(?:[.,]\d+)?)",
        r"(\d[\d']*(?:[.,]\d+)?)\s*CHF",
        r"(\d[\d']*(?:[.,]\d+)?)\s*Fr\.?",
        r"(\d[\d']*(?:[.,]\d+)?)\.?-",
    ]
    for patron in patrones:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace("'", "").replace(",", "."))
            except ValueError:
                continue
    return None

async def enviar_alerta(mensaje: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error enviando alerta: {e}")

# ---------------------------------------------------------------------------
# Scraper Tutti.ch — curl_cffi + __NEXT_DATA__ (sin navegador)
# ---------------------------------------------------------------------------
_tutti_session: cf_requests.Session | None = None

def _get_tutti_session() -> cf_requests.Session:
    global _tutti_session
    if _tutti_session is None:
        _tutti_session = cf_requests.Session(impersonate="chrome124")
    return _tutti_session

def _extraer_edges(html: str) -> list:
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.DOTALL,
    )
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        return (
            data["props"]["pageProps"]["dehydratedState"]
                ["queries"][0]["state"]["data"]["listings"]["edges"]
        )
    except (KeyError, IndexError, json.JSONDecodeError):
        return []

async def scrape_tutti() -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scrapeando Tutti...")
    session = _get_tutti_session()

    # Dos búsquedas cubren todos los títulos posibles
    for busqueda in ["PS5", "PlayStation 5"]:
        try:
            url = f"https://www.tutti.ch/de/q?query={busqueda.replace(' ', '+')}&sorting=newest"
            resp = session.get(url, timeout=20)

            if resp.status_code != 200:
                print(f"  Tutti '{busqueda}': HTTP {resp.status_code}")
                continue

            edges = _extraer_edges(resp.text)
            print(f"  Tutti '{busqueda}': {len(edges)} anuncios")

            for edge in edges:
                try:
                    node      = edge["node"]
                    lid       = node["listingID"]
                    titulo    = node.get("title", "")
                    descripcion = node.get("body", "")
                    precio_str  = node.get("formattedPrice", "")
                    slug      = node.get("seoInformation", {}).get("deSlug", "")
                    pc_info   = node.get("postcodeInformation", {})
                    postcode  = pc_info.get("postcode", "")
                    ubicacion = pc_info.get("locationName", "")
                    link      = f"https://www.tutti.ch/de/vi/{slug}/{lid}" if slug else f"https://www.tutti.ch/de/vi/{lid}"

                    id_anuncio = f"tutti_{lid}"
                    if ya_visto(id_anuncio):
                        continue

                    if not _en_radio_zurich(postcode):
                        marcar_visto(id_anuncio)
                        print(f"  \U0001f4cd SKIP (lejos): {ubicacion} ({postcode})")
                        continue

                    etiqueta, precio_max = clasificar_ps5(titulo, descripcion)
                    if etiqueta is None:
                        continue

                    precio = extraer_precio(precio_str)
                    if precio is None:
                        continue

                    # Marcar siempre para no reprocesar, alertar solo si está bajo el límite
                    marcar_visto(id_anuncio)

                    if 100 <= precio <= precio_max:
                        msg = (
                            f"\U0001f6a8 <b>ALERTA TUTTI.CH — {etiqueta}</b>\n"
                            f"\U0001f4e6 {titulo}\n"
                            f"\U0001f4b0 {precio_str} (max {precio_max} CHF)\n"
                            f"\U0001f4cd {ubicacion}\n"
                            f"\U0001f4dd {descripcion[:150]}\n"
                            f"\U0001f517 {link}"
                        )
                        await enviar_alerta(msg)
                        print(f"  \u2705 ALERTA: [{etiqueta}] {titulo} — {precio_str}")
                    else:
                        print(f"  \u23e9 SKIP (caro): [{etiqueta}] {titulo} — {precio_str} > {precio_max}")

                except Exception:
                    continue

        except Exception as e:
            print(f"  Error en Tutti '{busqueda}': {e}")

# ---------------------------------------------------------------------------
# Scraper Facebook Marketplace
# ---------------------------------------------------------------------------
_ZURICH_LAT  = 47.3769
_ZURICH_LNG  = 8.5417
_TS_2025     = 1735689600  # 2025-01-01 00:00:00 UTC

# Precios mínimos por tier en FB Marketplace (más estrictos que en otros sitios
# porque FB está lleno de scams a precios absurdamente bajos)
_FB_MIN_PRECIO: dict[str, float] = {
    "PS5 Pro":          400.0,
    "PS5 Disc Edition": 150.0,
    "PS5 Slim Digital": 150.0,
    "PS5":              150.0,
}

def _fb_verificar_listing(lid: str, seller_id: str, session, headers: dict) -> tuple[bool, bool]:
    """
    Fetches the seller's marketplace profile page to verify:
    - can_message: True if seller has Messenger enabled, False if email-only scam
    - is_new_account: True if seller registered in 2025 or later (bot)
    Returns (can_message, is_new_account). On error returns (True, False) — let it pass.
    """
    try:
        hdrs = {**headers, "Referer": "https://www.facebook.com/marketplace/"}

        # registration_time is on the seller's profile page, not the listing page
        is_new = False
        can_msg = True
        if seller_id:
            profile_url = f"https://www.facebook.com/marketplace/profile/{seller_id}/"
            r = session.get(profile_url, headers=hdrs, timeout=15)
            if r.status_code == 200:
                t = r.text
                m = re.search(r'"registration_time"\s*:\s*(\d+)', t)
                if m and int(m.group(1)) >= _TS_2025:
                    is_new = True
                if '"can_message_seller":false' in t:
                    can_msg = False

        return can_msg, is_new
    except Exception:
        return True, False


def _fb_extract_listings(text: str) -> list[dict]:
    """Extract FB Marketplace listing objects (GroupCommerceProductItem) from page HTML."""
    results = []
    seen: set[str] = set()
    # FB uses GroupCommerceProductItem as the typename for marketplace listings
    for m in re.finditer(r'\{"__typename":"GroupCommerceProductItem"', text):
        start = m.start()
        depth = 0
        end = start
        for i, ch in enumerate(text[start:], start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        try:
            obj = json.loads(text[start:end])
            # Only keep top-level listing objects (have marketplace_listing_title)
            if "marketplace_listing_title" not in obj:
                continue
            lid = obj.get("id")
            if lid and lid not in seen:
                seen.add(lid)
                results.append(obj)
        except Exception:
            continue
    return results


async def scrape_facebook_marketplace() -> None:
    fb_cookie = os.getenv("FB_COOKIE", "").strip()
    if not fb_cookie:
        print("  [FB] FB_COOKIE no configurado — saltando")
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scrapeando Facebook Marketplace...")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        "Cookie": fb_cookie,
        "Referer": "https://www.facebook.com/marketplace/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }

    session = _get_tutti_session()

    for query in ["ps5", "playstation 5"]:
        url = (
            "https://www.facebook.com/marketplace/zurich/search/"
            f"?query={query.replace(' ', '+')}"
            "&radius=20"
            "&sortBy=creation_time_descend"
            "&price_lower_bound=100"
            "&price_upper_bound=600"
            "&exact=false"
        )
        try:
            resp = session.get(url, headers=headers, timeout=25)
            if resp.status_code != 200:
                print(f"  [FB] '{query}': HTTP {resp.status_code}")
                continue

            listings = _fb_extract_listings(resp.text)
            print(f"  [FB] '{query}': {len(listings)} anuncios")

            for lst in listings:
                try:
                    lid = lst.get("id")
                    if not lid:
                        continue

                    id_anuncio = f"fb_{lid}"
                    if ya_visto(id_anuncio):
                        continue

                    titulo = lst.get("marketplace_listing_title", "")

                    # --- Filtro: ya vendido ---
                    if lst.get("is_sold") or not lst.get("is_live", True):
                        marcar_visto(id_anuncio)
                        continue

                    # --- Filtro: cuenta creada antes de 2025 ---
                    # FB no expone registration_time en resultados de búsqueda;
                    # usamos creation_time del listing como proxy: si la cuenta
                    # es nueva también el listing será reciente y con id alto.
                    seller = lst.get("marketplace_listing_seller") or {}
                    if isinstance(seller, dict):
                        rt = seller.get("registration_time")
                        reg_ts = rt.get("time") if isinstance(rt, dict) else rt
                        if reg_ts is not None and int(reg_ts) >= _TS_2025:
                            marcar_visto(id_anuncio)
                            print(f"  🤖 SKIP (cuenta 2025+): {seller.get('name', '?')} — {titulo[:40]}")
                            continue

                    # --- Filtro: mensajería (can_message_seller si está disponible) ---
                    if lst.get("can_message_seller") is False:
                        marcar_visto(id_anuncio)
                        print(f"  🚫 SKIP (sin mensajería): {titulo[:55]}")
                        continue

                    # --- Precio ---
                    price_info = lst.get("listing_price") or {}
                    try:
                        precio = float(price_info.get("amount") or 0)
                    except (ValueError, TypeError):
                        precio = extraer_precio(str(price_info))
                    if not precio:
                        continue

                    # --- Clasificación PS5 ---
                    etiqueta, precio_max = clasificar_ps5(titulo)
                    if etiqueta is None:
                        continue

                    precio_min = _FB_MIN_PRECIO.get(etiqueta, 100.0)

                    # --- Ciudad ---
                    loc = lst.get("location") or {}
                    city = (loc.get("reverse_geocode") or {}).get("city", "Zürich")

                    link = f"https://www.facebook.com/marketplace/item/{lid}/"

                    if not (precio_min <= precio <= precio_max):
                        marcar_visto(id_anuncio)
                        if precio < precio_min:
                            print(f"  🚫 SKIP (sospechoso <{precio_min:.0f}): [{etiqueta}] {titulo} — CHF {precio:.0f}")
                        else:
                            print(f"  ⏩ SKIP (caro): [{etiqueta}] {titulo} — CHF {precio:.0f} > {precio_max}")
                        continue

                    # --- Verificación individual: mensajería y antigüedad de cuenta ---
                    seller_id = (lst.get("marketplace_listing_seller") or {}).get("id", "")
                    can_msg, is_new_account = _fb_verificar_listing(lid, seller_id, session, headers)
                    if not can_msg:
                        marcar_visto(id_anuncio)
                        print(f"  🚫 SKIP (email-only scam): {titulo[:55]}")
                        continue
                    if is_new_account:
                        marcar_visto(id_anuncio)
                        print(f"  🤖 SKIP (cuenta 2025+): {titulo[:55]}")
                        continue

                    marcar_visto(id_anuncio)
                    msg = (
                        f"🚨 <b>ALERTA FB MARKETPLACE — {etiqueta}</b>\n"
                        f"📦 {titulo}\n"
                        f"💰 CHF {precio:.0f} (max {precio_max} CHF)\n"
                        f"📍 {city}\n"
                        f"🔗 {link}"
                    )
                    await enviar_alerta(msg)
                    print(f"  ✅ ALERTA FB: [{etiqueta}] {titulo} — CHF {precio:.0f}")

                except Exception:
                    continue

            await asyncio.sleep(3)

        except Exception as e:
            print(f"  [FB] Error '{query}': {e}")


# ---------------------------------------------------------------------------
# Scraper Ricardo.ch — RSC payload parsing
# ---------------------------------------------------------------------------
def _extraer_listings_ricardo(html: str) -> list:
    rsc_chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
    full = "".join(rsc_chunks)
    try:
        decoded = json.loads('"' + full + '"')
    except Exception:
        decoded = full

    listings = []
    for m in re.finditer(r'\{"id":"(\d+)","title":', decoded):
        start = m.start()
        depth = 0
        end = start
        for i, c in enumerate(decoded[start:], start):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        try:
            obj = json.loads(decoded[start:end])
            if "hasBuyNow" in obj:
                listings.append(obj)
        except Exception:
            continue
    return listings

async def scrape_ricardo() -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scrapeando Ricardo...")
    session = _get_tutti_session()
    now = datetime.now(timezone.utc)

    for busqueda in ["ps5", "playstation 5"]:
        try:
            url = f"https://www.ricardo.ch/de/s/{busqueda.replace(' ', '%20')}/"
            resp = session.get(url, timeout=20)

            if resp.status_code != 200:
                print(f"  Ricardo '{busqueda}': HTTP {resp.status_code}")
                continue

            listings = _extraer_listings_ricardo(resp.text)
            print(f"  Ricardo '{busqueda}': {len(listings)} anuncios")

            for node in listings:
                try:
                    lid       = node["id"]
                    titulo    = node.get("title", "")
                    has_buynow = node.get("hasBuyNow", False)
                    has_auction = node.get("hasAuction", False)
                    buy_price  = node.get("buyNowPrice")
                    bid_price  = node.get("bidPrice")
                    end_date_str = node.get("endDate", "")
                    shipping   = node.get("shipping", [{}])
                    ciudad     = shipping[0].get("city", "") if shipping else ""

                    id_anuncio = f"ricardo_{lid}"

                    # Construir link
                    slug = re.sub(r"[^a-z0-9]+", "-", titulo.lower()).strip("-")
                    link = f"https://www.ricardo.ch/de/a/{slug}-{lid}/"

                    etiqueta, precio_max = clasificar_ps5(titulo)
                    if etiqueta is None:
                        continue

                    # --- BUY NOW ---
                    if has_buynow and buy_price is not None:
                        if not ya_visto(id_anuncio + "_bn"):
                            marcar_visto(id_anuncio + "_bn")
                            if 100 <= buy_price <= precio_max:
                                msg = (
                                    f"\U0001f6a8 <b>ALERTA RICARDO.CH — {etiqueta} (BUY NOW)</b>\n"
                                    f"\U0001f4e6 {titulo}\n"
                                    f"\U0001f4b0 CHF {buy_price} (max {precio_max} CHF)\n"
                                    f"\U0001f4cd {ciudad}\n"
                                    f"\U0001f517 {link}"
                                )
                                await enviar_alerta(msg)
                                print(f"  \u2705 ALERTA BN: [{etiqueta}] {titulo} — CHF {buy_price}")
                            else:
                                print(f"  \u23e9 SKIP BN (caro): [{etiqueta}] {titulo} — CHF {buy_price} > {precio_max}")

                    # --- SUBASTA: alerta solo si quedan <5h y precio bajo ---
                    if has_auction and bid_price is not None and end_date_str:
                        try:
                            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            horas_restantes = (end_dt - now).total_seconds() / 3600
                        except Exception:
                            horas_restantes = 999

                        alert_key = id_anuncio + "_auc"
                        if not ya_visto(alert_key) and 0 < horas_restantes < 5 and 100 <= bid_price <= precio_max:
                            marcar_visto(alert_key)
                            mins_rest = int((end_dt - now).total_seconds() / 60)
                            msg = (
                                f"\U0001f6a8\U0001f525 <b>SUBASTA RICARDO — {etiqueta} — TERMINA EN {mins_rest} MIN</b>\n"
                                f"\U0001f4e6 {titulo}\n"
                                f"\U0001f4b0 Puja actual: CHF {bid_price} (max {precio_max} CHF)\n"
                                f"\U0001f4cd {ciudad}\n"
                                f"\U0001f517 {link}"
                            )
                            await enviar_alerta(msg)
                            print(f"  \u2705 ALERTA SUBASTA: [{etiqueta}] {titulo} — CHF {bid_price} ({mins_rest} min restantes)")

                except Exception:
                    continue

        except Exception as e:
            print(f"  Error en Ricardo '{busqueda}': {e}")

# ---------------------------------------------------------------------------
# Scraper Anibis.ch — BeautifulSoup
# ---------------------------------------------------------------------------
async def scrape_anibis() -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scrapeando Anibis...")
    session = _get_tutti_session()

    for busqueda in ["ps5", "playstation 5"]:
        try:
            url = f"https://www.anibis.ch/de/q?query={busqueda.replace(' ', '+')}"
            resp = session.get(url, timeout=20)

            if resp.status_code != 200:
                print(f"  Anibis '{busqueda}': HTTP {resp.status_code}")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Cada listing tiene dos <a href="/de/vi/...">:
            # el primero contiene la imagen (texto = nº fotos),
            # el segundo contiene el título real.
            # Agrupamos por href y nos quedamos con el texto más largo.
            hrefs: dict[str, dict] = {}
            for a in soup.find_all("a", href=re.compile(r"^/de/vi/")):
                href = a["href"]
                texto = a.get_text(separator=" ", strip=True)
                if href not in hrefs or len(texto) > len(hrefs[href]["titulo"]):
                    hrefs[href] = {"titulo": texto, "tag": a}

            print(f"  Anibis '{busqueda}': {len(hrefs)} anuncios")

            for href, datos in hrefs.items():
                try:
                    m = re.search(r"/(\d+)(?:[/?#]|$)", href)
                    if not m:
                        continue
                    lid = m.group(1)

                    id_anuncio = f"anibis_{lid}"
                    if ya_visto(id_anuncio):
                        continue

                    titulo = datos["titulo"]
                    if not titulo or len(titulo) < 3:
                        continue

                    # Precio y ubicacion estan en el contenedor padre del tag
                    contenedor = datos["tag"].parent
                    for _ in range(5):
                        texto_cont = contenedor.get_text(separator="|", strip=True)
                        if re.search(r"\d[\d']*\.\-", texto_cont):
                            break
                        contenedor = contenedor.parent

                    texto_cont = contenedor.get_text(separator="|", strip=True)

                    # Precio: formato "640.-" o "1'200.-"
                    precio_str = ""
                    m_precio = re.search(r"(\d[\d']*)\.\-", texto_cont)
                    if m_precio:
                        precio_str = m_precio.group(0)

                    # Ubicacion: texto antes de la fecha (patron "Stadt, NNNN")
                    ubicacion = ""
                    postcode_anibis = ""
                    m_ubi = re.search(r"([^|]+,\s*(\d{4}))", texto_cont)
                    if m_ubi:
                        ubicacion = m_ubi.group(1).strip()
                        postcode_anibis = m_ubi.group(2)

                    link = f"https://www.anibis.ch{href}"

                    if not _en_radio_zurich(postcode_anibis):
                        marcar_visto(id_anuncio)
                        print(f"  \U0001f4cd SKIP (lejos): {ubicacion}")
                        continue

                    etiqueta, precio_max = clasificar_ps5(titulo)
                    if etiqueta is None:
                        continue

                    precio = extraer_precio(precio_str) if precio_str else None
                    if precio is None:
                        continue

                    marcar_visto(id_anuncio)

                    if 100 <= precio <= precio_max:
                        msg = (
                            f"\U0001f6a8 <b>ALERTA ANIBIS.CH — {etiqueta}</b>\n"
                            f"\U0001f4e6 {titulo}\n"
                            f"\U0001f4b0 {precio_str} (max {precio_max} CHF)\n"
                            f"\U0001f4cd {ubicacion}\n"
                            f"\U0001f517 {link}"
                        )
                        await enviar_alerta(msg)
                        print(f"  \u2705 ALERTA: [{etiqueta}] {titulo} — {precio_str}")
                    else:
                        print(f"  \u23e9 SKIP (caro): [{etiqueta}] {titulo} — {precio_str} > {precio_max}")

                except Exception:
                    continue

        except Exception as e:
            print(f"  Error en Anibis '{busqueda}': {e}")

# ---------------------------------------------------------------------------
# Monitor grupo Telegram
# ---------------------------------------------------------------------------
async def monitor_telegram(group_identifier: str) -> None:
    client = TelegramClient("session", API_ID, API_HASH)
    for intento in range(2):
        try:
            await client.start()
            break
        except Exception as e:
            if "database is locked" in str(e) and intento == 0:
                journal = "session.session-journal"
                if os.path.exists(journal):
                    try:
                        os.remove(journal)
                        print("[Telegram] Journal eliminado, reintentando...")
                    except Exception:
                        pass
            else:
                print(f"[Telegram] No se pudo iniciar Telethon: {e}")
                return
    else:
        print("[Telegram] No se pudo iniciar Telethon tras reintentos")
        return
    print("Monitorizando grupo Telegram...")

    @client.on(events.NewMessage(pattern="/status"))
    async def status_handler(event):
        # Solo responder en el grupo monitoreado o en chat privado con el usuario
        is_group = str(event.chat_id) == str(group_identifier)
        is_private = event.is_private
        if not is_group and not is_private:
            return
        restante = max(0, int(_next_scrape - datetime.now().timestamp()))
        mins, secs = divmod(restante, 60)
        await enviar_alerta(
            f"✅ <b>Bot PS5 activo</b>\n"
            f"⏱ Próximo scrape en <b>{mins}m {secs}s</b>"
        )

    @client.on(events.NewMessage())
    async def handler(event):
        try:
            if str(event.chat_id) != str(group_identifier):
                return
            texto = event.message.text or ""
            if not texto:
                return

            etiqueta, precio_max = clasificar_ps5(texto)
            if etiqueta is None:
                return

            precio = extraer_precio(texto)
            keywords_regalo = ["gratis", "regalo", "free", "verschenke", "schenke", "umsonst", "nehmt"]
            es_regalo = any(k in texto.lower() for k in keywords_regalo)

            if es_regalo or (precio is not None and precio <= precio_max):
                msg = (
                    f"\U0001f381 <b>ALERTA GRUPO TELEGRAM — {etiqueta}</b>\n"
                    f"\U0001f4b0 {'GRATIS' if es_regalo else f'{precio} CHF'} (max {precio_max} CHF)\n"
                    f"\U0001f4dd {texto[:300]}"
                )
                await enviar_alerta(msg)
                print(f"  \u2705 ALERTA TELEGRAM: {etiqueta}")
        except Exception:
            pass

    await client.run_until_disconnected()

# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------
async def bot_polling_loop() -> None:
    """Polls the Bot API for /status commands sent to the bot's private chat."""
    import httpx
    offset = 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                    timeout=35,
                )
                for update in resp.json().get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip()
                    if msg.get("chat", {}).get("type") == "private":
                        if text.startswith("/help"):
                            await enviar_alerta(
                                "<b>Comandos disponibles</b>\n\n"
                                "<b>/status</b>\n"
                                "  Estado del bot y tiempo hasta el próximo scrape automático.\n\n"
                                "<b>/scrap</b>\n"
                                "  Lanza un scrape inmediato buscando nuevos anuncios.\n"
                                "  Al terminar te dice cuántos artículos nuevos encontró.\n\n"
                                "<b>/help</b>\n"
                                "  Muestra este mensaje."
                            )
                        elif text.startswith("/status"):
                            restante = max(0, int(_next_scrape - datetime.now().timestamp()))
                            mins, secs = divmod(restante, 60)
                            await enviar_alerta(
                                f"✅ <b>Bot PS5 activo</b>\n"
                                f"⏱ Próximo scrape en <b>{mins}m {secs}s</b>"
                            )
                        elif text.startswith("/scrap"):
                            global _force_scrape
                            _force_scrape = True
                            await enviar_alerta("🔍 Scrape manual iniciado...")
            except Exception as e:
                print(f"[Bot polling] Error: {e}")
                await asyncio.sleep(5)


async def run_all_scrapers() -> None:
    for name, fn in [("Tutti", scrape_tutti), ("Anibis", scrape_anibis),
                     ("Ricardo", scrape_ricardo), ("Facebook", scrape_facebook_marketplace)]:
        try:
            await fn()
        except Exception as e:
            print(f"Error en loop ({name}): {e}")


async def loop_scrapers() -> None:
    global _next_scrape, _force_scrape
    while True:
        await run_all_scrapers()
        _next_scrape = datetime.now().timestamp() + 3600
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Esperando 1 hora...")

        # Wait up to 1 hour, but wake immediately if _force_scrape is set
        deadline = datetime.now().timestamp() + 3600
        while datetime.now().timestamp() < deadline:
            if _force_scrape:
                _force_scrape = False
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Scrape manual forzado")
                antes = len(seen_ads)
                await run_all_scrapers()
                _next_scrape = datetime.now().timestamp() + 3600
                nuevos = len(seen_ads) - antes
                if nuevos > 0:
                    await enviar_alerta(f"✅ Scrape completado — {nuevos} artículo{'s' if nuevos > 1 else ''} nuevo{'s' if nuevos > 1 else ''} encontrado{'s' if nuevos > 1 else ''}.")
                else:
                    await enviar_alerta("✅ Scrape completado — sin artículos nuevos.")
            await asyncio.sleep(5)

async def main() -> None:
    print("🤖 Bot PS5 Alert arrancando...")
    _fb_on = bool(os.getenv("FB_COOKIE", "").strip())
    fuentes = ("Tutti.ch + Anibis.ch + Ricardo.ch + FB Marketplace"
               if _fb_on else "Tutti.ch + Anibis.ch + Ricardo.ch")
    await enviar_alerta(
        f"🤖 <b>Bot PS5 Alert iniciado</b>\n"
        + f"Monitorizando {fuentes}\n"
        + "Pro ≤ 550 CHF | Disc ≤ 300 CHF | Slim Digital ≤ 280 CHF | Genérico ≤ 250 CHF"
    )
    GROUP_ID = "-1001280863188"
    await asyncio.gather(
        loop_scrapers(),
        monitor_telegram(GROUP_ID),
        bot_polling_loop(),
        return_exceptions=True,
    )

if __name__ == "__main__":
    asyncio.run(main())
