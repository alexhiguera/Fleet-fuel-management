"""
Parser para facturas de E.S. GOIRI, S.A. (facturas de una sola pagina,
tabla lineal Vehiculo/Concepto/Fecha/Pvp/Cantidad/Dto/Importe, muy
parecida a Gasolinera Ortuella).

A diferencia de Ortuella, el bloque de consumo NO tiene siempre 7 lineas:
la linea de matricula puede faltar por completo (el registro empieza
directamente en el concepto) o venir sustituida por el texto literal
"GARRAFAS" (repostado a bidones, no a un vehiculo). Por eso el parser
ancla cada registro en la linea de CONCEPTO (conocida de antemano por el
catalogo de productos), no en la matricula, y mira hacia atras para
decidir si hay matricula, "GARRAFAS" o nada.

Observado en los PDF reales:

    1234ABC
    Gna 95
    12/01/2021 9:40
    1,289€
    22,50
    0,000€
    29,00€

    GARRAFAS
    Gna 95
    07/01/2022 13:40
    1,569€
    80,02
    0,000€
    125,55€

    ... (bloque anterior)
    41,53€
    Gna 95                 <- sin linea de matricula antes de este concepto
    26/07/2021 13:35
    1,479€
    86,20
    0,000€
    127,49€
"""

import re
from datetime import datetime

import fitz

from modelos import Registro, Incidencia, ResultadoFactura
import productos
import destinos

RE_PLATE = re.compile(r"^\d{4}[A-Z]{3}$")
RE_FECHA_FACTURA_LINEA = re.compile(r"^Factura\b.*?(\d{2}/\d{2}/\d{4})\s*$")
RE_IMPORTE_EUR = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2,3}\s*\S?\s*$")
RE_NUMERO = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
RE_FECHA_HORA = re.compile(r"^\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}(:\d{2})?\s*$")

# Conceptos conocidos que pueden anclar un registro (case-insensitive).
# De mas largo a mas corto para evitar que un prefijo matchee antes.
_CONCEPTOS = sorted(
    ["Diesel E+10", "Gna 95", "Gna 98", "Diesel", "Adblue"], key=len, reverse=True
)


def _parse_litros(texto: str):
    if not RE_NUMERO.match(texto):
        return None
    return float(texto.replace(".", "").replace(",", "."))


def _extraer_fecha_factura(lineas):
    candidatas = []
    for linea in lineas:
        m = RE_FECHA_FACTURA_LINEA.match(linea.strip())
        if m:
            candidatas.append(m.group(1))
    if not candidatas:
        return None, "no se encontro ninguna linea 'Factura ... DD/MM/YYYY' en la pagina"
    if len(candidatas) > 1 and len(set(candidatas)) > 1:
        return None, f"se encontraron varias fechas de factura distintas: {candidatas}"
    fecha_str = candidatas[0]
    try:
        return datetime.strptime(fecha_str, "%d/%m/%Y").date(), None
    except ValueError:
        return None, f"fecha de factura con formato invalido: {fecha_str!r}"


def _es_linea_concepto(linea: str):
    for concepto in _CONCEPTOS:
        if linea.strip().lower() == concepto.lower():
            return concepto
    return None


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

    fecha_factura, error_fecha = _extraer_fecha_factura(lineas)
    if fecha_factura is None:
        resultado.procesado_ok = False
        resultado.incidencias.append(Incidencia(nombre_archivo, "Fecha de factura", error_fecha))
        return resultado

    n = len(lineas)
    bloques_interpretados = 0

    for c in range(n):
        concepto = _es_linea_concepto(lineas[c])
        if concepto is None:
            continue
        if c + 5 >= n:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"Bloque de consumo incompleto al final de la pagina (concepto {concepto!r}, linea {c}).",
                )
            )
            continue

        fecha_hora = lineas[c + 1]
        pvp = lineas[c + 2]
        cantidad_txt = lineas[c + 3]
        dto = lineas[c + 4]
        importe = lineas[c + 5]

        forma_valida = (
            RE_FECHA_HORA.match(fecha_hora)
            and RE_IMPORTE_EUR.match(pvp)
            and RE_NUMERO.match(cantidad_txt)
            and RE_IMPORTE_EUR.match(importe)
        )
        if not forma_valida:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"El bloque que empieza en el concepto {concepto!r} (linea {c}) no tiene la forma "
                    f"esperada (fecha={fecha_hora!r}, pvp={pvp!r}, cantidad={cantidad_txt!r}, "
                    f"importe={importe!r}).",
                )
            )
            continue

        matricula = None
        matricula_es_real = False
        destino, etiqueta = destinos.VEHICULOS, ""
        if c - 1 >= 0:
            anterior = lineas[c - 1]
            if destinos.es_matricula(anterior):
                matricula = anterior
                matricula_es_real = True
            else:
                destino, etiqueta = destinos.clasificar(anterior)
                if destino:
                    matricula = etiqueta

        sin_matricula = matricula is None
        if sin_matricula:
            matricula = "SIN MATRICULA"

        litros = _parse_litros(cantidad_txt)
        bloques_interpretados += 1

        categoria, motivo = productos.clasificar(concepto)

        if categoria == productos.EXCLUIDO:
            resultado.excluidos.append((concepto, motivo, matricula))
            continue
        if categoria == productos.DESCONOCIDO:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tipo de producto",
                    f"Concepto {concepto!r} (matricula {matricula}, litros {cantidad_txt}) no esta en el "
                    f"catalogo de productos conocidos.",
                    "Clasificar manualmente y, si procede, anadir al catalogo (productos.py).",
                )
            )
            continue
        if categoria == productos.DIESEL_B100:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tipo de producto",
                    f"Concepto {concepto!r} (matricula {matricula}, litros {cantidad_txt}) se ha "
                    f"identificado como Diesel B100, pero la plantilla no tiene una tabla para B100.",
                    "Anadir manualmente en una tabla aparte, tal y como se solicito.",
                )
            )
            continue

        if litros is None:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Litros",
                    f"No se pudo interpretar la cantidad {cantidad_txt!r} para la matricula {matricula}.",
                )
            )
            continue

        # Lo que va a garrafas/deposito/tarjeta no es una incidencia:
        # se registra en su propia hoja, que ya deja claro que no es un
        # vehiculo.
        if not matricula_es_real and not destino:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Matricula",
                    f"El registro de {concepto!r} con fecha de consumo {fecha_hora!r} no tiene una "
                    f"matricula de vehiculo real (se ha usado {matricula!r}).",
                    "Confirmar que la ausencia de matricula es correcta.",
                )
            )

        resultado.registros.append(
            Registro(
                referencia=nombre_archivo,
                matricula=matricula,
                fecha_factura=fecha_factura,
                litros=litros,
                producto=categoria,
                destino=destino,
                destino_detalle=etiqueta,
                concepto_original=concepto,
                fecha_consumo=fecha_hora,
                origen_detalle="tabla principal, pagina 1",
            )
        )

    if bloques_interpretados == 0:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tabla de consumos",
                "No se ha encontrado ningun bloque de consumo con un concepto reconocible en la pagina.",
            )
        )

    return resultado
