import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # visible para ver qué pasa
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        resultados = []
        
        async def handle_response(response):
            if "/graphql" in response.url and response.status == 200:
                try:
                    data = await response.json()
                    edges = (data.get("data", {})
                                .get("searchListingsByQuery", {})
                                .get("listings", {})
                                .get("edges", []))
                    if edges:
                        print(f"✅ GraphQL capturado: {len(edges)} anuncios")
                        for e in edges[:3]:
                            print(e["node"].get("title"), "-", e["node"].get("formattedPrice"))
                        resultados.extend(edges)
                except Exception as ex:
                    print(f"Error parseando: {ex}")
        
        page.on("response", handle_response)
        
        await page.goto("https://www.tutti.ch/de/search?query=TV&sorting=newest", 
                       timeout=60000, wait_until="networkidle")
        await page.wait_for_timeout(5000)
        
        if not resultados:
            print("❌ No se capturó GraphQL")
            # Guarda el HTML para debug
            content = await page.content()
            with open("debug.html", "w", encoding="utf-8") as f:
                f.write(content)
            print("HTML guardado en debug.html")
        
        await browser.close()

asyncio.run(test())