# Campañas relevantes y atención de Cuenta de Ahorro

Prototipo para seleccionar una audiencia de campañas con reglas explicables y atender consultas sobre esas campañas. El primer alcance es Cuenta de Ahorro en Colombia, con una demo histórica del **1 de marzo de 2026 a las 12:00**, hora de Bogotá asumida para los timestamps sin zona del dataset.

El día 1 está implementado: preparación de datos, catálogo, auditoría de calidad, reglas, permisos de prueba y selección reproducible. La interfaz conversacional, la autenticación de la aplicación, el componente aprendido y la solicitud persistida de asesor pertenecen a las etapas siguientes.

## Ejecutar el día 1

Las carpetas `data/`, `docs/` y `outputs/`, los entornos virtuales y las cachés de Python están excluidos de Git para mantener ligero el repositorio. Al clonar, copia el dataset en `data/` antes de ejecutar los scripts. Los documentos de `docs/` se conservan localmente y deben compartirse por separado.

Requiere Python 3.10 o posterior. El código usa únicamente la biblioteca estándar; no requiere credenciales ni servicios externos. Ejecutar desde la raíz del repositorio:

```powershell
python scripts/day1.py build
python scripts/day1.py verify
python -m unittest discover -s tests -v
```

`build` lee todas las filas de `customers.csv`, `marketing_campaigns.csv` y las particiones de `campaign_sends`, prepara SQLite y genera los resultados en `outputs/day1/`. Puede tardar varios minutos y muestra avance por archivos. El directorio generado está excluido de Git; los CSV originales no se editan.

Para repetir la selección usando la base preparada:

```powershell
python scripts/day1.py select
python scripts/day1.py verify
```

Si cambias [config/day1.json](config/day1.json), ejecuta `build` para reconstruir los resultados. La configuración contiene fecha, producto, país, excepciones explícitas de replay y reglas del equipo. Permite evaluar otra fecha o país sin asumir que habrá campañas utilizables.

## Entregables

| Archivo generado en `outputs/day1/` | Uso |
|---|---|
| `quality_report.md` / `quality.json` | Reporte legible y auditoría estructurada de las tres tablas principales; inventario de las demás |
| `catalog.json` | Campañas de ahorro que declaran Colombia, con motivos de exclusión y metadatos originales |
| `campaign_review.json` | Revisión de todas las campañas del producto, incluidos país ausente y otros países |
| `baseline_audience.jsonl` | Pares campaña/cliente seleccionados por reglas, ordenados y con trazabilidad |
| `baseline_summary.json` | Conteos de selección, exclusiones y etiquetas históricas de la campaña |
| `prepared.sqlite` | Proyección mínima de clientes, campañas, envíos, incidencias y metadatos |
| `development_scenarios.json` | Diez familias de escenarios, cada una con consulta en español y portugués |
| `source_manifest.json` | Hash, tamaño, esquema y conteo de cada fuente auditada |
| `resolved_scope.json` | Configuración exacta usada en la ejecución |
| `artifact_hashes.json` / `verification.json` | Integridad de artefactos y resultado de verificación |

Consulta [el alcance y las decisiones](docs/day1/alcance.md) y [los contratos de datos](docs/day1/contratos.md) antes de construir la aplicación.

## Interpretar los resultados

La campaña principal es `CMP-PHK8DTE4KLJO`, de reactivación, segmento Basic y canal original Voice. El archivo la marca `Completed`; la configuración permite reproducirla dentro de su ventana como **supuesto explícito de demo**, conservando ese estado original. No prueba que estuviera activa ese día.

La selección describe afinidad con reglas de marketing de prueba. Los archivos no permiten comprobar tenencia del producto, elegibilidad financiera ni beneficio individual. Tampoco reconstruyen el consentimiento o el estado histórico del cliente. Los timestamps sirven para excluir incoherencias evidentes.

No hay tasas, comisiones ni términos aprobados para explicar una oferta concreta. La demo deberá reconocer esos límites y ofrecer atención humana. Estos scripts no envían publicidad, no crean solicitudes de asesor y no aprueban productos.

## Verificación y actualizaciones

Las pruebas cubren consentimiento, fechas, campañas pausadas, frecuencia, acceso a otro cliente, confirmación, claves duplicadas, integridad de fuentes y reconstrucción con una llegada tardía. Son pruebas de desarrollo, no la evaluación conversacional reservada requerida para la entrega final.

`verify` comprueba integridad SQLite, orden y unicidad de la audiencia, cumplimiento de reglas para cada par seleccionado, conteos de frecuencia, conciliación con el resumen y hashes de fuentes y artefactos. Las fixtures comprueban además la audiencia esperada completa. No demuestra resultados comerciales ni autenticación de una aplicación todavía no construida.

Las cargas son reconstrucciones completas y reemplazan la base preparada mediante un archivo temporal. Repetir una carga idéntica no acumula filas. Una fuente añadida o corregida requiere `build` y `verify`; el día 1 incluye una prueba de llegada tardía que cambia la audiencia esperada. No se implementa una carga incremental.
