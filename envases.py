"""
Deteccion de repostajes que NO van a un vehiculo sino a un envase
portatil (garrafas, bidones, barriles, garrafones...).

En el campo "vehiculo"/"matricula" de algunas facturas aparece, en vez de
una matricula, el texto del envase en el que se sirvio el combustible
(p.ej. "GARRAFAS" en Gasolinera Ortuella y E.S. Goiri, "GARAFAS" -asi,
con una sola erre- en Repsol Comercial). Esos litros son reales pero no
son consumo de un vehiculo identificable, asi que van a una tabla aparte
en vez de a la hoja 'vehiculos'.

Un DEPOSITO fijo (p.ej. "DEPOSITO BASAURI") NO es un envase: es un
tanque de la instalacion desde el que luego se reposta, y se mantiene en
la hoja de vehiculos tal y como se decidio.
"""

import re
import unicodedata

# Texto tal cual puede aparecer -> tipo de envase normalizado que se
# escribe en la tabla. La clave se compara ya normalizada (mayusculas,
# sin acentos, sin espacios sobrantes).
_CATALOGO_ENVASES = {
    "GARRAFAS": "Garrafas",
    "GARRAFA": "Garrafa",
    "GARAFAS": "Garrafas",          # variante con una sola erre (Repsol Comercial)
    "GARAFA": "Garrafa",
    "GARRAFONES": "Garrafones",
    "GARRAFON": "Garrafon",
    "BIDONES": "Bidones",
    "BIDON": "Bidon",
    "BARRILES": "Barriles",
    "BARRIL": "Barril",
    "GALONES": "Galones",
    "GALON": "Galon",
    "BOMBONAS": "Bombonas",
    "BOMBONA": "Bombona",
    "TAMBORES": "Tambores",
    "TAMBOR": "Tambor",
}

# "GAR" aparece una sola vez (E.S. Bidebarri, 2022): el campo de
# matricula de esa factura viene cortado a 3 caracteres. No es una
# matricula ni un codigo conocido, y en esa misma columna el resto de
# facturas usa "GARRAFAS", asi que se trata como garrafas dejando
# constancia de que el texto original estaba truncado.
_TRUNCADOS = {
    "GAR": "Garrafas (texto truncado en la factura: 'GAR')",
}


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.strip().upper())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.split())


def detectar(texto: str):
    """
    Devuelve el tipo de envase si el texto identifica uno, o None si no
    lo es (matricula real, deposito fijo, codigo de tarjeta, etc.).
    """
    if not texto:
        return None

    norm = _normalizar(texto)
    if norm in _CATALOGO_ENVASES:
        return _CATALOGO_ENVASES[norm]
    if norm in _TRUNCADOS:
        return _TRUNCADOS[norm]

    # "2 GARRAFAS", "GARRAFAS 20L", etc.: basta con que una de las
    # palabras del texto sea un envase conocido.
    for palabra in re.findall(r"[A-Z]+", norm):
        if palabra in _CATALOGO_ENVASES:
            return _CATALOGO_ENVASES[palabra]

    return None
