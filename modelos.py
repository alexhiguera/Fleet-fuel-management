"""Estructuras de datos compartidas por los parsers y el resto del pipeline."""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Registro:
    """Un consumo individual ya clasificado, listo para volcar al Excel."""

    referencia: str  # nombre exacto del PDF de origen
    matricula: str
    fecha_factura: date
    litros: float
    producto: str  # GASOLINA_E5 / DIESEL_B7 / DIESEL_B100 / ADBLUE
    # Si el combustible no se sirvio a un vehiculo identificable, aqui se
    # indica a que hoja del Excel va (ver destinos.py: envases, depositos,
    # tarjetas, cuadrillas) y con que etiqueta se identifica la linea.
    # destino vacio = hoja 'vehiculos'.
    destino: str = ""
    destino_detalle: str = ""
    # trazabilidad interna, no va al Excel:
    concepto_original: str = ""
    fecha_consumo: str = ""  # texto tal cual aparece en la factura (informativo)
    origen_detalle: str = ""  # p.ej. "pagina 4, tarjeta 0262"


@dataclass
class Incidencia:
    """Una entrada para errores.md."""

    archivo: str
    campo: str
    problema: str
    accion: str = "Revisar manualmente."


@dataclass
class ResultadoFactura:
    archivo: str
    registros: list = field(default_factory=list)
    incidencias: list = field(default_factory=list)
    excluidos: list = field(default_factory=list)  # (concepto, motivo) informativos
    procesado_ok: bool = True
