"""Validaciones que cruzan datos entre facturas (duplicados, valores atipicos)."""

import re
import unicodedata

from modelos import Incidencia

LITROS_MIN_PLAUSIBLE = 2.0
LITROS_MAX_PLAUSIBLE = 400.0


def _normalizar_matricula(m: str) -> str:
    m = unicodedata.normalize("NFKD", m)
    m = "".join(c for c in m if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9]", "", m).upper()


def detectar_duplicados(resultados):
    """
    Compara TODOS los registros de TODAS las facturas ya procesadas y
    marca cuando un mismo (matricula normalizada, fecha de factura,
    litros, producto) aparece en DOS ARCHIVOS DISTINTOS, que es el caso
    peligroso: el mismo consumo facturado dos veces.

    Las repeticiones dentro de una MISMA factura no se marcan: repostar
    dos veces la misma cantidad el mismo dia con la misma tarjeta es
    normal (varias garrafas, varios vehiculos que comparten tarjeta,
    importes redondos), y solo generaban ruido. No se elimina nada
    automaticamente, solo se deja constancia para revision.
    """
    incidencias = []
    vistos = {}

    for resultado in resultados:
        for reg in resultado.registros:
            clave = (
                _normalizar_matricula(reg.matricula),
                reg.fecha_factura,
                round(reg.litros, 2),
                reg.producto,
            )
            vistos.setdefault(clave, []).append(reg)

    for clave, regs in vistos.items():
        referencias = sorted({r.referencia for r in regs})
        if len(referencias) <= 1:
            continue
        matricula, fecha, litros, producto = clave
        problema = (
            f"El mismo consumo (matricula {matricula}, fecha factura {fecha}, {litros} L, {producto}) "
            f"aparece en {len(referencias)} archivos distintos: {referencias}."
        )
        incidencias.append(
            Incidencia(
                archivo=", ".join(referencias),
                campo="Posible duplicado",
                problema=problema,
                accion="Revisar manualmente si son consumos distintos coincidentes o un duplicado real.",
            )
        )

    return incidencias


def chequear_litros_atipicos(resultados):
    """
    El rango plausible esta pensado para un repostaje de vehiculo, asi
    que solo se aplica a los registros que van a la hoja de vehiculos:
    una entrega a granel a un deposito (miles de litros) o el consumo
    mensual de una tarjeta de cuadrilla no tienen por que caer dentro.
    """
    incidencias = []
    for resultado in resultados:
        for reg in resultado.registros:
            if reg.destino:
                continue
            # Lecturas manuales: ya se han verificado contra los totales
            # impresos. AdBlue: rellenos de 1-2 L son normales.
            if reg.origen_detalle.startswith("lectura manual"):
                continue
            if reg.producto == "ADBLUE" and reg.litros > 0 and reg.litros <= LITROS_MAX_PLAUSIBLE:
                continue
            if reg.litros < LITROS_MIN_PLAUSIBLE or reg.litros > LITROS_MAX_PLAUSIBLE:
                incidencias.append(
                    Incidencia(
                        archivo=reg.referencia,
                        campo="Litros",
                        problema=(
                            f"Cantidad de {reg.litros} L para la matricula {reg.matricula} "
                            f"({reg.producto}) esta fuera del rango habitual "
                            f"({LITROS_MIN_PLAUSIBLE}-{LITROS_MAX_PLAUSIBLE} L)."
                        ),
                        accion="Revisar manualmente si el valor es correcto.",
                    )
                )
    return incidencias


def verificar_archivos_no_repetidos(nombres_archivo):
    incidencias = []
    vistos = {}
    for nombre in nombres_archivo:
        vistos[nombre] = vistos.get(nombre, 0) + 1
    for nombre, veces in vistos.items():
        if veces > 1:
            incidencias.append(
                Incidencia(
                    archivo=nombre,
                    campo="Archivo",
                    problema=f"El archivo se ha encontrado/procesado {veces} veces en esta ejecucion.",
                    accion="Revisar que no haya archivos duplicados en el directorio de entrada.",
                )
            )
    return incidencias
