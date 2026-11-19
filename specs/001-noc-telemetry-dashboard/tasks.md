# Tasks: NOC Topology & Real-Time Telemetry Dashboard

**Input**: Documentos de diseño en `/specs/001-noc-telemetry-dashboard/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: SÍ solicitados (suite `pytest` en `test_telemetry.py`). Las pruebas de cada historia se escriben antes de la implementación y deben fallar primero.

**Organization**: Tareas agrupadas por historia de usuario. Rutas relativas a la raíz del repositorio. Todo el código Python MUST llevar *type hints* y comentarios en español técnico (constitución, Principio IV).

## Format: `[ID] [P?] [Story] Descripción`

- **[P]**: paralelizable (archivos distintos, sin dependencias pendientes)
- **[Story]**: US1–US4 según [spec.md](spec.md)
- Nota: `telemetry_engine.py`, `main.py` y `test_telemetry.py` son archivos únicos; las tareas que los modifican NO son [P] entre sí.

---

## Phase 1: Setup (Infraestructura compartida)

**Purpose**: Documentación alineada, dependencias fijadas y entorno listo

- [X] T001 Actualizar `ARCHITECTURE.md`: estados en minúsculas `up`/`degraded`/`down` (sustituir `UP`/`DOWN`/`DEGRADED` en estilos de color, contrato de payload y descripciones), actualizar la sección de contrato del payload según `contracts/telemetry-message.schema.json` (nodos/enlaces como listas, campo `events`, `cpu_percent`) y las reglas de estado de `research.md` R4
- [X] T002 [P] Crear `requirements.txt` fijando versiones **exactas** (`==`) de `fastapi`, `uvicorn[standard]`, `websockets`, `python-dotenv`, `icmplib`, `pysnmp-lextudio`, `pytest` y `httpx`, más las dependencias derivadas del plan: `jinja2`, `pytest-asyncio` y `jsonschema` (para pruebas de contrato). Últimas versiones consultadas en PyPI el 2026-11-18 (usar como punto de partida y validar tras instalar): `fastapi==0.164.0`, `uvicorn[standard]==0.54.0`, `websockets==17.4`, `python-dotenv==1.2.4`, `icmplib==3.0.4`, `pysnmp-lextudio==6.3.0`, `pytest==9.2.0`, `httpx==0.28.1`, `pytest-asyncio==1.5.0`, `jinja2==3.1.6`, `jsonschema==4.26.0`; si alguna no es compatible entre sí o con Python 3.12, resolver con `pip index versions <paquete>` y fijar una combinación compatible con Python 3.12; para `pysnmp-lextudio` comprobar que expone `pysnmp.hlapi.asyncio` y anotar en un comentario la forma de API (camelCase/snake_case) elegida
- [X] T003 [P] Ampliar `.env.example` con `LATENCY_DEGRADED_MS=50`, `CPU_DEGRADED_PERCENT=80`, `ICMP_PRIVILEGED=false`, `LINK_MAX_KBPS=1000000`, `SIMULATION_SEED=` (vacío) y comentarios en español; confirmar que `.env`, `venv/` y `__pycache__/` siguen en `.gitignore`
- [X] T004 Crear el entorno virtual `venv`, instalar `requirements.txt` y verificar `python -c "import fastapi, icmplib, pysnmp, dotenv, httpx, jsonschema"`; crear los directorios `templates/` y `static/js/` y la configuración `pytest.ini` (`asyncio_mode = auto`, `testpaths = .`)

---

## Phase 2: Foundational (Prerrequisitos bloqueantes)

**Purpose**: Topología, configuración, tipos, funciones puras y esqueleto de la app que TODAS las historias necesitan

**⚠️ CRITICAL**: Ninguna historia puede empezar hasta completar esta fase

### Pruebas fundacionales

- [X] T005 Crear `test_telemetry.py` con pruebas (deben fallar primero) de: `calculate_kbps` — caso normal, *wrap* de 32 bits (`prev=2**32-1000`, `curr=500`), primera muestra (`prev=None` → `0.0`), `delta_s <= 0` → `0.0`, resultado nunca negativo (SC-008); y de validación de topología (ids únicos, referencias válidas, IP válida, sin auto-enlaces ni duplicados) según `data-model.md`

### Implementación fundacional

- [X] T006 [P] Implementar `topology_config.py`: enums `NodeType`, dataclasses congeladas `Interface` (`Interface.name`, `Interface.if_index`), `Node` (`Node.id`, `Node.name`, `Node.ip`, `Node.type`, `Node.role`, `Node.position`, `Node.snmp_cpu_index`) y `Edge` (`Edge.id`, `Edge.source`, `Edge.target`, `Edge.source_if`, `Edge.target_if`) conforme a `data-model.md`; definir `NODES` = `R1-Core` (`192.168.76.10`, router, core), `R2-Dist` (`10.0.12.2`, router, distribution), `R3-Edge` (`10.0.13.2`, router, edge) y `EDGES` = `link-r1-r2` (origen `GigabitEthernet0/1`, `if_index=2`) y `link-r1-r3` (origen `GigabitEthernet0/2`, `if_index=3`), lado destino `GigabitEthernet0/1` en R2/R3 (supuesto del plan); `validate_topology()` con las reglas de validación del modelo; `topology_as_dict()` conforme a `contracts/rest-api.yaml` (`Topology`); soporte de carga desde JSON (`load_topology_from_json(path)`)
- [X] T007 Implementar en `telemetry_engine.py` (parte 1): `Settings` (dataclass congelada) y `load_settings()` con `python-dotenv` (única lectura de `.env`; variables de T003 más `SIMULATION_MODE`, `POLL_INTERVAL_SECONDS`, `POLL_TIMEOUT_SECONDS`, `SNMP_COMMUNITY`, `SNMP_PORT`) validando tipos y rangos sin exponer secretos en mensajes de error; enums `Status` y `EventKind`; dataclasses `NodeTelemetry` (`NodeTelemetry.status`, `latency_ms`, `packet_loss`, `cpu_percent`, `bw_in_kbps`, `bw_out_kbps`), `EdgeTelemetry` (`EdgeTelemetry.status`, `oper_status`, `bw_in_kbps`, `bw_out_kbps`), `AlertEvent` y `TelemetrySnapshot` según `data-model.md`, con `to_dict()` que respete `contracts/telemetry-message.schema.json`; constantes de OIDs (`IF_OPER_STATUS`, `IF_IN_OCTETS`, `IF_OUT_OCTETS`, `CPU_5SEC_REV`) y `COUNTER32_MODULO = 2**32`
- [X] T008 Implementar en `telemetry_engine.py` (parte 2, tras T007): `calculate_kbps(prev, curr, delta_s, counter_bits=32)` (research R3, incluyendo descarte por `LINK_MAX_KBPS`), clase `CounterTracker` (muestras por `(node_id, if_index)` con `time.monotonic()`), clase abstracta `BasePoller` con `async def poll() -> TelemetrySnapshot`. Hacer pasar las pruebas de T005
- [X] T009 Escribir en `test_telemetry.py` las pruebas de umbrales de estado (tabla de casos límite de `plan.md`): latencia 49.9/50 ms, pérdida 0/1/50/51/99/100 %, CPU 79.9/80 %, SNMP sin métricas → `degraded`, `ifOperStatus != 1` → `down`, nodo con todas las interfaces caídas → `down` vs. una de varias → `degraded`, prioridad `down > degraded > up`; estado de enlace (interfaz caída, nodo extremo `down`, SNMP parcial)
- [X] T010 Implementar en `telemetry_engine.py` (parte 3, tras T008): `evaluate_node_status(...)` y `evaluate_edge_status(...)` como funciones puras según `research.md` R4, usando umbrales de `Settings`; hacer pasar T009
- [X] T011 Implementar el esqueleto de `main.py` (tras T007): app FastAPI con `lifespan` vacío, montaje de `/static`, `Jinja2Templates("templates")`, `ConnectionManager` (`connect`, `disconnect`, `broadcast` con `asyncio.gather(..., return_exceptions=True)`, descarte de clientes que fallan, captura de `WebSocketDisconnect` y `RuntimeError`), `GET /api/config` y `GET /api/topology` (usa `topology_as_dict()`); `logging` configurado en español

**Checkpoint**: Fundación lista; las pruebas de T005 y T009 pasan.

---

## Phase 3: User Story 1 - Ver la topología y su estado en vivo (Priority: P1) 🎯 MVP

**Goal**: Dashboard con grafo vivo (3 nodos, 2 enlaces) que se actualiza cada 3 s vía WebSocket con telemetría simulada, colores verde/ámbar/rojo y reconexión automática.

**Independent Test**: Quickstart Escenario 2 pasos 1–3 y Escenario 3: abrir el dashboard en modo simulación, ver 3 nodos/2 enlaces verdes con métricas cambiando cada 3 s; detener y reiniciar Uvicorn y comprobar reconexión.

### Tests for User Story 1 ⚠️ (escribir primero; deben fallar)

- [X] T012 [US1] En `test_telemetry.py` añadir pruebas de `SimulatedPoller` sin fallos: devuelve 3 nodos y 2 enlaces con ids de `topology_config.py`, todos `up`, métricas en rango (`packet_loss` 0–100, `cpu_percent` 0–100, Kbps ≥ 0), reproducible con `SIMULATION_SEED`, contadores con *wrap* simulado sin tasas negativas
- [X] T013 [US1] En `test_telemetry.py` añadir prueba de contrato: `TelemetrySnapshot.to_dict()` valida contra `specs/001-noc-telemetry-dashboard/contracts/telemetry-message.schema.json` con `jsonschema`, y serializa a JSON sin error (incluye valores `null`)
- [X] T014 [US1] En `test_telemetry.py` añadir pruebas de `main.py` con `httpx.AsyncClient`/`TestClient`: `GET /` devuelve HTML 200, `GET /api/topology` cumple el esquema `Topology` de `contracts/rest-api.yaml`, `GET /api/config` indica `simulation`; WebSocket `/ws/telemetry`: recibe el último snapshot al conectar (FR-024), recibe broadcast periódico, múltiples clientes reciben el mismo mensaje, y la desconexión abrupta de un cliente no impide que otro siga recibiendo ni lanza excepciones (FR-020)

### Implementation for User Story 1

- [X] T015 [US1] Implementar `SimulatedPoller` en `telemetry_engine.py` (research R5, sin toggles todavía): latencia base por nodo + *jitter* gaussiano, tráfico base + sinusoide + ruido con contadores acumulativos de octetos (wrap a `2**32`) procesados con `calculate_kbps`, CPU con paseo aleatorio acotado y picos ocasionales, estado derivado con `evaluate_node_status`/`evaluate_edge_status`, semilla opcional; ejecución 100 % asíncrona (sin `time.sleep`). Hacer pasar T012–T013
- [X] T016 [US1] Completar `main.py` (tras T015): `GET /` renderiza `templates/dashboard.html`; en `lifespan` instanciar el poller según `SIMULATION_MODE` (real se completa en US4: mientras tanto lanzar error de configuración claro), guardar el último snapshot, crear la tarea de fondo con ciclo cada `POLL_INTERVAL_SECONDS` (`asyncio.wait_for(poller.poll(), …)`, duerme `max(0, intervalo − duración)`, captura y registra excepciones sin matar la tarea) y cancelarla limpiamente al apagar; endpoint `/ws/telemetry` que envía el último snapshot al conectar y mantiene la conexión sin excepciones no controladas; `GET /api/telemetry/latest` (503 si no hay datos). Hacer pasar T014
- [X] T017 [P] [US1] Crear `templates/dashboard.html`: HTML5 semántico (`header`, `main`, `aside`, `section`), modo oscuro estilo NOC con Tailwind CDN, Cytoscape.js y Chart.js por CDN con versiones fijadas, contenedor `#cy`, indicador de conexión WebSocket, KPIs globales (nodos up/degraded/down), `<aside id="node-panel">` oculto, sección `#events-log` y `#sim-controls` ocultas por defecto, carga de `/static/js/noc_dashboard.js`
- [X] T018 [US1] Crear `static/js/noc_dashboard.js` (parte 1): constantes de color (`up` `#10b981`, `degraded` `#f59e0b`, `down` `#ef4444`); `GET /api/topology` → inicializar Cytoscape con posiciones preestablecidas, etiquetas de nodo y de enlace (`source_if` ↔ `target_if` + estado); cliente WebSocket con reconexión automática (*backoff* exponencial 1 s → 10 s) e indicador de conexión; `applySnapshot(msg)` con `cy.batch()` para colorear nodos/enlaces y actualizar KPIs; ignorar mensajes inválidos o de `type` desconocido; mostrar `null` como "N/D"

**Checkpoint**: US1 funcional y comprobable de forma independiente (MVP).

---

## Phase 4: User Story 2 - Inspeccionar un equipo con ficha técnica y gráficas (Priority: P2)

**Goal**: Clic en un router abre un panel lateral con ficha técnica y dos gráficas deslizantes (Kbps In/Out y CPU %).

**Independent Test**: Quickstart Escenario 2 paso 4: clic en `R1-Core`, ver ficha y ambas gráficas añadiendo un punto cada 3 s; cambiar de nodo reinicia las gráficas.

### Tests for User Story 2 ⚠️

- [X] T019 [US2] En `test_telemetry.py` añadir prueba de que `GET /` incluye los contenedores requeridos por el panel (`node-panel`, canvas de ancho de banda y de CPU) y que `GET /static/js/noc_dashboard.js` se sirve con 200 (verificación estática del contrato UI)

### Implementation for User Story 2

- [X] T020 [P] [US2] Ampliar `templates/dashboard.html`: estructura del panel lateral (ficha: nombre, IP, tipo/rol, estado con color, latencia, pérdida, CPU, botón de cerrar) y dos `<canvas>` (`#chart-bw`, `#chart-cpu`) con etiquetas accesibles
- [X] T021 [US2] Ampliar `static/js/noc_dashboard.js` (parte 2): evento `tap` de nodo → abrir panel y poblar ficha desde `/api/topology` + último snapshot; historial por nodo en `Map` con ventana deslizante de 40 muestras (FR-011); gráficas Chart.js de línea (`animation: false`): Kbps entrada/salida y CPU % (eje 0–100) que se actualizan en cada `applySnapshot`; al seleccionar otro nodo se reinician las gráficas; cierre del panel sin afectar al grafo; manejo de `null` en CPU/latencia

**Checkpoint**: US1 y US2 funcionan de forma independiente.

---

## Phase 5: User Story 3 - Simular fallos y registrar alertas (Priority: P2)

**Goal**: Con `SIMULATION_MODE=true`, endpoints y controles de UI tumban/restauran nodos y enlaces con propagación; los cambios se registran en un panel de eventos en vivo.

**Independent Test**: Quickstart Escenario 2 pasos 5–7: toggle de `link-r1-r2` lo pone en rojo ≤ 4 s con evento; repetir lo restaura; toggle de `R2-Dist` también tumba `link-r1-r2`; id inexistente → 404.

### Tests for User Story 3 ⚠️

- [X] T022 [US3] En `test_telemetry.py` añadir pruebas de `SimulatedPoller.toggle_node` / `toggle_link`: alternan `forced_down`, nodo caído → nodo `down` con `packet_loss=100`, `latency_ms=None` y enlaces dependientes `down` (propagación), restaurar un nodo no restaura un enlace apagado manualmente, estados independientes combinados (`data-model.md` SimulationState), ids inexistentes lanzan `KeyError`
- [X] T023 [US3] En `test_telemetry.py` añadir pruebas de `EventTracker`: una transición genera exactamente un evento (`down`, `degraded`, `recovered`) con `previous`/`current` correctos, sin transición no hay evento, buffer máximo de 100 (`AlertEvent` según `data-model.md`), `GET /api/events` devuelve más recientes primero
- [X] T024 [US3] En `test_telemetry.py` añadir pruebas REST de `POST /api/simulate/toggle-node/{node_id}` y `/toggle-link/{edge_id}` con `httpx`: 200 con `ToggleResult` (`forced_down`, `status`), 404 para id inexistente, 409 con `SIMULATION_MODE=false`, y que el siguiente broadcast por WebSocket refleje el cambio y contenga el evento

### Implementation for User Story 3

- [X] T025 [US3] Implementar en `telemetry_engine.py`: `SimulatedPoller.toggle_node(node_id) -> bool` y `toggle_link(edge_id) -> bool` (`SimulationState.nodes_down`/`edges_down`, `KeyError` si no existe) con propagación nodo → enlaces en el cálculo de estado efectivo; clase `EventTracker` (estado previo por elemento, `deque(maxlen=100)`, ids incrementales, mensajes en español) integrada para llenar `TelemetrySnapshot.events`. Hacer pasar T022–T023
- [X] T026 [US3] Ampliar `main.py` (tras T025): `POST /api/simulate/toggle-node/{node_id}` y `POST /api/simulate/toggle-link/{edge_id}` (404 → `HTTPException`, 409 si no es simulación, respuesta `ToggleResult` con estado resultante) y `GET /api/events`; tras un toggle, forzar un ciclo de sondeo inmediato para difundir sin esperar 3 s. Hacer pasar T024
- [X] T027 [P] [US3] Ampliar `templates/dashboard.html`: sección `#sim-controls` (botón por nodo y por enlace, generados dinámicamente) y `#events-log` (lista desplazable con color por tipo de evento)
- [X] T028 [US3] Ampliar `static/js/noc_dashboard.js` (parte 3): consultar `GET /api/config` y mostrar `#sim-controls` solo si `simulation` es `true` (FR-015); botones que invocan los `POST` de toggle con manejo de errores y estado de carga; anexar `msg.events` al log (más reciente primero, máx. 100 visibles, con hora/elemento/transición) y cargar `GET /api/events` al iniciar

**Checkpoint**: US1–US3 funcionan; el demo completo en modo simulador está disponible.

---

## Phase 6: User Story 4 - Monitorear equipos reales del laboratorio (Priority: P3)

**Goal**: Con `SIMULATION_MODE=false`, el sondeo real (ICMP + SNMPv2c, 100 % asíncrono) alimenta el mismo contrato y UI.

**Independent Test**: Quickstart Escenario 4 (requiere GNS3); las pruebas unitarias con *mocks* se ejecutan sin laboratorio.

### Tests for User Story 4 ⚠️

- [X] T029 [US4] En `test_telemetry.py` añadir pruebas de `RealPoller` con `async_ping` y `snmp_get` sustituidos por *mocks*: nodo con ICMP ok y SNMP completo → `up`; pérdida 100 % → `down` y SNMP no se invoca; ICMP ok y SNMP con timeout → `degraded` conservando la latencia; `ifOperStatus != 1` → enlace `down`; nodo lento (mock que duerme > 1.5 s) no retrasa a los demás y el ciclo total ≤ ~1.6 s (Principio I); contadores con *wrap* de 32 bits producen Kbps ≥ 0; excepciones inesperadas no se propagan

### Implementation for User Story 4

- [X] T030 [US4] Implementar en `telemetry_engine.py` `ping_node(ip, settings)`: `icmplib.async_ping(count=3, interval=0.2, timeout=0.4, privileged=settings.icmp_privileged)` envuelto en `asyncio.wait_for(..., 1.5)`; devuelve `latency_ms` (`avg_rtt`) y `packet_loss` (%) y trata `NameLookupError`/`ICMPLibError`/`TimeoutError` como pérdida 100 % (research R1)
- [X] T031 [US4] Implementar en `telemetry_engine.py` `snmp_get(ip, oids, settings)`: única función que toca `pysnmp.hlapi.asyncio` (`CommunityData(..., mpModel=1)`, `UdpTransportTarget((ip, 161), timeout=1.0, retries=0)`), GET único con todos los OIDs del nodo (`ifOperStatus`/`ifInOctets`/`ifOutOctets` por `Interface.if_index` + `CPU_5SEC_REV`), envuelta en `asyncio.wait_for(..., 1.5)`; devuelve `dict[str, int] | None` y `None` ante timeout, `errorIndication` o `errorStatus`; la comunidad se toma solo de `Settings` y nunca se registra en logs (research R2)
- [X] T032 [US4] Implementar `RealPoller` en `telemetry_engine.py`: por nodo ejecutar `ping_node` y, si hay respuesta, `snmp_get`; concurrencia entre nodos con `asyncio.gather(..., return_exceptions=True)`; calcular Kbps con `CounterTracker` + `calculate_kbps` (suma de interfaces por nodo para `NodeTelemetry.bw_in_kbps`/`bw_out_kbps`); derivar estados con `evaluate_node_status`/`evaluate_edge_status`; integrar `EventTracker`; hacer pasar T029
- [X] T033 [US4] Actualizar `main.py` `lifespan`: seleccionar `RealPoller` cuando `SIMULATION_MODE=false` (retirar el error provisional de T016), comprobar que `/api/simulate/*` responde 409 y que `GET /api/config` oculta controles; documentar en logs (sin secretos) el modo activo y la advertencia de privilegios ICMP en Windows

**Checkpoint**: Las cuatro historias están completas.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T034 Ejecutar `pytest -q` completo y corregir fallos hasta 100 % verde, sin laboratorio ni red real (SC-009)
- [X] T035 Ejecutar los Escenarios 1–3 de `quickstart.md` en modo simulador con `uvicorn main:app` y verificar manualmente SC-001, SC-002, SC-005 y SC-007; anotar desviaciones y corregirlas
- [X] T036 [P] Revisión de gobernanza (constitución v1.0.0): buscar llamadas bloqueantes (`time.sleep`, `requests`, SNMP síncrono), secretos hardcodeados, funciones sin *type hints* y comentarios/docstrings que no estén en español; verificar que `.gitignore` ignora `.env`, `venv/` y `__pycache__/`
- [X] T037 [P] Actualizar `ARCHITECTURE.md` y `.env.example` con cualquier desviación surgida durante la implementación (p. ej. versiones de API de `pysnmp-lextudio`) y añadir un `README.md` breve con instrucciones de arranque que referencie `quickstart.md`
- [X] T038 Si hay laboratorio GNS3 disponible, ejecutar el Escenario 4 de `quickstart.md`, verificar `ifIndex` reales y los nombres de interfaz de R2/R3, y ajustar `topology_config.py`

---

## Dependencies & Execution Order

### Phase Dependencies
- **Setup (1)**: sin dependencias. **Foundational (2)**: depende de Setup; bloquea todas las historias.
- **US1 (3)** → depende de Foundational. **US2 (4)** → depende de US1 (reutiliza `applySnapshot` y el grafo). **US3 (5)** → depende de US1 (poller simulado y WebSocket); independiente de US2. **US4 (6)** → depende de Foundational y de `main.py` de US1 (T016); independiente de US2/US3, aunque `EventTracker` (T025) se reutiliza: ejecutar US3 antes de T032 o extraer `EventTracker` primero.
- **Polish (7)**: tras las historias deseadas.

### Orden dentro de archivos compartidos
- `telemetry_engine.py`: T007 → T008 → T010 → T015 → T025 → T030 → T031 → T032
- `main.py`: T011 → T016 → T026 → T033
- `test_telemetry.py`: T005 → T009 → T012 → T013 → T014 → T019 → T022 → T023 → T024 → T029
- `static/js/noc_dashboard.js`: T018 → T021 → T028; `templates/dashboard.html`: T017 → T020 → T027

### Parallel Opportunities
- Setup: T002 y T003 en paralelo.
- Foundational: T006 (`topology_config.py`) en paralelo con la escritura de T005 si se separan archivos de prueba lógicamente; T007 no depende de T006.
- US1: T017 (HTML) en paralelo con T015/T016 (backend).
- US2: T020 (HTML) en paralelo con la prueba T019.
- US3: T027 (HTML) en paralelo con T025/T026.
- Tras Foundational, un desarrollador puede tomar US4 (ICMP/SNMP) mientras otro avanza con el frontend de US1–US3.

### Parallel Example: User Story 1
```text
Task: "Crear templates/dashboard.html"                  (T017)
Task: "Implementar SimulatedPoller en telemetry_engine.py" (T015)
```

---

## Implementation Strategy

### MVP First (solo User Story 1)
1. Setup → 2. Foundational → 3. US1 → **PARAR y validar** con Quickstart Escenarios 2 (pasos 1–3) y 3 → demo.

### Incremental Delivery
1. Setup + Foundational → base lista.
2. US1 (MVP: grafo vivo) → validar.
3. US2 (panel + gráficas) → validar.
4. US3 (simulación de fallos + eventos) → demo completo sin laboratorio.
5. US4 (modo real) → validar con GNS3.
6. Polish.

## Notes
- Cada tarea se cierra con su prueba en verde; hacer *commit* por tarea o grupo lógico.
- Todo sondeo MUST usar `asyncio.gather` con timeout de 1.5 s; nunca llamadas bloqueantes en el event loop.
- Verificar que las pruebas fallen antes de implementar.
