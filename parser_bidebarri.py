"""
Parser para facturas de E.S. Bidebarri, S.L. (uno o varios paginas; la
tabla de consumos siempre cabe en la pagina 1, las paginas siguientes son
solo el resto del texto legal/resumen).

Cada registro de consumo tiene esta forma (observada en los PDF reales):

    18413                          <- Ref. (numero de operacion, informativo)
    12-01-2021 08:29               <- fecha/hora (ancla del registro)
    EFITEC 95 GASOLINA             <- producto
    E.S. BIDEBARRI MARGEN DRCHA    <- establecimiento (se ignora)
    5,00                           <- cantidad (litros)
    1,299                          <- P.Un.
    0,00                           <- Dto.
    [0]                            <- Km, columna OPCIONAL (entero sin coma)
    6,50                           <- Importe
    €
    €
    €
    6097 hcl                       <- matricula, TAMBIEN OPCIONAL (puede faltar)

Como la matricula (y a veces la columna Km) pueden faltar, el parser
ancla cada registro en la linea fecha/hora (formato DD-MM-YYYY HH:MM,
inconfundible con la fecha de factura que usa barras), no en la matricula
ni en un numero fijo de lineas.
"""

import re
from datetime import datetime

import fitz

from modelos import Registro, Incidencia, ResultadoFactura
import productos
import destinos

RE_FECHA_HORA = re.compile(r"^\d{2}-\d{2}-\d{4}\s+\d{1,2}:\d{2}(:\d{2})?$")
RE_FECHA_FACTURA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
RE_NUM_DECIMAL = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2,3}$")
RE_ENTERO = re.compile(r"^\d+$")
RE_PLATE_NUEVA = re.compile(r"^\d{4}[\s-]?[A-Za-z]{3}$")
RE_PLATE_VIEJA = re.compile(r"^[A-Za-z]{2}\d{4}[\s-]?[A-Za-z]{2}$")
# Codigo de tarjeta interno pegado al nombre de la localidad sin espacio
# ("19125GATIK" = tarjeta 19125 + Gatika, "19311GETX" = tarjeta 19311 +
# Getxo): no tiene forma de matricula real, se reconoce para mandarlo a
# la hoja de tarjetas en vez de darlo por "sin matricula".
RE_TARJETA_CON_LOCALIDAD = re.compile(r"^\d{4,6}[A-Za-z]{3,}$")


def _parse_litros(texto: str):
    if not RE_NUM_DECIMAL.match(texto):
        return None
    return float(texto.replace(".", "").replace(",", "."))


def _normalizar_matricula(texto: str) -> str:
    return re.sub(r"\s+", "", texto).upper()


def _parece_matricula(texto: str) -> bool:
    return bool(RE_PLATE_NUEVA.match(texto) or RE_PLATE_VIEJA.match(texto))


def _extraer_fecha_factura(lineas):
    for linea in lineas:
        if RE_FECHA_FACTURA.match(linea):
            try:
                return datetime.strptime(linea, "%d/%m/%Y").date(), None
            except ValueError:
                continue
    return None, "no se encontro ninguna fecha de factura (formato DD/MM/YYYY) en el documento"


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

    texto_total = "\n".join(pagina.get_text() for pagina in doc)
    doc.close()

    if len(texto_total.strip()) < 20:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Archivo",
                "El documento no contiene texto extraible (posible PDF escaneado, requeriria OCR).",
                "Revisar manualmente o procesar con OCR.",
            )
        )
        return resultado

    lineas = [l.strip() for l in texto_total.split("\n")]
    n = len(lineas)

    fecha_factura, error_fecha = _extraer_fecha_factura(lineas)
    if fecha_factura is None:
        resultado.procesado_ok = False
        resultado.incidencias.append(Incidencia(nombre_archivo, "Fecha de factura", error_fecha))
        return resultado

    # limite de la tabla de consumos: todo lo que viene despues de "Totales:"
    # es resumen/legal, no un registro mas.
    limite = next((i for i, l in enumerate(lineas) if l.lower().startswith("totales")), n)

    bloques_interpretados = 0
    i = 0
    while i < limite:
        if not RE_FECHA_HORA.match(lineas[i]):
            i += 1
            continue

        idx_fecha_hora = i
        ref = lineas[i - 1] if i - 1 >= 0 else ""
        if idx_fecha_hora + 2 >= limite:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"Registro incompleto al final de la tabla (fecha/hora {lineas[idx_fecha_hora]!r}).",
                )
            )
            break

        concepto = lineas[idx_fecha_hora + 1]
        # idx_fecha_hora + 2 es el establecimiento, se ignora.
        idx = idx_fecha_hora + 3

        campos_decimales = []
        while idx < limite and RE_NUM_DECIMAL.match(lineas[idx]) and len(campos_decimales) < 3:
            campos_decimales.append(lineas[idx])
            idx += 1

        if len(campos_decimales) < 3:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"El registro con fecha/hora {lineas[idx_fecha_hora]!r} (concepto {concepto!r}) no tiene "
                    f"las 3 columnas numericas esperadas (cantidad/p.un./dto.) antes del importe.",
                )
            )
            i = idx_fecha_hora + 1
            continue

        cantidad_txt = campos_decimales[0]

        # columna Km opcional: un entero suelto (sin coma decimal) justo
        # antes del importe.
        if idx < limite and RE_ENTERO.match(lineas[idx]):
            idx += 1

        if idx >= limite or not RE_NUM_DECIMAL.match(lineas[idx]):
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"No se ha encontrado el importe del registro con fecha/hora {lineas[idx_fecha_hora]!r} "
                    f"(concepto {concepto!r}).",
                )
            )
            i = idx_fecha_hora + 1
            continue

        idx += 1  # importe consumido, no hace falta su valor

        # saltar hasta 3 lineas de simbolo de moneda (1-2 caracteres, sin
        # digitos ni letras alfanumericas relevantes).
        saltos = 0
        while idx < limite and saltos < 3 and len(lineas[idx]) <= 2 and not lineas[idx].isalnum():
            idx += 1
            saltos += 1

        matricula = None
        matricula_es_real = False
        destino, etiqueta = destinos.VEHICULOS, ""
        if idx < limite and lineas[idx] and not RE_ENTERO.match(lineas[idx]) and not RE_FECHA_HORA.match(lineas[idx]):
            candidato = lineas[idx]
            if _parece_matricula(candidato.replace(" ", "")) or any(ch.isalpha() for ch in candidato):
                matricula = _normalizar_matricula(candidato)
                matricula_es_real = _parece_matricula(candidato.replace(" ", ""))
                if not matricula_es_real:
                    destino, etiqueta = destinos.clasificar(candidato)
                    if not destino and RE_TARJETA_CON_LOCALIDAD.match(candidato.replace(" ", "")):
                        destino = destinos.TARJETAS
                        etiqueta = candidato.replace(" ", "").upper()
                    if destino:
                        matricula = etiqueta
                idx += 1

        sin_matricula = matricula is None
        if sin_matricula:
            matricula = "SIN MATRICULA"

        bloques_interpretados += 1
        litros = _parse_litros(cantidad_txt)

        categoria, motivo = productos.clasificar(concepto)

        if categoria == productos.EXCLUIDO:
            resultado.excluidos.append((concepto, motivo, matricula))
            i = idx
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
            i = idx
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
            i = idx
            continue

        if litros is None:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Litros",
                    f"No se pudo interpretar la cantidad {cantidad_txt!r} para la matricula {matricula}.",
                )
            )
            i = idx
            continue

        # Lo que va a garrafas/deposito/tarjeta no es una incidencia:
        # se registra en su propia hoja.
        if not matricula_es_real and not destino:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Matricula",
                    f"El registro de {concepto!r} con fecha de consumo {lineas[idx_fecha_hora]!r} no tiene "
                    f"una matricula de vehiculo reconocible (se ha usado {matricula!r}, ref. original {ref!r}).",
                    "Confirmar manualmente la matricula real de este consumo.",
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
                fecha_consumo=lineas[idx_fecha_hora],
                origen_detalle=f"ref. {ref}" if ref else "tabla principal",
            )
        )

        i = idx

    if bloques_interpretados == 0:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tabla de consumos",
                "No se ha encontrado ningun registro de consumo con fecha/hora reconocible en el documento.",
            )
        )

    return resultado
