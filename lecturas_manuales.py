"""
Registros introducidos A MANO tras leer visualmente (renderizando a imagen)
los PDF que no tienen ningun texto extraible -ni el parser automatico ni una
re-lectura de texto pueden hacer nada con ellos-, normalmente facturas
escaneadas como imagen.

Cada entrada usa como clave el nombre EXACTO del archivo PDF tal como
aparece en el directorio de entrada. Todos los valores deben transcribirse
directamente del documento (nunca inventados); si el propio documento trae
un total por producto que permite comprobar la suma de las lineas sueltas,
conviene verificarlo antes de dar la lectura por buena (y dejar la cuenta
en un comentario, como en el ejemplo de abajo).

Estructura de cada entrada:
  - "excluido": True + "motivo_exclusion" -> el documento no es combustible
    de vehiculo (p.ej. lubricante, peaje...), no aporta litros.
  - "fecha_factura": fecha de factura (date) + "registros": lista de
    {"matricula", "litros", "producto", "concepto", "fecha_consumo"?}.

Este diccionario empieza vacio: rellenalo con las facturas escaneadas de tu
propio caso. El ejemplo comentado de abajo muestra el formato esperado con
datos ficticios.
"""

from datetime import date

import productos

LECTURAS_MANUALES = {
    # Ejemplo (datos ficticios) de una entrega a granel leida a mano:
    # 950 L x 0,95 EUR/L = 902,50 EUR = base imponible impresa -> cuadra.
    # "FC24EJ-00001 PROVEEDOR EJEMPLO FRA 24 0001 01-01-2024.pdf": {
    #     "fecha_factura": date(2024, 1, 1),
    #     "registros": [
    #         {"matricula": "DEPOSITO EJEMPLO", "litros": 950.00, "producto": productos.DIESEL_B7,
    #          "concepto": "GASOLEO A", "fecha_consumo": "01-01-2024"},
    #     ],
    # },
    #
    # Ejemplo de factura excluida (no es combustible de vehiculo):
    # "FC24EJ-00002 PROVEEDOR EJEMPLO FRA 24 0002 05-01-2024.pdf": {
    #     "excluido": True,
    #     "motivo_exclusion": "Factura de lubricante, no es gasoleo ni combustible de vehiculo.",
    # },
}
