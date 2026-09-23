"""
Parser para facturas de EUROPOLEO, S.L. (entrega a granel de gasoleo en
el deposito fijo de una instalacion del cliente -nunca a un vehiculo
concreto-, cada deposito identificado por el nombre de su emplazamiento).
Una factura = normalmente una sola linea de producto en una sola pagina.

Quirk critico: la fuente incrustada en la mayoria de estos PDF no tiene
tabla Unicode, asi que PyMuPDF devuelve los codigos de caracter en bruto,
desplazados de forma constante -29 respecto al texto real (un "cifrado
Cesar" trivial de decodificar sumando 29 a cada caracter salvo el salto
de linea). Una minoria de archivos ya vienen con texto legible
directamente. El parser prueba primero el texto tal cual y, si no
contiene las palabras esperadas, aplica el desplazamiento.

Estructura real (ya decodificada), anclada en la linea exacta
"GASOLEO A" (indice g relativo):

    P20/1664                              <- referencia de pedido, OPCIONAL (puede faltar del
                                             todo, o venir al final del texto segun el cliente)
    EMPRESA TRANSPORTISTA S.L. GAS A SITIO <- concepto, incluye la ubicacion del deposito
                                             (p.ej. "ASASER S.A.-ZORNOTZA", "GAS A BASAURI", etc.)
    [02-04-2024]                          <- a veces hay una fecha duplicada aqui, se ignora
      1.500,00                            <- CANTIDAD (litros)              g-5
     0,817360                             <- PRECIO/litro (6 decimales)     g-4
       1.226,04                           <- IMPORTE                       g-3
    04-01-2021                            <- FECHA (fecha de factura)      g-2
    2101OA00005                           <- N. ALBARAN                    g-1
    GASOLEO A                             <- PRODUCTO                     g
    21,00                                 <- % IVA                        g+1
    L                                     <- unidad                       g+2

Segunda plantilla (series LI-/LC-/LR-, otro software de
facturacion): texto legible SIN cifrar y estructura distinta ("Albaran: ..."
seguido de importe, codigo, descripcion, cantidad, precio con punto decimal).
Aqui aparecen AdBlue a granel y lubricantes (VERKOPLUS, no combustible) y una
factura puede llevar varios albaranes. Ver _parse_plantilla_li().

Los offsets g-5..g-1 se han verificado estables independientemente de si
la referencia de pedido o la fecha duplicada estan presentes (solo
desplazan la linea de concepto/ubicacion, no el bloque numerico).
"""

import re
from datetime import datetime

import fitz

from modelos import Registro, Incidencia, ResultadoFactura
import productos
import destinos

RE_NUMERO = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
RE_PRECIO = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d+$")
RE_FECHA = re.compile(r"^\d{2}-\d{2}-\d{4}$")
RE_UBICACION = re.compile(r"GAS\s+A\s+(\S+)", re.IGNORECASE)
PRODUCTO_ANCLA = "GASOLEO A"
# Gasoleo B (bonificado/agricola): misma estructura de factura que Gasoleo A,
# solo cambia el texto del producto ancla; se ha visto entregado en los
# mismos depositos que el A (p.ej. "ASASER-PORTUGALETE GO-A"/"...GO-B").
ANCLAS_PRODUCTO = (PRODUCTO_ANCLA, "GASOLEO B")
# Sufijo con el codigo abreviado del producto que a veces se pega al final
# de la linea de concepto/ubicacion ("PORTUGALETE GO-A", "LOIU GOA").
RE_SUFIJO_PRODUCTO_EN_LUGAR = re.compile(r"\s+GO-?[AB]$", re.IGNORECASE)

TXT_ALBARAN = "Albarán:"

# Marcas que indican que el texto ya es legible (no cifrado).
_MARCAS_LEGIBLE = ("IMPORTE", PRODUCTO_ANCLA, "Total Bruto", TXT_ALBARAN)

RE_P_REF = re.compile(r"^P\d{2}[A-Z]{1,3}/\d+$")
RE_CONCEPTO_LUGAR = re.compile(r"^[A-Z][A-Z\. ]*-\s*(\S.*)$")
RE_NUM_PUNTO = re.compile(r"^-?\d+\.\d+$")
RE_FECHA_ALB = re.compile(r"Fecha:\s*(\d{2}-\d{2}-\d{4})")

# Conceptos de la plantilla LI-/LC-/LR- que no estan (aun) en productos.py.
# Si productos.clasificar() ya los conoce, prevalece el catalogo.
_MOTIVO_LUBRICANTE = "lubricante/aceite (no combustible)"


def _decodificar_si_hace_falta(texto: str) -> str:
    if any(m in texto for m in _MARCAS_LEGIBLE):
        return texto
    desplazado = "".join(c if c == "\n" else chr(ord(c) + 29) for c in texto)
    return desplazado


def _parse_litros(texto: str):
    if not RE_NUMERO.match(texto):
        return None
    return float(texto.replace(".", "").replace(",", "."))


def _parse_precio(texto: str):
    if not RE_PRECIO.match(texto):
        return None
    return float(texto.replace(".", "").replace(",", "."))


def _buscar_ubicacion(lineas, idx_producto):
    inicio = max(0, idx_producto - 12)
    for i in range(idx_producto - 1, inicio - 1, -1):
        m = RE_UBICACION.search(lineas[i])
        if m:
            return m.group(1).upper()
    return None


def _buscar_lugar_concepto(lineas, idx_producto, idx_importe):
    """
    Lugar de entrega a partir de la linea de concepto de la factura
    (p.ej. "ASASER S.A.-ZORNOTZA" -> "ZORNOTZA"). Esa linea esta justo encima
    de la cantidad, o encima de la fecha de operacion si esta existe (facturas
    de 2024, con columna FECHA OPERACION). Solo se acepta si tiene la forma
    "EMPRESA-LUGAR" (con guion). El bloque 'MERCANCIA ENTREGADA EN' de la
    cabecera puede no servir: en algunos clientes es siempre la misma
    direccion fiscal y no el deposito real de cada entrega.
    """
    for j in range(idx_producto - 6, idx_importe, -1):
        l = lineas[j]
        if not l or RE_FECHA.match(l):
            continue
        m = RE_CONCEPTO_LUGAR.match(l)
        if m and not RE_P_REF.match(l):
            lugar = RE_SUFIJO_PRODUCTO_EN_LUGAR.sub("", m.group(1).strip()).strip()
            return lugar.upper() or None
        return None
    return None


def _clasificar_concepto(desc: str):
    """productos.clasificar() con respaldo local para conceptos de la
    plantilla LI-/LC-/LR- aun no catalogados."""
    categoria, motivo = productos.clasificar(desc)
    if categoria != productos.DESCONOCIDO:
        return categoria, motivo
    up = desc.upper()
    if "ADBLUE" in up:
        return productos.ADBLUE, None
    if "VERKOPLUS" in up or "ACEITE" in up or "SIGAUS" in up:
        return productos.EXCLUIDO, _MOTIVO_LUBRICANTE
    return categoria, motivo


def _parse_plantilla_li(lineas, nombre_archivo, resultado):
    """
    Plantilla sin cifrar (LI-/LC-/LR-). Cada albaran:
        Albaran: 23-  1055 -  Fecha: 26-10-2023  [- P23ASI/0274]
        589.41 | ADBLUE1000 | ADBLUE B-1000L GRANEL | 999.00 | 0.59
        [Aportacion SIGAUS | 0.0600 | 2.40]
    Devuelve el numero de lineas de producto interpretadas.
    """
    try:
        fin = lineas.index("Total Bruto")
    except ValueError:
        fin = len(lineas)
    inicios = [i for i, l in enumerate(lineas[:fin]) if l.startswith(TXT_ALBARAN)]
    if not inicios:
        resultado.incidencias.append(
            Incidencia(nombre_archivo, "Tabla de consumos", "No se ha encontrado ninguna linea 'Albaran:'.")
        )
        return 0
    interpretadas = 0
    for n, ini in enumerate(inicios):
        sig = inicios[n + 1] if n + 1 < len(inicios) else fin
        seg = lineas[ini:sig]
        m = RE_FECHA_ALB.search(seg[0])
        albaran = re.sub(r"\s+", " ", seg[0].split("Fecha:")[0].replace(TXT_ALBARAN, "")).strip(" -")
        fecha = None
        if m:
            try:
                fecha = datetime.strptime(m.group(1), "%d-%m-%Y").date()
            except ValueError:
                fecha = None
        # cuerpo: importe, codigo, descripcion, cantidad, precio [SIGAUS...]
        cuerpo = [l for l in seg[1:] if l and not RE_FECHA.match(l)]
        if (
            len(cuerpo) < 5
            or not RE_NUM_PUNTO.match(cuerpo[0])
            or not RE_NUM_PUNTO.match(cuerpo[3])
            or not RE_NUM_PUNTO.match(cuerpo[4])
            or fecha is None
        ):
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"El albaran {albaran!r} no tiene la forma esperada ({seg[:8]!r}).",
                )
            )
            continue
        importe, desc, cant, precio = float(cuerpo[0]), cuerpo[2], float(cuerpo[3]), float(cuerpo[4])
        if abs(cant * precio - importe) > 0.01 * importe + 0.02:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"Albaran {albaran!r}: cantidad x precio ({cant} x {precio}) no cuadra con el "
                    f"importe ({importe}); revisar la lectura.",
                )
            )
            continue
        categoria, motivo = _clasificar_concepto(desc)
        if categoria == productos.EXCLUIDO:
            resultado.excluidos.append((desc, motivo, ""))
            interpretadas += 1
            continue
        if categoria == productos.DESCONOCIDO:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tipo de producto",
                    f"Concepto {desc!r} (cantidad {cant}) no esta en el catalogo de productos conocidos.",
                    "Clasificar manualmente y, si procede, anadir al catalogo (productos.py).",
                )
            )
            continue
        if categoria == productos.DIESEL_B100:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tipo de producto",
                    f"Concepto {desc!r} (cantidad {cant}) se ha identificado como Diesel B100, pero la "
                    f"plantilla no tiene una tabla para B100.",
                    "Anadir manualmente en una tabla aparte, tal y como se solicito.",
                )
            )
            continue
        interpretadas += 1
        matricula = "DEPOSITO"  # esta plantilla no indica el lugar de entrega
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Lugar de entrega",
                f"Albaran {albaran!r} ({desc!r}, {cant:g} L): la factura no indica el lugar de entrega; "
                f"se asigna a 'DEPOSITO' generico.",
                "Si se conoce el deposito real, corregirlo manualmente.",
            )
        )
        resultado.registros.append(
            Registro(
                referencia=nombre_archivo,
                matricula=matricula,
                fecha_factura=fecha,
                litros=cant,
                producto=categoria,
                destino=destinos.DEPOSITOS,
                destino_detalle=matricula,
                concepto_original=desc,
                fecha_consumo=m.group(1),
                origen_detalle=f"albaran {albaran}",
            )
        )
    return interpretadas


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

    texto_crudo = doc[0].get_text()
    n_paginas = doc.page_count
    doc.close()

    if n_paginas > 1:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Archivo",
                f"El PDF tiene {n_paginas} paginas y solo se lee la primera.",
                "Revisar manualmente.",
            )
        )

    if len(texto_crudo.strip()) < 20:
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

    texto = _decodificar_si_hace_falta(texto_crudo)

    if "Total Bruto" in texto and TXT_ALBARAN in texto:
        lineas_li = [l.strip() for l in texto.split("\n")]
        if _parse_plantilla_li(lineas_li, nombre_archivo, resultado) == 0:
            resultado.procesado_ok = False
        return resultado

    if not any(a in texto for a in ANCLAS_PRODUCTO) and "IMPORTE" not in texto:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Archivo",
                "No se ha podido interpretar el texto del PDF (ni directamente ni aplicando el "
                "desplazamiento de caracteres conocido de EUROPOLEO).",
                "Revisar manualmente el archivo.",
            )
        )
        return resultado

    lineas = [l.strip() for l in texto.split("\n")]
    n = len(lineas)

    indices_producto = [i for i, l in enumerate(lineas) if l in ANCLAS_PRODUCTO]
    if not indices_producto:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tabla de consumos",
                f"No se ha encontrado ninguna linea de producto {' / '.join(ANCLAS_PRODUCTO)!r} en el documento.",
            )
        )
        return resultado

    idx_importe = lineas.index("IMPORTE") if "IMPORTE" in lineas else None

    # Aviso si hay mas lineas de unidad ("L", "UN"...) que lineas de producto
    # interpretadas: posible producto/concepto que el parser no esta leyendo.
    if idx_importe is not None and "BASE" in lineas[idx_importe:]:
        idx_base = lineas.index("BASE", idx_importe)
        unidades = [l for l in lineas[idx_importe:idx_base] if l in ("L", "UN", "KG", "U", "M3")]
        if len(unidades) != len(indices_producto):
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"La tabla tiene {len(unidades)} lineas de mercancia pero solo {len(indices_producto)} "
                    f"de {' / '.join(ANCLAS_PRODUCTO)}: puede haber un producto no leido.",
                )
            )

    bloques_interpretados = 0
    for idx in indices_producto:
        producto_texto = lineas[idx]
        if idx - 5 < 0:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"El bloque de {producto_texto!r} en la linea {idx} no tiene suficientes lineas "
                    f"anteriores para leer cantidad/fecha/albaran.",
                )
            )
            continue

        cantidad_txt = lineas[idx - 5]
        fecha_txt = lineas[idx - 2]
        albaran = lineas[idx - 1]

        litros = _parse_litros(cantidad_txt)
        precio = _parse_precio(lineas[idx - 4])
        importe = _parse_litros(lineas[idx - 3])
        if (
            litros is not None
            and precio is not None
            and importe is not None
            and abs(litros * precio - importe) > 0.01 * abs(importe) + 0.02
        ):
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"cantidad x precio ({cantidad_txt} x {lineas[idx - 4]}) no cuadra con el importe "
                    f"({lineas[idx - 3]}); revisar la lectura de la cantidad.",
                )
            )

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
                    f"El bloque de {producto_texto!r} en la linea {idx} no tiene la forma esperada "
                    f"(cantidad={cantidad_txt!r}, fecha={fecha_txt!r}).",
                )
            )
            continue

        categoria, motivo = productos.clasificar(producto_texto)

        if categoria == productos.EXCLUIDO:
            resultado.excluidos.append((producto_texto, motivo, ""))
            bloques_interpretados += 1
            continue
        if categoria == productos.DESCONOCIDO:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tipo de producto",
                    f"Concepto {producto_texto!r} (litros {cantidad_txt}) no esta en el catalogo de "
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
                    f"Concepto {producto_texto!r} (litros {cantidad_txt}) se ha identificado como Diesel "
                    f"B100, pero la plantilla no tiene una tabla para B100.",
                    "Anadir manualmente en una tabla aparte, tal y como se solicito.",
                )
            )
            continue

        bloques_interpretados += 1
        ubicacion = _buscar_ubicacion(lineas, idx)
        if ubicacion is None and idx_importe is not None:
            ubicacion = _buscar_lugar_concepto(lineas, idx, idx_importe)
        matricula = f"DEPOSITO {ubicacion}" if ubicacion else "DEPOSITO"

        # Entrega a granel a un deposito fijo: va a la hoja 'depositos',
        # no a la de vehiculos, asi que no hace falta avisar de nada.
        resultado.registros.append(
            Registro(
                referencia=nombre_archivo,
                matricula=matricula,
                fecha_factura=fecha_factura,
                litros=litros,
                producto=categoria,
                destino=destinos.DEPOSITOS,
                destino_detalle=matricula,
                concepto_original=producto_texto,
                fecha_consumo=fecha_txt,
                origen_detalle=f"albaran {albaran}",
            )
        )

    if bloques_interpretados == 0:
        resultado.procesado_ok = False

    return resultado
