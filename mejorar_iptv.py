#!/usr/bin/env python3
"""Limpia la lista iptv-org: solo canales en español latinoamericano,
solo streams ESTABLES, LIVIANOS y REPRODUCIBLES, y una sola fuente por canal.

Pensado para conexiones de ~5 Mbps y para teléfonos Android.

Dos etapas:
  1. Prueba rápida: ¿el stream responde?
  2. Prueba de calidad:
     - descarga varios fragmentos y mide si llegan más rápido de lo que se
       reproducen (si no, se congela),
     - mide el bitrate (descarta los muy pesados),
     - confirma que el en vivo realmente avanza,
     - abre el fragmento y comprueba que trae VIDEO y AUDIO con códecs que
       el teléfono reproduce (H.264/HEVC y AAC/MP3). Así se descartan los
       canales de "solo audio", "solo video" y "reloj que corre sin imagen".

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
BW_MAX = 2_500_000    # bits/s: variante HLS preferida (liviana)
MAX_BITRATE = 3_000_000  # bits/s: más que esto se descarta (conexión ~5 Mbps)
MAX_KBPS = MAX_BITRATE / 8 / 1024 * 1.3  # tope en streams directos (KB/s)
RATIO_MAX = 0.6       # tiempo de descarga / duración del fragmento (promedio)
RATIO_PICO = 1.0      # ningún fragmento puede tardar más de lo que dura
MAX_SEG_DUR = 12      # fragmentos más largos hacen lento el arranque
FUENTES_POR_CANAL = 2  # alternativas por canal (si una falla, usa la otra)
VENTANA = 8           # segundos de prueba para streams directos
KBPS_MIN = 30         # velocidad mínima sostenida en streams directos

# Tipos de stream MPEG-TS que un teléfono reproduce sin problemas.
# (Se dejan fuera MPEG-2 video y AC-3/E-AC-3/DTS: muchos teléfonos no los
#  decodifican y dan "solo audio" o "solo video".)
VIDEO_OK = {0x1B, 0x24}               # H.264, HEVC
AUDIO_OK = {0x0F, 0x11, 0x03, 0x04}   # AAC (ADTS/LATM), MP3


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


# ------------------------------------- contenido: ¿trae video Y audio?
def _sync(data):
    """Desfase donde empiezan los paquetes MPEG-TS de 188 bytes, o None."""
    for i in range(188):
        if (len(data) > i + 564 and data[i] == 0x47 and data[i + 188] == 0x47
                and data[i + 376] == 0x47 and data[i + 564] == 0x47):
            return i
    return None


def analizar_ts(data):
    """None si `data` no es MPEG-TS. Si lo es, devuelve (hay_video, hay_audio)
    con códecs reproducibles y datos reales."""
    off = _sync(data)
    if off is None:
        return None
    pmt_pid, streams, datos = None, {}, {}
    for k in range((len(data) - off) // 188):
        p = data[off + k * 188: off + (k + 1) * 188]
        if p[0] != 0x47:
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        pusi = bool(p[1] & 0x40)
        afc = (p[3] >> 4) & 3
        i = 4
        if afc & 2:
            i += 1 + p[4]
        if not (afc & 1) or i >= 187:
            continue
        datos[pid] = datos.get(pid, 0) + 1
        try:
            if pid == 0 and pmt_pid is None and pusi:
                t = p[i + 1 + p[i]:]
                if t[0] == 0:
                    n = ((t[1] & 0x0F) << 8) | t[2]
                    j = 8
                    while j + 4 <= min(3 + n - 4, len(t)):
                        prog = (t[j] << 8) | t[j + 1]
                        if prog != 0:
                            pmt_pid = ((t[j + 2] & 0x1F) << 8) | t[j + 3]
                            break
                        j += 4
            elif pid == pmt_pid and pusi and not streams:
                t = p[i + 1 + p[i]:]
                if t[0] == 2:
                    n = ((t[1] & 0x0F) << 8) | t[2]
                    pil = ((t[10] & 0x0F) << 8) | t[11]
                    j, fin = 12 + pil, min(3 + n - 4, len(t))
                    while j + 5 <= fin:
                        esl = ((t[j + 3] & 0x0F) << 8) | t[j + 4]
                        streams[((t[j + 1] & 0x1F) << 8) | t[j + 2]] = t[j]
                        j += 5 + esl
        except IndexError:
            continue
    video = any(st in VIDEO_OK and datos.get(pid, 0) > 0
                for pid, st in streams.items())
    audio = any(st in AUDIO_OK and datos.get(pid, 0) > 0
                for pid, st in streams.items())
    return video, audio


def analizar_mp4(data):
    """Para fragmentos fMP4/CMAF: ¿el inicio declara video y audio comunes?"""
    video = any(t in data for t in (b"avc1", b"avc3", b"hvc1", b"hev1"))
    return video and b"mp4a" in data


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


# ------------------------------------------------- etapa 2: ¿es bueno?
def leer_hls(url, h):
    """Devuelve (texto, url_base, variante_fijada) de la lista de medios.

    Si la lista es maestra (varias calidades) elige la liviana y devuelve su
    URL como `variante_fijada`; si no, `variante_fijada` es None.
    """
    r = get(url, h)
    if r.status_code != 200 or "#EXTM3U" not in r.text:
        return None
    texto, base, fijada, separado = r.text, r.url, None, False
    if "#EXT-X-STREAM-INF" in texto:
        # ¿el audio viene en pistas aparte (varios idiomas)? Entonces el video
        # solo no es un error: el reproductor suma la pista de audio.
        separado = bool(re.search(
            r"#EXT-X-MEDIA:[^\n]*TYPE=AUDIO[^\n]*URI=", texto))
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
        if aptas:
            elegido = max(aptas)[1]
        else:
            sin_bw = [v for v in variantes if v[0] == 0]
            if sin_bw:
                elegido = sin_bw[0][1]
            else:
                menor = min(variantes)
                if menor[0] > MAX_BITRATE:
                    return None  # ni su calidad más baja cabe en tu conexión
                elegido = menor[1]
        r = get(elegido, h)
        if r.status_code != 200:
            return None
        texto, base, fijada = r.text, r.url, elegido
    return texto, base, fijada, separado


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
    texto, base, fijada, separado = res
    segs, seq, fin = parsear_media(texto, base)
    if fin or len(segs) < 2:  # VOD o lista sin contenido: no es en vivo
        return None
    if max(d for _, d in segs) > MAX_SEG_DUR:  # arranque lento
        return None

    llaves = [l for l in texto.splitlines()
              if l.startswith("#EXT-X-KEY") and "METHOD=NONE" not in l]
    if any("SAMPLE-AES" in l for l in llaves):
        return None  # cifrado que los reproductores simples no abren
    cifrado = bool(llaves)  # AES-128: no se puede inspeccionar, se acepta

    elegidos = segs[-3:]
    ratios, bitrates, muestra = [], [], b""
    for n, (uri, dur) in enumerate(elegidos):
        ultimo = n == len(elegidos) - 1
        t0 = time.time()
        r = get(uri, h, timeout=12, stream=True)
        if r.status_code != 200:
            return None
        total = 0
        for chunk in r.iter_content(65536):
            total += len(chunk)
            if ultimo and len(muestra) < 800_000:
                muestra += chunk
            if total > 8_000_000:
                break
        r.close()
        if total < 2000:
            return None
        ratios.append((time.time() - t0) / max(dur, 1.0))
        bitrates.append(total * 8 / max(dur, 1.0))
    media = sum(ratios) / len(ratios)
    if media > RATIO_MAX or max(ratios) > RATIO_PICO:
        return None
    if sum(bitrates) / len(bitrates) > MAX_BITRATE:  # demasiado pesado
        return None

    # ¿trae video Y audio que el teléfono reproduce?
    if not cifrado:
        ts = analizar_ts(muestra)
        if ts is None:  # no es MPEG-TS: probar fMP4/CMAF
            mapa = re.search(r'#EXT-X-MAP:.*URI="([^"]+)"', texto)
            if not mapa:
                return None
            ri = get(urljoin(base, mapa.group(1)), h, timeout=10)
            if ri.status_code != 200 or not analizar_mp4(ri.content):
                return None
        else:
            video, audio = ts
            if not video or not (audio or separado):
                return None

    # el en vivo debe avanzar (los canales "pegados" repiten lo mismo)
    time.sleep(min(max(d for _, d in segs) * 1.5, 14))
    res2 = leer_hls(url, h)
    if not res2:
        return None
    segs2, seq2, _ = parsear_media(res2[0], res2[1])
    if not segs2:
        return None
    if seq2 <= seq and segs2[-1][0] == segs[-1][0]:
        return None

    # Se conserva la URL ORIGINAL (lista maestra): así el reproductor puede
    # cambiar de pista de audio, adaptar la calidad y renovar sus tokens.
    # Fijar una variante rompía eso y hacía que el canal se parara a los minutos.
    return 1.0 / max(media, 0.01), url


def estable_directo(url, h):
    t0 = time.time()
    r = get(url, h, timeout=(6, 6), stream=True)
    if r.status_code != 200:
        return None
    cubos = [0] * VENTANA
    total, muestra = 0, b""
    for chunk in r.iter_content(16384):
        dt = time.time() - t0
        if dt >= VENTANA:
            break
        cubos[int(dt)] += len(chunk)
        total += len(chunk)
        if len(muestra) < 800_000:
            muestra += chunk
    r.close()
    if sum(1 for c in cubos if c > 0) < VENTANA - 2:
        return None
    kbps = total / VENTANA / 1024
    if kbps < KBPS_MIN:
        return None
    mitad = VENTANA // 2
    cola = sum(cubos[mitad:]) / (VENTANA - mitad) / 1024
    if cola > MAX_KBPS:  # demasiado pesado para tu conexión
        return None
    ts = analizar_ts(muestra)
    if ts is not None and not (ts[0] and ts[1]):  # sin video o sin audio útil
        return None
    return kbps, url


def prueba_estable(canal):
    """Devuelve (puntuación, url_final) o None si es inestable o pesado."""
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

    print("Etapa 2: ¿estables, livianos y con video y audio? (tarda más)")
    buenos = correr(prueba_estable, vivos, HILOS_ESTABLE, "etapa 2")
    print(f"  {len(buenos)} pasan.")

    # hasta FUENTES_POR_CANAL fuentes por canal (la mejor primero), para tener
    # una alternativa si la primera falla desde tu ubicación
    por_canal = {}
    for (score, url_final), c in sorted(buenos, key=lambda x: -x[0][0]):
        lista = por_canal.setdefault(clave_canal(c), [])
        if (len(lista) < FUENTES_POR_CANAL
                and all(x["url"] != url_final for x in lista)):
            lista.append(dict(c, url=url_final))
    mejores = [c for lista in por_canal.values() for c in lista]

    with open(SALIDA, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for c in sorted(mejores, key=lambda c: c["extinf"].split(",")[-1]):
            f.write(c["extinf"] + "\n")
            for o in c["opts"]:
                f.write(o + "\n")
            f.write(c["url"] + "\n")
    print(f"Listo: {len(por_canal)} canales ({len(mejores)} fuentes) -> {SALIDA}")


if __name__ == "__main__":
    main()
