# Cuencas hidroeléctricas del Ecuador: guía de despliegue

> **Cómo quedó en Keyword (2026-10-22).** Este repo solo *recolecta*: GitHub Actions corre `hidro.py` cada hora y
> publica `public/datos.json` en el proyecto de Vercel `cuencas-hidro` (team `adm1n-s-projects`). La página la dibuja
> el módulo **"Cuencas Hidroeléctricas"** del Monitor Energía de Keyword (archivos `cuencas.js` y `api/cuencas.js`
> de ese proyecto), que lee ese JSON vía la variable `CUENCAS_DATA_URL`. El detalle de en qué sitios está el módulo
> vive en el `CLAUDE.md` del monitor, no aquí (este repo es público).
> La plantilla original (`plantilla.html`) ya no está en este repo: se adaptó al diseño del monitor; hay copia en
> `referencias/cuencas-plantilla-original.html` del monitor. Lo de abajo es la guía original del autor, con las
> partes de la página ajustadas.

Tablero de decisión sobre el agua que llega a las principales hidroeléctricas: cuánto llovió y cuánto va a llover en
cada cuenca, cuánta agua entra a cada central, cómo están los embalses y si algún río viene crecido. Coca Codo Sinclair
y el complejo Paute (Mazar, Molino, Sopladora) van primero; el resto de centrales, abajo y más compacto.

Versión de muestra (foto del 22 oct 2026, 17:24): https://claude.ai/artifact/1a95kPDrpEBXsV2NHVSL9T

Todo usa fuentes públicas, sin claves ni costo.

---

## 1. Qué hay en la carpeta

| Archivo | Qué es |
|---|---|
| `hidro.py` | Recolector. Una corrida baja todas las fuentes, cruza los datos por cuenca y genera la página. Solo usa la biblioteca estándar de Python 3.9+ (no hay `requirements.txt`). Tarda ~2,5 min. |
| `provincias.json` | Contorno de las provincias para el mapa (geoBoundaries ADM1, CC BY 4.0, simplificado). |
| `.github/workflows/tablero.yml` | Corre `hidro.py` cada hora y publica `public/` en Vercel. |
| `datos.json` *(se genera)* | Salida de la última corrida (~180 KB). Útil para depurar. |
| `public/datos.json` *(se genera)* | Copia de `datos.json`: **lo único que se sube a Vercel**. |

```bash
python3 hidro.py   # corrida completa: datos.json + public/datos.json
```

## 2. Fuentes y cosas que hay que saber de cada una

| Fuente | Qué se toma | Frecuencia / retraso |
|---|---|---|
| **CELEC Sur** (`generacioncsr.celec.gob.ec:8443/ords/csr/sardomcsr/pointValues?mrid=…`) | Caudal y cota horarios de Coca Codo, Mazar, Molino, Sopladora, Minas San Francisco, Agoyán y Manduriacu. Los `mrid` están en `CENTRALES` dentro de `hidro.py`. | Cada hora, ~15 min de retraso. |
| **Visor INAMHI–GEOGLOWS** (`services.geoglows.org`) | `api/hydropowers/*`: registro diario de entrada desde 2016 y pronóstico de caudal (51 escenarios ECMWF corregidos). GeoServer: polígonos de cuencas, lluvia por satélite PERSIANN-PDIR (horaria y diaria), lluvia pronosticada WRF del INAMHI (pasos de 3 h, ~48 h), FFGS (humedad del suelo, crecida repentina), tramos de río y alertas meteorológicas. | Satélite ~5 h de retraso; pronósticos 1 vez al día. |
| **Red de estaciones INAMHI** (`inamhi.gob.ec/api_visor`) | Pluviómetros y limnígrafos que están transmitiendo dentro de cada cuenca. | Cada hora, ~1 h de retraso. |

Detalles comprobados al construirlo (no los "arreglen" sin revisar):

- **CELEC devuelve un día local por consulta** (01:00 a 00:00 de Ecuador), según el parámetro `fecha`; por eso se piden 8 consultas por serie. Sus horas `loctimestamp` son UTC reales.
- **El certificado TLS del puerto 8443 de CELEC no trae la cadena completa**; `hidro.py` lo pide sin verificar certificado (`tls_laxo`). Si CELEC lo arregla, se puede quitar.
- **El "Observado" del visor es el mismo caudal de CELEC, pero con la hora de Ecuador marcada como UTC.** Se corrige sumando 5 h (verificado en Coca Codo, Mazar, Molino, Minas y Agoyán).
- **El pronóstico del visor para Daule Peripa sale con caudales negativos**: se detecta y no se muestra.
- **Manduriacu**: CELEC a veces deja 0 en las últimas horas cuando no llega el dato. La página lo trata como dato faltante y lo avisa.
- **Ríos**: el ícono del visor (R2…R100) es el peor de 51 escenarios y exagera. Se usa el escenario central contra los máximos anuales 1980-hoy de cada tramo.
- Sin pluviómetros transmitiendo hoy: Minas San Francisco (Jubones) y Due. Paute tiene uno solo.
- **No existe dato público de sedimentos** para Coca Codo.

## 3. Despliegue recomendado: GitHub Actions + Vercel

La página es un solo archivo estático (`public/index.html`, con los datos adentro), así que en Vercel no hace falta
ninguna función ni servidor. El reparto que recomendamos:

- **GitHub Actions** corre `hidro.py` cada hora y publica la carpeta `public/` en Vercel con su CLI.
- **Vercel** solo sirve la página estática.

Por qué no correr el script dentro de Vercel (Vercel Cron + función Python): en el plan Hobby los cron jobs corren
**una vez al día**, y la corrida dura ~2,5 min y hace cientos de pedidos a las fuentes, lo que choca con los límites
de duración de las funciones. En Actions no hay ese problema.

Cosas a tener en cuenta:

- **Minutos de Actions:** cada hora × 2,5 min ≈ 1.800 min/mes. Un repositorio **público** no consume minutos; uno
  **privado** tiene 2.000 min gratis al mes compartidos con todo lo demás de la cuenta. No hay claves en el código
  (todas las fuentes son abiertas), así que puede ser público. Si tiene que ser privado, correr cada 2 horas.
- **Deploys en Vercel:** cada corrida es un deploy de producción (24 al día). Queda dentro del límite diario de deploys
  del plan gratuito, pero revisen el límite vigente de su plan.
- **Plan Hobby de Vercel:** es para uso personal / no comercial. Si esto se entrega a un cliente, probablemente
  corresponda el plan Pro de la cuenta del equipo.

### 3.1 Preparar el proyecto en Vercel (una vez)

1. Crear el proyecto vacío y enlazarlo desde la carpeta del repo: `npx vercel link` (elige equipo y nombre, por
   ejemplo `cuencas-hidro`). Eso crea `.vercel/project.json` con `orgId` y `projectId` (no se sube al repo; está en
   `.gitignore`).
2. Crear un token en Vercel (Account Settings → Tokens).
3. En el repo de GitHub, agregar tres secrets: `VERCEL_TOKEN`, `VERCEL_ORG_ID` (= `orgId`) y `VERCEL_PROJECT_ID`
   (= `projectId`).
4. En el proyecto de Vercel, **Settings → Git**: desconectar el repo si Vercel lo conectó solo. Si no, cada commit
   dispararía un build propio sin `public/` (esa carpeta solo existe dentro de la corrida de Actions).

### 3.2 `.github/workflows/tablero.yml` (ya incluido)

```yaml
name: Tablero de cuencas
on:
  schedule:
    - cron: "10 * * * *"   # cada hora, a los 10 min (CELEC ya publicó la hora anterior)
  workflow_dispatch: {}
concurrency: tablero
jobs:
  correr:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: python3 hidro.py
      - uses: actions/setup-node@v4
        with: { node-version: "20" }
      - name: Publicar en Vercel
        run: npx --yes vercel deploy public --prod --yes --token "$VERCEL_TOKEN"
        env:
          VERCEL_TOKEN: ${{ secrets.VERCEL_TOKEN }}
          VERCEL_ORG_ID: ${{ secrets.VERCEL_ORG_ID }}
          VERCEL_PROJECT_ID: ${{ secrets.VERCEL_PROJECT_ID }}
      - name: Revisar fuentes   # rojo si alguna fuente falló
        run: python3 -c "..."
```

Queda en `https://<proyecto>.vercel.app`; el dominio propio se agrega en Settings → Domains.

### 3.3 Verificar en la primera corrida

- Que **CELEC responda desde los servidores de GitHub** (fuera de Ecuador). Desde Ecuador funciona; si bloquea IPs
  extranjeras, la corrida sigue pero sin caudal ni cota de CELEC (queda anotado en `datos.json → errores`).
- Que `datos.json → errores` esté vacío. Una fuente caída no rompe la corrida: la página muestra lo demás y lista
  qué falló al pie.
- El último paso del workflow ("Revisar fuentes") pone la corrida en rojo si `errores` no está vacío, así GitHub manda correo.

## 4. Recomendaciones

### 4.1 Un solo dato de "cuánto llovió en 24 h": ajustar el satélite con los pluviómetros

Hoy la página muestra dos cifras separadas: el máximo de los pluviómetros y el promedio del satélite sobre la cuenca.
**Recomendamos fusionarlas en una sola estimación de lluvia observada en 24 h por cuenca.** El satélite (PERSIANN-PDIR,
~4 km) cubre toda la cuenca pero es poco preciso, sobre todo en la sierra, donde la lluvia orográfica y de nubes cálidas
le cuesta y suele quedarse corto. El pluviómetro mide bien pero en un punto. La idea es usar los pluviómetros para
corregir el sesgo del satélite y quedarse con la forma espacial del satélite.

Propuesta técnica, de menos a más compleja (lo ideal es que lo definan entre los dos equipos):

1. **Alinear las ventanas primero.** El PERSIANN horario llega con ~5 h de retraso. Para comparar, sumar pluviómetro y
   satélite sobre exactamente las mismas horas UTC, y aparte rellenar las últimas horas con el pluviómetro solo
   (o marcarlas como "sin satélite aún").
2. **Sesgo medio por cuenca (*mean field bias*).** Para cada pluviómetro *i* con dato, tomar la celda del satélite
   que lo contiene: `B = Σ Gᵢ / Σ Sᵢ` (suma de pluviómetros entre suma de satélite en esas celdas). La lluvia ajustada de
   la cuenca es `B × S̄` (promedio del satélite en la cuenca).
   - Calcular `B` con una ventana móvil más larga (72 h o 7 días) que la de 24 h, para que no salte con un solo aguacero.
   - Acotar `B`, por ejemplo entre 0,3 y 3, y exigir un mínimo de pluviómetros y de lluvia (por ejemplo 3 estaciones
     y Σ Gᵢ ≥ 5 mm). Si no se cumple, usar el satélite solo y decirlo en la página.
3. **Sesgo local (mejor, cuando hay varias estaciones).** Calcular el cociente `Gᵢ / Sᵢ` en cada estación e interpolarlo
   en la rejilla del satélite (IDW o kriging), multiplicar celda por celda y promediar en la cuenca. Alternativa: el
   *conditional merging* (kriging de los pluviómetros + la estructura espacial del satélite), que es el método estándar
   en radar-pluviómetro.
4. **Control de calidad de los pluviómetros antes de fusionar:** descartar estaciones que se quedan en 0 cuando
   todas las vecinas llueven, saltos imposibles (más de ~100 mm/h) y huecos de más de 6 h en 24 h. Hoy el tablero ya
   exige dato en las últimas 4 h y al menos 18 de 24 h.
5. **Validar** con *leave-one-out*: sacar un pluviómetro, ajustar con el resto y comparar contra el que se sacó. Así se
   elige entre 2 y 3 con números.
6. **Segunda opinión satelital:** el mismo GeoServer tiene **IMERG Early Run** (NASA). Conviene comparar los dos con el
   paso 5 en cada cuenca antes de elegir cuál ajustar.

Dónde rinde y dónde no: en **Coca Codo** (10 pluviómetros, varios de CELEC y EPMAPS), **Delsitanisagua** (14, UNL) y
**Marcel Laniado** (11, CELEC) se puede hacer bien. En **Paute** hay uno solo y en **Jubones** y **Due** ninguno: ahí el
sesgo tendría que venir de una cuenca vecina o de un factor climatológico fijo calculado con datos históricos del INAMHI.

En el código, el satélite por cuenca se calcula en `correr()` (bloque "Satélite PERSIANN"); los pluviómetros en
`lluvia_estacion()`. Las coordenadas de cada pluviómetro ya están en `datos.json`, así que la fusión puede ir ahí mismo.

### 4.2 Recortar a lo que le importa al cliente

El tablero tiene **mucha información para un solo cliente**: 11 centrales, 7 cuencas, pluviómetros, niveles de río,
ríos GEOGLOWS, FFGS y alertas. Recomendamos conversar con el cliente qué centrales le importan y quitar el resto:

- **Qué centrales se recogen:** la lista `CENTRALES` y el diccionario `CUENCAS` al inicio de `hidro.py`. Quitar una
  entrada la saca de todo (también acorta la corrida).
- **Cuáles van destacadas arriba:** `PRINCIPALES` en `cuencas.js` del monitor y las funciones `panelCCS()` y `panelPaute()`.
  Hoy son Coca Codo y Paute; si el cliente es otro, se cambia ahí.
- **Qué bloques se ven:** los pluviómetros, ríos y alertas ya van plegados ("Pluviómetros, ríos y alertas de la
  cuenca"); se pueden quitar del todo en `masDeLaCuenca()`.

### 4.3 Otras mejoras posibles

- Guardar cada corrida (por ejemplo en Vercel Blob o en una base de datos pequeña) para tener historial propio de lluvia por cuenca; hoy solo
  se ve lo que las fuentes guardan (7 días de CELEC, 30 de los limnígrafos).
- Agregar la energía generada de CELEC: la misma API tiene `…/sardommaz/mazEnerDia`, `ccsEnerDia`, etc.
- Aviso por correo cuando una central pase a "Alerta".

## 5. Cómo se decide el estado de cada central

Explicado también en la página. Se compara con el registro diario propio de cada central desde 2016:

- **Alerta:** entra más agua que el 99 % de los días; o un tramo de río de la cuenca crece en el escenario central
  (periodo de retorno de 2 años o más, `RIO_AÑOS`); o el FFGS marca crecida repentina; o hay alerta INAMHI muy alta
  o de crecida.
- **Atención:** el agua que entra, o el pronóstico de los próximos 7 días, supera el percentil 90; o hay alerta INAMHI alta.
- **Poca agua:** entra menos que el percentil 10 (estiaje).
- **Normal:** nada de lo anterior.

Los umbrales están en la parte de arriba de `hidro.py` (`RIO_AÑOS`, `RIO_MINIMO`, `PLUVIO_HORAS_MIN`) y en la función
`analiza()` de `cuencas.js` del monitor.
