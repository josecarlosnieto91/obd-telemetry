# Auditoría de la webapp — 2026-10-28

Revisión de la web de OBD Telemetry (Flask, `webapp/`) con la BD real, el
servicio en marcha y el sitio abierto en navegador. Todo lo que aparece aquí
como «medido» salió de una ejecución, no de leer el código.

## Estado de partida (medido)

| Comprobación | Resultado |
|---|---|
| Tests del repo | 137 en verde |
| Páginas (`/`, `/trips`, `/alerts`, `/dtcs`, `/maintenance`, `/refuels`, `/map`) | 200, entre 1 y 9 ms |
| `/api/map` sin filtro | **1.064.401 B**, 69 ms (parseaba los 180 GPX en cada visita) |
| `/api/status` | 14,8 ms, con `SCAN can_readings` + `TEMP B-TREE` |
| `/api/trip/<id>` inexistente | **HTTP 500** (`dict(None)` → TypeError) |
| `/health`, `/favicon.ico`, `/robots.txt` | 404 (los dos primeros se piden solos) |
| Compresión | ninguna: 1 MB cruzaba la tailnet tal cual |
| Chart.js | desde `cdn.jsdelivr.net` |
| `/dtcs` | página existente y **huérfana** (sin enlace en el menú) |
| `static/nav.js` | **sin versionar** en git |

## Corregido

1. **Enlace al sitio en el resumen de viaje** (`collector/trip_summary.py`):
   cada resumen cierra con `🔗 <sitio>/trips?trip=<id>`, y esa URL abre el
   detalle de ESE viaje. La base sale de `webapp.site_url` en
   `obd_vehicle_config.json` (por defecto, MagicDNS de la tailnet).
2. **`/trips?trip=<id>`** abre el modal del viaje; si la sesión ya no existe
   (fusionada o reprocesada) lo explica en vez de romperse.
3. **404** en `/api/trip/<id>` inexistente.
4. **gzip** para JSON/HTML > 800 B: `/api/map` 1.064.401 → **251.625 B**.
5. **GPX con caché** por `(mtime, tamaño)` + `?tracks=0` + `?limit=N`.
6. **`/api/trips`** sin N+1: una consulta `IN (...)` para el consumo estimado.
7. **`/health`** y **`/favicon.ico`**.
8. **Chart.js local** (4.4.7 en `static/`): el gráfico ya no depende de Internet.
9. **`/dtcs` en el menú**, `nav-status` con `role=status aria-live=polite`, modal
   con `role=dialog`, cierre con botón real y Escape.
10. **Índice `can_readings(ts)`**: `/api/status` **14,8 → 1,8 ms**.
11. **`nav.js`** versionado.

Tests: **137 → 151**.

## Propuestas pendientes (por prioridad)

### Seguridad

- **POST sin autenticación**: `/api/dtcs/clear` (sin cuerpo marca como borrados
  TODOS los códigos), `/api/alerts/acknowledge`, `/api/refuels/*`. Cualquiera
  con acceso a la tailnet puede modificar el histórico. Propuesta: cabecera
  `X-OBD-Token` con valor del config, y que `/api/dtcs/clear` exija la lista de
  códigos.
- **`0.0.0.0:8765`**: no se puede cerrar a loopback porque el kiosco de la
  tablet apunta a la IP de la tailnet en ese puerto. Ruta: pasar el kiosco a
  `https://<host>:8446` (por `tailscale serve`) y después cerrar el puerto.

### Rendimiento y datos

- `/api/map` sin filtro sigue devolviendo 7.677 posiciones crudas (819 KB
  comprimidas a ~180 KB): decimar en servidor (1 punto cada N) y que el mapa use
  `?limit`.
- `/api/alerts`: 195 filas, `LIMIT 100` sin paginar.
- Rotación de backups: 5 `.bak` de la BD, 17 MB acumulados en `~/.hermes/data`.
- `sessions_merged_undo` con 92 filas sin política de limpieza.

### Producto / UX

- **Página de detalle real** (`/trip/<id>`) en vez del modal: enlaces que se
  puedan compartir fuera y rastrear.
- **Exportar** GPX/CSV de un viaje (los GPX están en disco pero la web no los
  sirve como descarga).
- **Filtro por fecha y paginación** en viajes (hoy `LIMIT 50` fijos; hay 207
  sesiones y 388 ids).
- **Aviso de datos obsoletos**: el dashboard pinta valores sin decir si la
  última lectura es de hace 3 semanas; con el coche parado se lee como si fuera
  de ahora.
- **Consumo histórico**: gráfica de l/100 km y €/100 km por mes.
- **PWA/offline** para la tablet (manifest + service worker).

### Mantenimiento

- No hay `.github/workflows`: el repo no tiene CI propio, solo puertas locales.
- `README.md` sin documentar `/health`, `?tracks=0`, `?limit` ni
  `webapp.site_url`.
- `/api/status` y `/api/map` no tienen tests (los nuevos cubren sonda, favicon,
  404, gzip y `tracks=0`).

## Cómo se verificó

`python -m pytest -q` (151 en verde), `systemctl --user restart
obd-webapp.service`, y medidas sobre el servicio en marcha con `curl`
(tamaños, `Content-Encoding`, `EXPLAIN QUERY PLAN`) y con navegador real
(`/trips?trip=388` abre el detalle del viaje del 28/10 14:10: 1,59 km, 25 min).
