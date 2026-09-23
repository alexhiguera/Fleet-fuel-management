"""
Parser para facturas de GASOLINERA ORTUELLA S.L. Coexisten DOS
maquetaciones distintas segun la fecha, detectadas por contenido (no por
nombre de archivo):

  - Layout CLASICO (hasta junio 2024), una sola pagina, tabla lineal
    Vehiculo/Concepto/Fecha/Pvp/Cantidad/Dto/Importe:

        1234ABC
        Gna 95
        18/01/2022 18:52
        1,609€
        50,46
        -2,436€
        78,75€

        Factura  N°:  A15       31/01/2022    <- fecha de factura (cabecera)

  - Layout NUEVO (desde julio 2024), multi-pagina, con la fecha y la
    "matricula" combinadas en una sola linea y el orden de columnas
    cambiado (LITROS antes que PRECIO/DTO/IMPORTE), agrupado con un
    recap "SUBTOTAL POR MATRICULA:" tras cada bloque:

        CR5 1349                <- N. de albaran (informativo)
        11/06/2024 0101         <- fecha + "matricula" (aqui, codigo de ruta/tarjeta)
        Gna 95                  <- concepto
        3,20                    <- LITROS
        1,709€                  <- precio
        -0,164€                 <- dto
        5,30€                   <- importe
        0101                    <- repite el codigo, se ignora
        SUBTOTAL POR MATRICULA: <- recap del bloque, se ignora
        ...

En ambos layouts, el campo "Vehiculo"/"Matricula" no siempre es una
matricula real: bastantes facturas usan un codigo de tarjeta/ruta
numerico de 4-6 digitos (p.ej. "16103", "0101") en vez de la matricula, y
algunos meses TODOS los consumos de la factura vienen asi. Tambien
aparece a veces el texto "GARRAFAS" (repostado a bidones) o directamente
ningun identificador en absoluto. Por eso cada registro se ancla en la
linea de CONCEPTO/fecha conocida (nunca en la matricula, que es
opcional), igual que en parser_goiri.py.
"""

import re
from datetime import datetime

import fitz

from modelos import Registro, Incidencia, ResultadoFactura
import productos
import destinos

RE_PLATE = re.compile(r"^\d{4}[A-Z]{3}$")
RE_CODIGO_TARJETA = re.compile(r"^\d{4,6}$")
# Algunas tarjetas se imprimen con un sufijo decimal ("20133.01"), y en un
# par de casos el "0" sale como letra "O" (glitch de fuente, igual que el
# desplazamiento de caracteres visto en EUROPOLEO): se reconoce la forma,
# pero el texto se guarda tal cual, sin "corregir" la O por un 0.
RE_CODIGO_TARJETA_SUFIJO = re.compile(r"^\d{4,6}\.[0-9OoIl]{1,4}$")
# Nombre de la propia empresa o de una cuadrilla en vez de un vehiculo
# (p.ej. "ORTUELLA", "OBRA"): solo letras, ninguna cifra -> para no
# tragarse un codigo mixto y ambiguo tipo "1822H0", que se deja tal cual
# pendiente de revision manual.
RE_ETIQUETA_CUADRILLA = re.compile(r"^[A-Z][A-Z .]{2,}$")
RE_ETIQUETA_ZONA = re.compile(r"[A-Z]{4,}")
RE_FECHA_FACTURA_LINEA = re.compile(r"^Factura\b.*?(\d{2}/\d{2}/\d{4})\s*$")
# El simbolo de moneda no se extrae de forma fiable (algunos PDF lo
# devuelven como caracter de reemplazo U+FFFD segun la fuente incrustada),
# asi que no se exige un simbolo de moneda concreto: solo el numero y,
# opcionalmente, un unico caracter final no numerico (el simbolo, sea cual sea).
# El precio por litro (Pvp) se imprime con 3 decimales (p.ej. "1,609"),
# mientras que importes/descuentos usan 2 -> se aceptan ambos.
RE_IMPORTE_EUR = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2,3}\s*\S?\s*$")
RE_NUMERO = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
RE_FECHA_HORA = re.compile(r"^\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}(:\d{2})?\s*$")
RE_FECHA_Y_RESTO = re.compile(r"^(\d{2}/\d{2}/\d{4})(?:\s+(\S.*))?$")
RE_ENVASE_ADBLUE = re.compile(r"^Blue\+?\s+Envase\s+(\d+)\s*L$", re.IGNORECASE)
MARCA_LAYOUT_NUEVO = "SUBTOTAL POR MATR"

# Conceptos conocidos que pueden anclar un registro (case-insensitive).
# De mas largo a mas corto para evitar que un prefijo matchee antes.
_CONCEPTOS = sorted(
    ["Diesel E+10", "Gna 95", "Gna 98", "Diesel", "Adblue"], key=len, reverse=True
)


def _parse_litros(texto: str):
    """'50,46' -> 50.46. Devuelve None si no es un número reconocible."""
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
        return None, "no se encontro ninguna linea 'Factura ... DD/MM/YYYY' en el documento"
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


def _clasificar_matricula(candidato):
    """(matricula, es_real, destino, etiqueta) a partir del texto crudo."""
    if candidato is None:
        return "SIN MATRICULA", False, destinos.VEHICULOS, ""
    if destinos.es_matricula(candidato):
        return candidato, True, destinos.VEHICULOS, ""

    destino, etiqueta = destinos.clasificar(candidato)
    if destino:
        return etiqueta, False, destino, etiqueta
    if RE_CODIGO_TARJETA.match(candidato) or RE_CODIGO_TARJETA_SUFIJO.match(candidato):
        etiqueta = f"TARJETA {candidato}"
        return etiqueta, False, destinos.TARJETAS, etiqueta
    if RE_ETIQUETA_CUADRILLA.match(candidato.upper()):
        return candidato.upper(), False, destinos.CUADRILLAS, candidato.upper()
    # Zona/centro con cifra ("CABBZONA1", "CAP ZONA 1"): al menos 4 letras
    # seguidas y nada que ver con una matricula. Un codigo corto y mixto
    # tipo "1822H0" NO cumple esto y sigue pendiente de revision manual.
    if RE_ETIQUETA_ZONA.search(candidato.upper()):
        return candidato.upper(), False, destinos.CUADRILLAS, candidato.upper()
    return "SIN MATRICULA", False, destinos.VEHICULOS, ""


def _registrar(resultado, nombre_archivo, concepto, matricula, matricula_es_real, litros_txt, fecha_factura,
                fecha_consumo, origen_detalle, destino="", etiqueta=""):
    litros = _parse_litros(litros_txt)
    categoria, motivo = productos.clasificar(concepto)

    if categoria == productos.EXCLUIDO:
        resultado.excluidos.append((concepto, motivo, matricula))
        return True
    if categoria == productos.DESCONOCIDO:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tipo de producto",
                f"Concepto {concepto!r} (matricula {matricula}, litros {litros_txt}) no esta en el "
                f"catalogo de productos conocidos.",
                "Clasificar manualmente y, si procede, anadir al catalogo (productos.py).",
            )
        )
        return False
    if categoria == productos.DIESEL_B100:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tipo de producto",
                f"Concepto {concepto!r} (matricula {matricula}, litros {litros_txt}) se ha identificado "
                f"como Diesel B100, pero la plantilla no tiene una tabla para B100.",
                "Anadir manualmente en una tabla aparte, tal y como se solicito.",
            )
        )
        return False

    if litros is None:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Litros",
                f"No se pudo interpretar la cantidad {litros_txt!r} para la matricula {matricula}.",
            )
        )
        return False

    # Lo que va a garrafas/deposito/tarjeta no es una incidencia: se
    # registra en su propia hoja, que ya deja claro que no es un vehiculo.
    if not matricula_es_real and not destino:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Matricula",
                f"El registro de {concepto!r} con fecha de consumo {fecha_consumo!r} no tiene una "
                f"matricula de vehiculo real (se ha usado {matricula!r}).",
                "Confirmar que la ausencia de matricula es correcta (tarjeta/ruta compartida u otro caso).",
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
            fecha_consumo=fecha_consumo,
            origen_detalle=origen_detalle,
        )
    )
    return True


def _procesar_layout_clasico(lineas, nombre_archivo, fecha_factura, resultado):
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

        # "Adblue / Blue+ Envase 10L": el concepto ocupa dos lineas y la
        # cantidad son unidades de envase; los litros salen del propio
        # texto del envase (unidades x litros por envase), y este bloque
        # no trae linea de matricula.
        m_envase = RE_ENVASE_ADBLUE.match(lineas[c + 1]) if concepto == "Adblue" else None
        if m_envase and c + 6 < n:
            desplazamiento = 1
            litros_por_envase = int(m_envase.group(1))
        else:
            desplazamiento = 0
        fecha_hora = lineas[c + 1 + desplazamiento]
        pvp = lineas[c + 2 + desplazamiento]
        cantidad_txt = lineas[c + 3 + desplazamiento]
        dto = lineas[c + 4 + desplazamiento]
        importe = lineas[c + 5 + desplazamiento]

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

        origen = "tabla principal (layout clasico)"
        if desplazamiento:
            unidades = _parse_litros(cantidad_txt)
            cantidad_txt = f"{unidades * litros_por_envase:.2f}".replace(".", ",") if unidades is not None else cantidad_txt
            candidato = None
            origen += f", envase de {litros_por_envase} L x {unidades:g} ud"
        else:
            candidato = lineas[c - 1] if c - 1 >= 0 else None
        matricula, matricula_es_real, destino, etiqueta = _clasificar_matricula(candidato)
        bloques_interpretados += 1

        _registrar(
            resultado, nombre_archivo, concepto, matricula, matricula_es_real, cantidad_txt,
            fecha_factura, fecha_hora, origen, destino, etiqueta,
        )

    return bloques_interpretados


def _separar_codigo_y_concepto(resto):
    """
    'CAP ZONA 1 Adblue' -> ('CAP ZONA 1', 'Adblue'). Si el resto de la linea
    no termina en un concepto conocido devuelve (resto, None).
    """
    for concepto in _CONCEPTOS:
        sufijo = " " + concepto.lower()
        if resto.lower().endswith(sufijo):
            return resto[: -len(sufijo)].strip(), concepto
    return resto, None


def _procesar_layout_nuevo(lineas, nombre_archivo, fecha_factura, resultado):
    n = len(lineas)
    bloques_interpretados = 0

    for i in range(n):
        m = RE_FECHA_Y_RESTO.match(lineas[i])
        if not m:
            continue
        fecha_consumo, resto = m.group(1), (m.group(2) or "").strip()

        # Tres formas observadas de la linea "fecha + codigo":
        #   - "24/06/2024 5678BCD"          -> codigo en la misma linea, concepto en la siguiente
        #   - "13/08/2024"                  -> sin codigo/matricula (cabecera vacia)
        #   - "17/09/2024 CAP ZONA 1 Adblue"-> codigo (con espacios) y concepto en la misma linea
        candidato = resto or None
        concepto = None
        desplaza = 1  # indice (relativo a i) de la linea de litros
        if resto:
            codigo, concepto = _separar_codigo_y_concepto(resto)
            if concepto is not None:
                candidato = codigo or None
        if concepto is None:
            if i + 2 >= n:
                continue
            concepto = _es_linea_concepto(lineas[i + 1])
            if concepto is None:
                continue
            desplaza = 2
        elif i + 1 >= n:
            continue

        litros_txt = lineas[i + desplaza]
        if not RE_NUMERO.match(litros_txt):
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"El bloque que empieza en {lineas[i]!r} (concepto {concepto!r}) no tiene una cantidad "
                    f"de litros reconocible a continuacion (se leyo {litros_txt!r}).",
                )
            )
            continue

        matricula, matricula_es_real, destino, etiqueta = _clasificar_matricula(candidato)
        bloques_interpretados += 1

        _registrar(
            resultado, nombre_archivo, concepto, matricula, matricula_es_real, litros_txt,
            fecha_factura, fecha_consumo, "tabla principal (layout nuevo, 2024+)", destino, etiqueta,
        )

    return bloques_interpretados


def parse(ruta_pdf: str, nombre_archivo: str) -> ResultadoFactura:
    resultado = ResultadoFactura(archivo=nombre_archivo)

    try:
        doc = fitz.open(ruta_pdf)
    except Exception as exc:  # PDF corrupto / ilegible
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

    fecha_factura, error_fecha = _extraer_fecha_factura(lineas)
    if fecha_factura is None:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(nombre_archivo, "Fecha de factura", error_fecha)
        )
        # Sin fecha de factura fiable no generamos registros: mejor
        # dejarlo todo pendiente de revision que asignar una fecha erronea.
        return resultado

    if MARCA_LAYOUT_NUEVO in texto_total.upper():
        bloques_interpretados = _procesar_layout_nuevo(lineas, nombre_archivo, fecha_factura, resultado)
    else:
        bloques_interpretados = _procesar_layout_clasico(lineas, nombre_archivo, fecha_factura, resultado)

    if bloques_interpretados == 0:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tabla de consumos",
                "No se ha encontrado ningun bloque de consumo con un concepto reconocible en el documento.",
            )
        )

    return resultado
