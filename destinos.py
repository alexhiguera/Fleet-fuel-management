"""
A que hoja del Excel va cada registro.

La hoja 'vehiculos' solo debe contener consumo atribuible a un vehiculo
identificable. Todo lo demas -que es real y hay que contabilizar, pero
no es un vehiculo- va a su propia hoja:

  - envases    : garrafas, bidones, barriles... (ver envases.py)
  - depositos  : entregas a granel al deposito fijo de una instalacion
                 (EUROPOLEO, VIZCAINA, y el deposito de Campodon)
  - tarjetas   : la factura identifica la operacion con un codigo de
                 tarjeta interno en vez de con la matricula (Gasolinera
                 Ortuella, Repsol Comercial)
  - cuadrillas : la tarjeta esta a nombre de una cuadrilla o de un
                 centro, no de un vehiculo (SOLRED corta esas etiquetas
                 a 10 caracteres: "LOIU JARD.", "GUENES JAR"...)

Un registro sin matricula legible ("SIN MATRICULA") NO se desvia: lo mas
probable es que sea un vehiculo cuya matricula la factura no imprimio,
asi que se queda en 'vehiculos' con su incidencia para revision manual.
"""

import re

import envases

VEHICULOS = ""
ENVASES = "envases"
DEPOSITOS = "depositos"
TARJETAS = "tarjetas"
CUADRILLAS = "cuadrillas"
CAMION_LIMPIEZA = "camion de limpieza"
GLP = "GLP"
OTROS_COMBUSTIBLES = "otros combustibles"

# Orden en el que se crean las hojas detras de 'vehiculos'.
HOJAS_EXTRA = (ENVASES, DEPOSITOS, CAMION_LIMPIEZA, TARJETAS, CUADRILLAS, GLP, OTROS_COMBUSTIBLES)

# Titulo de la columna que identifica cada linea en cada hoja.
TITULO_ETIQUETA = {
    ENVASES: "Tipo de envase",
    DEPOSITOS: "Deposito",
    TARJETAS: "Tarjeta",
    CUADRILLAS: "Cuadrilla / centro",
    CAMION_LIMPIEZA: "Deposito / lugar de entrega",
    GLP: "Matricula / tarjeta",
    OTROS_COMBUSTIBLES: "Matricula / tarjeta",
}

_RE_MATRICULA_NUEVA = re.compile(r"^\d{4}[\s-]?[A-Z]{3}$")
_RE_MATRICULA_VIEJA = re.compile(r"^[A-Z]{1,2}[\s-]?\d{4}[\s-]?[A-Z]{1,2}$")
# Prefijos de codigo de deposito/tarjeta observados en las facturas reales
# (p.ej. "BAKIO20100"); anyade aqui el prefijo que use tu propio cliente.
_RE_DEPOSITO_CAMPODON = re.compile(r"^(BAKIO)\s*\d{3,}$")
_RE_TARJETA = re.compile(r"^TARJETA\s+\S+$")
_RE_CODIGO_NUMERICO = re.compile(r"^\d{4,6}$")


def es_matricula(texto: str) -> bool:
    """True si el texto tiene forma de matricula espanola (nueva o vieja)."""
    if not texto:
        return False
    norm = texto.strip().upper()
    return bool(_RE_MATRICULA_NUEVA.match(norm) or _RE_MATRICULA_VIEJA.match(norm))


def clasificar(texto: str):
    """
    Devuelve (destino, etiqueta) para el texto que la factura pone en el
    campo de matricula. destino == VEHICULOS ("") significa que el
    registro se queda en la hoja de vehiculos.
    """
    if not texto:
        return VEHICULOS, ""

    norm = " ".join(texto.strip().upper().split())

    if es_matricula(norm):
        return VEHICULOS, ""

    tipo_envase = envases.detectar(norm)
    if tipo_envase:
        return ENVASES, tipo_envase

    if "CAMION DE LIMPIEZA" in norm:
        return CAMION_LIMPIEZA, norm

    if norm.startswith("DEPOSITO") or _RE_DEPOSITO_CAMPODON.match(norm):
        return DEPOSITOS, norm

    if _RE_TARJETA.match(norm):
        return TARJETAS, norm
    if _RE_CODIGO_NUMERICO.match(norm):
        return TARJETAS, f"TARJETA {norm}"

    return VEHICULOS, ""
