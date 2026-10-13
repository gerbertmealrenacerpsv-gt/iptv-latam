#!/usr/bin/env python3
"""Limpia la lista iptv-org: solo canales en español latinoamericano,
solo streams que responden, y una sola fuente (la más rápida) por canal.

Usa la lista de idioma español de iptv-org y descarta los canales de España.
Incluye canales hispanos de EE. UU. y canales internacionales en español.

Uso:
    pip install requests
    python mejorar_iptv.py                # todo el español latino
    python mejorar_iptv.py gt mx sv       # solo estos países
Salida: iptv_latam_limpia.m3u
"""
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import requests

URL = "https://iptv-org.github.io/iptv/languages/spa.m3u"
SALIDA = "iptv_latam_limpia.m3u"
EXCLUIR = {"es", "ad", "gq"}  # España, Andorra, Guinea Ecuatorial
TIMEOUT = 8
HILOS = 40
MAX_LATENCIA = 6.0  # segundos; más lento que esto se descarta


def parsear(texto):
    """Devuelve lista de dicts: extinf, opciones (#EXTVLCOPT...), url."""
    canales, extinf, opts = [], None, []
    for linea in texto.splitlines():
        linea = linea.strip()
        if linea.startswith("#EXTINF"):
            extinf, opts = linea, []
        elif linea.startswith("#EXTVLCOPT") and extinf:
            opts.append(linea)
        elif linea and not linea.startswith("#") and extinf:
            canales.append({"extinf": extinf, "opts": opts, "url": linea})
            extinf, opts = None, []
    return canales


def pais(canal):
    m = re.search(r'tvg-id="[^"]*\.([a-z]{2})(?:@[^"]*)?"', canal["extinf"])
    return m.group(1) if m else None


def cabeceras(canal):
    h = {"User-Agent": "Mozilla/5.0"}
    for o in canal["opts"]:
        if "http-referrer=" in o:
            h["Referer"] = o.split("=", 1)[1]
        elif "http-user-agent=" in o:
            h["User-Agent"] = o.split("=", 1)[1]
    return h


def probar(canal):
    """Devuelve latencia en segundos, o None si el stream no sirve."""
    url, h = canal["url"], cabeceras(canal)
    t0 = time.time()
    try:
        r = requests.get(url, headers=h, timeout=TIMEOUT, stream=True)
        if r.status_code != 200:
            return None
        if ".m3u8" in url.split("?")[0].lower():
            cuerpo = r.text
            if "#EXTM3U" not in cuerpo:
                return None
            # buscar el primer segmento o sublista y confirmar que responde
            lineas = [l for l in cuerpo.splitlines()
                      if l and not l.startswith("#")]
            if not lineas:
                return None
            sub = urljoin(url, lineas[0])
            r2 = requests.get(sub, headers=h, timeout=TIMEOUT, stream=True)
            if r2.status_code != 200:
                return None
            next(r2.iter_content(2048), None)
        else:
            if not next(r.iter_content(2048), None):
                return None
        lat = time.time() - t0
        return lat if lat <= MAX_LATENCIA else None
    except Exception:
        return None


def main():
    solo = {p.lower() for p in sys.argv[1:]}
    print("Descargando lista de canales en español...")
    texto = requests.get(URL, timeout=60).text
    canales = []
    for c in parsear(texto):
        p = pais(c)
        if solo:
            if p in solo:
                canales.append(c)
        elif p not in EXCLUIR:
            canales.append(c)
    print(f"{len(canales)} streams a probar...")

    resultados = []
    with ThreadPoolExecutor(HILOS) as ex:
        futs = {ex.submit(probar, c): c for c in canales}
        for i, f in enumerate(as_completed(futs), 1):
            lat = f.result()
            if lat is not None:
                resultados.append((lat, futs[f]))
            if i % 100 == 0:
                print(f"  {i}/{len(canales)} probados, {len(resultados)} vivos")

    # una fuente por canal (la más rápida)
    mejores = {}
    for lat, c in sorted(resultados, key=lambda x: x[0]):
        m = re.search(r'tvg-id="([^"]*)"', c["extinf"])
        clave = (m.group(1) if m and m.group(1) else c["extinf"].split(",")[-1])
        clave = clave.split("@")[0]
        mejores.setdefault(clave, c)

    with open(SALIDA, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for c in sorted(mejores.values(), key=lambda c: c["extinf"].split(",")[-1]):
            f.write(c["extinf"] + "\n")
            for o in c["opts"]:
                f.write(o + "\n")
            f.write(c["url"] + "\n")
    print(f"Listo: {len(mejores)} canales funcionando -> {SALIDA}")


if __name__ == "__main__":
    main()
