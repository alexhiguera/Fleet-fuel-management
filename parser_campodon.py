"""
Parser para facturas de Estacion de Servicio Campodon, S.A. Solo aparece
en las carpetas de 2021-2022. Bajo el mismo nombre de proveedor conviven
TRES formatos de documento distintos, que se distinguen por CONTENIDO,
no por nombre de archivo:

  1. Facturas de ALQUILER del local de Bakio: no son combustible, se
     omiten (se detectan por contener "ALQUILER LOCAL").
  2. "Serie B" (numeros de factura tipo A2xxxxxxxx, ancla "Albaran n. X
     del DD/MM/YYYY"): factura de combustible a un deposito fijo
     ("Matricula BAKIO20100"), pero con un defecto grave de extraccion de
     texto de PyMuPDF: las columnas numericas (cantidad/precio/descuento/
     iva/importe) salen todas juntas DESPUES de todos los bloques
     descriptivos de la pagina, en vez de pegadas a su fila -> hay que
     reconstruir por POSICION (bloque i-esimo <-> valor i-esimo de cada
     columna), nunca por proximidad textual. Decimal con PUNTO (al reves
     que el resto del documento).
  2b. "Serie B antigua" (ancla "Fra.Simpl. N.: X  DD/MM/YYYY", solo vista
     en una factura de finales de 2021): mismo concepto de deposito fijo
     ("Mat.: BAKIO20100"), pero aqui SI que los 4 valores numericos
     (cantidad/precio/dto/importe) van pegados a su bloque, con decimal
     COMA -> se puede leer directamente por desplazamiento fijo desde la
     linea de producto, sin reconstruccion posicional.
  3. "Serie C" (numero de factura tipo CRTxxxxxxx): formato limpio estilo
     SOLRED/RESSA, agrupado por "N. de Matricula: <codigo>" (tambien un
     deposito fijo, nunca un vehiculo real). Decimal con COMA.

Ninguna de las series de combustible tiene un vehiculo real: el "codigo"
(p.ej. "BAKIO20100", "<CLIENTE>20100"...) es un deposito/tarjeta fija, se
usa tal cual como matricula (es el dato real del documento, no se inventa).
"""

import re
from datetime import datetime

import fitz

from modelos import Registro, Incidencia, ResultadoFactura
import productos
import destinos

RE_ALBARAN = re.compile(r"Albar.n\s*n.\s*(\d+)\s*del\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
# El campo viene como "Matricula BAKIO 20100          - 0 kms" (a veces
# sin espacio: "BAKIO20100", a veces vacio del todo). Capturar solo hasta
# el primer espacio partiria "BAKIO 20100" en "BAKIO", asi que se coge
# todo lo que hay entre la etiqueta y el "- N kms" final.
RE_MATRICULA_SERIE_B = re.compile(r"Matr.cula\s*(.*?)\s*-\s*\d*\s*kms", re.IGNORECASE)
RE_NUMERO_PUNTO = re.compile(r"^-?\d+\.\d+$")
RE_FECHA_FACTURA = re.compile(r"^\d{2}/\d{2}/\d{4}$")

RE_FRA_SIMPL = re.compile(r"Fra\.?\s*Simpl", re.IGNORECASE)
RE_MAT_ANTIGUA = re.compile(r"Mat\.\s*:?\s*(\S.*?)\s*$", re.IGNORECASE)
RE_LABEL_MATRICULA_SERIE_C = re.compile(r"^N.\s*de\s*Matr.cula\s*:?\s*$", re.IGNORECASE | re.MULTILINE)
RE_FECHA_HORA_SERIE_C = re.compile(r"^\d{2}-\d{2}-\d{4}\s+\d{1,2}:\d{2}$")
RE_NUMERO_COMA = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2,3}$")
RE_ENTERO = re.compile(r"^\d+$")
_PRODUCTOS_SERIE_C = ("avia innova 95", "avia innova diesel")
_PRODUCTOS_SERIE_B_ANTIGUA = ("i95", "idiesel")


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


def _normalizar_codigo(texto):
    """
    Limpia el codigo de deposito tal y como aparece en la factura: la
    misma instalacion se escribe "BAKIO20100", "BAKIO 20100" o
    "BAKIO 20100," segun la linea, y conviene que sea siempre el mismo
    valor para no partir el consumo en varios "vehiculos" distintos.
    """
    if not texto:
        return ""
    limpio = re.sub(r"\s+", "", texto.strip())
    return limpio.rstrip(",.;").upper()


def _extraer_fecha_factura(lineas):
    for l in lineas:
        if RE_FECHA_FACTURA.match(l.strip()):
            try:
                return datetime.strptime(l.strip(), "%d/%m/%Y").date()
            except ValueError:
                continue
    return None


def _clasificar_y_registrar(resultado, nombre_archivo, concepto, matricula, litros, fecha_factura, origen):
    categoria, motivo = productos.clasificar(concepto)
    if categoria == productos.EXCLUIDO:
        resultado.excluidos.append((concepto, motivo, matricula))
        return True
    if categoria == productos.DESCONOCIDO:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tipo de producto",
                f"Concepto {concepto!r} (matricula/deposito {matricula}) no esta en el catalogo de "
                f"productos conocidos.",
                "Clasificar manualmente y, si procede, anadir al catalogo (productos.py).",
            )
        )
        return False
    if categoria == productos.DIESEL_B100:
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tipo de producto",
                f"Concepto {concepto!r} (matricula/deposito {matricula}) se ha identificado como Diesel "
                f"B100, pero la plantilla no tiene una tabla para B100.",
                "Anadir manualmente en una tabla aparte, tal y como se solicito.",
            )
        )
        return False

    destino, etiqueta = destinos.clasificar(matricula)
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
            fecha_consumo="",
            origen_detalle=origen,
        )
    )
    return True


def _procesar_serie_b(paginas, nombre_archivo, fecha_factura, resultado):
    interpretados = 0
    matriculas_no_reales = set()

    for num_pagina, texto_pagina in enumerate(paginas):
        lineas = [l.strip() for l in texto_pagina.split("\n")]
        n = len(lineas)

        bloques = []  # (matricula, producto)
        fin_bloques = 0
        i = 0
        while i < n:
            m = RE_ALBARAN.search(lineas[i])
            if not m:
                i += 1
                continue
            if i + 2 >= n:
                break
            m_mat = RE_MATRICULA_SERIE_B.search(lineas[i + 1])
            matricula = _normalizar_codigo(m_mat.group(1)) if m_mat else ""
            matricula = matricula or "SIN MATRICULA"
            producto = lineas[i + 2].strip()
            bloques.append((matricula, producto))
            i += 3
            fin_bloques = i

        if not bloques:
            continue

        n_bloques = len(bloques)
        # localizar la primera linea numerica (formato con punto) despues
        # del ultimo bloque descriptivo; puede haber texto intermedio
        # (p.ej. "INFORMACION TOTALES") que se ignora.
        inicio_numeros = None
        for j in range(fin_bloques, n):
            if RE_NUMERO_PUNTO.match(lineas[j]):
                inicio_numeros = j
                break
        if inicio_numeros is None:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"Pagina {num_pagina + 1}: se han encontrado {n_bloques} albaranes pero ninguna "
                    f"columna de valores numericos a continuacion.",
                )
            )
            continue

        valores = []
        k = inicio_numeros
        while k < n and RE_NUMERO_PUNTO.match(lineas[k]):
            valores.append(float(lineas[k]))
            k += 1

        if len(valores) != 5 * n_bloques:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"Pagina {num_pagina + 1}: no se ha podido reconstruir la tabla de forma fiable "
                    f"({n_bloques} albaranes mencionados, pero {len(valores)} valores numericos "
                    f"encontrados en vez de {5 * n_bloques}).",
                    "Revisar manualmente esta pagina.",
                )
            )
            continue

        cantidades = valores[0:n_bloques]
        for idx, (matricula, producto) in enumerate(bloques):
            litros = cantidades[idx]
            if matricula == "SIN MATRICULA":
                matriculas_no_reales.add(matricula)
            ok = _clasificar_y_registrar(
                resultado,
                nombre_archivo,
                producto,
                matricula,
                litros,
                fecha_factura,
                f"pagina {num_pagina + 1}, albaran #{idx + 1} (layout de columnas reconstruido)",
            )
            if ok:
                interpretados += 1

    return interpretados


def _procesar_serie_b_antigua(texto_total, nombre_archivo, fecha_factura, resultado):
    lineas = [l.strip() for l in texto_total.split("\n")]
    n = len(lineas)
    interpretados = 0

    for p in range(n):
        if lineas[p].lower() not in _PRODUCTOS_SERIE_B_ANTIGUA:
            continue
        producto = lineas[p]
        if p + 1 >= n:
            continue
        cantidad_txt = lineas[p + 1]
        if not RE_NUMERO_COMA.match(cantidad_txt):
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Tabla de consumos",
                    f"No se pudo leer la cantidad del registro {producto!r} (linea {p}).",
                )
            )
            continue
        litros = float(cantidad_txt.replace(".", "").replace(",", "."))

        matricula = "SIN MATRICULA"
        if p - 1 >= 0:
            m_mat = RE_MAT_ANTIGUA.search(lineas[p - 1])
            if m_mat and m_mat.group(1):
                matricula = _normalizar_codigo(m_mat.group(1)) or "SIN MATRICULA"

        ok = _clasificar_y_registrar(
            resultado,
            nombre_archivo,
            producto,
            matricula,
            litros,
            fecha_factura,
            "factura simplificada (formato antiguo)",
        )
        if ok:
            interpretados += 1

    return interpretados


def _procesar_serie_c(texto_total, nombre_archivo, fecha_factura, resultado):
    lineas = [l.strip() for l in texto_total.split("\n")]
    n = len(lineas)

    bloques_matricula = []  # (indice_linea, codigo)
    for i, l in enumerate(lineas):
        if RE_LABEL_MATRICULA_SERIE_C.match(l) and i - 1 >= 0:
            bloques_matricula.append((i, _normalizar_codigo(lineas[i - 1]) or "SIN MATRICULA"))

    if not bloques_matricula:
        return 0

    def matricula_para(idx_linea):
        actual = "SIN MATRICULA"
        for idx_label, codigo in bloques_matricula:
            if idx_label <= idx_linea:
                actual = codigo
            else:
                break
        return actual

    interpretados = 0
    for i in range(n):
        if not RE_FECHA_HORA_SERIE_C.match(lineas[i]):
            continue
        if i + 2 >= n:
            continue
        concepto = lineas[i + 1]
        idx = i + 3  # +2 es el establecimiento, se ignora

        campos = []
        while idx < n and RE_NUMERO_COMA.match(lineas[idx]) and len(campos) < 3:
            campos.append(lineas[idx])
            idx += 1
        if len(campos) < 3:
            continue
        cantidad_txt = campos[0]

        if idx < n and RE_ENTERO.match(lineas[idx]):
            idx += 1  # Km opcional

        if idx >= n or not RE_NUMERO_COMA.match(lineas[idx]):
            continue

        litros = float(cantidad_txt.replace(".", "").replace(",", "."))
        matricula = matricula_para(i)

        ok = _clasificar_y_registrar(
            resultado,
            nombre_archivo,
            concepto,
            matricula,
            litros,
            fecha_factura,
            "extracto serie C (estilo SOLRED)",
        )
        if ok:
            interpretados += 1

    return interpretados


def parse(ruta_pdf: str, nombre_archivo: str) -> ResultadoFactura:
    resultado = ResultadoFactura(archivo=nombre_archivo)

    paginas, error = _abrir_paginas(ruta_pdf)
    if error:
        resultado.procesado_ok = False
        resultado.incidencias.append(Incidencia(nombre_archivo, "Archivo", error, "Revisar el archivo."))
        return resultado

    texto_total = "\n".join(paginas)

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

    if "ALQUILER LOCAL" in texto_total.upper():
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tipo de documento",
                "Factura de alquiler del local, no de combustible: se ha omitido.",
                "No requiere accion.",
            )
        )
        return resultado

    fecha_factura = _extraer_fecha_factura([l.strip() for l in texto_total.split("\n")])
    if fecha_factura is None:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(nombre_archivo, "Fecha de factura", "No se ha encontrado ninguna fecha de factura (DD/MM/YYYY) en el documento.")
        )
        return resultado

    es_serie_b_antigua = bool(RE_FRA_SIMPL.search(texto_total))
    es_serie_b = bool(RE_ALBARAN.search(texto_total))
    es_serie_c = bool(RE_LABEL_MATRICULA_SERIE_C.search(texto_total))

    if es_serie_b_antigua:
        interpretados = _procesar_serie_b_antigua(texto_total, nombre_archivo, fecha_factura, resultado)
    elif es_serie_b:
        interpretados = _procesar_serie_b(paginas, nombre_archivo, fecha_factura, resultado)
    elif es_serie_c:
        interpretados = _procesar_serie_c(texto_total, nombre_archivo, fecha_factura, resultado)
    else:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Archivo",
                "No se ha reconocido ninguno de los formatos conocidos de Campodon (alquiler, serie de "
                "albaranes, o extracto tipo SOLRED).",
                "Revisar manualmente el archivo.",
            )
        )
        return resultado

    if interpretados == 0:
        resultado.procesado_ok = False
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tabla de consumos",
                "No se ha podido interpretar ningun consumo de combustible en este documento.",
            )
        )
    # El combustible de estas facturas va siempre al deposito fijo de
    # Bakio: se vuelca en la hoja 'depositos', que ya lo deja claro, asi
    # que no se genera ninguna incidencia por ello.

    return resultado
