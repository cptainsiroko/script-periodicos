"""Prueba de lectura de titulares de 8 periódicos: portada (principal) y RSS (cotejo).

En GitHub Actions guarda en data/AAAA-MM-DD/; en local, en local/AAAA-MM-DD/ (ignorado por git).
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

MEDIOS = [
    {"medio": "El País", "dominio": "elpais.com", "portada": "https://elpais.com",
     "rss": "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada"},
    {"medio": "El Mundo", "dominio": "elmundo.es", "portada": "https://www.elmundo.es",
     "rss": "https://e00-elmundo.uecdn.es/elmundo/rss/portada.xml"},
    {"medio": "ABC", "dominio": "abc.es", "portada": "https://www.abc.es",
     "rss": "https://www.abc.es/rss/2.0/portada/"},
    {"medio": "elDiario.es", "dominio": "eldiario.es", "portada": "https://www.eldiario.es",
     "rss": "https://www.eldiario.es/rss/"},
    {"medio": "OKDiario", "dominio": "okdiario.com", "portada": "https://okdiario.com",
     "rss": "https://okdiario.com/feed"},
    {"medio": "Europa Press", "dominio": "europapress.es", "portada": "https://www.europapress.es",
     "rss": "https://www.europapress.es/rss/rss.aspx"},
    {"medio": "RTVE", "dominio": "rtve.es", "portada": "https://www.rtve.es/noticias",
     "rss": None},  # sin feed vivo (el conocido está parado desde 2022)
    {"medio": "20minutos", "dominio": "20minutos.es", "portada": "https://www.20minutos.es",
     "rss": "https://www.20minutos.es/rss/"},
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
    "Accept-Language": "es-ES,es;q=0.9",
}
TIMEOUT = 20
MIN_PALABRAS = 5
MIN_TITULARES = 10
MIN_BYTES = 5000  # una respuesta 200 por debajo de esto se considera "casi vacía"
TEXTOS_BLOQUEO = ["just a moment", "cf-chl", "attention required"]

# Secciones que no son noticias (primer tramo de la ruta de la URL)
SECCIONES_EXCLUIDAS = {
    # Patrocinado, compras y corporativo
    "contentfactory", "impulsamos", "edcreativo", "uestudio", "escaparate",
    "ofertas-descuentos", "ofertas-regalos", "okshopping", "comunicacion", "redaccion", "eventos",
    # Opinión y blogs de firma
    "opinion", "blog", "caballodenietzsche", "carnecruda",
    # Vídeo y programas
    "videos", "play", "podcast", "album",
    # Contenido ligero
    "gente", "smoda", "icon", "icon-design", "yodona", "mujer", "estilo-de-vida", "eps", "babelia",
    "gastronomia", "recetas", "mascotas", "animaleros", "bebes", "mamas-papas", "nidos",
    "curiosidades", "horoscopo", "cool", "happyfm", "okclub", "okplanes", "metabolic", "techy",
    "cinemania", "television", "vertele", "gonzoo", "theluxonomist", "viajes", "viajar",
    "elviajero", "favorito", "loteria-navidad", "capaces",
}
PREFIJO_OPINION = "opinión"  # 20minutos marca así la opinión dentro de secciones normales

ATOM = "{http://www.w3.org/2005/Atom}"
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"


def ahora():
    # Hora del sistema; en Actions el workflow fija TZ=Europe/Madrid
    return datetime.now().astimezone()


def limpiar(texto):
    return " ".join((texto or "").split())


def descargar(url):
    """Devuelve (respuesta o None, error o None)."""
    try:
        return requests.get(url, headers=HEADERS, timeout=TIMEOUT), None
    except requests.RequestException as e:
        return None, f"{type(e).__name__}: {e}"


def indicios_bloqueo(resp):
    indicios = []
    if resp.status_code in (403, 429):
        indicios.append(f"HTTP {resp.status_code}")
    cuerpo = resp.content[:200_000].decode("utf-8", errors="ignore").lower()
    for t in TEXTOS_BLOQUEO:
        if t in cuerpo:
            indicios.append(f'texto "{t}"')
    if resp.status_code == 200 and len(resp.content) < MIN_BYTES:
        indicios.append(f"200 casi vacía ({len(resp.content)} bytes)")
    return indicios


def es_del_medio(url, dominio):
    # Solo el dominio principal y www; los subdominios (webs hermanas) quedan fuera
    host = (urlparse(url).hostname or "").lower()
    return host in (dominio, "www." + dominio)


def es_noticia_valida(url, titular, dominio):
    """Filtros comunes a portada y RSS: dominio propio, sección permitida y sin opinión."""
    if not es_del_medio(url, dominio):
        return False
    partes = [p for p in urlparse(url).path.lower().split("/") if p]
    if partes and partes[0] in SECCIONES_EXCLUIDAS:
        return False
    return not titular.lower().startswith(PREFIJO_OPINION)


def parece_noticia(url):
    path = urlparse(url).path
    if re.search(r"\.s?html?$", path):
        return True
    if re.search(r"\d{6,}", path):
        return True
    partes = [p for p in path.split("/") if p]
    return len(partes) >= 3 and partes[-1].count("-") >= 3


def fecha_en_bloque(a):
    """Busca <time datetime> en el enlace o en sus contenedores cercanos."""
    nodo = a
    for _ in range(4):
        if nodo is None:
            break
        t = nodo.find("time", attrs={"datetime": True}) if hasattr(nodo, "find") else None
        if t:
            return t["datetime"]
        if nodo.name == "article":
            break
        nodo = nodo.parent
    return None


def titulares_portada(m, resp, capturado):
    # Si el servidor declara la codificación, se usa; si no, la deduce BeautifulSoup
    declarada = "charset" in resp.headers.get("Content-Type", "").lower()
    soup = BeautifulSoup(resp.content, "html.parser",
                         from_encoding=resp.encoding if declarada else None)
    # Primero los enlaces de titular (más fiables), después el resto de enlaces de artículos
    enlaces = soup.select("h1 a[href], h2 a[href], h3 a[href]")
    enlaces += [a for a in soup.find_all("a", href=True) if a.find(["h1", "h2", "h3"])]
    enlaces += soup.select("article a[href]")

    vistos, salida = set(), []
    for a in enlaces:
        url = urljoin(resp.url, a["href"]).split("#")[0].split("?")[0]
        if url in vistos:
            continue
        texto = limpiar(a.get_text(" "))
        if len(texto.split()) < MIN_PALABRAS:
            continue
        if not es_noticia_valida(url, texto, m["dominio"]) or not parece_noticia(url):
            continue
        vistos.add(url)
        salida.append({"medio": m["medio"], "metodo": "portada", "titular": texto, "url": url,
                       "fecha_publicacion": fecha_en_bloque(a), "capturado_en": capturado})
    return salida


def normalizar_fecha(texto):
    texto = limpiar(texto)
    if not texto:
        return None
    try:
        return parsedate_to_datetime(texto).isoformat()
    except (TypeError, ValueError):
        return texto


def titulares_rss(m, resp, capturado):
    raiz = ET.fromstring(resp.content)
    vistos, salida = set(), []
    for item in raiz.iter("item"):
        titulo = limpiar(item.findtext("title"))
        url = limpiar(item.findtext("link"))
        fecha = item.findtext("pubDate") or item.findtext(DC_DATE)
        if titulo and url and url not in vistos and es_noticia_valida(url, titulo, m["dominio"]):
            vistos.add(url)
            salida.append({"medio": m["medio"], "metodo": "rss", "titular": titulo, "url": url,
                           "fecha_publicacion": normalizar_fecha(fecha), "capturado_en": capturado})
    for entry in raiz.iter(f"{ATOM}entry"):
        titulo = limpiar(entry.findtext(f"{ATOM}title"))
        link = entry.find(f"{ATOM}link")
        url = link.get("href", "") if link is not None else ""
        if titulo and url and url not in vistos and es_noticia_valida(url, titulo, m["dominio"]):
            vistos.add(url)
            salida.append({"medio": m["medio"], "metodo": "rss", "titular": titulo, "url": url,
                           "fecha_publicacion": entry.findtext(f"{ATOM}updated"),
                           "capturado_en": capturado})
    return salida


def probar(m, metodo):
    """Devuelve (titulares, fila del informe)."""
    url = m[metodo]
    fila = {"medio": m["medio"], "metodo": metodo, "http": None, "n": 0,
            "bloqueo": [], "error": None, "sin_fuente": url is None}
    if url is None:
        return [], fila
    resp, error = descargar(url)
    if resp is None:
        fila["error"] = error
        return [], fila
    fila["http"] = resp.status_code
    fila["bloqueo"] = indicios_bloqueo(resp)
    titulares = []
    if resp.status_code == 200:
        capturado = ahora().isoformat(timespec="seconds")
        try:
            extraer = titulares_portada if metodo == "portada" else titulares_rss
            titulares = extraer(m, resp, capturado)
        except ET.ParseError as e:
            fila["error"] = f"XML no válido: {e}"
    fila["n"] = len(titulares)
    return titulares, fila


def funciona(f):
    return f["http"] == 200 and f["n"] >= MIN_TITULARES and not f["bloqueo"]


def informe(filas, fecha, origen):
    lineas = [f"# Informe de titulares {fecha}", "",
              f"Origen: **{origen}**. Criterio de \"funciona\": HTTP 200, "
              f"{MIN_TITULARES} titulares o más y sin indicios de bloqueo.", "",
              "| Medio | Método | Responde | HTTP | Titulares | Indicios de bloqueo | Funciona |",
              "|---|---|---|---|---|---|---|"]
    for f in filas:
        if f["sin_fuente"]:
            lineas.append(f"| {f['medio']} | {f['metodo']} | sin feed | | | | |")
            continue
        responde = "sí" if f["http"] == 200 else "no"
        http = f["http"] if f["http"] is not None else (f["error"] or "")
        bloqueo = ", ".join(f["bloqueo"]) or "ninguno"
        if f["error"] and f["http"] is not None:
            bloqueo += f" ({f['error']})"
        lineas.append(f"| {f['medio']} | {f['metodo']} | {responde} | {http} | {f['n']} | "
                      f"{bloqueo} | {'sí' if funciona(f) else 'no'} |")
    return "\n".join(lineas) + "\n"


def main():
    en_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    fecha = ahora().strftime("%Y-%m-%d")
    carpeta = Path(__file__).resolve().parent / ("data" if en_actions else "local") / fecha
    carpeta.mkdir(parents=True, exist_ok=True)

    todos, filas = [], []
    for m in MEDIOS:
        for metodo in ("portada", "rss"):
            titulares, fila = probar(m, metodo)
            todos += titulares
            filas.append(fila)
            print(f"{m['medio']:<13} {metodo:<8} HTTP {fila['http']}  {fila['n']} titulares  "
                  f"{', '.join(fila['bloqueo']) or ''} {fila['error'] or ''}")

    (carpeta / "titulares.json").write_text(
        json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")
    origen = "GitHub Actions" if en_actions else "local (casa)"
    (carpeta / "informe.md").write_text(informe(filas, fecha, origen), encoding="utf-8")
    print(f"Guardado en {carpeta}")


if __name__ == "__main__":
    main()
