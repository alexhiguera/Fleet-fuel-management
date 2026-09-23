"""
Parser para facturas de VIZCAINA DE PETROLEOS (entrega a granel de
gasoleo a un deposito, NUNCA a un vehiculo concreto). Una factura = una
sola pagina con UNA O VARIAS lineas de detalle (se ha visto desde una
sola hasta 5 por factura, segun el cliente), todas "GASOLEO A" en las
facturas revisadas.

Estructura real de cada linea de detalle (fecha en la posicion d):

        726.00        <- CANTIDAD (litros)          d-3
      0.891700         <- PRECIO/litro               d-2
        647.37         <- IMPORTE                    d-1
    16-02-2021          <- FECHA (fecha de la linea) d
    5B   373             <- N. VALE (albaran)         d+1
    GASOLEO A            <- DENOMINACION              d+2
    P23ASI/0008          <- (opcional) texto libre: pedido/obra/lugar

Las lineas se localizan por su patron (3 numeros + fecha + vale +
denominacion), no por un offset relativo al texto "GASOLEO A", asi que una
denominacion distinta (otro gasoleo, AdBlue, lubricante...) NO se pierde en
silencio: pasa por productos.clasificar() y, si no se conoce, va a
errores.md. Ademas se comprueba que cantidad x precio ~ importe y que la
suma de importes coincide con la 'Base imponible' de la factura.

Quirk critico: el formato numerico es el UNICO de todo el proyecto con
punto como separador decimal y coma como separador de miles (al reves
que Ortuella/SOLRED/GOIRI/BIDEBARRI/EUROPOLEO, que usan coma decimal). No
se reutiliza el parser numerico de los demas proveedores.

Ademas, la misma factura aparece a veces guardada en 2-3 archivos PDF
distintos con nombres distintos pero contenido identico (mismo numero de
factura/fecha/cantidad) -> la deduplicacion de facturas con el mismo
conjunto de registros se hace de forma centralizada en main.py, no aqui.
"""

import re
from datetime import datetime

import fitz

from modelos import Registro, Incidencia, ResultadoFactura
import productos
import destinos

RE_NUMERO_PUNTO = re.compile(r"^-?[\d,]+(\.\d+)?$")
RE_FECHA = re.compile(r"^\d{2}-\d{2}-\d{4}$")
MATRICULA_DEPOSITO = "DEPOSITO"


def _parse_litros(texto: str):
    """'726.00' / '1,300.93' -> float (punto decimal, coma de miles)."""
    texto = texto.strip()
    if not RE_NUMERO_PUNTO.match(texto):
        return None
    return float(texto.replace(",", ""))


def _es_numero(texto: str) -> bool:
    return bool(RE_NUMERO_PUNTO.match(texto.strip()))


def _inicio_bloque(lineas, d: int) -> bool:
    """
    True si lineas[d] es la fecha de una linea de detalle: tres numeros
    justo antes (cantidad, precio, importe) y dos lineas de texto despues
    (vale y denominacion).
    """
    return (
        d >= 3
        and d + 2 < len(lineas)
        and bool(RE_FECHA.match(lineas[d]))
        and _es_numero(lineas[d - 3])
        and _es_numero(lineas[d - 2])
        and _es_numero(lineas[d - 1])
        and not _es_numero(lineas[d + 2])
    )


def _extraer_bloques(lineas):
    """
    Devuelve todas las lineas de detalle de la factura. Solo se busca entre
    'Sede Gipuzkoa:' y 'Base imponible' (si existen) para no confundir la
    fecha de cabecera o de vencimiento con una linea de detalle.
    """
    ini = 0
    fin = len(lineas)
    if "Sede Gipuzkoa:" in lineas:
        ini = lineas.index("Sede Gipuzkoa:") + 1
    if "Base imponible" in lineas:
        fin = lineas.index("Base imponible")

    fechas = [d for d in range(max(ini, 3), fin) if _inicio_bloque(lineas, d)]
    bloques = []
    for n, d in enumerate(fechas):
        limite = (fechas[n + 1] - 3) if n + 1 < len(fechas) else fin
        libre = [l for l in lineas[d + 3 : limite] if l]
        bloques.append(
            {
                "cantidad_txt": lineas[d - 3],
                "litros": _parse_litros(lineas[d - 3]),
                "precio": _parse_litros(lineas[d - 2]),
                "importe": _parse_litros(lineas[d - 1]),
                "fecha_txt": lineas[d],
                "vale": lineas[d + 1],
                "denominacion": lineas[d + 2],
                "referencia": " ".join(libre),
            }
        )
    return bloques


def _comprobar_totales(resultado, nombre_archivo, lineas, bloques):
    """
    Salvaguardas contra perdidas silenciosas: (1) cantidad x precio ~ importe
    en cada linea (1 %), (2) suma de importes = 'Base imponible'. Solo generan
    incidencias, no alteran los registros.
    """
    for b in bloques:
        q, p, imp = b["litros"], b["precio"], b["importe"]
        if q is None or p is None or imp is None:
            continue
        if abs(q * p - imp) > max(0.01 * abs(imp), 0.02):
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Importe",
                    f"Linea vale {b['vale']}: cantidad {q} x precio {p} = {q * p:.2f}, pero el importe "
                    f"es {imp} (desviacion > 1 %).",
                    "Revisar que cantidad/precio/importe se han leido bien.",
                )
            )
    if "Base imponible" in lineas:
        k = lineas.index("Base imponible")
        # tras 'Base imponible' vienen '%IVA', 'Cuota IVA' y luego la base
        base = None
        for cand in lineas[k + 1 : k + 6]:
            if _es_numero(cand):
                base = _parse_litros(cand)
                break
        suma = round(sum(b["importe"] for b in bloques if b["importe"] is not None), 2)
        if base is None or abs(suma - base) > 0.02:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"La suma de importes de las {len(bloques)} linea(s) leidas ({suma}) no coincide con la "
                    f"'Base imponible' de la factura ({base}): posible linea sin leer.",
                    "Revisar manualmente el PDF.",
                )
            )


def parse(ruta_pdf: str, nombre_archivo: str) -> ResultadoFactura:
    resultado = ResultadoFactura(archivo=nombre_archivo)

    try:
        doc = fitz.open(ruta_pdf)
    except Exception as exc:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(nombre_archivo, "Archivo", f"No se pudo abrir el PDF: {exc}", "Revisar el archivo.")
        )
        return resultado

    if doc.page_count == 0:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(nombre_archivo, "Archivo", "El PDF no tiene paginas.", "Revisar el archivo.")
        )
        return resultado

    texto = doc[0].get_text()
    doc.close()

    if len(texto.strip()) < 20:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Archivo",
                "La pagina no contiene texto extraible (posible PDF escaneado, requeriria OCR).",
                "Revisar manualmente o procesar con OCR.",
            )
        )
        return resultado

    lineas = [l.strip() for l in texto.split("\n")]

    bloques = _extraer_bloques(lineas)
    if not bloques:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tabla de consumos",
                "No se ha encontrado ninguna linea de detalle (cantidad, precio, importe, fecha, vale, "
                "denominacion) en el documento.",
            )
        )
        return resultado

    _comprobar_totales(resultado, nombre_archivo, lineas, bloques)

    bloques_interpretados = 0
    for b in bloques:
        producto_txt = b["denominacion"]
        cantidad_txt = b["cantidad_txt"]
        litros = b["litros"]
        fecha_txt = b["fecha_txt"]
        vale = b["vale"]

        fecha_factura = None
        if RE_FECHA.match(fecha_txt):
            try:
                fecha_factura = datetime.strptime(fecha_txt, "%d-%m-%Y").date()
            except ValueError:
                fecha_factura = None

        if litros is None or fecha_factura is None:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"La linea de {producto_txt!r} (vale {vale}) no tiene la forma esperada "
                    f"(cantidad={cantidad_txt!r}, fecha={fecha_txt!r}).",
                )
            )
            continue

        if litros <= 0:
            # Abono / anulacion / linea a cero: no se vuelca como consumo ni
            # se resta por su cuenta; se deja a revision humana.
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Cantidad",
                    f"La linea de {producto_txt!r} (vale {vale}, {fecha_txt}) tiene cantidad "
                    f"{cantidad_txt!r} (cero o negativa: posible abono/anulacion).",
                    "Revisar manualmente si debe restarse de otra factura.",
                )
            )
            continue

        categoria, motivo = productos.clasificar(producto_txt)

        if categoria == productos.EXCLUIDO:
            resultado.excluidos.append((producto_txt, motivo, ""))
            bloques_interpretados += 1
            continue
        if categoria == productos.DESCONOCIDO:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tipo de producto",
                    f"Concepto {producto_txt!r} (litros {cantidad_txt}) no esta en el catalogo de "
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
                    f"Concepto {producto_txt!r} (litros {cantidad_txt}) se ha identificado como Diesel "
                    f"B100, pero la plantilla no tiene una tabla para B100.",
                    "Anadir manualmente en una tabla aparte, tal y como se solicito.",
                )
            )
            continue

        bloques_interpretados += 1

        # Entrega a granel a un deposito fijo: va a la hoja 'depositos',
        # no a la de vehiculos, asi que no hace falta avisar de nada.
        # Etiqueta: si la linea trae un texto libre bajo la denominacion
        # (pedido/obra/lugar, p.ej. 'P23ASI/0008', 'AS-16175', 'HERNANI')
        # se anade tal cual entre parentesis para poder trazar la entrega.
        etiqueta = MATRICULA_DEPOSITO
        resultado.registros.append(
            Registro(
                referencia=nombre_archivo,
                matricula=MATRICULA_DEPOSITO,
                fecha_factura=fecha_factura,
                litros=litros,
                producto=categoria,
                destino=destinos.DEPOSITOS,
                destino_detalle=etiqueta,
                concepto_original=producto_txt,
                fecha_consumo=fecha_txt,
                origen_detalle=f"vale {vale}" + (f", ref. {b['referencia']}" if b["referencia"] else ""),
            )
        )

    if bloques_interpretados == 0:
        resultado.procesado_ok = False

    return resultado
