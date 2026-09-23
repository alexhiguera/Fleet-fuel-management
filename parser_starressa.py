"""
Parser para facturas de STAR RESSA / RED ESPANOLA DE SERVICIOS S.A.U.
(RESSA) -misma empresa, solo cambio el nombre comercial en 2022, mismo
formato de documento-.

A diferencia de Ortuella, aqui la factura "plana" (portada, 1 pagina) es
solo un resumen de totales SIN matricula ni detalle por vehiculo; el
fichero "(EXTRACTO)"/"(DETALLE)" adjunto es el que trae el desglose real
por matricula/tarjeta. Por eso este parser recibe DOS rutas (cualquiera
de las dos puede faltar) y las combina en un unico ResultadoFactura.

Portada (1 pagina), ancla en las etiquetas de columna sueltas:

    NUMERO
    2021-724-100015463
    FECHA
    31-01-21
    ...
    SE APORTA DETALLE DEL PRESENTE F A C T U R A EN EL EXTRACTO ANEXO

Extracto/detalle (multi-pagina), bloques por matricula/tarjeta:

    *** MATRICULA/TARJETA 1234-ABC *** 700000000000000000 ***
    BERRIZ I BERRIZ
    2021-01-07 08:44          <- a veces en una linea, a veces partida en dos
    05190009368
    DIESEL STAR
    48,18                     <- litros (primer numero tras el concepto)
    1,158
    55,84
    55,84
    ...
    TOTAL MATRICULA/TARJETA1234-ABC, AGRUPADOS LOS CONCEPTOS SEGUN
    ... (recapitulacion, NO son transacciones, se ignora) ...

    *** MATRICULA/TARJETA ... (siguiente bloque) ...
    ...
    TOTAL MATRICULA/TARJETA(S) EMPRESA , AGRUPADOS LOS CONCEPTOS SEGUN   <- total global, NO es un vehiculo

Se ancla cada transaccion en el texto de concepto conocido (igual tecnica
que parser_solred.py), nunca en un numero fijo de lineas, porque el
formato de fecha/hora cambia de una linea a dos entre 2021 y 2022+.
"""

import re
from datetime import datetime

import fitz

from modelos import Registro, Incidencia, ResultadoFactura
import productos

RE_MARCADOR_BLOQUE = re.compile(r"\*\*\*\s*MATRICULA/TARJETA\s+(\S+)\s*\*\*\*")
RE_TOTAL_BLOQUE = re.compile(r"TOTAL MATRICULA/TARJETA")
RE_CORRESPONDIENTE_A = re.compile(r"CORRESPONDIENTE A\s+(\S+)")
RE_EXTRACTO_HASTA = re.compile(r"EXTRACTO HASTA:\s*(\d{2}-\d{2}-\d{4})")
RE_NUMERO = re.compile(r"-?\d{1,3}(?:\.\d{3})*,\d{2,3}")

# Conceptos de combustible/AdBlue + excluidos que pueden anclar una
# transaccion (ver productos.py). De mas largo a mas corto para que el
# regex no matchee un prefijo antes que el concepto completo.
_TODOS_LOS_CONCEPTOS = [
    "DIESEL STAR",
    "DIESEL OPTIMA",
    "SIN PLOMO",
    "ECOBLUE",
    "AUTOPISTAS DE PEAJE",
    "OTRAS COMPRAS",
    "CUOTA(S) TARJETA(S)",
    "BONIFICACION ESTATAL RD 6/2022",
    "DTO. CEPSA EXTRA",
]
_CONCEPTOS_ORDENADOS = sorted(_TODOS_LOS_CONCEPTOS, key=len, reverse=True)


def _concepto_a_regex(concepto: str) -> str:
    partes = concepto.split()
    return r"\s+".join(re.escape(p) for p in partes)


RE_TRANSACCION = re.compile(
    "(?:" + "|".join(_concepto_a_regex(c) for c in _CONCEPTOS_ORDENADOS) + ")"
)


def _texto_de(ruta_pdf):
    """Devuelve (texto_por_pagina, error) sin lanzar excepcion."""
    try:
        doc = fitz.open(ruta_pdf)
    except Exception as exc:
        return None, f"No se pudo abrir el PDF: {exc}"
    if doc.page_count == 0:
        doc.close()
        return None, "El PDF no tiene paginas."
    paginas = [p.get_text() for p in doc]
    doc.close()
    return paginas, None


def _extraer_de_portada(paginas_cover):
    texto = paginas_cover[0]
    lineas = [l.strip() for l in texto.split("\n")]
    numero = None
    fecha = None
    for i, l in enumerate(lineas):
        if l == "NUMERO" and i + 1 < len(lineas):
            numero = lineas[i + 1]
        elif l == "FECHA" and i + 1 < len(lineas):
            fecha = lineas[i + 1]
    return numero, fecha


def parse(ruta_cover, ruta_detalle, nombre_archivo: str) -> ResultadoFactura:
    resultado = ResultadoFactura(archivo=nombre_archivo)

    paginas_cover = None
    if ruta_cover:
        paginas_cover, error = _texto_de(ruta_cover)
        if error:
            resultado.incidencias.append(Incidencia(nombre_archivo, "Archivo", error, "Revisar el archivo."))
            paginas_cover = None
        elif len("".join(paginas_cover).strip()) < 20:
            paginas_cover = None

    paginas_detalle = None
    if ruta_detalle:
        paginas_detalle, error = _texto_de(ruta_detalle)
        if error:
            resultado.incidencias.append(Incidencia(nombre_archivo, "Archivo", error, "Revisar el archivo."))
            paginas_detalle = None
        elif len("".join(paginas_detalle).strip()) < 50:
            paginas_detalle = None

    if paginas_detalle is None:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Archivo",
                "No se ha encontrado (o no se ha podido leer) el extracto/detalle de esta factura: sin el, "
                "no se puede extraer el consumo por matricula (la portada solo trae totales agregados).",
                "Buscar manualmente el extracto adjunto o revisar el PDF si es un posible escaneado.",
            )
        )
        return resultado

    numero_factura, fecha_str = (None, None)
    if paginas_cover is not None:
        numero_factura, fecha_str = _extraer_de_portada(paginas_cover)

    texto_detalle = "\n".join(paginas_detalle)

    if fecha_str is None:
        m = RE_EXTRACTO_HASTA.search(texto_detalle)
        if m:
            fecha_str = m.group(1)
            fecha_str = "-".join([fecha_str[:2], fecha_str[3:5], fecha_str[8:10]])  # DD-MM-YYYY -> DD-MM-YY
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Fecha de factura",
                    "No se ha encontrado (o no se ha podido leer) la portada de esta factura: la fecha se ha "
                    "tomado del 'EXTRACTO HASTA' del propio extracto.",
                    "Confirmar que la fecha de factura es correcta.",
                )
            )

    fecha_factura = None
    if fecha_str:
        try:
            fecha_factura = datetime.strptime(fecha_str, "%d-%m-%y").date()
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

    bloques = list(RE_MARCADOR_BLOQUE.finditer(texto_detalle))
    if not bloques:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tabla de consumos",
                "No se ha encontrado ningun bloque '*** MATRICULA/TARJETA ... ***' en el extracto.",
            )
        )
        return resultado

    bloques_interpretados = 0
    ultimo_fin_procesado = 0
    for bloque in bloques:
        if bloque.start() < ultimo_fin_procesado:
            # Marcador de "continuacion" de un bloque que ya se ha
            # procesado entero (la misma matricula/tarjeta se repite al
            # principio de la pagina siguiente cuando su tabla no cabe
            # entera en una sola pagina); si no se saltara, sus
            # transacciones se contarian dos veces.
            continue

        matricula = bloque.group(1)
        inicio = bloque.end()
        m_total = RE_TOTAL_BLOQUE.search(texto_detalle, inicio)
        fin = m_total.start() if m_total else len(texto_detalle)
        ultimo_fin_procesado = fin

        transacciones = list(RE_TRANSACCION.finditer(texto_detalle, inicio, fin))
        for idx, match in enumerate(transacciones):
            concepto = re.sub(r"\s+", " ", match.group(0)).strip()
            categoria, motivo = productos.clasificar(concepto)

            if categoria == productos.EXCLUIDO:
                resultado.excluidos.append((concepto, motivo, matricula))
                bloques_interpretados += 1
                continue
            if categoria == productos.DESCONOCIDO:
                resultado.incidencias.append(
                    Incidencia(
                        nombre_archivo,
                        "Tipo de producto",
                        f"Concepto {concepto!r} (matricula/tarjeta {matricula}) no esta en el catalogo de "
                        f"productos conocidos.",
                        "Clasificar manualmente y, si procede, anadir al catalogo (productos.py).",
                    )
                )
                continue
            if categoria == productos.DIESEL_B100:
                resultado.incidencias.append(
                    Incidencia(
                        nombre_archivo,
                        "Tipo de producto",
                        f"Concepto {concepto!r} (matricula/tarjeta {matricula}) se ha identificado como "
                        f"Diesel B100, pero la plantilla no tiene una tabla para B100.",
                        "Anadir manualmente en una tabla aparte, tal y como se solicito.",
                    )
                )
                continue

            fin_ventana = transacciones[idx + 1].start() if idx + 1 < len(transacciones) else fin
            ventana = texto_detalle[match.end():fin_ventana]
            m_litros = RE_NUMERO.search(ventana)
            if not m_litros:
                resultado.incidencias.append(
                    Incidencia(
                        nombre_archivo,
                        "Litros",
                        f"No se encontro un valor de litros para la operacion {concepto!r} "
                        f"(matricula/tarjeta {matricula}).",
                    )
                )
                continue

            litros = float(m_litros.group(0).replace(".", "").replace(",", "."))
            bloques_interpretados += 1

            if not re.match(r"^\d{4}-[A-Z]{3}$", matricula):
                resultado.incidencias.append(
                    Incidencia(
                        nombre_archivo,
                        "Matricula",
                        f"La 'matricula/tarjeta' {matricula!r} no tiene forma de matricula de vehiculo real.",
                        "Confirmar manualmente a que corresponde esta tarjeta.",
                    )
                )

            resultado.registros.append(
                Registro(
                    referencia=nombre_archivo,
                    matricula=matricula,
                    fecha_factura=fecha_factura,
                    litros=litros,
                    producto=categoria,
                    concepto_original=concepto,
                    fecha_consumo="",
                    origen_detalle=f"extracto, tarjeta {matricula}",
                )
            )

    if bloques_interpretados == 0:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tabla de consumos",
                "No se ha podido interpretar ninguna transaccion dentro de los bloques por matricula/tarjeta.",
            )
        )

    return resultado
