"""
Parser para facturas de tarjeta flota SOLRED S.A. (multi-pagina).

Estructura real observada:

  - Portada(s): resumen con el numero de factura y su fecha, ademas a
    veces del numero de una factura rectificativa (RRA...) asociada que
    NO es la que nos interesa. El numero de factura NO se extrae del
    nombre de archivo (se ha visto que puede no coincidir exactamente,
    p.ej. con un digito de menos, o que el archivo ni siquiera tenga el
    numero en el nombre): se localiza directamente en el documento,
    buscando la etiqueta "Num. Factura" seguida de su valor, en la
    pagina "Facturacion por operaciones realizadas con tarjeta SOLRED".
    Esa pagina NO esta siempre en la posicion 0: alguna factura incluye
    antes una pagina publicitaria (visto en FC22GA-00368), asi que se
    busca en las primeras paginas, no solo en la primera.

  - Si el numero de factura encontrado empieza por "RRA" (en vez de
    "A"), el documento es EN SI MISMO una factura rectificativa (nota
    de abono/correccion), no una factura de consumo normal. Se procesa
    igualmente (puede no tener ningun consumo de combustible, solo
    peajes, en cuyo caso no genera registros) pero se deja una
    incidencia informativa para que se revise manualmente.

  - Una tabla-resumen compacta por tarjeta/matricula (paginas 2-3 en
    el ejemplo). Se ignora deliberadamente: sus columnas numericas
    quedan pegadas en la extraccion de texto y es facil desalinearlas.

  - El detalle real, fiable, esta en las secciones
    "Resumen de operaciones realizadas con tarjetas SOLRED": un bloque
    por matricula ("N. de Matricula XXXX-XXX") con una linea por
    operacion:
        <ref> DD/MM HH:MM <CONCEPTO>
        <establecimiento (1-2 lineas)>
        <Litros>,<Precio litro>,<Importe>,... (si es combustible)
    Las operaciones de peaje/aparcamiento/taller no tienen litros.

  Como el orden de "ref" y "fecha/hora concepto" varia (a veces van en
  la misma linea de texto, a veces en lineas separadas, segun el ancho
  del ref), NO se parsea linea a linea: se buscan directamente, sobre
  el texto completo del PDF, las apariciones de "DD/MM HH:MM CONCEPTO"
  usando el catalogo cerrado de conceptos conocidos como ancla. Esto es
  inmune a como se hayan partido las lineas.
"""

import re
from datetime import datetime

import fitz

from modelos import Registro, Incidencia, ResultadoFactura
import productos
import destinos

RE_LABEL_NUM_FACTURA = re.compile(r"N[uú]m\.\s*Factura")
RE_NUM_FACTURA_VALOR = re.compile(r"\b(RRA\d{6,10}|A\d{6,10})\b")
RE_FECHA = re.compile(r"\d{2}/\d{2}/\d{4}")
RE_MATRICULA_HEADER = re.compile(r"N[ºo°]?\.?\s*de\s*Matr[íi]cula\s*[:\s]\s*([^\r\n]+)", re.IGNORECASE)
RE_NUMERO = re.compile(r"-?\d{1,3}(?:\.\d{3})*,\d{2}")
_NUM = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
# Una linea de combustible con litros imprime "<litros> <precio/litro con 3
# decimales> [<importe>...]". El AdBlue "a granel" de algunas estaciones
# (concepto "ADBLUE" a secas) NO trae litros ni precio: solo el importe, dos
# veces seguidas (importe y total). Sin esta comprobacion se tomaba el importe
# en euros como si fueran litros.
RE_LITROS_Y_PRECIO = re.compile(r"(?P<litros>" + _NUM + r")\s+-?\d{1,2},\d{3}(?!\d)")
RE_PAR_IMPORTES_IGUALES = re.compile(r"(?<![\d,.])(?P<a>" + _NUM + r")\s+(?P=a)(?![\d,])")

# Conceptos de combustible/AdBlue reconocidos (ver productos.py) + los NO
# combustible que tambien aparecen anclados a "DD/MM HH:MM CONCEPTO", para
# poder delimitar correctamente el final de cada transaccion.
_TODOS_LOS_CONCEPTOS = [
    "DIESEL E+10 NEO",
    "DIESEL E+ NEO",
    "EFITEC 95 N",
    "EFITEC 98 N",
    "G95PREMIUM N",
    "ADBLUE REPSOL",
    "ADBLUE",
    "APARC. VIAT",
    "AUTOPISTAS",
    "VIA T",
    "LAVADOS/LUBRICS.",
    "LAVADOS",
    "TALLER / APARCAMTO",
    "TALLER",
]
# de mas largo a mas corto, para que el regex no matchee un prefijo
# (p.ej. "DIESEL E+ NEO" antes que "DIESEL E+10 NEO" partiria mal el texto).
_CONCEPTOS_ORDENADOS = sorted(_TODOS_LOS_CONCEPTOS, key=len, reverse=True)
_CONCEPTOS_ALT = "|".join(re.escape(c) for c in _CONCEPTOS_ORDENADOS)

# Concepto NO catalogado anclado a "DD/MM HH:MM": se captura la linea entera
# (en vez de ignorarlo) para que un combustible nuevo (AUTOGAS, "DIESEL E+5",
# ...) no desaparezca en silencio sino que llegue a errores.md.
RE_TRANSACCION = re.compile(
    r"(?P<dia_mes>\d{2}/\d{2})\s+(?P<hora>\d{1,2}:\d{2})"
    r"(?:\s+(?P<concepto>" + _CONCEPTOS_ALT + r")\b|[ \t]+(?P<otro>[^\r\n]+))"
)

# Conceptos que no son combustible y aparecen en el detalle aunque no esten
# en el catalogo compartido (productos.py): se excluyen sin incidencia.
_EXCLUIDOS_LOCALES = {
    "LUBRICANTES": "lubricantes (no combustible)",
    "PROMOCIONES": "promocion/descuento comercial (no combustible)",
}


def _extraer_numero_y_fecha_factura(paginas_texto, nombre_archivo, resultado):
    """
    Busca la etiqueta "Num. Factura" en las primeras paginas y, justo
    despues, su valor (A......... o RRA.........) seguido de la fecha.
    No depende del nombre de archivo. Devuelve (numero_factura, fecha)
    o (None, None) si no se puede determinar con seguridad.
    """
    paginas_a_mirar = paginas_texto[: min(6, len(paginas_texto))]

    pares_encontrados = set()  # {(numero, fecha_str)}
    etiqueta_encontrada = False

    for texto_pagina in paginas_a_mirar:
        for m_label in RE_LABEL_NUM_FACTURA.finditer(texto_pagina):
            etiqueta_encontrada = True
            ventana = texto_pagina[m_label.end() : m_label.end() + 60]
            m_num = RE_NUM_FACTURA_VALOR.search(ventana)
            if not m_num:
                continue
            resto = texto_pagina[m_label.end() + m_num.end() : m_label.end() + m_num.end() + 40]
            m_fecha = RE_FECHA.search(resto)
            if m_fecha:
                pares_encontrados.add((m_num.group(1), m_fecha.group(0)))

    if not etiqueta_encontrada:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Fecha de factura",
                f"No se ha encontrado la etiqueta 'Num. Factura' en las primeras {len(paginas_a_mirar)} "
                f"paginas del PDF.",
                "Revisar manualmente la portada de la factura.",
            )
        )
        return None, None

    if not pares_encontrados:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Fecha de factura",
                "Se encontro la etiqueta 'Num. Factura' pero no se pudo leer un numero de factura y una "
                "fecha inmediatamente despues.",
            )
        )
        return None, None

    numeros = {p[0] for p in pares_encontrados}
    if len(numeros) > 1:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Fecha de factura",
                f"Se han encontrado varios numeros de factura distintos junto a la etiqueta 'Num. Factura': "
                f"{sorted(numeros)}.",
                "Revisar manualmente la portada de la factura.",
            )
        )
        return None, None

    fechas = {p[1] for p in pares_encontrados}
    if len(fechas) > 1:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Fecha de factura",
                f"Se encontraron varias fechas distintas junto al numero de factura: {sorted(fechas)}.",
                "Revisar manualmente la portada de la factura para confirmar la fecha de emision correcta.",
            )
        )
        return None, None

    numero_factura = next(iter(numeros))
    fecha_str = next(iter(fechas))
    return numero_factura, datetime.strptime(fecha_str, "%d/%m/%Y").date()


def _localizar_matriculas(texto_completo):
    """Lista de (posicion_en_texto, matricula) en orden de aparicion."""
    out = []
    for m in RE_MATRICULA_HEADER.finditer(texto_completo):
        etiqueta = m.group(1).strip()
        # Alguna matricula real sale con un punto final ("1234ABC."): sin
        # el, es_matricula() la rechazaba y acababa en la hoja de
        # cuadrillas. Solo se limpia si con ello pasa a ser una matricula
        # valida; una etiqueta de cuadrilla ("LOIU JARD.") se deja intacta.
        if etiqueta.lower() == "conductor":
            # tarjeta sin matricula impresa: el regex se comio la etiqueta
            # de la columna siguiente ("Conductor") como si fuera el valor.
            etiqueta = "SIN MATRICULA"
        limpia = etiqueta.rstrip(" .,;:")
        if limpia != etiqueta and destinos.es_matricula(limpia):
            etiqueta = limpia
        out.append((m.start(), etiqueta))
    return out


def _matricula_para_posicion(matriculas, pos):
    actual = None
    for p, mat in matriculas:
        if p <= pos:
            actual = mat
        else:
            break
    return actual


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

    paginas_texto = [pagina.get_text() for pagina in doc]
    doc.close()

    texto_total = "\n".join(paginas_texto)
    if len(texto_total.strip()) < 50:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Archivo",
                "El PDF apenas contiene texto extraible (posible PDF escaneado, requeriria OCR).",
                "Revisar manualmente o procesar con OCR.",
            )
        )
        return resultado

    numero_factura, fecha_factura = _extraer_numero_y_fecha_factura(paginas_texto, nombre_archivo, resultado)
    if fecha_factura is None:
        resultado.procesado_ok = False
        return resultado

    if numero_factura.startswith("RRA"):
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tipo de documento",
                f"Este PDF es en si mismo una factura rectificativa ({numero_factura}), no una factura de "
                f"consumo normal. Se ha procesado igualmente (si no contiene combustible, no generara "
                f"registros).",
                "Revisar manualmente si esta rectificativa afecta a litros ya contabilizados en otra factura.",
            )
        )

    matriculas = _localizar_matriculas(texto_total)
    if not matriculas:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Matricula",
                "No se ha encontrado ninguna seccion 'N. de Matricula' en el PDF.",
            )
        )
        return resultado

    transacciones = list(RE_TRANSACCION.finditer(texto_total))
    if not transacciones:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tabla de consumos",
                "No se ha encontrado ninguna operacion con el patron 'DD/MM HH:MM <concepto>' en el PDF.",
            )
        )
        return resultado

    for idx, match in enumerate(transacciones):
        concepto = match.group("concepto") or match.group("otro").strip()
        matricula = _matricula_para_posicion(matriculas, match.start())
        if matricula is None:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Matricula",
                    f"Operacion {concepto!r} en la posicion {match.start()} del texto no tiene ninguna "
                    f"matricula asociada antes de ella.",
                )
            )
            continue

        categoria, motivo = productos.clasificar(concepto)
        if categoria == productos.DESCONOCIDO and concepto.upper() in _EXCLUIDOS_LOCALES:
            categoria, motivo = productos.EXCLUIDO, _EXCLUIDOS_LOCALES[concepto.upper()]

        # ventana de busqueda de litros: desde el final de este match hasta
        # el inicio de la siguiente transaccion (o 300 caracteres si es la ultima).
        fin_ventana = transacciones[idx + 1].start() if idx + 1 < len(transacciones) else match.end() + 300
        ventana = texto_total[match.end():fin_ventana]

        if categoria == productos.EXCLUIDO:
            resultado.excluidos.append((concepto, motivo, matricula))
            continue
        if categoria == productos.DESCONOCIDO:
            m_lit_desc = RE_LITROS_Y_PRECIO.search(ventana)
            litros_txt = f", {m_lit_desc.group('litros')} L" if m_lit_desc else ""
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tipo de producto",
                    f"Concepto {concepto!r} (matricula {matricula}, {match.group('dia_mes')}{litros_txt}) "
                    f"no esta en el catalogo de productos conocidos.",
                    "Clasificar manualmente y, si procede, anadir al catalogo (productos.py).",
                )
            )
            continue

        if categoria == productos.DIESEL_B100:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tipo de producto",
                    f"Concepto {concepto!r} (matricula {matricula}) se ha identificado como Diesel B100, "
                    f"pero la plantilla no tiene una tabla para B100.",
                    "Anadir manualmente en una tabla aparte, tal y como se solicito.",
                )
            )
            continue

        m_litros = RE_LITROS_Y_PRECIO.search(ventana)
        m_importe = RE_PAR_IMPORTES_IGUALES.search(ventana) if m_litros is None else None
        if m_importe is not None:
            # Solo trae importe en euros (sin litros): no es un consumo en
            # litros, asi que no entra en ninguna tabla.
            resultado.excluidos.append(
                (concepto, f"solo importe ({m_importe.group('a')} EUR), sin litros en la factura", matricula)
            )
            continue
        if m_litros is None:
            m_litros = RE_NUMERO.search(ventana)  # comportamiento anterior (formato no visto)
        if not m_litros:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Litros",
                    f"No se encontro un valor de litros para la operacion {concepto!r} "
                    f"(matricula {matricula}, fecha consumo {match.group('dia_mes')}).",
                )
            )
            continue

        litros = float((m_litros.groupdict().get("litros") or m_litros.group(0)).replace(".", "").replace(",", "."))

        # El campo "N. de Matricula" de SOLRED viene recortado a 10
        # caracteres y, cuando la tarjeta esta a nombre de una cuadrilla o
        # de un centro en vez de un vehiculo, trae ese nombre ("LOIU
        # JARD.", "SERV. MUSK"...). Esas lineas van a la hoja de
        # cuadrillas, no a la de vehiculos.
        destino, etiqueta = destinos.clasificar(matricula)
        if matricula == "SIN MATRICULA":
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Matricula",
                    f"La tarjeta de la operacion {concepto!r} ({match.group('dia_mes')}, {litros} L) no "
                    f"tiene matricula impresa.",
                    "Confirmar a que vehiculo corresponde.",
                )
            )
        elif not destino and not destinos.es_matricula(matricula):
            destino, etiqueta = destinos.CUADRILLAS, matricula

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
                fecha_consumo=f"{match.group('dia_mes')} {match.group('hora')}",
                origen_detalle="detalle de operaciones por tarjeta",
            )
        )

    return resultado
