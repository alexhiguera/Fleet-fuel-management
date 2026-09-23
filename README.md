# ⛽ Fuel Invoice Extractor — facturas PDF de combustible → Excel

**Convierte facturas PDF de combustible (SOLRED, Repsol, gasolineras y tarjetas de flota) en un informe de consumo en Excel, agrupado por matrícula, sin escribir una sola fila a mano.** Pensado para gestores de flotas, asesorías y administración que reciben decenas o cientos de facturas en PDF al mes y necesitan litros por vehículo, por depósito o por tarjeta — sin depender de que cada proveedor use el mismo formato.

Extrae **referencia, matrícula, fecha de factura, litros y tipo de producto** de cada PDF y los vuelca en una copia de tu propia plantilla Excel. Principio de diseño: **nunca inventa un dato**. Si una fecha, matrícula o producto no está clara en la factura, esa línea se deja fuera del Excel y se anota en `errores.md` para revisión manual, en vez de adivinar.

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/) [![License: GPL v3](https://img.shields.io/badge/license-GPLv3-blue)](LICENSE)

---

## Índice

- [¿Por qué esta herramienta?](#-por-qué-esta-herramienta)
- [Proveedores soportados](#-proveedores-soportados)
- [¿Puedo reutilizarlo para mis propias facturas?](#-puedo-reutilizarlo-para-mis-propias-facturas)
- [Instalación](#-instalación)
- [Uso](#️-uso)
- [Hojas del Excel de salida](#-hojas-del-excel-de-salida)
- [Qué genera](#-qué-genera)
- [Archivos que se omiten automáticamente](#-archivos-que-se-omiten-automáticamente)
- [Facturas con portada + detalle en dos PDF](#-facturas-con-portada--detalle-en-dos-pdf)
- [PDFs escaneados (sin texto)](#️-pdfs-escaneados-sin-texto)
- [Cómo añadir un proveedor nuevo](#-cómo-añadir-un-proveedor-nuevo)
- [Lo que la herramienta nunca hace](#-lo-que-la-herramienta-nunca-hace)

## 🤔 ¿Por qué esta herramienta?

Cualquiera que gestione el combustible de una flota conoce el problema: cada mes llegan decenas de facturas en PDF de proveedores distintos (tarjetas de flota tipo SOLRED, gasolineras locales, distribuidores de gasóleo a granel...), cada uno con su propio formato, y hay que sacar de ahí los litros consumidos por matrícula para rellenar un Excel de control. Hacerlo a mano es lento y propenso a errores; automatizarlo mal (por ejemplo, calculando litros a partir del importe) es peor, porque produce números que parecen correctos pero no lo son.

Este proyecto resuelve ese problema con un enfoque simple:

- 📄 **Un parser por proveedor**: cada formato de factura tiene su propio módulo (`parser_*.py`) que ancla la lectura en el texto real de la factura (conceptos, fechas, referencias), no en contar líneas fijas — así aguanta variaciones de maquetación entre años.
- 🚫 **Cero litros inventados**: si una factura es ambigua, está escaneada sin texto, o el producto no está en el catálogo, esa línea no se cuenta y queda anotada en `errores.md` para que la revises tú.
- 🧾 **Distingue vehículo real de "todo lo demás"**: entregas a granel a un depósito fijo, repostajes a garrafas/bidones, tarjetas a nombre de una cuadrilla... es consumo real, pero no de un vehículo, así que va a su propia hoja del Excel en vez de mezclarse o descartarse.
- 🔁 **Reutilizable año tras año**: cambia la plantilla Excel de columnas y sigue funcionando, porque las columnas se detectan por su título, no por su posición.

## ⛽ Proveedores soportados

Gasolinera Ortuella · SOLRED · E.S. Goiri · E.S. Bidebarri · Europoleo · Vizcaína de Petróleos · STAR RESSA / Red Española de Servicios (RESSA) · Repsol Comercial de Productos Petrolíferos · Estación de Servicio Campodón (2021-2022)

No todo el combustible facturado va a un vehículo: hay entregas a granel a depósitos fijos, repostajes a garrafas y tarjetas a nombre de una cuadrilla. Todo eso se contabiliza igual, pero en su propia hoja del Excel (ver más abajo), nunca inventando una matrícula.

## ✅ ¿Puedo reutilizarlo para mis propias facturas?

Depende de qué cambie respecto a lo ya soportado:

| Qué cambia | ¿Funciona tal cual? |
|---|---|
| 🗓️ Facturas de **otro año**, mismos proveedores | ✅ Sí, sin tocar nada |
| 🏢 Facturas de **otro cliente/flota**, pero de estos mismos proveedores | ✅ Sí, sin tocar nada |
| 📊 Una **plantilla Excel con columnas distintas** (p.ej. columnas "de"/"a" en vez de una sola fecha, orden distinto) | ✅ Normalmente sí: las columnas se detectan leyendo los títulos de la fila de cabecera (`Ref interna`, `de`, `a`, `MATRICULA`, `litros`...), no están fijadas por letra |
| 🧾 Facturas de un **proveedor nuevo** (otra gasolinera, otra tarjeta de flota) | ⚠️ Necesita un parser nuevo ([ver cómo añadirlo](#-cómo-añadir-un-proveedor-nuevo)) |
| ⛽ Un **producto nuevo** de un proveedor ya soportado (p.ej. un nombre comercial distinto de gasóleo) | ⚠️ Se detecta solo y se marca "pendiente de revisar" en `errores.md`; para clasificarlo automáticamente basta con añadirlo a `productos.py` (1 línea) |
| 🚗 Una columna **"TIPO DE VEHICULO"** en la plantilla | ℹ️ Se detecta pero se deja siempre en blanco a propósito: las facturas no dicen el tipo de vehículo y no se inventa ese dato (salvo la marca automática `Maquinaria`, ver más abajo) |

En resumen: para **años o flotas nuevas con los mismos proveedores**, es plug-and-play, incluso si la plantilla Excel cambia de columnas. Para **proveedores nuevos**, la arquitectura ya está pensada para añadirlos sin rehacer nada.

## 📦 Instalación

Requiere Python 3.9 o superior.

```bash
git clone https://github.com/alexhiguera/fleet-fuel-management.git
cd fleet-fuel-management
pip install -r requirements.txt
```

## ▶️ Uso

### Uso más simple (recomendado)

1. Pon todos los PDF de facturas y tu plantilla `.xlsx` en la **misma carpeta** que este proyecto (o indica otra con `--pdf-dir`/`--template`).
2. Renombra tu plantilla a `tablas para informe.xlsx`, o pásala explícitamente con `--template`.
3. Ejecuta:

```bash
python main.py
```

Por defecto:
- Busca los PDF en la carpeta del proyecto (y en sus subcarpetas, si organizas las facturas por proveedor).
- Usa la plantilla `tablas para informe.xlsx` de esa misma carpeta.
- Genera `informe_combustibles.xlsx`, `errores.md` y `log_trazabilidad.txt`.

### Uso con rutas personalizadas (otro año, otra carpeta, otra plantilla)

```bash
python main.py --pdf-dir "C:\ruta\a\facturas_2024" --template "C:\ruta\a\plantilla_2024.xlsx" --output "C:\ruta\a\informe_2024.xlsx"
```

Parámetros disponibles:

| Parámetro | Qué es | Por defecto |
|---|---|---|
| `--pdf-dir` | Carpeta donde están los PDF de las facturas (busca también en subcarpetas) | carpeta del proyecto |
| `--template` | Plantilla Excel a rellenar (no se modifica, se copia) | `tablas para informe.xlsx` |
| `--output` | Excel de salida | `informe_combustibles.xlsx` |
| `--errores-md` | Ruta del archivo de incidencias | `errores.md` |
| `--log` | Log interno de trazabilidad (detalle por registro) | `log_trazabilidad.txt` |

## 📊 Hojas del Excel de salida

La hoja `vehiculos` de la plantilla solo recibe el consumo que se puede atribuir a un **vehículo identificable**. Todo lo demás es consumo real, pero no de un vehículo, así que va a su propia hoja creada automáticamente detrás de `vehiculos` (misma estructura en todas: fecha de factura, nombre del PDF, producto, litros, etiqueta y fecha de consumo, con el total por producto al pie):

| Hoja | Qué recoge |
|---|---|
| `vehiculos` | Consumo con matrícula real (nueva `1234ABC` o antigua `BI1234CV`) |
| `envases` | Repostado a garrafas, bidones, barriles, galones o bombonas |
| `depositos` | Entregas a granel al depósito fijo de una instalación |
| `tarjetas` | La factura identifica la operación con un código de tarjeta interno en vez de con la matrícula |
| `camion de limpieza` | Entregas a granel al depósito de un camión de limpieza, cuando la factura lo identifica así |
| `GLP` | Autogás (GLP) repostado a un vehículo: no es gasolina ni gasóleo, no entra en las tablas de `vehiculos` |
| `otros combustibles` | Gasóleos/gasolinas de marca no asimilados al B7/E5 estándar (p.ej. gasóleo bonificado/agrícola, variantes "Zero" o 100% renovables) |
| `cuadrillas` | La tarjeta está a nombre de una cuadrilla o de un centro de coste, no de un vehículo |

Reglas adicionales:

- El **AdBlue** va siempre a su propia tabla dentro de `vehiculos`, aunque no tenga matrícula (se usa como etiqueta el depósito, envase o tarjeta que sí conste).
- Solo se registran consumos en **litros**: cualquier línea que solo traiga importe en euros, sin litros, no entra en ninguna tabla — no se inventan litros a partir de un precio.
- Una matrícula que aparece a la vez en la tabla de gasolina y en la de diésel del mismo informe se marca como `Maquinaria` en la columna `TIPO DE VEHICULO` de la tabla de gasolina (heurística útil para maquinaria bicombustible o flotas mixtas).
- Para los proveedores que facturan por **albarán de entrega a granel**, se usa la fecha del albarán, no la de emisión de la factura.
- Una línea **sin matrícula legible** no se desvía a ninguna hoja aparte: lo más probable es que sea un vehículo cuya matrícula la factura no imprimió, así que se queda en `vehiculos` con su incidencia para revisión manual.

## 📁 Qué genera

- **`informe_combustibles.xlsx`** → copia de tu plantilla con los litros rellenados en la hoja `vehiculos` (Gasolina E5, Diésel B7, AdBlue) y las hojas extra que hagan falta. La plantilla original **nunca se toca**.
- **`errores.md`** → todo lo que no se ha podido determinar con seguridad (fechas ambiguas, matrículas no identificadas, productos desconocidos, posibles duplicados...). Si está vacío o dice "No se han detectado incidencias", perfecto.
- **`log_trazabilidad.txt`** → detalle interno de cada registro (de qué PDF viene, concepto original, fecha del consumo) por si necesitas investigar un dato concreto. **No lo compartas ni lo subas a un repositorio si contiene datos reales**: incluye matrículas y factura por factura, así que es información sensible de tu propia flota.

Un resumen con los totales (PDFs procesados, registros por tipo de combustible, incidencias) se imprime en pantalla al terminar.

## 📎 Archivos que se omiten automáticamente

- **Duplicados exactos** (mismo contenido byte a byte, aunque el nombre sea distinto, p.ej. un `_OK` de más): se procesa solo uno y se avisa en `errores.md`.
- **Facturas duplicadas por contenido** (mismos registros —matrícula/depósito, fecha, litros y producto— que otra factura de un archivo distinto, aunque los bytes no coincidan): se conserva solo una copia y se avisa en `errores.md`.
- **Adjuntos "(DETALLE)" de Ortuella**: algunas facturas de Ortuella vienen acompañadas de un PDF con el mismo sufijo `(DETALLE)` en el nombre — es un desglose alternativo de la MISMA factura (se comprueba que matrículas/fechas/litros coinciden), no información nueva. Se omite y se usa solo la factura normal, para no duplicar registros.
- **Facturas de alquiler de Campodón**: alguna sucursal factura tanto combustible como alquiler de local; las facturas de alquiler no tienen combustible y se omiten.
- **Facturas AUTOGAS de Repsol Comercial**: GLP para una instalación fija medido en m³, no es combustible de vehículo; se excluyen.
- **Facturas rectificativas SOLRED** (número de factura que empieza por `RRA` en vez de `A`): se procesan igual que cualquier otra, pero se avisa en `errores.md` porque no son una factura de consumo estándar.

Ninguno de estos casos borra el PDF original: solo se excluye de esa ejecución, y siempre queda anotado por qué.

## 📑 Facturas con portada + detalle en dos PDF

STAR RESSA/Red Española de Servicios y Repsol Comercial emiten cada factura en dos PDF: uno de "portada" (solo totales, sin matrícula) y uno de "extracto"/"detalle" (el desglose real por matrícula). La herramienta los detecta por nombre de archivo (quitando el sufijo `(EXTRACTO)`/`(DETALLE)`/`EXTRACTO DE CONSUMO CLIENTE`) y los combina en una sola llamada: la fecha/número de factura salen de la portada y los litros por matrícula del extracto. Si falta el extracto adjunto, esa factura queda sin litros y se anota en `errores.md`, porque la portada por sí sola no trae el desglose por vehículo.

## 🖨️ PDFs escaneados (sin texto)

Si un PDF (de cualquier proveedor) no tiene texto extraíble —es una imagen escaneada—, no se hace OCR: se anota en `errores.md` como pendiente de revisión manual y no se cuenta ningún litro de ese archivo, para no inventar datos.

Si quieres transcribir a mano los datos de un PDF escaneado (leyéndolo tú visualmente), añade una entrada en `lecturas_manuales.py` con el nombre exacto del archivo — el formato y un ejemplo están documentados en ese mismo fichero.

## 🧩 Cómo añadir un proveedor nuevo

1. Crea `parser_nuevoproveedor.py` con una función `parse(ruta_pdf, nombre_archivo) -> ResultadoFactura` (usa `parser_ortuella.py` como plantilla de referencia, es el más sencillo).
2. En `main.py`, dentro de `_detectar_proveedor`, añade cómo identificar sus facturas (p.ej. por texto en el nombre de archivo).
3. Justo debajo, en `procesar_directorio`, añade la llamada a tu nuevo parser.
4. Añade los nombres de producto de ese proveedor a `productos.py`.

## ⛔ Lo que la herramienta nunca hace

- No usa la fecha de un consumo individual como fecha de factura.
- No usa importes económicos para deducir litros.
- No adivina una matrícula, fecha o producto si no está claro: lo deja pendiente en `errores.md`.
- No borra ni sobrescribe archivos de entrada ni la plantilla original.
- No incluye datos de facturas reales en este repositorio: las facturas PDF, las plantillas rellenas y los informes generados son tuyos y se quedan en tu máquina (ver `.gitignore`).

## 📄 Licencia

Este proyecto se distribuye bajo la [GNU General Public License v3.0](LICENSE). Puedes usarlo, modificarlo y redistribuirlo libremente, siempre que cualquier trabajo derivado se publique también bajo GPLv3.

---

¿Encontraste un formato de factura que no encaja, o quieres añadir un proveedor? Las contribuciones (issues y pull requests) son bienvenidas.
