"""
Extraccion automatica de litros de combustible desde facturas PDF (varios
proveedores, ver parser_*.py) y volcado en una copia de la plantilla Excel
(hoja 'vehiculos').

Uso:
    python main.py
        Procesa todos los PDF de este mismo directorio, usando la
        plantilla 'tablas para informe.xlsx' de aqui mismo.

    python main.py --pdf-dir RUTA --template RUTA.xlsx --output RUTA.xlsx

Cada factura se procesa de forma independiente: un fallo en una no
impide procesar el resto. Nunca se inventa un dato: si algo no se
puede determinar con confianza, se deja fuera del Excel y se registra
en errores.md para revision manual.
"""

import argparse
import glob
import hashlib
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime

import parser_ortuella
import parser_solred
import parser_goiri
import parser_bidebarri
import parser_europoleo
import parser_vizcaina
import parser_starressa
import parser_repsolcomercial
import parser_campodon
import productos
import destinos
import validate
import excel_writer
from modelos import Incidencia, Registro, ResultadoFactura
from lecturas_manuales import LECTURAS_MANUALES

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PDF_DIR = SCRIPT_DIR
DEFAULT_TEMPLATE = os.path.join(DEFAULT_PDF_DIR, "tablas para informe.xlsx")
DEFAULT_OUTPUT = os.path.join(DEFAULT_PDF_DIR, "informe_combustibles.xlsx")
DEFAULT_ERRORES_MD = os.path.join(DEFAULT_PDF_DIR, "errores.md")
DEFAULT_LOG = os.path.join(SCRIPT_DIR, "log_trazabilidad.txt")

CATEGORIAS_EXCEL = (productos.GASOLINA_E5, productos.DIESEL_B7, productos.ADBLUE)
NOMBRES_CATEGORIA = {
    productos.GASOLINA_E5: "Gasolina E5",
    productos.DIESEL_B7: "Diesel B7",
    productos.ADBLUE: "AdBlue",
    productos.DIESEL_B100: "Diesel B100",
}


def _hash_archivo(ruta):
    h = hashlib.md5()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            h.update(bloque)
    return h.hexdigest()


def _elegir_duplicados_a_omitir(rutas_pdf):
    """
    Agrupa por hash MD5 exacto. Si hay duplicados byte-a-byte, se conserva
    UNA sola copia (preferentemente la que NO tenga sufijos tipicos de
    copia como '_OK', '_copia', '(1)') y se registra la decision como
    incidencia informativa. No se borra ningun archivo del disco: solo
    se excluye de este procesamiento.
    """
    por_hash = defaultdict(list)
    for ruta in rutas_pdf:
        por_hash[_hash_archivo(ruta)].append(ruta)

    a_omitir = set()
    incidencias = []
    sufijos_copia = ("_ok", "_copia", "_copy", "(1)", "(2)")

    for hash_, rutas in por_hash.items():
        if len(rutas) <= 1:
            continue
        rutas_ordenadas = sorted(rutas, key=lambda r: os.path.basename(r).lower())

        def parece_copia(r):
            base = os.path.splitext(os.path.basename(r))[0].lower()
            return any(base.endswith(s) for s in sufijos_copia)

        candidatos_originales = [r for r in rutas_ordenadas if not parece_copia(r)]
        conservado = candidatos_originales[0] if candidatos_originales else rutas_ordenadas[0]

        for ruta in rutas_ordenadas:
            if ruta != conservado:
                a_omitir.add(ruta)

        nombres = [os.path.basename(r) for r in rutas_ordenadas]
        incidencias.append(
            Incidencia(
                archivo=", ".join(nombres),
                campo="Archivo duplicado",
                problema=(
                    f"Estos {len(rutas)} archivos son identicos byte a byte (mismo contenido). "
                    f"Se ha procesado solo {os.path.basename(conservado)!r} para no duplicar registros."
                ),
                accion="Confirmar que la eleccion es correcta; si no, indicar que archivo usar.",
            )
        )

    return a_omitir, incidencias


def _letras(nombre):
    """Nombre en mayusculas, sin acentos y solo letras: 'EURÓPOLEO S.L.' -> 'EUROPOLEOSL'."""
    n = unicodedata.normalize("NFKD", nombre.upper())
    n = "".join(c for c in n if not unicodedata.combining(c))
    return re.sub(r"[^A-Z]", "", n)


def _detectar_proveedor(nombre_archivo):
    n = nombre_archivo.upper()
    letras = _letras(nombre_archivo)
    # El orden importa: hay facturas de EUROPOLEO/VIZCAINA que llevan el
    # nombre de la obra o del sitio en el nombre de archivo ("... ASASER
    # ORTUELLA.pdf") y no deben caer en el parser de Gasolinera Ortuella.
    # Ortuella, que es el nombre de un pueblo, se comprueba el ultimo.
    if "AUTOSERVICIOBILBAO" in letras:
        return "autoservicio"
    if "EUROPOLEO" in letras or "EUROPOELO" in letras:
        return "europoleo"
    if "VIZCAINA" in letras:
        return "vizcaina"
    # CAMPODON primero: su nombre viene con el acento mal codificado en
    # bastantes archivos (CAMP?D?N), asi que se tolera cualquier caracter
    # en las posiciones acentuadas en vez de exigir "CAMPODON" literal.
    if re.search(r"CAMP.D.N", n):
        return "campodon"
    if "SOLRED" in n:
        return "solred"
    if "GOIRI" in n:
        return "goiri"
    if "BIDEBARRI" in n:
        return "bidebarri"
    # tolera el acento de "EUROPOLEO" mal codificado en el nombre de
    # archivo (EUROP?LEO / EUROP�LEO).
    if re.search(r"EUROP.LEO", n):
        return "europoleo"
    # "RESSA" cubre tanto el nombre comercial antiguo "STAR RESSA" como el
    # nombre legal posterior "RED ESPANOLA DE SERVICIOS ... (RESSA)"; en
    # algunos archivos de 2024 el "(RESSA)" final se omite del todo, asi
    # que "RED ESPA?OLA" (tolerando el acento mal codificado) es el
    # respaldo para detectarlos igualmente.
    if "RESSA" in n or re.search(r"RED ESPA.OLA", n):
        return "starressa"
    if "REPSOL" in n:
        return "repsolcomercial"
    if "ORTUELLA" in n:
        return "ortuella"
    return None


# Proveedores cuya factura real esta repartida en dos archivos: uno de
# "portada" (solo totales, sin matricula) y uno de "detalle"/"extracto"
# (el que trae el desglose real por matricula). Al reves que el "(DETALLE)"
# de Ortuella (que es redundante y se descarta), aqui hace falta CONSERVAR
# y combinar ambos archivos.
PROVEEDORES_CON_PORTADA_Y_DETALLE = {"starressa", "repsolcomercial"}

# Sufijos de nombre de archivo que marcan el fichero de detalle/extracto de
# estos proveedores. Tolera variantes reales observadas: con o sin
# parentesis, con o sin espacio previo, "EXTRACTO DE CONSUMO CLIENTE"
# completo, y un typo real de origen "(EXTRACTO)pdf" con la extension
# duplicada dentro del nombre.
_RE_SUFIJO_PORTADA_DETALLE = re.compile(
    r"\s*[\(_]?\s*(EXTRACTO(?:\s+DE\s+CONSUMO\s+CLIENTE)?|DETALLE)\s*\)?\s*(?:pdf)?\s*$",
    re.IGNORECASE,
)
# Se ha visto al menos un archivo real donde "EXTRACTO DE CONSUMO CLIENTE"
# no va al final del nombre sino ANTES de la fecha (".. SA EXTRACTO DE
# CONSUMO CLIENTE 13-09-22.pdf"): _RE_SUFIJO_PORTADA_DETALLE no lo detecta
# por no estar anclado al final, asi que se comprueba tambien sin ancla.
_RE_MARCA_DETALLE_EN_CUALQUIER_POSICION = re.compile(
    r"EXTRACTO\s+DE\s+CONSUMO\s+CLIENTE", re.IGNORECASE
)


def _es_archivo_detalle(nombre_sin_extension):
    return bool(_RE_SUFIJO_PORTADA_DETALLE.search(nombre_sin_extension)) or bool(
        _RE_MARCA_DETALLE_EN_CUALQUIER_POSICION.search(nombre_sin_extension)
    )


def _normalizar_stem_para_emparejar(nombre_sin_extension):
    plano = _RE_SUFIJO_PORTADA_DETALLE.sub("", nombre_sin_extension).strip()
    return re.sub(r"\s+", " ", plano)


def _emparejar_portada_detalle(rutas):
    """
    Agrupa rutas de un mismo proveedor (ya filtradas de antemano) en pares
    (portada, detalle) comparando el nombre de archivo sin el sufijo
    "(EXTRACTO)"/"(DETALLE)" y con los espacios normalizados (se han visto
    pares con doble espacio en un archivo y espacio simple en el otro).
    Devuelve una lista de dicts {"cover": ruta|None, "detalle": ruta|None}.
    """
    grupos = {}
    for ruta in rutas:
        nombre = os.path.basename(ruta)
        stem = os.path.splitext(nombre)[0]
        clave = _normalizar_stem_para_emparejar(stem)
        grupo = grupos.setdefault(clave, {"cover": None, "detalle": None})
        if _es_archivo_detalle(stem):
            grupo["detalle"] = ruta
        else:
            grupo["cover"] = ruta
    return [grupos[clave] for clave in sorted(grupos.keys())]


_RE_SUFIJO_DETALLE = re.compile(r"\s*\(detalle\)", re.IGNORECASE)


def _detectar_adjuntos_detalle(rutas_pdf):
    """
    Algunas facturas de Ortuella vienen acompanadas de un PDF extra con
    sufijo '(DETALLE)': mismo consumo, mismas matriculas/fechas/litros,
    solo que reordenado y con totales por categoria anadidos. Son un
    documento redundante, no complementario -si se procesaran los dos
    se duplicarian todos los registros-, asi que se omite el adjunto
    '(DETALLE)' y se procesa unicamente la factura normal.

    Si un archivo '(DETALLE)' no tiene una factura normal homonima en la
    misma carpeta, NO se omite (no hay con que sustituirlo): se deja tal
    cual para que salga como incidencia (no hay parser para ese formato).
    """
    por_nombre = {os.path.basename(r): r for r in rutas_pdf}
    a_omitir = set()
    incidencias = []

    for ruta in rutas_pdf:
        nombre = os.path.basename(ruta)
        if not _RE_SUFIJO_DETALLE.search(nombre):
            continue
        nombre_plano = _RE_SUFIJO_DETALLE.sub("", nombre)
        if nombre_plano in por_nombre:
            a_omitir.add(ruta)
            incidencias.append(
                Incidencia(
                    archivo=f"{nombre}, {nombre_plano}",
                    campo="Archivo adjunto redundante",
                    problema=(
                        f"{nombre!r} es un desglose alternativo de la misma factura {nombre_plano!r} "
                        f"(mismas matriculas/fechas/litros, verificado). Se ha procesado solo la factura "
                        f"normal para no duplicar registros."
                    ),
                    accion="Confirmar que la eleccion es correcta.",
                )
            )
        else:
            incidencias.append(
                Incidencia(
                    archivo=nombre,
                    campo="Archivo adjunto redundante",
                    problema=(
                        f"{nombre!r} parece un desglose '(DETALLE)' pero no se ha encontrado la factura "
                        f"normal {nombre_plano!r} en la misma carpeta para usar en su lugar. No existe un "
                        f"parser para el formato '(DETALLE)', asi que no se ha podido procesar."
                    ),
                    accion="Revisar manualmente este archivo.",
                )
            )
            a_omitir.add(ruta)

    return a_omitir, incidencias


def _clave_normalizada_matricula(matricula):
    return re.sub(r"[^A-Za-z0-9]", "", matricula).upper()


def _descartar_facturas_duplicadas_por_contenido(resultados):
    """
    Descarta facturas completas cuyo CONJUNTO de registros coincide
    exactamente con el de otra factura de un archivo distinto (visto en
    VIZCAINA: la misma factura guardada por error en 2-3 archivos con
    bytes distintos pero el mismo contenido). Es un descarte, no solo un
    aviso: el usuario ha pedido explicitamente que las facturas
    duplicadas no cuenten dos veces. Los duplicados PARCIALES (una
    coincidencia suelta entre archivos, sin que coincida la factura
    entera) los sigue marcando validate.detectar_duplicados solo para
    revision manual, sin descartar nada.
    """
    incidencias = []
    grupos = defaultdict(list)
    for resultado in resultados:
        if not resultado.registros:
            continue
        clave = tuple(
            sorted(
                (
                    _clave_normalizada_matricula(reg.matricula),
                    reg.fecha_factura,
                    round(reg.litros, 2),
                    reg.producto,
                )
                for reg in resultado.registros
            )
        )
        grupos[clave].append(resultado)

    for lista in grupos.values():
        if len(lista) <= 1:
            continue
        lista_ordenada = sorted(lista, key=lambda r: r.archivo.lower())
        conservado = lista_ordenada[0]
        for resultado in lista_ordenada[1:]:
            n_registros = len(resultado.registros)
            resultado.registros = []
            incidencias.append(
                Incidencia(
                    resultado.archivo,
                    "Factura duplicada",
                    f"Esta factura tiene exactamente los mismos {n_registros} registro(s) (misma "
                    f"matricula/deposito, fecha, litros y producto) que {conservado.archivo!r}. Se ha "
                    f"descartado esta copia para no duplicar el consumo.",
                    "Confirmar que la eleccion es correcta; si no, indicar que factura usar.",
                )
            )
    return incidencias


_PROVEEDORES_FECHA_ALBARAN = {"europoleo", "vizcaina"}


def _fecha_albaran(texto):
    """'25-05-2021' o '25/05/2021' -> date; None si no es una fecha completa."""
    m = re.fullmatch(r"\s*(\d{2})[-/.](\d{2})[-/.](\d{4})\s*", texto or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _resultado_desde_lectura_manual(nombre_archivo, entrada):
    """
    Construye un ResultadoFactura a partir de una entrada de
    lecturas_manuales.py: PDF sin texto extraible (escaneado) cuyos datos
    se han transcrito a mano tras leer visualmente el documento.
    """
    resultado = ResultadoFactura(archivo=nombre_archivo)

    if entrada.get("excluido"):
        resultado.excluidos.append(("(lectura manual)", entrada["motivo_exclusion"], ""))
        resultado.incidencias.append(
            Incidencia(
                nombre_archivo,
                "Tipo de documento",
                "PDF escaneado, leido manualmente: " + entrada["motivo_exclusion"],
                "No requiere accion.",
            )
        )
        return resultado

    fecha_factura = entrada["fecha_factura"]
    # Criterio del usuario: en EUROPOLEO y VIZCAINA la fecha que cuenta es
    # la del albaran (entrega), no la de emision de la factura.
    usar_albaran = _detectar_proveedor(nombre_archivo) in _PROVEEDORES_FECHA_ALBARAN
    for reg in entrada["registros"]:
        matricula = reg["matricula"]
        fecha_registro = (_fecha_albaran(reg.get("fecha_consumo")) if usar_albaran else None) or fecha_factura
        destino, etiqueta = destinos.clasificar(matricula)
        # Lo que va a garrafas/deposito/tarjeta no es una incidencia: se
        # registra en su propia hoja.
        if not destinos.es_matricula(matricula) and not destino:
            resultado.incidencias.append(
                Incidencia(
                    nombre_archivo,
                    "Matricula",
                    f"El registro de {reg.get('concepto', '')!r} (lectura manual) no tiene una matricula "
                    f"de vehiculo real (se ha usado {matricula!r}).",
                    "Confirmar que la ausencia de matricula es correcta.",
                )
            )
        resultado.registros.append(
            Registro(
                referencia=nombre_archivo,
                matricula=matricula,
                fecha_factura=fecha_registro,
                litros=reg["litros"],
                producto=reg["producto"],
                destino=destino,
                destino_detalle=etiqueta,
                concepto_original=reg.get("concepto", ""),
                fecha_consumo=reg.get("fecha_consumo", ""),
                origen_detalle="lectura manual (PDF escaneado, sin texto extraible)",
            )
        )

    resultado.incidencias.append(
        Incidencia(
            nombre_archivo,
            "Archivo",
            "Este PDF es una imagen escaneada sin texto extraible: los datos se han introducido a mano "
            "tras leer visualmente el documento (ver lecturas_manuales.py).",
            "Confirmar que los valores transcritos son correctos.",
        )
    )
    return resultado


_PARSERS_UN_ARCHIVO = {
    "solred": parser_solred.parse,
    "ortuella": parser_ortuella.parse,
    "goiri": parser_goiri.parse,
    "bidebarri": parser_bidebarri.parse,
    "europoleo": parser_europoleo.parse,
    "vizcaina": parser_vizcaina.parse,
    "campodon": parser_campodon.parse,
}

_PARSERS_PORTADA_Y_DETALLE = {
    "starressa": parser_starressa.parse,
    "repsolcomercial": parser_repsolcomercial.parse,
}


def _encaminar(reg):
    """
    Devuelve (destino, registro) definitivo de un registro ya validado:
      - GLP y otros combustibles van a su propia hoja.
      - El AdBlue va SIEMPRE a la tabla de AdBlue de 'vehiculos', aunque no
        tenga matricula (se usa la etiqueta de deposito/envase/tarjeta).
    """
    if reg.producto == productos.GLP:
        return destinos.GLP, replace(reg, destino_detalle=reg.matricula)
    if reg.producto == productos.OTRO_COMBUSTIBLE:
        return destinos.OTROS_COMBUSTIBLES, replace(reg, destino_detalle=reg.matricula)
    if reg.producto == productos.ADBLUE and reg.destino:
        return "", replace(reg, matricula=reg.destino_detalle or reg.matricula, destino="", destino_detalle="")
    if reg.destino and "CAMION DE LIMPIEZA" in (reg.destino_detalle or reg.matricula).upper():
        return destinos.CAMION_LIMPIEZA, reg
    return reg.destino, reg


def procesar_directorio(pdf_dir):
    # Recursivo: algunos clientes reparten los PDF de cada anyo en
    # subcarpetas por proveedor en vez de dejarlos todos sueltos.
    rutas_pdf = sorted(glob.glob(os.path.join(pdf_dir, "**", "*.pdf"), recursive=True))
    incidencias_globales = []

    a_omitir, inc_dup = _elegir_duplicados_a_omitir(rutas_pdf)
    incidencias_globales.extend(inc_dup)

    # El adjunto "(DETALLE)" redundante solo se ha observado en Ortuella:
    # se limita a esos archivos para no interferir con RESSA/REPSOL, donde
    # un archivo "DETALLE"/"EXTRACTO" es justo el que SI hace falta leer.
    rutas_ortuella = [r for r in rutas_pdf if _detectar_proveedor(os.path.basename(r)) == "ortuella"]
    a_omitir_detalle, inc_detalle = _detectar_adjuntos_detalle(rutas_ortuella)
    a_omitir |= a_omitir_detalle
    incidencias_globales.extend(inc_detalle)

    rutas_restantes = [r for r in rutas_pdf if r not in a_omitir]

    rutas_por_proveedor_pareado = defaultdict(list)
    rutas_individuales = []
    for ruta in rutas_restantes:
        proveedor = _detectar_proveedor(os.path.basename(ruta))
        if proveedor in _PARSERS_PORTADA_Y_DETALLE:
            rutas_por_proveedor_pareado[proveedor].append(ruta)
        else:
            rutas_individuales.append((ruta, proveedor))

    resultados = []

    for ruta, proveedor in rutas_individuales:
        nombre = os.path.basename(ruta)
        if nombre in LECTURAS_MANUALES:
            resultado = _resultado_desde_lectura_manual(nombre, LECTURAS_MANUALES[nombre])
            resultados.append(resultado)
            continue

        parse_fn = _PARSERS_UN_ARCHIVO.get(proveedor)
        if parse_fn is not None:
            resultado = parse_fn(ruta, nombre)
        else:
            resultado = ResultadoFactura(archivo=nombre, procesado_ok=False)
            resultado.incidencias.append(
                Incidencia(
                    nombre,
                    "Proveedor",
                    "No se ha podido identificar el proveedor a partir del nombre de archivo. No existe "
                    "un parser para este formato.",
                    "Anadir un parser especifico para este proveedor o revisar el archivo manualmente.",
                )
            )
        resultados.append(resultado)

    for proveedor, rutas in rutas_por_proveedor_pareado.items():
        parse_fn = _PARSERS_PORTADA_Y_DETALLE[proveedor]
        for par in _emparejar_portada_detalle(rutas):
            cover = par["cover"]
            detalle = par["detalle"]
            nombre = os.path.basename(cover) if cover else os.path.basename(detalle)
            # Si el PDF esta escaneado y ya se ha transcrito a mano, se usa la
            # transcripcion (igual que en el bucle de archivos individuales).
            nombre_manual = next(
                (os.path.basename(r) for r in (cover, detalle) if r and os.path.basename(r) in LECTURAS_MANUALES),
                None,
            )
            if nombre_manual:
                resultados.append(
                    _resultado_desde_lectura_manual(nombre_manual, LECTURAS_MANUALES[nombre_manual])
                )
                continue
            resultados.append(parse_fn(cover, detalle, nombre))

    incidencias_globales.extend(_descartar_facturas_duplicadas_por_contenido(resultados))
    incidencias_globales.extend(validate.detectar_duplicados(resultados))
    incidencias_globales.extend(validate.chequear_litros_atipicos(resultados))
    incidencias_globales.extend(
        validate.verificar_archivos_no_repetidos([os.path.basename(r) for r in rutas_pdf])
    )

    return rutas_pdf, a_omitir, resultados, incidencias_globales


_CAMPOS_INFORMATIVOS = {
    "Tipo de documento",
    "Archivo duplicado",
    "Archivo adjunto redundante",
    "Factura duplicada",
    "Lugar de entrega",
}


def _es_informativa(inc):
    """
    Avisos que no piden ninguna accion: documentos leidos a mano, facturas
    excluidas por no ser combustible, duplicados ya descartados, lineas sin
    matricula (decision del usuario: se quedan en 'vehiculos' como SIN
    MATRICULA) y entregas sin lugar concreto. Van al log de trazabilidad,
    no a errores.md.
    """
    if inc.campo in _CAMPOS_INFORMATIVOS:
        return True
    if inc.campo == "Archivo" and "introducido a mano" in inc.problema:
        return True
    if inc.campo == "Matricula" and "SIN MATRICULA" in inc.problema:
        return True
    return False


def _incidencias_por_archivo(resultados, incidencias_globales):
    pendientes = defaultdict(list)
    informativas = defaultdict(list)
    todas = [(r.archivo, inc) for r in resultados for inc in r.incidencias]
    todas += [(inc.archivo, inc) for inc in incidencias_globales]
    for archivo, inc in todas:
        (informativas if _es_informativa(inc) else pendientes)[archivo].append(inc)
    return pendientes, informativas


def escribir_errores_md(ruta_salida, resultados, incidencias_globales):
    por_archivo, _ = _incidencias_por_archivo(resultados, incidencias_globales)
    total = sum(len(v) for v in por_archivo.values())

    with open(ruta_salida, "w", encoding="utf-8") as f:
        f.write("# Errores y cosas a revisar\n\n")
        if total == 0:
            f.write("No se han detectado incidencias.\n")
            return

        for archivo in sorted(por_archivo.keys()):
            f.write(f"## {archivo}\n\n")
            for inc in por_archivo[archivo]:
                f.write(f"- **Campo:** {inc.campo}\n")
                f.write(f"- **Problema:** {inc.problema}\n")
                f.write(f"- **Accion:** {inc.accion}\n\n")


def escribir_log_trazabilidad(ruta_salida, resultados, incidencias_globales=()):
    _, informativas = _incidencias_por_archivo(resultados, incidencias_globales)
    with open(ruta_salida, "w", encoding="utf-8") as f:
        f.write(f"Log de trazabilidad - generado {datetime.now().isoformat()}\n")
        f.write("=" * 80 + "\n\n")
        for resultado in resultados:
            f.write(f"ARCHIVO: {resultado.archivo}\n")
            f.write(f"  procesado_ok: {resultado.procesado_ok}\n")
            f.write(f"  registros: {len(resultado.registros)}\n")
            f.write(f"  excluidos (no combustible): {len(resultado.excluidos)}\n")
            f.write(f"  incidencias: {len(resultado.incidencias)}\n")
            for reg in resultado.registros:
                f.write(
                    f"    - {reg.producto:12s} matricula={reg.matricula:12s} litros={reg.litros:8.2f} "
                    f"fecha_factura={reg.fecha_factura} concepto_original={reg.concepto_original!r} "
                    f"fecha_consumo={reg.fecha_consumo!r} origen={reg.origen_detalle!r}\n"
                )
            for concepto, motivo, matricula in resultado.excluidos:
                f.write(f"    x EXCLUIDO concepto={concepto!r} motivo={motivo!r} matricula={matricula!r}\n")
            f.write("\n")

        if informativas:
            f.write("=" * 80 + "\n")
            f.write("NOTAS INFORMATIVAS (no requieren accion)\n")
            f.write("=" * 80 + "\n\n")
            for archivo in sorted(informativas):
                f.write(f"{archivo}\n")
                for inc in informativas[archivo]:
                    f.write(f"  - [{inc.campo}] {inc.problema}\n")
                f.write("\n")


def imprimir_resumen(rutas_pdf, a_omitir, resultados, incidencias_globales, escritas):
    encontrados = len(rutas_pdf)
    procesados = len(resultados)
    con_incidencias_archivos = set()
    for resultado in resultados:
        if resultado.incidencias:
            con_incidencias_archivos.add(resultado.archivo)
    for inc in incidencias_globales:
        for nombre in inc.archivo.split(", "):
            con_incidencias_archivos.add(nombre)

    correctos = procesados - len([r for r in resultados if r.archivo in con_incidencias_archivos])
    pendientes, _ = _incidencias_por_archivo(resultados, incidencias_globales)
    total_incidencias = sum(len(v) for v in pendientes.values())

    print("\n" + "=" * 60)
    print("RESUMEN DEL PROCESAMIENTO")
    print("=" * 60)
    print(f"PDFs encontrados:                {encontrados}")
    print(f"PDFs omitidos (duplicados/adjuntos redundantes): {len(a_omitir)}")
    print(f"PDFs procesados:                  {procesados}")
    print(f"  - sin incidencias:              {correctos}")
    print(f"  - con incidencias:              {len(con_incidencias_archivos)}")
    print()
    for cat in CATEGORIAS_EXCEL:
        print(f"Registros {NOMBRES_CATEGORIA[cat]:12s}: {escritas.get(cat, 0)}")
    for destino in destinos.HOJAS_EXTRA:
        if escritas.get(destino):
            print(f"Registros fuera de vehiculos, hoja '{destino}': {escritas[destino]}")
    print(f"Incidencias / registros pendientes de revision (ver errores.md): {total_incidencias}")
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf-dir", default=DEFAULT_PDF_DIR)
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--errores-md", default=DEFAULT_ERRORES_MD)
    ap.add_argument("--log", default=DEFAULT_LOG)
    args = ap.parse_args()

    if not os.path.isfile(args.template):
        print(f"ERROR: no se encuentra la plantilla: {args.template}", file=sys.stderr)
        sys.exit(1)

    rutas_pdf, a_omitir, resultados, incidencias_globales = procesar_directorio(args.pdf_dir)

    if not rutas_pdf:
        print(f"AVISO: no se ha encontrado ningun PDF en {args.pdf_dir}")

    registros_por_categoria = defaultdict(list)
    registros_por_destino = defaultdict(list)
    for resultado in resultados:
        for reg in resultado.registros:
            destino, reg = _encaminar(reg)
            if destino:
                registros_por_destino[destino].append(reg)
            else:
                registros_por_categoria[reg.producto].append(reg)

    escritas = excel_writer.escribir(
        args.template, args.output, registros_por_categoria, registros_por_destino
    )

    escribir_errores_md(args.errores_md, resultados, incidencias_globales)
    escribir_log_trazabilidad(args.log, resultados, incidencias_globales)

    imprimir_resumen(rutas_pdf, a_omitir, resultados, incidencias_globales, escritas)

    print(f"\nExcel generado en: {args.output}")
    print(f"Incidencias en:     {args.errores_md}")
    print(f"Log de trazabilidad: {args.log}")


if __name__ == "__main__":
    main()
