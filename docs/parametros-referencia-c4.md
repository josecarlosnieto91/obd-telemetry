# Parámetros de referencia — Citroën C4 Grand Picasso I 2.0 HDi 138 (DW10BTED4)

Valores de referencia del modelo frente a lo que mide este coche. Sirve para calibrar los
umbrales de `obd_vehicle_config.json` con criterio documentado y no con una corazonada.

Fecha: 2026-10-28. Fuentes al final.

## Motor y vehículo (ficha del modelo)

| Dato | Valor |
|---|---|
| Motor | DW10BTED4, 1.997 cm³, 4 cilindros, 16v, common rail |
| Potencia / par | 136 CV (100 kW) a 4.000 rpm · 270 Nm a 2.000 rpm |
| Alimentación | Turbo de geometría variable + intercooler |
| Relación de compresión | 17,6:1 |
| Consumo NEDC | 7,9 urbano · 5,1 extraurbano · **6,1 medio** l/100 km |
| CO₂ | 159 g/km |
| Depósito | 60 L · Peso 1.695 kg · Cx 0,31 · Superficie frontal 2,61 m² |
| Velocidad máxima | 195 km/h |

## Refrigerante y termostato (lo que ha destapado el problema)

| Parámetro | Referencia | Medido en este coche | Veredicto |
|---|---|---|---|
| Inicio de apertura del termostato | **83 °C** | — | dato del fabricante |
| Apertura completa | **95 °C** | — | dato del fabricante |
| Temperatura de servicio | 85-92 °C | **73,8 °C** de media en crucero | **por debajo del inicio de apertura** |
| Lecturas en crucero por debajo de 83 °C | — | **97 %** (1.980 muestras a >70 km/h) | motor frío |
| Viajes de >20 km que no pasan de 80 °C | — | **11 de 37** (uno de 109 km acabó en 80,0 °C) | motor frío |
| Corte del aire acondicionado | 115 °C | — | protección |
| Temperatura de aviso | 118 °C | — | límite de avería |

**Lectura:** un termostato que abre a 83 °C y no cierra deja el motor en 70-78 °C. Los
síntomas descritos por otros propietarios del mismo motor son exactamente estos: temperatura
que baja a 75-80 °C en carretera, sube a 95 °C en ciudad y tarda 15 minutos en coger dos
rayas. Consecuencias: más consumo, más desgaste y **el FAP no llega a regenerar** (necesita
temperatura sostenida) — coherente con los 0 eventos de regeneración en 3 meses.

Umbrales aplicados: `thermostat_open_c=83`, `thermostat_full_open_c=95`,
`thermostat_min_c=85`, `thermostat_cruise_min_c=83`, `thermostat_cruise_frias_pct=50`,
`coolant_warn_c=105` (95 es normal en ciudad), `coolant_critical_c=115`.

## Resto de parámetros (medido y coherencia)

| Parámetro | Medido | Comprobación |
|---|---|---|
| MAF en ralentí | 8,56 g/s (media 10,2 · rango 2,5-22,5) | coherente: 2,0 L a ~850 rpm con llenado bajo |
| MAF en crucero (>100 km/h) | 47,1 g/s de media (máx 132,8) | coherente: ~2.000-2.400 rpm con llenado alto |
| MAP en ralentí | 100,9 kPa | atmósfera (sin sobrepresión), correcto |
| MAP en crucero (>100 km/h) | 127,8 kPa de media · **máx 231 kPa** | máx ≈ 1,3 bar de sobrepresión, coherente con el VGT de 136 CV |
| Temperatura de admisión | 26,8 °C de media (9-54) | sigue al ambiente, el intercooler trabaja |
| Tensión en marcha (alternador) | 14,25 V (rango 12,8-14,8) | dentro de 13,5-14,5 |
| Tensión parado | 12,84 V (calibración 12,6) | batería sana |
| RPM en ralentí | 831-845 rpm | correcto |
| Carga de motor en ralentí | 13,4 % de media | correcto |
| Velocidad máxima registrada | 144 km/h | por debajo de los 195 del modelo (no se ha buscado) |
| **Consumo real (surtidor)** | **9,73 l/100 km** | +59 % sobre el 6,1 homologado |
| Consumo que marca el cuadro (CAN) | 5,39 l/100 km | **por debajo del urbano homologado (7,9)**: no es fiable |
| Estimación por MAF (sin corregir) | 5,08 l/100 km | subestimaba ×1,92 frente al surtidor |

**Por qué el consumo real es un 59 % más alto que el homologado:** 141 de los 207 viajes
registrados son de menos de 5 minutos y la mayoría no completan un trayecto largo; un diésel
que no pasa de 78 °C consume como un motor frío permanentemente. La cifra del cuadro (5,39)
no cuadra ni con el extraurbano homologado, así que la web muestra el consumo real de
surtidor y etiqueta el resto como estimación.

## Parámetros que este coche NO expone por OBD (comprobado sobre 7.566 lecturas)

`throttle_pos` (0111), `fuel_level` (012F), `fuel_pressure` (0123), `dtc_count`, `ambient`
(0146): cero lecturas. `fuel_rate` (015E): 144 de 7.566 (1,9 %). Están declarados en
`vehicle.unsupported_pids` y el panel no los pinta.

## Fuentes

- **Termostato del DW10BTED4** (inicio 83 °C, apertura completa 95 °C; corte A/C 115 °C;
  aviso 118 °C): tabla de especificaciones PSA citada en frenchcarforum.co.uk, hilo
  «Peugeot 407 SW 2.0 HDi 136 Thermostat Replacement» (mismo motor).
  <https://frenchcarforum.co.uk/forum/viewtopic.php?t=64373&start=30>
- **Síntomas en el mismo motor** (75-80 °C en carretera, 95 °C en ciudad, 15 min a dos rayas,
  «cuando baja de 83 grados es que no cierra a su debido momento»): foro C4atreros,
  «Temperatura baja C4 2.0 HDI».
  <https://c4atreros.es/foro/viewtopic.php?f=36&t=58094>
- **Ficha técnica y consumos NEDC** del Grand C4 Picasso HDi 138 (2006-2008): km77.
  <https://www.km77.com/coches/citroen/grand-c4-picasso/2007/estandar/sx/grand-c4-picasso-hdi-138-fap-cmp-sx/datos>
- Valores medidos: `~/.hermes/data/obd_telemetry.db` el 2026-10-28 (consultas en el goal y en
  `docs/auditoria-web-2026-10-28.md`). Los cálculos de caudal de aire son comprobaciones de
  coherencia, no datos de fuente.
