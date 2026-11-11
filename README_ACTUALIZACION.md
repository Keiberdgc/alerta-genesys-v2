# Genesys Cloud → Telegram · Versión con aviso de regreso a cola

Esta versión envía las alertas existentes (más de 75 minutos `OFF_QUEUE` y más de 20 minutos en la presencia personalizada **Baño**) y una **notificación de regreso a cola**, solo si existió una alerta previa.

## Condiciones de recuperación

- El programa exige presencia de sistema `ON_QUEUE` y routing distinto de `OFF_QUEUE`. No interpreta automáticamente cualquier ausencia de `OFF_QUEUE` como un regreso.
- Si se activó la alerta por fuera de cola, se notifica al detectar el regreso a cola.
- Si se activó la alerta por Baño, se notifica cuando Baño haya terminado **y** se confirme regreso a cola.
- Si las dos alertas estaban pendientes, se informa de ambas en un solo mensaje cuando las dos estén resueltas.
- Sin alerta previa, no se envía notificación de regreso a cola.
- Las alertas de incumplimiento y recuperación se persisten en `estado_alertas_v2.json`; al cargar un estado del formato v2, el programa migra a v3 automáticamente. Se conserva el nombre del archivo para mantener historial.

## Instalación local sin perder los datos existentes

1. Detén el bot anterior con `Ctrl + C`.
2. Realiza una copia completa de la carpeta actual, especialmente `.env`, `config.yaml` y `estado_alertas_v2.json`.
3. Copia **solo** este `alerta_genesys.py` actualizado a tu carpeta de trabajo (puedes renombrar el viejo como `alerta_genesys_backup.py`).
4. Conserva **tu** `.env`, `config.yaml` y `estado_alertas_v2.json` sin reemplazarlos por ejemplos.
5. Restaura el límite de 75 minutos si lo habías cambiado a 1 minuto en la prueba anterior. Esta versión ya trae 75 y 20 minutos.
6. Instala dependencias: `py -3.14 -m pip install -r requirements.txt`.
7. Ejecuta `py -3.14 test_logica.py` (no usa la API ni manda mensajes).
8. Ejecuta `py -3.14 alerta_genesys.py --dry-run` para validar conexión/estados. **El modo dry-run no lee las alertas previas y no simula las notificaciones de recuperación; solo las alertas de tiempo.**
9. Ejecuta en vivo solo con CCR Admin: `py -3.14 alerta_genesys.py`. Si ese usuario tiene alertas previas pendientes, al volver a cola recibirá una notificación de recuperación.
10. Cuando se confirme la prueba, agrega los correos del resto del equipo en `config.yaml`.

**Precaución**: nunca ejecutes dos copias del monitor en paralelo con el mismo chat y estado. Evita duplicados usando solo una instancia. No publiques `.env` ni los JSON de estado.

## Traslado futuro al servidor

Antes de pasar a una VM, averigua el sistema operativo y la forma de acceso. Para Linux es recomendable `systemd` con cuenta de servicio y directorio de trabajo persistente; para Windows, programador de tareas o servicio Windows. Copia `.env`, `config.yaml`, `estado_alertas_v2.json`, script y requirements usando un medio seguro. Verifica salida HTTPS 443 a los dominios de API y login de tu región de Genesys y a `api.telegram.org`. Mantén zona horaria `America/Caracas` para las alertas.
