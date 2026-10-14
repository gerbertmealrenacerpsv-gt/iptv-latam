#!/usr/bin/env python3
"""Limpia la lista iptv-org: solo canales en español latinoamericano,
solo streams ESTABLES, y una sola fuente (la mejor) por canal.

Dos etapas:
  1. Prueba rápida: ¿el stream responde?
  2. Prueba de estabilidad: descarga varios fragmentos y mide si llegan más
     rápido de lo que se reproducen, y confirma que el en vivo realmente
     avanza. Descarta los que se congelan o repiten.

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
HILOS = 40            # etapa 1
HILOS_ESTABLE = 30    # etapa 2
MAX_LATENCIA = 6.0    # segundos (etapa 1)
BW_MAX = 3_000_000    # bits/s: variante HLS que se prueba (pensado para móvil)
RATIO_MAX = 0.7       # tiempo de descarga / duración del fragmento
VENTANA = 8           # segundos de prueba para streams directos
KBPS_MIN = 30         # velocidad mínima sostenida en streams directos


# ---------------------------------------------------------------- utilidades
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


def es_hls(url):
    return ".m3u8" in url.split("?")[0].lower()


def get(url, h, timeout=TIMEOUT, **kw):
    return requests.get(url, headers=h, timeout=timeout, **kw)


# ------------------------------------------------------- etapa 1: ¿responde?
def probar(canal):
    """Devuelve latencia en segundos, o None si el stream no sirve."""
    url, h = canal["url"], cabeceras(canal)
    t0 = time.time()
    try:
        r = get(url, h, stream=True)
        if r.status_code != 200:
            return None
        if es_hls(url):
            cuerpo = r.text
            if "#EXTM3U" not in cuerpo:
                return None
            lineas = [l for l in cuerpo.splitlines()
                      if l and not l.startswith("#")]
            if not lineas:
                return None
            sub = urljoin(r.url, lineas[0])
            r2 = get(sub, h, stream=True)
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


# ------------------------------------------------- etapa 2: ¿es estable?
def leer_hls(url, h):
    """Devuelve (texto, url_base) de la lista de medios, eligiendo variante."""
    r = get(url, h)
    if r.status_code != 200 or "#EXTM3U" not in r.text:
        return None
    texto, base = r.text, r.url
    if "#EXT-X-STREAM-INF" in texto:
        lineas = texto.splitlines()
        variantes = []
        for i, l in enumerate(lineas):
            if l.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", l)
                bw = int(m.group(1)) if m else 0
                for sig in lineas[i + 1:]:
                    sig = sig.strip()
                    if sig and not sig.startswith("#"):
                        variantes.append((bw, urljoin(base, sig)))
                        break
        if not variantes:
            return None
        aptas = [v for v in variantes if 0 < v[0] <= BW_MAX]
        elegido = max(aptas)[1] if aptas else min(variantes)[1]
        r = get(elegido, h)
        if r.status_code != 200:
            return None
        texto, base = r.text, r.url
    return texto, base


def parsear_media(texto, base):
    segs, dur, seq = [], None, 0
    fin = "#EXT-X-ENDLIST" in texto
    for l in texto.splitlines():
        l = l.strip()
        if l.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(l.split(":", 1)[1])
            except ValueError:
                pass
        elif l.startswith("#EXTINF:"):
            try:
                dur = float(l.split(":", 1)[1].split(",")[0])
            except ValueError:
                dur = None
        elif l and not l.startswith("#"):
            segs.append((urljoin(base, l), dur or 6.0))
            dur = None
    return segs, seq, fin


def estable_hls(url, h):
    res = leer_hls(url, h)
    if not res:
        return None
    segs, seq, fin = parsear_media(*res)
    if fin or len(segs) < 2:  # VOD o lista sin contenido: no es en vivo
        return None

    ratios = []
    for uri, dur in segs[-3:]:
        t0 = time.time()
        r = get(uri, h, timeout=12, stream=True)
        if r.status_code != 200:
            return None
        total = 0
        for chunk in r.iter_content(65536):
            total += len(chunk)
            if total > 8_000_000:
                break
        r.close()
        if total < 2000:
            return None
        ratios.append((time.time() - t0) / max(dur, 1.0))
    media = sum(ratios) / len(ratios)
    if media > RATIO_MAX or max(ratios) > 1.2:
        return None

    # el en vivo debe avanzar (los canales "pegados" repiten lo mismo)
    time.sleep(min(max(d for _, d in segs) * 1.5, 14))
    res2 = leer_hls(url, h)
    if not res2:
        return None
    segs2, seq2, _ = parsear_media(*res2)
    if not segs2:
        return None
    if seq2 <= seq and segs2[-1][0] == segs[-1][0]:
        return None
    return 1.0 / max(media, 0.01)


def estable_directo(url, h):
    t0 = time.time()
    r = get(url, h, timeout=(6, 6), stream=True)
    if r.status_code != 200:
        return None
    cubos = [0] * VENTANA
    total = 0
    for chunk in r.iter_content(16384):
        dt = time.time() - t0
        if dt >= VENTANA:
            break
        cubos[int(dt)] += len(chunk)
        total += len(chunk)
    r.close()
    if sum(1 for c in cubos if c > 0) < VENTANA - 2:
        return None
    kbps = total / VENTANA / 1024
    return kbps if kbps >= KBPS_MIN else None


def prueba_estable(canal):
    """Devuelve una puntuación (mayor = mejor) o None si es inestable."""
    url, h = canal["url"], cabeceras(canal)
    try:
        return estable_hls(url, h) if es_hls(url) else estable_directo(url, h)
    except Exception:
        return None


# ------------------------------------------------------------------- main
def correr(funcion, items, hilos, etiqueta):
    resultados = []
    with ThreadPoolExecutor(hilos) as ex:
        futs = {ex.submit(funcion, c): c for c in items}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r is not None:
                resultados.append((r, futs[f]))
            if i % 100 == 0:
                print(f"  [{etiqueta}] {i}/{len(items)} probados, "
                      f"{len(resultados)} buenos", flush=True)
    return resultados


def clave_canal(c):
    m = re.search(r'tvg-id="([^"]*)"', c["extinf"])
    clave = m.group(1) if m and m.group(1) else c["extinf"].split(",")[-1]
    return clave.split("@")[0]


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
    print(f"{len(canales)} streams a probar.")

    print("Etapa 1: ¿responden?")
    vivos = [c for _, c in correr(probar, canales, HILOS, "etapa 1")]
    print(f"  {len(vivos)} responden.")

    print("Etapa 2: ¿son estables? (tarda más)")
    estables = correr(prueba_estable, vivos, HILOS_ESTABLE, "etapa 2")
    print(f"  {len(estables)} estables.")

    # una fuente por canal: la de mejor puntuación
    mejores = {}
    for score, c in sorted(estables, key=lambda x: -x[0]):
        mejores.setdefault(clave_canal(c), c)

    with open(SALIDA, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for c in sorted(mejores.values(), key=lambda c: c["extinf"].split(",")[-1]):
            f.write(c["extinf"] + "\n")
            for o in c["opts"]:
                f.write(o + "\n")
            f.write(c["url"] + "\n")
    print(f"Listo: {len(mejores)} canales estables -> {SALIDA}")


if __name__ == "__main__":
    main()
