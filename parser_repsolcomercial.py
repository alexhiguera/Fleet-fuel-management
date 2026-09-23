"""
Parser para facturas de REPSOL COMERCIAL (DE PRODUCTOS PETROLIFEROS),
emitidas por LLOANA E.S. S.L. "en nombre y por cuenta de" Repsol.

Como en STAR RESSA, la portada (1 pagina) es solo un resumen de totales
por producto SIN matricula; el fichero "(EXTRACTO)"/"(DETALLE)" adjunto
trae el desglose real. Recibe (ruta_cover, ruta_detalle, nombre_archivo),
cualquiera de las dos puede faltar.

Ademas hay dos casos especiales que se detectan por CONTENIDO, no por
nombre de archivo:

  - Facturas "AUTOGAS": GLP para una instalacion fija, medido en M3 (no
    litros), sin matricula -> se excluyen enteras, no son combustible de
    vehiculo.
  - En 2024 se ha visto un unico PDF que combina portada (pagina 0) y
    extracto (paginas siguientes) en el mismo archivo -> se detecta por
    contener "Extracto de consumo cliente" y se trata igual que un par.

El extracto en si tiene DOS variantes de maquetacion segun la fecha,
independientemente de si el nombre de archivo dice "EXTRACTO" o
"DETALLE":

  Layout VIEJO (hasta ~sept-2021), sin agrupar por matricula:
      01/12/2020 8:01
      630 95Efitec        <- "<numero> <Producto>" concatenado
      1,239�
      4,90                <- CANTIDAD (litros)
      6,07�

  Layout NUEVO (oct-2021 en adelante), agrupado por matricula/tarjeta:
      1234ABC                      <- matricula, o "GARAFAS", o codigo de tarjeta
           LLOANA ESTACIONES DE    <- marca fija de inicio de grupo (2 lineas)
      SERVICIO
      04/02/2022
      CR5 50
      E+Diesel
      1,509�
      63,50                       <- CANTIDAD (litros), offset fijo producto+2
      95,82�
      63,50                       <- subtotal del grupo REPETIDO 2 veces mas,
      95,82�                         se ignora porque no sigue a un "Producto"
      63,50
      95,82
"""

import re
from datetime import datetime

import fitz

from modelos import Registro, Incidencia, ResultadoFactura
import productos
import destinos

RE_FECHA_HORA_VIEJO = re.compile(r"^\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}$")
RE_NUMERO_PRODUCTO_VIEJO = re.compile(r"^\d+\s+(\S.*)$")
RE_NUMERO = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2,3}\D*$")
RE_FECHA_SIMPLE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
RE_PLATE = re.compile(r"^\d{4}[A-Z]{3}$")

MARCA_LLOANA_1 = "LLOANA ESTACIONES DE"
MARCA_LLOANA_2 = "SERVICIO"
PRODUCTOS_DETALLE = ("e+diesel", "95efitec")
# El layout nuevo agrupa las lineas "LLOANA ESTACIONES DE" / "SERVICIO" en 2
# lineas SEPARADAS justo despues de la etiqueta de grupo; el layout viejo
# solo menciona "LLOANA ESTACIONES DE SERVICIO" en una unica linea de pie de
# pagina ("Estacion: ..."), asi que buscar las 2 lineas contiguas distingue
# ambos casos sin falsos positivos.
RE_MARCA_GRUPO_NUEVO = re.compile(
    r"\n\s*" + re.escape(MARCA_LLOANA_1) + r"\s*\n" + re.escape(MARCA_LLOANA_2) + r"\s*\n"
)


def _abrir_paginas(ruta):
    try:
        doc = fitz.open(ruta)
    except Exception as exc:
        return None, f"No se pudo abrir el PDF: {exc}"
    if doc.page_count == 0:
        doc.close()
        return None, "El PDF no tiene paginas."
    paginas = [p.get_text() for p in doc]
    doc.close()
    return paginas, None


def _es_autogas(texto: str) -> bool:
    mayus = texto.upper()
    return "AUTOGAS" in mayus and " M3" in mayus


def _extraer_de_portada(texto_cover):
    lineas = [l.strip() for l in texto_cover.split("\n")]
    numero = None
    fecha = None
    for i, l in enumerate(lineas):
        if "Fakturaren" in l and i + 1 < len(lineas):
            numero = lineas[i + 1]
        elif l == "Fecha / Data:" and i + 1 < len(lineas):
            fecha = lineas[i + 1]
    return numero, fecha


def _parse_litros(texto):
    m = re.match(r"^(-?\d{1,3}(?:\.\d{3})*,\d{2,3})", texto)
    if not m:
        return None
    return float(m.group(1).replace(".", "").replace(",", "."))


def _procesar_layout_viejo(texto_detalle, nombre_archivo, fecha_factura, resultado):
    lineas = [l.strip() for l in texto_detalle.split("\n")]
    n = len(lineas)
    interpretados = 0
    matricula = "SIN MATRICULA (LLOANA)"
    for i in range(n):
        if not RE_FECHA_HORA_VIEJO.match(lineas[i]):
            continue
        if i + 4 >= n:
            continue
        m_np = RE_NUMERO_PRODUCTO_VIEJO.match(lineas[i + 1])
        if not m_np:
            continue
        concepto = m_np.group(1).strip()
        cantidad_txt = lineas[i + 3]
        litros = _parse_litros(cantidad_txt)
        if litros is None:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"No se pudo leer la cantidad del registro {lineas[i + 1]!r} (fecha {lineas[i]!r}).",
                )
            )
            continue

        categoria, motivo = productos.clasificar(concepto)
        if categoria == productos.EXCLUIDO:
            resultado.excluidos.append((concepto, motivo, matricula))
            interpretados += 1
            continue
        if categoria == productos.DESCONOCIDO:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tipo de producto",
                    f"Concepto {concepto!r} no esta en el catalogo de productos conocidos.",
                    "Clasificar manualmente y, si procede, anadir al catalogo (productos.py).",
                )
            )
            continue
        if categoria == productos.DIESEL_B100:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tipo de producto",
                    f"Concepto {concepto!r} se ha identificado como Diesel B100, pero la plantilla no "
                    f"tiene una tabla para B100.",
                    "Anadir manualmente en una tabla aparte, tal y como se solicito.",
                )
            )
            continue

        interpretados += 1
        resultado.registros.append(
            Registro(
                referencia=nombre_archivo,
                matricula=matricula,
                fecha_factura=fecha_factura,
                litros=litros,
                producto=categoria,
                concepto_original=concepto,
                fecha_consumo=lineas[i],
                origen_detalle="extracto (layout antiguo, sin matricula)",
            )
        )

    if interpretados > 0:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Matricula",
                "Este extracto usa el formato antiguo de REPSOL COMERCIAL, que no agrupa por matricula: "
                f"todos sus consumos se han registrado bajo {matricula!r}.",
                "No requiere accion: es una limitacion del formato de origen, no un error de lectura.",
            )
        )
    return interpretados


def _procesar_layout_nuevo(texto_detalle, nombre_archivo, fecha_factura, resultado):
    lineas = [l.strip() for l in texto_detalle.split("\n")]
    n = len(lineas)

    inicios_grupo = []
    for i in range(n - 2):
        if lineas[i + 1] == MARCA_LLOANA_1 and lineas[i + 2] == MARCA_LLOANA_2:
            inicios_grupo.append(i)

    interpretados = 0
    for idx_g, i in enumerate(inicios_grupo):
        etiqueta = lineas[i]
        region_fin = inicios_grupo[idx_g + 1] if idx_g + 1 < len(inicios_grupo) else n
        etiqueta_es_plate = destinos.es_matricula(etiqueta)
        destino, etiqueta_destino = (
            (destinos.VEHICULOS, "") if etiqueta_es_plate else destinos.clasificar(etiqueta)
        )

        p = i + 3
        while p < region_fin:
            if lineas[p].lower() not in PRODUCTOS_DETALLE:
                p += 1
                continue

            concepto = lineas[p]
            if p + 2 >= region_fin:
                p += 1
                continue
            cantidad_txt = lineas[p + 2]
            litros = _parse_litros(cantidad_txt)
            if litros is None:
                resultado.incidencias.append(
                    Incidencia(
                        nombre_archivo,
                        "Tabla de consumos",
                        f"No se pudo leer la cantidad del registro {concepto!r} (etiqueta {etiqueta!r}).",
                    )
                )
                p += 1
                continue

            categoria, motivo = productos.clasificar(concepto)
            if categoria == productos.DESCONOCIDO:
                resultado.incidencias.append(
                    Incidencia(
                        nombre_archivo,
                        "Tipo de producto",
                        f"Concepto {concepto!r} (etiqueta {etiqueta!r}) no esta en el catalogo de "
                        f"productos conocidos.",
                        "Clasificar manualmente y, si procede, anadir al catalogo (productos.py).",
                    )
                )
                p += 1
                continue
            if categoria == productos.DIESEL_B100:
                resultado.incidencias.append(
                    Incidencia(
                        nombre_archivo,
                        "Tipo de producto",
                        f"Concepto {concepto!r} (etiqueta {etiqueta!r}) se ha identificado como Diesel "
                        f"B100, pero la plantilla no tiene una tabla para B100.",
                        "Anadir manualmente en una tabla aparte, tal y como se solicito.",
                    )
                )
                p += 1
                continue

            interpretados += 1
            # Lo que va a garrafas/deposito/tarjeta no es una
            # incidencia: se registra en su propia hoja.
            if not etiqueta_es_plate and not destino:
                resultado.incidencias.append(
                    Incidencia(
                        nombre_archivo,
                        "Matricula",
                        f"La etiqueta de grupo {etiqueta!r} no tiene forma de matricula de vehiculo real.",
                        "Confirmar manualmente a que corresponde (tarjeta, centro de coste, etc.).",
                    )
                )

            resultado.registros.append(
                Registro(
                    referencia=nombre_archivo,
                    matricula=etiqueta_destino or etiqueta,
                    fecha_factura=fecha_factura,
                    litros=litros,
                    producto=categoria,
                    destino=destino,
                    destino_detalle=etiqueta_destino,
                    concepto_original=concepto,
                    fecha_consumo="",
                    origen_detalle="extracto (layout nuevo, agrupado por matricula)",
                )
            )
            p += 1

    return interpretados


def parse(ruta_cover, ruta_detalle, nombre_archivo: str) -> ResultadoFactura:
    resultado = ResultadoFactura(archivo=nombre_archivo)

    paginas_cover = None
    if ruta_cover:
        paginas_cover, error = _abrir_paginas(ruta_cover)
        if error:
            resultado.incidencias.append(Incidencia(nombre_archivo, "Archivo", error, "Revisar el archivo."))
            paginas_cover = None

    paginas_detalle = None
    if ruta_detalle:
        paginas_detalle, error = _abrir_paginas(ruta_detalle)
        if error:
            resultado.incidencias.append(Incidencia(nombre_archivo, "Archivo", error, "Revisar el archivo."))
            paginas_detalle = None

    texto_para_autogas = "\n".join((paginas_cover or []) + (paginas_detalle or []))
    if texto_para_autogas.strip() and _es_autogas(texto_para_autogas):
        resultado.excluidos.append(
            ("AUTOGAS", "gas licuado de instalacion fija (m3), no es combustible de vehiculo", "")
        )
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tipo de documento",
                "Factura de AUTOGAS (instalacion fija, medido en M3): no es combustible de vehiculo, se "
                "excluye de la hoja de vehiculos.",
                "No requiere accion.",
            )
        )
        return resultado

    # PDF de 2024 que combina portada + extracto en un unico archivo.
    # "Extracto de consumo cliente" es el pie de pagina de CUALQUIER
    # extracto (tambien de uno que sea, el solo, un archivo de detalle
    # multi-pagina sin portada) -> exigir ADEMAS que la pagina 0 tenga
    # pinta de portada real ("Fakturaren", etiqueta bilingue exclusiva de
    # la portada) para no tratar un extracto normal como si fuera portada.
    if paginas_cover is not None and paginas_detalle is None and len(paginas_cover) > 1:
        texto_resto = "\n".join(paginas_cover[1:])
        if "Fakturaren" in paginas_cover[0] and "Extracto de consumo cliente" in texto_resto:
            paginas_detalle = paginas_cover[1:]
            paginas_cover = [paginas_cover[0]]

    if paginas_cover is not None and len("".join(paginas_cover).strip()) < 20:
        paginas_cover = None
    if paginas_detalle is not None and len("".join(paginas_detalle).strip()) < 20:
        paginas_detalle = None

    if paginas_cover is None and paginas_detalle is None:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Archivo",
                "Ninguno de los archivos de esta factura contiene texto extraible (posible PDF escaneado, "
                "requeriria OCR).",
                "Revisar manualmente o procesar con OCR.",
            )
        )
        return resultado

    if paginas_detalle is None:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Archivo",
                "No se ha encontrado (o no se ha podido leer) el extracto/detalle de esta factura: sin el, "
                "no se puede atribuir litros por matricula (la portada solo trae totales agregados por "
                "producto).",
                "Buscar manualmente el extracto adjunto o revisar el PDF si es un posible escaneado.",
            )
        )
        return resultado

    numero_factura, fecha_str = (None, None)
    if paginas_cover is not None:
        numero_factura, fecha_str = _extraer_de_portada(paginas_cover[0])

    texto_detalle = "\n".join(paginas_detalle)

    fecha_factura = None
    if fecha_str and re.match(r"^\d{2}/\d{2}/\d{4}$", fecha_str):
        try:
            fecha_factura = datetime.strptime(fecha_str, "%d/%m/%Y").date()
        except ValueError:
            fecha_factura = None

    if fecha_factura is None:
        m = re.search(r"Hasta:\s*(\d{2}/\d{2}/\d{4})", texto_detalle)
        if m:
            try:
                fecha_factura = datetime.strptime(m.group(1), "%d/%m/%Y").date()
                resultado.incidencias.append(
                    Incidencia(
                        nombre_archivo,
                        "Fecha de factura",
                        "No se ha encontrado (o no se ha podido leer) la portada de esta factura: la fecha "
                        "se ha tomado del fin de periodo ('Hasta:') del propio extracto.",
                        "Confirmar que la fecha de factura es correcta.",
                    )
                )
            except ValueError:
                fecha_factura = None

    if fecha_factura is None:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Fecha de factura",
                "No se ha podido determinar la fecha de esta factura (ni en la portada ni en el extracto).",
            )
        )
        return resultado

    if RE_MARCA_GRUPO_NUEVO.search("\n" + texto_detalle + "\n"):
        interpretados = _procesar_layout_nuevo(texto_detalle, nombre_archivo, fecha_factura, resultado)
    else:
        interpretados = _procesar_layout_viejo(texto_detalle, nombre_archivo, fecha_factura, resultado)

    if interpretados == 0:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tabla de consumos",
                "No se ha podido interpretar ninguna transaccion en el extracto de esta factura.",
            )
        )

    return resultado
