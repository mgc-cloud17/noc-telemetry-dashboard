# Investigación (Fase 0): NOC Topology & Real-Time Telemetry Dashboard

Resuelve las incógnitas del Technical Context de [plan.md](plan.md). Formato: Decisión / Justificación / Alternativas.

## R1. Ping ICMP asíncrono (`icmplib`)
- **Decisión**: `icmplib.async_ping(address, count=3, interval=0.2, timeout=0.4, privileged=<ICMP_PRIVILEGED>)` envuelto en `asyncio.wait_for(..., 1.5)`. Se usa `host.avg_rtt` → `latency_ms` y `host.packet_loss * 100` → `packet_loss` (%).
- **Justificación**: tres paquetes dan una pérdida con granularidad (0/33/67/100 %) para los umbrales 1–50 % y `==100 %`; el peor caso (~1.0 s) cabe en los 1.5 s estrictos (Principio I).
- **Nota de plataforma**: en Linux `privileged=False` funciona sin root si `net.ipv4.ping_group_range` lo permite; en Windows se requieren sockets raw (terminal como Administrador). Se expone `ICMP_PRIVILEGED` en `.env` (por defecto `false`).
- **Alternativas**: `ping` por `subprocess` (rechazado: parseo frágil, más costoso), `scapy` (rechazado: dependencia pesada).

## R2. SNMPv2c asíncrono (`pysnmp-lextudio`)
- **Decisión**: una sola consulta GET por nodo con todos los OID necesarios (ifOperStatus, ifInOctets, ifOutOctets de cada interfaz monitoreada + `cpmCPUTotal5secRev.1`), vía API `pysnmp.hlapi.asyncio`, `CommunityData(community, mpModel=1)`, `UdpTransportTarget((ip, 161), timeout=1.0, retries=0)`, envuelta en `asyncio.wait_for(..., 1.5)`.
- **Justificación**: un solo viaje por nodo minimiza latencia y paquetes UDP; `retries=0` + timeout externo garantizan el límite de 1.5 s.
- **Riesgo / mitigación**: los nombres de la API cambian entre versiones de `pysnmp-lextudio` (camelCase `getCmd` vs snake_case `get_cmd`; `UdpTransportTarget.create()` en versiones recientes). Se **fija la versión exacta en `requirements.txt`** y se aísla el acceso en una única función `snmp_get()` dentro de `telemetry_engine.py`, que las pruebas sustituyen por un *mock*.
- **Alternativas**: `pysnmp` clásico (sin mantenimiento), `snmpget` por subproceso (rechazado: no asíncrono nativo).

## R3. Cálculo de tasas en Kbps con *wrap* de 32 bits
- **Decisión**: `calculate_kbps(prev, curr, delta_s, counter_bits=32) -> float`:
  `delta = curr - prev`; si `delta < 0` → `delta += 2**counter_bits`; `kbps = delta * 8 / delta_s / 1000`. Devuelve `0.0` si `prev is None` o `delta_s <= 0`.
- **Justificación**: los contadores `ifInOctets`/`ifOutOctets` son `Counter32`; a 1 Gbps el wrap ocurre cada ~34 s, por lo que es un caso frecuente, no excepcional.
- **Limitación aceptada**: un reinicio del equipo (contador a 0) es indistinguible de un wrap y puede producir un pico espurio único; se descarta si la tasa supera la velocidad nominal del enlace (`LINK_MAX_KBPS`, por defecto 1 000 000).
- **Alternativas**: contadores de 64 bits (`ifHCInOctets`), descartados porque el requisito exige `ifInOctets`/`ifOutOctets` y wrap de 32 bits.

## R4. Reglas de estado (umbrales)
Definidos por el usuario; se concretan huecos:

| Estado | Condición de nodo |
|---|---|
| `down` | pérdida ICMP `== 100 %` **o** todas las interfaces monitoreadas del nodo con `ifOperStatus != 1` |
| `degraded` | ICMP responde y (latencia `>= 50 ms` **o** pérdida `> 0 %` y `< 100 %` **o** CPU `>= 80 %` **o** SNMP sin métricas **o** alguna (no todas) interfaz caída) |
| `up` | pérdida `0 %`, latencia `< 50 ms`, CPU `< 80 %`, SNMP completo, interfaces con `ifOperStatus == 1` |

| Estado | Condición de enlace |
|---|---|
| `down` | `ifOperStatus != 1` en cualquiera de sus interfaces **o** algún nodo extremo `down` |
| `degraded` | `ifOperStatus == 1` pero sin métricas SNMP de la interfaz |
| `up` | `ifOperStatus == 1` con métricas válidas y nodos extremos no `down` |

Decisiones sobre huecos del requerimiento:
- **Pérdida 51 %–99 %**: no definida por el usuario → `degraded` (el equipo aún responde).
- **Prioridad**: `down` > `degraded` > `up`.
- **Nodo con varias interfaces (R1-Core)**: una interfaz caída no pone en rojo todo el nodo (sería engañoso); lo deja `degraded`. Nodos con una sola interfaz monitoreada (R2, R3) pasan a `down`, coherente con el requisito.
- **Si ICMP está `down`** no se consulta SNMP (se ahorra el timeout) y los campos SNMP se reportan `null`.
- **`ifOperStatus`**: valor `1` = up; cualquier otro (2 down, 3 testing, 5 dormant, 7 lowerLayerDown…) = caído.
- **Umbrales configurables** en `.env`: `LATENCY_DEGRADED_MS=50`, `CPU_DEGRADED_PERCENT=80`.

## R5. Motor simulado
- **Decisión**: `SimulatedPoller` con estado mutable en memoria: `node_forced_down: set[str]`, `edge_forced_down: set[str]`, contadores de octetos acumulativos por interfaz (con wrap a 2³² reales para ejercitar la misma ruta de cálculo que el poller real) y semilla opcional (`SIMULATION_SEED`) para pruebas deterministas.
- **Propagación**: un nodo en `down` fuerza `down` en sus enlaces; restaurar el nodo no restaura un enlace que fue apagado manualmente (estados independientes, se evalúan combinados).
- **Realismo**: latencia base por nodo + *jitter* gaussiano; tráfico = base + onda sinusoidal lenta + ruido; CPU con paseo aleatorio acotado; ocasional pico para disparar `degraded`.
- **Mismo contrato** que el poller real: ambos implementan `BasePoller.poll() -> TelemetrySnapshot`.

## R6. WebSocket y bucle de fondo
- **Decisión**: `ConnectionManager` con `set[WebSocket]`, `broadcast()` que envía con `asyncio.gather(..., return_exceptions=True)` y retira clientes que fallan; captura `WebSocketDisconnect` y `RuntimeError`. Al conectar se envía el último snapshot (FR-024). Tarea de fondo creada en `lifespan`, cancelada limpiamente al apagar; cada ciclo mide su duración y duerme `max(0, 3 − duración)`, y cualquier excepción del ciclo se registra sin matar la tarea.
- **Alternativas**: Server-Sent Events (rechazado: el requisito exige WebSocket), Socket.IO (dependencia innecesaria).

## R7. Frontend zero-build
- **Decisión**: Tailwind (CDN `cdn.tailwindcss.com`), Cytoscape.js y Chart.js por CDN con versiones fijadas; Chart.js con `animation: false` y ventana de 40 muestras (≈2 min); reconexión con *backoff* exponencial 1 s → 10 s; el estado de nodos/enlaces se aplica con `cy.batch()`; el historial por nodo se mantiene en un `Map` en memoria del cliente.
- **Layout Cytoscape**: posiciones preestablecidas por rol (core al centro, dist y edge a los lados) para estabilidad visual en lugar de un layout dinámico.

## R8. Configuración y pruebas
- **Decisión**: `python-dotenv` carga `.env` una única vez en una función `load_settings()` que devuelve un `Settings` (dataclass congelada); no se leen variables en otros módulos. Pruebas con `pytest` + `pytest-asyncio` + `httpx` (`AsyncClient` con `ASGITransport`) y `TestClient` de Starlette para WebSocket; SNMP e ICMP simulados mediante *mocks*.
- **Dependencias adicionales** a las listadas por el usuario: `jinja2` (plantillas), `pytest-asyncio`.
