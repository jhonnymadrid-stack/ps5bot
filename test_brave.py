import undetected_chromedriver as uc
import time

# Cambia esta ruta a donde tienes Brave instalado
BRAVE_PATH = "C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe"

options = uc.ChromeOptions()
options.binary_location = BRAVE_PATH

driver = uc.Chrome(options=options, headless=False)

try:
    driver.get("https://www.tutti.ch/de/search?query=PS5&sorting=newest")
    time.sleep(5)
    print(f"Título de la página: {driver.title}")
    print(f"URL actual: {driver.current_url}")
    
    # Busca anuncios en el HTML
    html = driver.page_source
    if "PS5" in html or "PlayStation" in html:
        print("✅ Página cargada correctamente con resultados")
    else:
        print("❌ No se encontraron resultados en el HTML")
        
    with open("brave_debug.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("HTML guardado en brave_debug.html")
    
    input("Pulsa Enter para cerrar...")
finally:
    driver.quit()