import asyncio
import os
import re
import json
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events
import requests
import curl_cffi.requests as cf_requests

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
# Utilidades
# ---------------------------------------------------------------------------
seen_ads: set[str] = set()

def ya_visto(id_anuncio: str) -> bool:
    return id_anuncio in seen_ads

def marcar_visto(id_anuncio: str) -> None:
    seen_ads.add(id_anuncio)

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
                    ubicacion = node.get("postcodeInformation", {}).get("locationName", "")
                    link      = f"https://www.tutti.ch/de/vi/{slug}/{lid}" if slug else f"https://www.tutti.ch/de/vi/{lid}"

                    id_anuncio = f"tutti_{lid}"
                    if ya_visto(id_anuncio):
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
# Monitor grupo Telegram
# ---------------------------------------------------------------------------
async def monitor_telegram(group_identifier: str) -> None:
    client = TelegramClient("session", API_ID, API_HASH)
    await client.start()
    print("Monitorizando grupo Telegram...")

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
async def loop_scrapers() -> None:
    while True:
        try:
            await scrape_tutti()
        except Exception as e:
            print(f"Error en loop: {e}")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Esperando 5 minutos...")
        await asyncio.sleep(300)

async def main() -> None:
    print("\U0001f916 Bot PS5 Alert arrancando...")
    await enviar_alerta(
        "\U0001f916 <b>Bot PS5 Alert iniciado</b>\n"
        "Monitorizando Tutti.ch\n"
        "Pro \u2264 550 CHF | Disc \u2264 300 CHF | Slim Digital \u2264 280 CHF | Gen\u00e9rico \u2264 250 CHF"
    )
    GROUP_ID = "-1001280863188"
    await asyncio.gather(
        loop_scrapers(),
        monitor_telegram(GROUP_ID),
    )

if __name__ == "__main__":
    asyncio.run(main())
