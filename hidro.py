"""Lluvia, ríos y embalses de las cuencas de las principales hidroeléctricas del Ecuador.

Una corrida = recoger todas las fuentes públicas y dejar `datos.json` (copia en `public/` para publicarla en Vercel).
La página la dibuja el módulo "Cuencas" del Monitor Energía de Keyword. Solo biblioteca estándar. Nada se inventa:
cada cifra lleva su fuente y su hora.

Fuentes (todas públicas, sin clave):
- CELEC Sur (generacioncsr.celec.gob.ec:8443/ords/csr): caudal y cota horarios de cada central (los mismos datos de
  sus "Gráficas de Producción").
- Visor INAMHI / GEOGLOWS (services.geoglows.org):
  * api/hydropowers: caudal observado de entrada a la central, registro diario desde 2016 y pronóstico de 15 días
    (51 escenarios ECMWF, corregidos con lo observado).
  * geoserver ecuador:<cuenca>: polígonos de las cuencas de aporte.
  * WRF del INAMHI (lluvia pronosticada cada 3 h) y PERSIANN-PDIR (lluvia estimada por satélite, cada hora y diaria).
  * FFGS: humedad del suelo y riesgo de crecida repentina por subcuenca.
  * Ríos GEOGLOWS: tramos con crecida pronosticada; se usa el escenario central contra los máximos anuales 1980-hoy
    del tramo (el ícono del visor es el peor de 51 escenarios y exagera).
  * Alertas meteorológicas del INAMHI.
- INAMHI api_visor (inamhi.gob.ec/info): pluviómetros y limnígrafos (nivel de río) que están transmitiendo.
"""

import csv
import gzip
import io
import json
import math
import os
import re
import struct
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "monitoreo-hidro (tablero público de cuencas)"}
GEO = "https://services.geoglows.org"
CELEC = "https://generacioncsr.celec.gob.ec:8443/ords/csr/sardomcsr/pointValues"
INAMHI = "https://inamhi.gob.ec/api_visor"
ECUADOR = timezone(timedelta(hours=-5))
CAJA = (-80.2, -4.4, -77.2, 0.4)  # oeste, sur, este, norte: todas las cuencas

# Reglas (van explicadas en el tablero)
RIO_AÑOS = 2  # tramo con crecida >= la que en la historia se ve 1 vez cada 2 años (escenario central)
RIO_MINIMO = 1.0  # m3/s: tramos casi secos no cuentan
RIO_CANDIDATOS = 25  # tramos por cuenca que se revisan a fondo
PLUVIO_HORAS_MIN = 18  # un pluviómetro cuenta si tiene al menos 18 de las últimas 24 horas

CUENCAS = {
    "coca": "Coca Codo Sinclair",
    "paute": "Complejo Paute (Mazar, Molino, Sopladora)",
    "jubones": "Minas San Francisco",
    "agoyan": "Agoyán - San Francisco",
    "delsintanisagua": "Delsitanisagua",
    "marcel_laniado": "Marcel Laniado (Daule Peripa)",
    "due": "Due",
}

# Potencia: capa "hidroeléctricas mayores a 50 MW" del visor INAMHI. celec_*: códigos de sus gráficas de producción.
CENTRALES = [
    dict(id="ccs", nombre="Coca Codo Sinclair", mw=1500, cuenca="coca", geoglows="Coca Codo Sinclair",
         caudal=100037, cota=100540, embalse="Embalse compensador", rio="Río Coca (Quijos + Salado)"),
    dict(id="mazar", nombre="Mazar", mw=183, cuenca="paute", geoglows="Mazar",
         caudal=30538, cota=30031, embalse="Embalse Mazar", rio="Río Paute"),
    dict(id="molino", nombre="Paute-Molino", mw=1075, cuenca="paute", geoglows="Amaluza",
         caudal=24811, cota=24019, embalse="Embalse Amaluza", rio="Río Paute"),
    dict(id="sopladora", nombre="Sopladora", mw=487, cuenca="paute", geoglows=None,
         caudal=90537, cota=90919, embalse=None, rio="Río Paute (agua que sale de Molino)"),
    dict(id="msf", nombre="Minas San Francisco", mw=270, cuenca="jubones", geoglows="Minas San Francisco",
         caudal=650538, cota=650919, embalse="Embalse Minas", rio="Río Jubones"),
    dict(id="agoyan", nombre="Agoyán", mw=160, cuenca="agoyan", geoglows="Agoyán",
         caudal=140537, cota=140031, embalse="Embalse Agoyán", rio="Río Pastaza"),
    dict(id="pisayambo", nombre="Pucará (embalse Pisayambo)", mw=73, cuenca=None, geoglows="Pisayambo",
         caudal=None, cota=None, embalse=None, rio="Río Pisayambo"),
    dict(id="delsi", nombre="Delsitanisagua", mw=180, cuenca="delsintanisagua", geoglows="Delsitanisagua",
         caudal=None, cota=None, embalse=None, rio="Río Zamora"),
    dict(id="laniado", nombre="Marcel Laniado", mw=213, cuenca="marcel_laniado", geoglows="Daule Peripa",
         caudal=None, cota=None, embalse="Embalse Daule Peripa", rio="Río Daule"),
    dict(id="manduriacu", nombre="Manduriacu", mw=63.4, cuenca=None, geoglows=None,
         caudal=110537, cota=110031, embalse="Embalse Manduriacu", rio="Río Guayllabamba"),
    dict(id="due", nombre="Due", mw=64.3, cuenca="due", geoglows=None, caudal=None, cota=None, embalse=None,
         rio="Río Due"),
]


def pedir(url, datos=None, timeout=90, tls_laxo=False):
    cab = dict(UA)
    if datos is not None:
        datos = json.dumps(datos).encode()
        cab["Content-Type"] = "application/json"
    contexto = None
    if tls_laxo:  # el certificado del puerto 8443 de CELEC no trae la cadena completa
        import ssl
        contexto = ssl.create_default_context()
        contexto.check_hostname = False
        contexto.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(urllib.request.Request(url, data=datos, headers=cab), timeout=timeout,
                                context=contexto) as r:
        return r.read()


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def fecha(texto):
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


# ---------------------------------------------------------------- geometría


def poligonos(geojson):
    salida = []
    for f in geojson["features"]:
        g = f["geometry"]
        salida += g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    return salida


def dentro(x, y, polys):
    for p in polys:
        c = False
        for anillo in p:
            for (x1, y1), (x2, y2) in zip(anillo, anillo[1:] + anillo[:1]):
                if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                    c = not c
        if c:
            return True
    return False


def caja(polys):
    xs = [x for p in polys for a in p for x, _ in a]
    ys = [y for p in polys for a in p for _, y in a]
    return min(xs), min(ys), max(xs), max(ys)


def puntos_de_cuenca(polys, paso=0.02):
    o, s, e, n = caja(polys)
    return [(o + (j + 0.5) * paso, s + (i + 0.5) * paso)
            for i in range(int((n - s) / paso) + 1) for j in range(int((e - o) / paso) + 1)
            if dentro(o + (j + 0.5) * paso, s + (i + 0.5) * paso, polys)]


def simplificar(polys, tol=0.004):
    """Polígonos livianos para el mapa del tablero (Douglas-Peucker, solo anillo exterior)."""
    def dp(pts):
        if len(pts) < 3:
            return pts
        (x1, y1), (x2, y2) = pts[0], pts[-1]
        dx, dy = x2 - x1, y2 - y1
        largo = math.hypot(dx, dy) or 1e-12
        k, dmax = 0, -1
        for i, (x, y) in enumerate(pts[1:-1], 1):
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / largo
            if d > dmax:
                k, dmax = i, d
        if dmax <= tol:
            return [pts[0], pts[-1]]
        return dp(pts[:k + 1])[:-1] + dp(pts[k:])
    salida = []
    for p in polys:
        anillo = p[0]
        mitad = len(anillo) // 2  # anillo cerrado: se simplifica en dos mitades
        a = dp(anillo[:mitad + 1])[:-1] + dp(anillo[mitad:])
        if len(a) >= 4:
            salida.append([[round(x, 3), round(y, 3)] for x, y in a])
    return salida


# ---------------------------------------------------------------- rejillas de GeoServer (WCS en texto)


class Rejilla:
    def __init__(self, x0, y0, dx, dy, filas):
        self.x0, self.y0, self.dx, self.dy, self.filas = x0, y0, dx, dy, filas

    def valor(self, lon, lat):
        i, j = int((lat - self.y0) / self.dy), int((lon - self.x0) / self.dx)
        if 0 <= i < len(self.filas) and 0 <= j < len(self.filas[0]):
            v = self.filas[i][j]
            return None if v is None or v < 0 else v


def fechas_de(espacio, capa):
    xml = pedir(f"{GEO}/geoserver/{espacio}/wms?service=WMS&version=1.1.1&request=GetCapabilities", timeout=120).decode()
    trozo = xml[xml.index(f"<Name>{capa}</Name>"):]
    return [f for f in re.search(r'<Extent name="time"[^>]*>([^<]+)', trozo).group(1).strip().split(",") if f]


def cuadricula(espacio, capa, cuando):
    o, s, e, n = CAJA
    texto = pedir(
        f"{GEO}/geoserver/{espacio}/wcs?service=WCS&version=2.0.1&request=GetCoverage&coverageId={espacio}__{capa}"
        f"&format=text/plain&subset=Long({o},{e})&subset=Lat({s},{n})&subset=time(%22{cuando}%22)", timeout=180
    ).decode()
    par = lambda k: float(re.search(rf'{k}", ([-\d.E]+)', texto).group(1))
    filas = [[float(v) for v in linea.split()] for linea in texto.split("Band 0:")[1].strip().splitlines()]
    return Rejilla(par("elt_0_2"), par("elt_1_2"), par("elt_0_0"), par("elt_1_1"), filas)


def promedio(rejilla, puntos):
    vals = [v for v in (rejilla.valor(x, y) for x, y in puntos) if v is not None]
    return (sum(vals) / len(vals), max(vals)) if vals else (None, None)


# ---------------------------------------------------------------- teselas MVT (lector mínimo, como en lluvias.py)


def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def _campos(b):
    i = 0
    while i < len(b):
        k, i = _varint(b, i)
        f, t = k >> 3, k & 7
        if t == 0:
            v, i = _varint(b, i)
        elif t == 2:
            n, i = _varint(b, i)
            v = b[i:i + n]
            i += n
        elif t == 1:
            v = b[i:i + 8]
            i += 8
        elif t == 5:
            v = b[i:i + 4]
            i += 4
        else:
            raise ValueError(f"tipo protobuf {t}")
        yield f, t, v


def _valor(b):
    for f, _, v in _campos(b):
        if f == 1:
            return v.decode()
        if f == 2:
            return struct.unpack("<f", v)[0]
        if f == 3:
            return struct.unpack("<d", v)[0]
        if f in (4, 5):
            return v
        if f == 6:
            return (v >> 1) ^ -(v & 1)
        if f == 7:
            return bool(v)


def puntos_de_tesela(datos, z, x, y):
    if datos[:2] == b"\x1f\x8b":
        datos = gzip.decompress(datos)
    salida = []
    for f, _, capa in _campos(datos):
        if f != 3:
            continue
        claves, valores, objetos, ext = [], [], [], 4096
        for g, _, w in _campos(capa):
            if g == 3:
                claves.append(w.decode())
            elif g == 4:
                valores.append(_valor(w))
            elif g == 2:
                objetos.append(w)
            elif g == 5:
                ext = w
        for obj in objetos:
            etiquetas, geom = b"", []
            for g, _, w in _campos(obj):
                if g == 2:
                    etiquetas = w
                elif g == 4:
                    geom = w
            nums, i = [], 0
            while i < len(etiquetas):
                v, i = _varint(etiquetas, i)
                nums.append(v)
            props = {claves[nums[k]]: valores[nums[k + 1]] for k in range(0, len(nums), 2)}
            g3, i = [], 0
            while i < len(geom) and len(g3) < 3:
                v, i = _varint(geom, i)
                g3.append(v)
            if len(g3) == 3:
                px, py = (g3[1] >> 1) ^ -(g3[1] & 1), (g3[2] >> 1) ^ -(g3[2] & 1)
                n = 2 ** z
                props["lon"] = (x + px / ext) / n * 360 - 180
                props["lat"] = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + py / ext) / n))))
            salida.append(props)
    return salida


TESELAS = [(8, 15), (9, 15), (8, 16), (9, 16)]  # Ecuador en zoom 5
NIVELES = {"R0": 0, "R2": 2, "R5": 5, "R10": 10, "R25": 25, "R50": 50, "R100": 100}


# ---------------------------------------------------------------- fuentes


def celec(mrid, desde, hasta):
    """Serie horaria de CELEC Sur: [(hora UTC, valor)]. Sus marcas de tiempo son UTC (último dato ~15 min atrás)."""
    q = urllib.parse.urlencode({
        "mrid": mrid, "fechaInicio": desde.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "fechaFin": hasta.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "fecha": desde.astimezone(ECUADOR).strftime("%d/%m/%Y %H:%M:%S")})
    items = json.loads(pedir(f"{CELEC}?{q}", timeout=60, tls_laxo=True))["items"]
    return sorted((it["loctimestamp"][:16] + "Z", round(it["valueedit"], 2)) for it in items
                  if it["valueedit"] is not None)


def celec_semana(mrid, ahora):
    """Últimos 8 días locales. La API devuelve un día de Ecuador (01:00 a 00:00) por consulta, como su página."""
    hoy = ahora.astimezone(ECUADOR).date()
    serie = []
    for d in range(7, -1, -1):
        dia = hoy - timedelta(days=d)
        a = datetime(dia.year, dia.month, dia.day, 1, tzinfo=ECUADOR)
        serie += celec(mrid, a, a + timedelta(days=1))
    return sorted(dict(serie).items())


def geoglows_central(nombre, hoy):
    q = urllib.parse.quote(nombre)
    obs = json.loads(pedir(f"{GEO}/api/hydropowers/get-observed-data-plot?hydropower={q}&width=800&height=400",
                           timeout=120).decode().replace("NaN", "null"))["hs"]["data"][0]
    diario = [(x[:10], y) for x, y in zip(obs["x"], obs["y"]) if y is not None]
    fp = json.loads(pedir(f"{GEO}/api/hydropowers/get-forecast-plot?hydropower={q}&width=800&height=400&date={hoy}",
                          timeout=120).decode().replace("NaN", "null"))["fp"]["data"]
    por = {t["name"].replace("<br>", "").strip(): t for t in fp}
    # El visor marca como UTC lo que es hora de Ecuador: su "Observado" es el caudal de CELEC 5 h antes
    # (comprobado con Coca Codo, Mazar, Molino, Minas y Agoyán). Se pasa a UTC de verdad.
    utc = lambda x: (datetime.fromisoformat(x[:19]) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%MZ")
    media = por.get("Pronóstico Media") or {}
    p25, p75 = por.get("25%") or {}, por.get("75%") or {}
    pron = [(utc(x), round(m, 1), round(a, 1) if a is not None else None, round(b, 1) if b is not None else None)
            for x, m, a, b in zip(media.get("x", []), media.get("y", []), p25.get("y", []), p75.get("y", []))
            if m is not None]
    observado = [(utc(x), round(y, 1)) for x, y in zip(por.get("Observado", {}).get("x", []),
                                                             por.get("Observado", {}).get("y", [])) if y is not None]
    valores = sorted(v for _, v in diario)
    pct = lambda p: round(valores[min(len(valores) - 1, int(p / 100 * len(valores)))], 1)
    # El mismo mes en los años anteriores: cuánto suele entrar en esta época.
    mes = hoy[5:7]
    del_mes = sorted(v for d, v in diario if d[5:7] == mes and d[:4] != hoy[:4])
    return {
        "desde": diario[0][0] if diario else None,
        "diario": [(d, round(v, 1)) for d, v in diario[-60:]],
        "percentiles": {"p10": pct(10), "p50": pct(50), "p90": pct(90), "p99": pct(99), "max": round(valores[-1], 1),
                        "fecha_max": max(diario, key=lambda t: t[1])[0]} if valores else None,
        "mes_mediana": round(del_mes[len(del_mes) // 2], 1) if del_mes else None,
        "observado_3h": observado,
        "pronostico": pron,
        # Escenario medio negativo = la corrección con lo observado se rompió (pasa con Daule Peripa).
        "pronostico_valido": bool(pron) and min(m for _, m, _, _ in pron) >= 0,
    }


def estaciones():
    return json.loads(pedir(f"{INAMHI}/station_information/estaciones/visores/?id_aplicacion=vs_1h_inh", timeout=120))


def lluvia_estacion(e, ahora):
    d = json.loads(pedir(f"{INAMHI}/station_data_automaticas/get_precipitation/", {"id_estacion": e["id_estacion"]},
                         timeout=60))
    serie = next((s for s in d.get("series", []) if "PRECIP" in s["name"].upper()), None)
    if not serie:
        return None
    horas = {fecha(p["date"]): p["value"] for p in serie["data"] if p["value"] is not None}
    ult = max(horas, default=None)
    h24 = [v for t, v in horas.items() if t > ahora - timedelta(hours=24)]
    if not ult or ult < ahora - timedelta(hours=4) or len(h24) < PLUVIO_HORAS_MIN:
        return None
    inicio = (ahora - timedelta(hours=48)).replace(minute=0, second=0, microsecond=0)
    return {
        "nombre": e["punto_obs"].strip(), "codigo": e["codigo"], "propietario": e["propietario"],
        "lat": float(e["latitud"]), "lon": float(e["longitud"]), "altitud": float(e["altitud"] or 0),
        "ultimo": iso(ult), "horas_24": len(h24),
        "mm_1h": round(horas.get(ult, 0), 1),
        "mm_6h": round(sum(v for t, v in horas.items() if t > ahora - timedelta(hours=6)), 1),
        "mm_24h": round(sum(h24), 1),
        "mm_72h": round(sum(v for t, v in horas.items() if t > ahora - timedelta(hours=72)), 1),
        "horaria": [round(horas.get(inicio + timedelta(hours=k), 0), 1) for k in range(1, 49)],
        "horaria_desde": iso(inicio + timedelta(hours=1)),
    }


def nivel_estacion(e):
    params = json.loads(pedir(f"{INAMHI}/station_information/estaciones/parametros/?id_estacion={e['id_estacion']}"
                              f"&id_aplicacion=vs_1h_inh", timeout=60))
    nemo = next((q["nemonico"] for p in params if "NIVEL" in p["name_param"].upper() for q in p["params"]
                 if q["simbolo_estadistico"] == "PROM"), None)
    if not nemo:
        return None
    d = json.loads(pedir(f"{INAMHI}/station_data_automaticas/get_data_hour/",
                         {"id_estacion": e["id_estacion"], "table_names": [nemo]}, timeout=60))
    datos = [(p["date"][:16] + "Z", p["value"]) for s in d.get("series", []) for p in s["data"] if p["value"] is not None]
    if len(datos) < 24:
        return None
    vals = [v for _, v in datos]
    return {"nombre": e["punto_obs"].strip(), "codigo": e["codigo"], "propietario": e["propietario"],
            "lat": float(e["latitud"]), "lon": float(e["longitud"]),
            "serie": datos[-24 * 7:], "max_30d": max(vals), "min_30d": min(vals), "ultimo": datos[-1]}


def ffgs(ahora):
    hoy = ahora.date()
    for dia in (hoy, hoy - timedelta(days=1)):
        for hora in ("19", "13", "07", "01"):
            dt = f"{dia} {hora}:00:00"
            cuencas = {}
            for x, y in TESELAS:
                url = f"{GEO}/martin/mvt_ffgs/5/{x}/{y}?datetime={urllib.parse.quote(dt)}"
                for p in puntos_de_tesela(pedir(url), 5, x, y):
                    cuencas[p["basin"]] = p
            if cuencas:
                return list(cuencas.values()), dt
    raise RuntimeError("el FFGS no tiene corridas de hoy ni de ayer")


def alertas_inamhi(ahora):
    lista = json.loads(pedir(f"{GEO}/api/hydroviewer/get-meteo-warnings").decode().replace("NaN", "null"))["result"]
    vigentes, vistos = [], set()
    for a in lista:
        ini = datetime.fromisoformat(a["start"]).replace(tzinfo=ECUADOR)
        fin = datetime.fromisoformat(a["end"]).replace(tzinfo=ECUADOR)
        if fin >= ahora and ini <= ahora + timedelta(hours=24) and a["warning"] not in vistos:
            vistos.add(a["warning"])
            gj = json.loads(pedir(f"{GEO}/api/met-data-explorer/get-meteo-warnings?no={a['warning']}").decode()
                            .replace("NaN", "null"))
            vigentes.append({"no": a["warning"], "tipo": a["type"], "inicio": a["start"], "fin": a["end"], "geo": gj})
    return vigentes


def tramos_de_rio(dia):
    puntos = []
    for x, y in TESELAS:
        puntos += puntos_de_tesela(pedir(f"{GEO}/martin/mvt_hydroviewer_warnings_points/5/{x}/{y}?datetime={dia}"),
                                   5, x, y)
    return puntos


def maximos_anuales(cid):
    texto = pedir(f"{GEO}/api/hydroviewer/get-historical-csv?comid={cid}", timeout=180).decode()
    anual = defaultdict(float)
    for x in csv.DictReader(io.StringIO(texto)):
        if x["value"]:
            anual[x["datetime"][:4]] = max(anual[x["datetime"][:4]], float(x["value"]))
    return sorted(anual.values())


def escenario_central(cid, inicio):
    texto = pedir(f"{GEO}/api/hydroviewer/get-forecast-csv?comid={cid}&date={inicio}", timeout=180).decode()
    dia = defaultdict(list)
    for x in csv.DictReader(io.StringIO(texto)):
        if x["flow_avg"]:
            dia[x["datetime"][:10]].append(float(x["flow_avg"]))
    medias = sorted(dia.items())[:7]  # 7 días: más allá el pronóstico pierde confianza
    return max(((sum(v) / len(v), d) for d, v in medias), default=(0, None))


# ---------------------------------------------------------------- corrida


def correr():
    ahora = datetime.now(timezone.utc)
    hoy = ahora.astimezone(ECUADOR).date().isoformat()
    errores = {}

    def intentar(nombre, f, *a):
        try:
            return f(*a)
        except Exception as e:
            errores[nombre] = f"{type(e).__name__}: {e}"[:200]
            print(f"  ! {nombre}: {errores[nombre]}")

    print("Cuencas...")
    geo_cuencas = {}
    for c in CUENCAS:
        gj = json.loads(pedir(f"{GEO}/geoserver/ecuador/ows?service=WFS&version=2.0.0&request=GetFeature"
                              f"&typeNames=ecuador:{c}&outputFormat=application/json&srsName=EPSG:4326", timeout=120))
        polys = poligonos(gj)
        geo_cuencas[c] = {"polys": polys, "puntos": puntos_de_cuenca(polys)}
        print(f"  {c}: {len(geo_cuencas[c]['puntos'])} celdas")

    # Lluvia por satélite: últimas 24 horas disponibles de PERSIANN horario + 7 días de PERSIANN diario.
    print("Satélite PERSIANN...")
    sat = {}
    horas = intentar("persiann_horas", fechas_de, "satellite_based_precipitation", "persiann_pdir_hourly")
    dias = intentar("persiann_dias", fechas_de, "satellite_based_precipitation", "persiann_pdir_24h")
    if horas:
        ultimas = horas[-24:]
        with ThreadPoolExecutor(6) as ex:
            rejillas = list(ex.map(lambda f: intentar(f"persiann {f}", cuadricula, "satellite_based_precipitation",
                                                      "persiann_pdir_hourly", f), ultimas))
        for c, g in geo_cuencas.items():
            por_hora = [promedio(r, g["puntos"])[0] if r else None for r in rejillas]
            sat[c] = {"mm_24h": round(sum(v for v in por_hora if v), 1), "horas": len([v for v in por_hora if v is not None]),
                      "hasta": ultimas[-1][:16] + "Z", "desde": ultimas[0][:16] + "Z",
                      "horaria": [round(v, 2) if v is not None else None for v in por_hora]}
    if dias:
        ult7 = dias[-7:]
        with ThreadPoolExecutor(4) as ex:
            rejillas = list(ex.map(lambda f: intentar(f"persiann dia {f}", cuadricula, "satellite_based_precipitation",
                                                      "persiann_pdir_24h", f), ult7))
        for c, g in geo_cuencas.items():
            sat.setdefault(c, {})["diaria"] = [(f[:10], round(promedio(r, g["puntos"])[0] or 0, 1) if r else None)
                                               for f, r in zip(ult7, rejillas)]

    # Lluvia pronosticada por el INAMHI (WRF, pasos de 3 h).
    print("Pronóstico WRF...")
    wrf = {}
    pasos = intentar("wrf_fechas", fechas_de, "wrf", "wrf_precipitation")
    if pasos:
        futuros = [f for f in pasos if ahora < fecha(f) <= ahora + timedelta(hours=48)]
        with ThreadPoolExecutor(6) as ex:
            rejillas = list(ex.map(lambda f: intentar(f"wrf {f}", cuadricula, "wrf", "wrf_precipitation", f), futuros))
        for c, g in geo_cuencas.items():
            serie = [(f[:16] + "Z",) + tuple(round(v, 2) if v is not None else None for v in promedio(r, g["puntos"]))
                     for f, r in zip(futuros, rejillas) if r]
            s24 = [m for f, m, _ in serie if m is not None and fecha(f) <= ahora + timedelta(hours=24)]
            wrf[c] = {"mm_24h": round(sum(s24), 1), "mm_48h": round(sum(m for _, m, _ in serie if m), 1),
                      "max_celda_24h": round(max((x for f, _, x in serie if x is not None and
                                                  fecha(f) <= ahora + timedelta(hours=24)), default=0), 1),
                      "pasos": serie, "hasta": serie[-1][0] if serie else None}

    # Pluviómetros y limnígrafos del INAMHI que transmiten y están dentro de cada cuenca.
    print("Estaciones INAMHI...")
    lista = intentar("inamhi_estaciones", estaciones) or []
    vivas = [e for e in lista if e["id_estado_transmision"] == 1 and e["latitud"] and e["longitud"]]
    por_cuenca = {c: [e for e in vivas if dentro(float(e["longitud"]), float(e["latitud"]), g["polys"])]
                  for c, g in geo_cuencas.items()}
    pluvios, niveles = defaultdict(list), defaultdict(list)
    trabajos = [(c, e) for c, l in por_cuenca.items() for e in l]
    with ThreadPoolExecutor(8) as ex:
        res_p = list(ex.map(lambda t: intentar(f"pluvio {t[1]['codigo']}", lluvia_estacion, t[1], ahora)
                            if t[1]["categoria"] != "HIDROLOGICA" else None, trabajos))
        res_n = list(ex.map(lambda t: intentar(f"nivel {t[1]['codigo']}", nivel_estacion, t[1])
                            if t[1]["categoria"] != "METEOROLOGICA" else None, trabajos))
    vistos = set()
    for (c, e), p, n in zip(trabajos, res_p, res_n):
        if p and (c, p["codigo"]) not in vistos:  # el INAMHI repite estaciones con dos ids
            vistos.add((c, p["codigo"]))
            pluvios[c].append(p)
        if n and (c, "n" + n["codigo"]) not in vistos:
            vistos.add((c, "n" + n["codigo"]))
            niveles[c].append(n)

    # Suelo y crecida repentina (FFGS).
    print("FFGS...")
    suelo = {}
    r = intentar("ffgs", ffgs, ahora)
    if r:
        sub, corrida = r
        for c, g in geo_cuencas.items():
            dentro_c = [p for p in sub if dentro(p["lon"], p["lat"], g["polys"])]
            asm = [float(p["asm"]) for p in dentro_c if 0 <= float(p.get("asm") or -1) <= 1]
            riesgo = [p for p in dentro_c if float(p.get("ffg") or -1) > 0 and float(p.get("fmap") or -1) >= float(p["ffg"])]
            suelo[c] = {"corrida": corrida, "subcuencas": len(dentro_c),
                        "humedad": round(sum(asm) / len(asm), 2) if asm else None,
                        "crecida_repentina": len(riesgo)}

    # Alertas meteorológicas del INAMHI que tocan la cuenca.
    print("Alertas INAMHI...")
    alertas = defaultdict(list)
    for a in intentar("alertas", alertas_inamhi, ahora) or []:
        for c, g in geo_cuencas.items():
            for f in a["geo"].get("features", []):
                polys = poligonos({"features": [f]})
                if any(dentro(x, y, polys) for x, y in g["puntos"][::7]):
                    alertas[c].append({"no": a["no"], "tipo": a["tipo"], "inicio": a["inicio"], "fin": a["fin"],
                                       "nivel": f["properties"].get("level")})
                    break

    # Ríos: tramos dentro de cada cuenca con crecida pronosticada.
    print("Ríos GEOGLOWS...")
    rios = defaultdict(list)
    tramos = None
    for d in (ahora.date(), ahora.date() - timedelta(days=1)):
        tramos = intentar(f"rios {d}", tramos_de_rio, d.isoformat())
        if tramos:
            inicio_rios = d.isoformat()
            break
    if tramos:
        claves = [f"wd{k:02d}" for k in range(1, 8)]
        candidatos = []
        for c, g in geo_cuencas.items():
            en = {}
            for p in tramos:
                icono = max(NIVELES.get(p.get(k), 0) for k in claves)
                if icono >= 2 and p["comid"] not in en and dentro(p["lon"], p["lat"], g["polys"]):
                    en[p["comid"]] = dict(p, icono=icono)
            candidatos += [(c, p) for p in sorted(en.values(), key=lambda p: -p["icono"])[:RIO_CANDIDATOS]]
        with ThreadPoolExecutor(8) as ex:
            maximos = list(ex.map(lambda t: intentar(f"max {t[1]['comid']}", maximos_anuales, t[1]["comid"]), candidatos))
            centrales = list(ex.map(lambda t: intentar(f"pron {t[1]['comid']}", escenario_central, t[1]["comid"],
                                                       inicio_rios), candidatos))
        for (c, p), anos, cen in zip(candidatos, maximos, centrales):
            if not anos or not cen or anos[len(anos) // 2] < RIO_MINIMO:
                continue
            q, dia = cen
            veces = sum(a >= q for a in anos)
            periodo = (len(anos) + 1) / veces if veces else len(anos) + 1
            rios[c].append({"comid": p["comid"], "lat": round(p["lat"], 4), "lon": round(p["lon"], 4),
                            "icono": p["icono"], "periodo": round(periodo, 1), "caudal": round(q, 1), "dia": dia,
                            "mediana_max_anual": round(anos[len(anos) // 2], 1), "nunca": not veces,
                            "crecida": periodo >= RIO_AÑOS})
        for c in rios:
            rios[c].sort(key=lambda r: -r["periodo"])

    # Centrales: CELEC (caudal y cota horarios) + visor INAMHI (registro diario y pronóstico).
    print("Centrales...")
    centrales = []
    for cen in CENTRALES:
        print(f"  {cen['nombre']}")
        dato = dict(cen)
        if cen["caudal"]:
            dato["celec_caudal"] = intentar(f"celec caudal {cen['id']}", celec_semana, cen["caudal"], ahora)
        if cen["cota"]:
            dato["celec_cota"] = intentar(f"celec cota {cen['id']}", celec_semana, cen["cota"], ahora)
        if cen["geoglows"]:
            dato["visor"] = intentar(f"visor {cen['id']}", geoglows_central, cen["geoglows"], hoy)
            st = intentar(f"punto {cen['id']}", lambda n: json.loads(pedir(
                f"{GEO}/api/hydropowers/get-station?hydropower={urllib.parse.quote(n)}"))["features"][0]["properties"],
                cen["geoglows"])
            if st:
                dato["lat"], dato["lon"] = st["latitude"], st["longitude"]
        centrales.append(dato)

    # Puntos de las centrales sin punto en el visor: capa de hidroeléctricas > 50 MW.
    capa = json.loads(pedir(f"{GEO}/geoserver/ecuador/ows?service=WFS&version=2.0.0&request=GetFeature"
                            f"&typeNames=ecuador:hidroelectricas_mayores_50W&outputFormat=application/json"
                            f"&srsName=EPSG:4326", timeout=120))
    nombres = {"sopladora": "Sopladora", "manduriacu": "Manduriacu", "due": "Due", "molino": "Paute-Molino",
               "ccs": "Coca-Codo Sinclair", "agoyan": "Agoyan", "laniado": "Marcel Laniado"}
    for d in centrales:
        if "lat" not in d and d["id"] in nombres:
            f = next((f for f in capa["features"] if f["properties"]["NombreCent"] == nombres[d["id"]]), None)
            if f:
                d["lat"], d["lon"] = f["properties"]["y"], f["properties"]["x"]
        if d["cuenca"] is None and "lat" in d:  # Pisayambo: ¿en qué cuenca cae?
            d["cuenca_mapa"] = next((c for c, g in geo_cuencas.items() if dentro(d["lon"], d["lat"], g["polys"])), None)

    # Contorno de provincias para el mapa: geoBoundaries ADM1 (CC BY 4.0), simplificado y sin Galápagos.
    provincias = json.load(open(os.path.join(AQUI, "provincias.json")))

    datos = {
        "generado": iso(ahora), "generado_local": ahora.astimezone(ECUADOR).strftime("%d/%m/%Y %H:%M"),
        "reglas": {"rio_años": RIO_AÑOS, "pluvio_horas_min": PLUVIO_HORAS_MIN},
        "cuencas": {c: {"nombre": n, "forma": simplificar(geo_cuencas[c]["polys"]),
                        "area_km2": round(len(geo_cuencas[c]["puntos"]) * (0.02 * 111.32) ** 2 *
                                          math.cos(math.radians(sum(y for _, y in geo_cuencas[c]["puntos"]) /
                                                                len(geo_cuencas[c]["puntos"])))),
                        "satelite": sat.get(c), "wrf": wrf.get(c), "pluviometros": pluvios.get(c, []),
                        "niveles": niveles.get(c, []), "suelo": suelo.get(c), "alertas": alertas.get(c, []),
                        "rios": rios.get(c, []),
                        "estaciones_total": len(por_cuenca.get(c, []))}
                    for c, n in CUENCAS.items()},
        "centrales": centrales,
        "provincias": provincias,
        "errores": errores,
    }
    with open(os.path.join(AQUI, "datos.json"), "w") as f:
        json.dump(datos, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Listo: datos.json ({os.path.getsize(os.path.join(AQUI, 'datos.json')) // 1024} KB), "
          f"{len(errores)} errores")
    return datos


def publicar():
    """Copia datos.json a public/: es lo único que se sube a Vercel. El monitor de energía lo lee desde ahí."""
    os.makedirs(os.path.join(AQUI, "public"), exist_ok=True)
    with open(os.path.join(AQUI, "datos.json")) as f, open(os.path.join(AQUI, "public", "datos.json"), "w") as g:
        g.write(f.read())


if __name__ == "__main__":
    correr()
    publicar()
