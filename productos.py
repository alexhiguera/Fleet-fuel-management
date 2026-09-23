"""
Catálogo de clasificación de productos observados en las facturas reales
(Gasolinera Ortuella y SOLRED, ejercicio 2022).

Es una lista EXPLÍCITA y cerrada a propósito: cualquier concepto que
aparezca en una factura y no esté en este diccionario se considera
"no clasificable" y se registra en errores.md en vez de adivinar su
categoría. Así evitamos clasificar mal un producto nuevo que aparezca
en facturas futuras.

Para añadir un proveedor o producto nuevo: añadir la cadena tal cual
aparece en el PDF (en minúsculas, sin acentos ni tildes) a la categoría
correspondiente.
"""

import unicodedata

GASOLINA_E5 = "GASOLINA_E5"
DIESEL_B7 = "DIESEL_B7"
DIESEL_B100 = "DIESEL_B100"  # sin columna en la plantilla actual -> siempre a revisión
ADBLUE = "ADBLUE"
GLP = "GLP"  # autogas: va a su propia hoja, no a las tablas de gasolina/diesel/adblue
OTRO_COMBUSTIBLE = "OTRO_COMBUSTIBLE"  # gasoleos/gasolinas de marca no asimilados a B7/E5: hoja aparte
EXCLUIDO = "EXCLUIDO"  # producto real pero fuera del alcance (peajes, taller, etc.)
DESCONOCIDO = "DESCONOCIDO"  # no reconocido, requiere revisión humana


def _normalizar(texto: str) -> str:
    """minúsculas, sin tildes, espacios colapsados, para comparar de forma robusta."""
    texto = texto.strip().lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = " ".join(texto.split())
    return texto


# Conceptos de combustible/AdBlue observados en las facturas reales.
_CATALOGO_COMBUSTIBLE = {
    # --- GASOLINERA ORTUELLA ---
    "gna 95": GASOLINA_E5,
    "gna 98": GASOLINA_E5,
    "diesel": DIESEL_B7,
    "diesel e+10": DIESEL_B7,
    "adblue": ADBLUE,
    "adblue b-1000l granel": ADBLUE,
    # --- SOLRED ---
    "diesel e+ neo": DIESEL_B7,
    "diesel e+10 neo": DIESEL_B7,
    "efitec 95 n": GASOLINA_E5,
    "efitec 98 n": GASOLINA_E5,
    "g95premium n": GASOLINA_E5,
    "adblue repsol": ADBLUE,
    "autogas": GLP,
    "diesel e+5": OTRO_COMBUSTIBLE,
    "diesel e+10 zero": OTRO_COMBUSTIBLE,
    "diesel nexa 100%": OTRO_COMBUSTIBLE,
    # --- EUROPOLEO / VIZCAINA DE PETROLEOS (entrega a granel a deposito) ---
    "gasoleo a": DIESEL_B7,
    # Gasoleo B (bonificado/agricola): fiscalmente distinto del B7 de carretera,
    # no se mezcla en la misma tabla -> va a "otros combustibles".
    "gasoleo b": OTRO_COMBUSTIBLE,
    # --- E.S. BIDEBARRI ---
    "efitec 95 gasolina": GASOLINA_E5,
    "efitec 95": GASOLINA_E5,
    "efitec 98 gasolina": GASOLINA_E5,
    "diesel e+": DIESEL_B7,
    # --- REPSOL COMERCIAL (tabla de detalle/extracto) ---
    "95efitec": GASOLINA_E5,
    "e+diesel": DIESEL_B7,
    # --- STAR RESSA / RED ESPANOLA DE SERVICIOS (RESSA) ---
    "ecoblue": ADBLUE,
    "sin plomo": GASOLINA_E5,
    "g. sin plomo": GASOLINA_E5,
    "g. sin plomo 95": GASOLINA_E5,
    "diesel star": DIESEL_B7,
    "diesel optima": DIESEL_B7,
    # --- ESTACION DE SERVICIO CAMPODON, serie B ---
    "idiesel": DIESEL_B7,
    "i95": GASOLINA_E5,
    # --- ESTACION DE SERVICIO CAMPODON, serie C ---
    "avia innova 95": GASOLINA_E5,
    "avia innova diesel": DIESEL_B7,
}

# Posibles variantes de Diésel B100 / biodiésel, no observadas en las
# facturas de 2022 pero que el usuario ha pedido tratar aparte si aparecen.
_CATALOGO_B100 = {
    "diesel b100": DIESEL_B100,
    "b100": DIESEL_B100,
    "biodiesel": DIESEL_B100,
    "biodiesel b100": DIESEL_B100,
    "hvo": DIESEL_B100,
}

# Conceptos reales pero que NO son combustible ni AdBlue: se excluyen
# silenciosamente del recuento de litros (no son un error, son ruido
# esperado de las facturas), pero se anotan en el log de trazabilidad.
_CATALOGO_EXCLUIDO = {
    "via t": "peaje autopista",
    "autopistas": "peaje autopista",
    "aparc. viat": "aparcamiento/peaje",
    "aparcamiento": "aparcamiento",
    "lavados": "lavado de vehiculo",
    "lavados/lubrics.": "lavado/lubricantes",
    "taller": "servicio de taller",
    "taller / aparcamto": "taller/aparcamiento",
    "rp anticongelan": "anticongelante (no combustible)",
    "rp limpia parab": "liquido limpiaparabrisas (no combustible)",
    "midel coche de": "producto/servicio no identificado como combustible",
    # línea agregada de la bonificación estatal RDL 6/2022; su "cantidad"
    # es la suma de litros del mes, no un consumo individual -> excluir.
    "rdl 6/2022": "bonificacion RDL 6/2022 (linea agregada, no es un consumo)",
    "bonificacion estatal rd 6/2022": "bonificacion RD 6/2022 (linea agregada, no es un consumo)",
    "dto. cepsa extra": "descuento comercial (no combustible)",
    "verkoplus 15w40 ld (5litros)": "lubricante/aceite (no combustible)",
    "aportacion sigaus": "tasa SIGAUS de aceite (no combustible)",
    "lubricantes": "lubricantes (no combustible)",
    "promociones": "promocion/descuento comercial (no combustible)",
    "autopistas de peaje": "peaje autopista",
    "otras compras": "compra varia no identificada como combustible",
    "cuota(s) tarjeta(s)": "cuota de mantenimiento de tarjeta (no combustible)",
}


def clasificar(concepto: str):
    """
    Devuelve (categoria, motivo) para un concepto de factura.
    categoria es una de GASOLINA_E5 / DIESEL_B7 / DIESEL_B100 / ADBLUE /
    EXCLUIDO / DESCONOCIDO.
    """
    norm = _normalizar(concepto)

    if norm in _CATALOGO_COMBUSTIBLE:
        return _CATALOGO_COMBUSTIBLE[norm], None

    if norm in _CATALOGO_B100:
        return DIESEL_B100, None

    if norm in _CATALOGO_EXCLUIDO:
        return EXCLUIDO, _CATALOGO_EXCLUIDO[norm]

    # La línea "RDL 6/2022 0,2€/l" incluye el importe del descuento en el
    # propio texto del concepto, así que se compara por prefijo.
    if norm.startswith("rdl "):
        return EXCLUIDO, _CATALOGO_EXCLUIDO["rdl 6/2022"]

    return DESCONOCIDO, "concepto no reconocido en el catalogo"
