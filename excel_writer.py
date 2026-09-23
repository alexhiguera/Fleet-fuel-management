"""
Escritura de los registros extraidos en la plantilla XLSX real, hoja
'vehiculos'.

La plantilla ha cambiado de estructura entre 2022 y 2023 (columnas
distintas, con o sin columna 'a' / 'TIPO DE VEHICULO', celdas combinadas
en tamanos y sitios distintos). Para no depender de un layout fijo -y
poder reutilizar el codigo con plantillas futuras sin tocarlo- las
columnas de cada bloque (Gasolina E5 / Diesel B7 / AdBlue) se detectan
automaticamente leyendo los encabezados reales de la fila de cabecera,
en vez de asumir letras de columna concretas.

Encabezados reconocidos dentro de cada bloque (busqueda por texto,
insensible a mayusculas/acentos):
  - "Ref interna"          -> columna de referencia (nombre del PDF)
  - "de"                   -> columna de fecha (fecha de factura)
  - "a"                    -> columna de fecha "hasta", si existe
                               (se rellena con la misma fecha de factura)
  - que contenga "matricula"
  - que contenga "litros"
  - que contenga "tipo de vehiculo" -> se detecta pero se deja SIEMPRE
    en blanco: las facturas no indican el tipo de vehiculo y no se
    inventa ese dato (decision del usuario).

Las celdas combinadas dentro del rango de datos de cada bloque se
separan siempre antes de escribir (independientemente de en que filas
esten combinadas), porque impiden escribir un valor distinto por fila
de consumo.
"""

import copy
import re
import unicodedata

import openpyxl

import productos
import destinos

HOJA = "vehiculos"

NOMBRES_PRODUCTO = {
    productos.GASOLINA_E5: "Gasolina E5",
    productos.DIESEL_B7: "Diesel B7",
    productos.DIESEL_B100: "Diesel B100",
    productos.ADBLUE: "AdBlue",
    productos.GLP: "GLP (autogas)",
}

_CATEGORIA_TITULOS = {
    productos.GASOLINA_E5: ("gasolina e5", "gasolina"),
    productos.DIESEL_B7: ("diesel b7",),
    productos.ADBLUE: ("adblue",),
}

_MAX_FILA_BUSQUEDA_CABECERA = 30
_MAX_FILA_BUSQUEDA_MERGES = 400


def _clave_matricula(matricula) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(matricula).upper())


def _norm(texto) -> str:
    if texto is None:
        return ""
    texto = str(texto).strip().lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.split())


def _localizar_bloques(ws):
    """
    Devuelve {categoria: {"referencia": col, "fecha_desde": col,
    "fecha_hasta": col|None, "matricula": col, "litros": col,
    "tipo_vehiculo": col|None}} y la fila de cabecera detectada.
    """
    fila_cabecera = None
    anclas = {}  # categoria -> columna del titulo del bloque

    for row in ws.iter_rows(min_row=1, max_row=_MAX_FILA_BUSQUEDA_CABECERA, max_col=ws.max_column):
        for cell in row:
            texto = _norm(cell.value)
            if not texto:
                continue
            for categoria, alias in _CATEGORIA_TITULOS.items():
                if texto in alias and categoria not in anclas:
                    anclas[categoria] = cell.column
                    fila_cabecera = cell.row

    if not anclas:
        raise ValueError(
            "No se han encontrado los titulos de bloque ('GASOLINA E5', 'DIESEL B7', 'ADBLUE') "
            "en las primeras filas de la hoja 'vehiculos'. Revisa manualmente la plantilla."
        )

    columnas_ancla_ordenadas = sorted(anclas.values())

    bloques = {}
    for categoria, col_ancla in anclas.items():
        idx = columnas_ancla_ordenadas.index(col_ancla)
        col_fin = (
            columnas_ancla_ordenadas[idx + 1] - 1
            if idx + 1 < len(columnas_ancla_ordenadas)
            else ws.max_column
        )

        campos = {"referencia": None, "fecha_desde": None, "fecha_hasta": None,
                  "matricula": None, "litros": None, "tipo_vehiculo": None}

        for col in range(col_ancla, col_fin + 1):
            texto = _norm(ws.cell(row=fila_cabecera, column=col).value)
            if not texto:
                continue
            if texto == "ref interna":
                campos["referencia"] = col
            elif texto == "de":
                campos["fecha_desde"] = col
            elif texto == "a":
                campos["fecha_hasta"] = col
            elif "tipo de vehiculo" in texto:
                campos["tipo_vehiculo"] = col
            elif "matricula" in texto:
                campos["matricula"] = col
            elif "litros" in texto:
                campos["litros"] = col

        faltan = [k for k in ("referencia", "fecha_desde", "matricula", "litros") if campos[k] is None]
        if faltan:
            raise ValueError(
                f"En el bloque {categoria!r} de la plantilla no se han encontrado las columnas: {faltan}. "
                f"Revisa los encabezados de la fila {fila_cabecera}."
            )

        bloques[categoria] = campos

    return bloques, fila_cabecera


def _separar_celdas_combinadas(ws, columnas_relevantes, fila_inicio):
    columnas_relevantes = set(columnas_relevantes)
    for merged in list(ws.merged_cells.ranges):
        if merged.min_row < fila_inicio or merged.min_row > _MAX_FILA_BUSQUEDA_MERGES:
            continue
        if merged.min_col in columnas_relevantes or merged.max_col in columnas_relevantes:
            ws.unmerge_cells(str(merged))


def _clonar_estilo_fila(ws, fila_origen, fila_destino, columnas):
    for col in columnas:
        origen = ws.cell(row=fila_origen, column=col)
        destino = ws.cell(row=fila_destino, column=col)
        destino.font = copy.copy(origen.font)
        destino.border = copy.copy(origen.border)
        destino.fill = copy.copy(origen.fill)
        destino.alignment = copy.copy(origen.alignment)
        destino.number_format = origen.number_format
    ws.row_dimensions[fila_destino].height = ws.row_dimensions[fila_origen].height


def _formato_litros(litros: float) -> str:
    """Cadena con coma decimal, igual que en las facturas de origen."""
    return f"{litros:.2f}".replace(".", ",")


def _nombre_producto(reg):
    if reg.producto == productos.OTRO_COMBUSTIBLE:
        return reg.concepto_original or "Otro combustible"
    return NOMBRES_PRODUCTO.get(reg.producto, reg.producto)


def _escribir_hoja_extra(wb, nombre_hoja, titulo_etiqueta, registros, indice):
    """
    Hoja aparte para el combustible que NO es consumo de un vehiculo
    identificable (garrafas, depositos fijos, tarjetas sin matricula,
    cuadrillas). La plantilla no trae estas hojas: se crean desde cero
    detras de 'vehiculos'.
    """
    if nombre_hoja in wb.sheetnames:
        del wb[nombre_hoja]
    ws = wb.create_sheet(nombre_hoja, indice)
    titulo_columna_cantidad = "Cant. de combustible (litros)"

    cabeceras = [
        "Fecha factura",
        "Ref interna (factura)",
        "Producto",
        titulo_columna_cantidad,
        titulo_etiqueta,
        "Fecha de consumo",
    ]
    anchos = [14, 62, 14, 26, 34, 20]
    negrita = openpyxl.styles.Font(bold=True)
    borde_fino = openpyxl.styles.Side(style="thin")
    borde = openpyxl.styles.Border(left=borde_fino, right=borde_fino, top=borde_fino, bottom=borde_fino)
    relleno_cabecera = openpyxl.styles.PatternFill("solid", fgColor="D9D9D9")

    for col, (titulo, ancho) in enumerate(zip(cabeceras, anchos), start=1):
        celda = ws.cell(row=1, column=col, value=titulo)
        celda.font = negrita
        celda.border = borde
        celda.fill = relleno_cabecera
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = ancho

    registros = sorted(registros, key=lambda r: (r.fecha_factura, r.destino_detalle, r.referencia))

    fila = 2
    for reg in registros:
        celda_fecha = ws.cell(row=fila, column=1, value=reg.fecha_factura)
        celda_fecha.number_format = "DD/MM/YYYY"
        ws.cell(row=fila, column=2, value=reg.referencia)
        ws.cell(row=fila, column=3, value=_nombre_producto(reg))
        celda_litros = ws.cell(row=fila, column=4, value=round(reg.litros, 2))
        celda_litros.number_format = "#,##0.00"
        ws.cell(row=fila, column=5, value=reg.destino_detalle)
        ws.cell(row=fila, column=6, value=reg.fecha_consumo)
        for col in range(1, len(cabeceras) + 1):
            ws.cell(row=fila, column=col).border = borde
        fila += 1

    # Totales por producto al final de la tabla.
    if registros:
        fila += 1
        ws.cell(row=fila, column=3, value="TOTAL por producto").font = negrita
        fila += 1
        por_producto = {}
        for reg in registros:
            nombre = _nombre_producto(reg)
            por_producto[nombre] = por_producto.get(nombre, 0.0) + reg.litros
        for nombre, litros in sorted(por_producto.items()):
            ws.cell(row=fila, column=3, value=nombre)
            celda = ws.cell(row=fila, column=4, value=round(litros, 2))
            celda.number_format = "#,##0.00"
            celda.font = negrita
            fila += 1

    ws.freeze_panes = "A2"
    return len(registros)


def escribir(ruta_plantilla: str, ruta_salida: str, registros_por_categoria: dict, registros_por_destino=None):
    """
    registros_por_categoria: dict {categoria: [Registro, ...]} para la
    hoja 'vehiculos'.
    registros_por_destino: dict {destino: [Registro, ...]} con lo que no
    es consumo de un vehiculo identificable (ver destinos.py); cada
    destino se vuelca en su propia hoja detras de 'vehiculos'.
    Devuelve un dict {categoria: num_filas_escritas} mas una clave por
    cada hoja extra escrita.
    """
    wb = openpyxl.load_workbook(ruta_plantilla)
    ws = wb[HOJA]

    bloques, fila_cabecera = _localizar_bloques(ws)
    fila_inicio = fila_cabecera + 1

    todas_las_columnas = sorted(
        {col for campos in bloques.values() for col in campos.values() if col is not None}
    )
    _separar_celdas_combinadas(ws, todas_las_columnas, fila_inicio)

    escritas = {}

    # Una matricula que repostó tanto gasolina como diesel se considera
    # maquinaria: en la tabla de gasolina se marca en "TIPO DE VEHICULO".
    matriculas_diesel = {
        _clave_matricula(r.matricula) for r in registros_por_categoria.get(productos.DIESEL_B7, [])
    }
    matriculas_maquinaria = {
        _clave_matricula(r.matricula)
        for r in registros_por_categoria.get(productos.GASOLINA_E5, [])
        if destinos.es_matricula(r.matricula)
    } & matriculas_diesel

    for categoria, campos in bloques.items():
        registros = registros_por_categoria.get(categoria, [])
        registros = sorted(registros, key=lambda r: (r.fecha_factura, r.matricula, r.referencia))

        columnas_bloque = [c for c in campos.values() if c is not None]

        fila = fila_inicio
        for reg in registros:
            if fila > fila_inicio:
                # Se clona siempre desde la primera fila de datos: es la
                # unica que conserva con seguridad el formato correcto en
                # todas las columnas (las celdas combinadas de la
                # plantilla dejan 'General' en las filas que no son ancla).
                _clonar_estilo_fila(ws, fila_inicio, fila, columnas_bloque)

            ws.cell(row=fila, column=campos["referencia"], value=reg.referencia)
            celda_desde = ws.cell(row=fila, column=campos["fecha_desde"], value=reg.fecha_factura)
            if campos["fecha_hasta"] is not None:
                celda_hasta = ws.cell(row=fila, column=campos["fecha_hasta"], value=reg.fecha_factura)
                # openpyxl reformatea automaticamente una celda con formato de
                # texto ('@') al asignarle una fecha; se iguala al formato de
                # la columna "de" para que ambas fechas se vean igual.
                celda_hasta.number_format = celda_desde.number_format
            # tipo_vehiculo: las facturas no lo indican; solo se rellena la
            # clasificacion "Maquinaria" (gasolina + diesel en la misma matricula).
            if (
                categoria == productos.GASOLINA_E5
                and campos["tipo_vehiculo"] is not None
                and _clave_matricula(reg.matricula) in matriculas_maquinaria
            ):
                ws.cell(row=fila, column=campos["tipo_vehiculo"], value="Maquinaria")
            ws.cell(row=fila, column=campos["matricula"], value=reg.matricula)
            ws.cell(row=fila, column=campos["litros"], value=_formato_litros(reg.litros))
            fila += 1

        escritas[categoria] = len(registros)

    registros_por_destino = registros_por_destino or {}
    indice = wb.sheetnames.index(HOJA) + 1 if HOJA in wb.sheetnames else len(wb.sheetnames)
    for destino in destinos.HOJAS_EXTRA:
        registros_destino = registros_por_destino.get(destino, [])
        if not registros_destino:
            continue
        escritas[destino] = _escribir_hoja_extra(
            wb, destino, destinos.TITULO_ETIQUETA[destino], registros_destino, indice
        )
        indice += 1

    wb.save(ruta_salida)
    return escritas
