# Implementation Plan: NOC Topology & Real-Time Telemetry Dashboard

**Branch**: `001-noc-telemetry-dashboard` | **Date**: 2026-11-18 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-noc-telemetry-dashboard/spec.md`

## Summary

Dashboard NOC que muestra en tiempo real una topología de 3 routers y 2 enlaces. Un bucle `asyncio` cada 3 s sondea los equipos (ICMP + SNMPv2c, o un motor simulado) con `asyncio.gather` y timeouts de 1.5 s, calcula el estado (`up`/`degraded`/`down`) según umbrales definidos, y difunde un snapshot JSON por WebSocket (`/ws/telemetry`). El frontend zero-build (Cytoscape.js + Chart.js + Tailwind por CDN) colorea el grafo, muestra un panel lateral con gráficas deslizantes y un panel de eventos. En modo simulador, endpoints REST permiten tumbar y restaurar nodos/enlaces. Decisiones técnicas en [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12 (backend); JavaScript ES2020 sin build (frontend)

**Primary Dependencies**: FastAPI, Uvicorn, `icmplib`, `pysnmp-lextudio` (versión fijada), `python-dotenv`, `jinja2`; CDN: Tailwind CSS, Cytoscape.js, Chart.js

**Storage**: N/A (estado en memoria; sin historial persistente)

**Testing**: `pytest`, `pytest-asyncio`, `httpx` (`AsyncClient`), `TestClient` de Starlette para WebSocket; SNMP/ICMP con *mocks*

**Target Platform**: Servidor Linux/Windows con Python 3.12; navegadores modernos

**Project Type**: web-service (backend + frontend estático servido por la misma app)

**Performance Goals**: ciclo de sondeo completo ≤ 1.5 s en el peor caso; difusión cada 3 s (±1 s en el 99 % de los ciclos); ≥ 10 clientes WebSocket simultáneos

**Constraints**: nunca bloquear el event loop; timeout estricto de 1.5 s por consulta; SNMP UDP/161 con `retries=0`; sin secretos en el código; ICMP requiere privilegios en Windows

**Scale/Scope**: 3 nodos, 2 enlaces (extensible por configuración), 1 pantalla de dashboard, 6 endpoints REST + 1 WebSocket

## Constitution Check

*GATE: debe pasar antes de la Fase 0. Re-evaluado tras el diseño de la Fase 1.*

| Principio | Cumplimiento en el diseño | Estado |
|---|---|---|
| I. Asincronía y Rendimiento NOC | Sondeo con `asyncio.gather(return_exceptions=True)` y `asyncio.wait_for(..., 1.5)` por consulta; `icmplib.async_ping` y API asyncio de pysnmp; SNMP omitido si ICMP está caído; trabajo bloqueante con `asyncio.to_thread` (R1, R2, R6) | ✅ |
| II. Modo Simulador Primero | `SIMULATION_MODE` selecciona `SimulatedPoller`/`RealPoller` tras una interfaz común `BasePoller` y contrato idéntico; endpoints toggle; pruebas sin laboratorio (R5) | ✅ |
| III. Resiliencia en Tiempo Real | `ConnectionManager` tolerante a fallos, reconexión con *backoff*, `calculate_kbps` con *wrap* de 32 bits y deltas inválidos (R3, R6) | ✅ |
| IV. Seguridad y Gobernanza | `load_settings()` con `python-dotenv`; `.env` ignorado por git; type hints y comentarios en español; pruebas automatizadas (R8) | ✅ |

**Post-diseño**: sin violaciones; Complexity Tracking no aplica.

## Project Structure

### Documentation (this feature)

```text
specs/001-noc-telemetry-dashboard/
├── plan.md              # Este archivo
├── research.md          # Fase 0
├── data-model.md        # Fase 1
├── quickstart.md        # Fase 1
├── contracts/           # Fase 1
│   ├── rest-api.yaml
│   ├── websocket-telemetry.md
│   └── telemetry-message.schema.json
├── checklists/
│   └── requirements.md
└── tasks.md             # Fase 2 (/speckit-tasks; NO creado aquí)
```

### Source Code (repository root)

```text
NOC-Telemetry-Dashboard/
├── main.py                  # FastAPI, REST, ConnectionManager, lifespan + bucle de fondo
├── telemetry_engine.py      # BasePoller, RealPoller, SimulatedPoller, calculate_kbps, evaluación de estado
├── topology_config.py       # Node/Edge/Interface, topología inicial, validación
├── templates/
│   └── dashboard.html
├── static/
│   └── js/
│       └── noc_dashboard.js
├── test_telemetry.py
├── requirements.txt
├── .env.example
└── .gitignore
```

**Structure Decision**: proyecto único con módulos en la raíz, tal como fija `ARCHITECTURE.md`; no se separan paquetes por el tamaño reducido. Si `telemetry_engine.py` crece demasiado, se evaluará dividir en un paquete en una enmienda posterior.

### Diseño de módulos (resumen)

- **`telemetry_engine.py`**
  - `Settings` y `load_settings()` (única lectura de `.env`): `SIMULATION_MODE`, `POLL_INTERVAL_SECONDS=3`, `POLL_TIMEOUT_SECONDS=1.5`, `SNMP_COMMUNITY`, `SNMP_PORT=161`, `LATENCY_DEGRADED_MS=50`, `CPU_DEGRADED_PERCENT=80`, `ICMP_PRIVILEGED`, `LINK_MAX_KBPS`, `SIMULATION_SEED`.
  - Funciones puras: `calculate_kbps`, `evaluate_node_status`, `evaluate_edge_status` (fáciles de probar).
  - `BasePoller.poll() -> TelemetrySnapshot`; `RealPoller` (ICMP+SNMP concurrentes por nodo) y `SimulatedPoller` (con `toggle_node`, `toggle_edge`).
  - `EventTracker`: compara el estado previo con el nuevo y genera `AlertEvent`.
  - OIDs como constantes: `IF_OPER_STATUS = 1.3.6.1.2.1.2.2.1.8`, `IF_IN_OCTETS = ...10`, `IF_OUT_OCTETS = ...16`, `CPU_5SEC_REV = 1.3.6.1.4.1.9.9.109.1.1.1.1.6.1`.
- **`main.py`**: `lifespan` crea el poller y la tarea de fondo; rutas según [rest-api.yaml](contracts/rest-api.yaml); `/ws/telemetry` según [websocket-telemetry.md](contracts/websocket-telemetry.md).
- **`noc_dashboard.js`**: módulos internos (conexión WS, grafo, panel, gráficas, eventos, controles de simulación visibles solo si `GET /api/config` indica `simulation: true`).
- **`test_telemetry.py`**: pruebas de `calculate_kbps`, umbrales de estado (tabla de casos límite: 49.9/50 ms, 0/1/50/51/100 % de pérdida, CPU 79.9/80), propagación nodo→enlaces, toggles 404/409, esquema JSON del payload, WebSocket (conexión, difusión, desconexión limpia).

## Cambios necesarios en documentos existentes
- `ARCHITECTURE.md` usa estados en mayúsculas (`UP`/`DOWN`/`DEGRADED`); este plan fija minúsculas (`up`/`degraded`/`down`) según la especificación. Se actualizará en la fase de implementación.
- Añadir `jinja2`, `pytest-asyncio` y `httpx` a `requirements.txt`, y las variables nuevas a `.env.example` (`LATENCY_DEGRADED_MS`, `CPU_DEGRADED_PERCENT`, `ICMP_PRIVILEGED`, `LINK_MAX_KBPS`, `SIMULATION_SEED`).

## Riesgos y supuestos abiertos
- Los `ifIndex` reales de IOSv deben verificarse en el laboratorio.
- Los nombres de interfaz de `R2-Dist`/`R3-Edge` (lado destino) se asumen `GigabitEthernet0/1`; ajustables en `topology_config.py`.
- API de `pysnmp-lextudio` variable entre versiones → versión fijada y función `snmp_get()` aislada.
- ICMP en Windows requiere privilegios de administrador.

## Complexity Tracking

Sin violaciones de la constitución; no aplica.
