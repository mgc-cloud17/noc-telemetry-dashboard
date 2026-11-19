# Feature Specification: NOC Topology & Real-Time Telemetry Dashboard

**Feature Branch**: `001-noc-telemetry-dashboard`

**Created**: 2026-11-18

**Status**: Draft

**Input**: User description: "Define la especificación funcional completa para el proyecto NOC Topology & Real-Time Telemetry Dashboard: topología gestionada (R1-Core, R2-Dist, R3-Edge), visualización interactiva en tiempo real con actualización cada 3 s, panel lateral con gráficas, y modos Simulador y Laboratorio Real."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Ver la topología y su estado en vivo (Priority: P1)

Como operador NOC, abro el dashboard y veo el diagrama de la red con tres routers (`R1-Core`, `R2-Dist`, `R3-Edge`) y sus dos enlaces (`link-r1-r2` vía `GigabitEthernet0/1`, `link-r1-r3` vía `GigabitEthernet0/2`). Cada elemento muestra su etiqueta y color de estado, y se actualiza solo cada 3 segundos sin recargar la página.

**Why this priority**: es el valor central del producto; sin visibilidad en vivo no hay NOC.

**Independent Test**: con el sistema en modo simulación, abrir el dashboard y verificar que se muestran 3 nodos y 2 enlaces en verde y que los valores cambian periódicamente sin recargar.

**Acceptance Scenarios**:

1. **Given** el dashboard abierto y todos los equipos operativos, **When** se recibe telemetría, **Then** los nodos y enlaces se muestran en verde (`#10b981`) con sus etiquetas de nodo e interfaz.
2. **Given** el dashboard abierto, **When** transcurren 3 segundos, **Then** el estado y las métricas se refrescan automáticamente sin recarga de página.
3. **Given** un nodo en estado `degraded`, **When** se recibe la telemetría, **Then** el nodo se muestra en ámbar (`#f59e0b`).
4. **Given** un nodo o interfaz en estado `down`, **When** se recibe la telemetría, **Then** el nodo y su enlace asociado cambian a rojo (`#ef4444`) en el siguiente ciclo.
5. **Given** que se pierde la conexión con el servidor, **When** el servidor vuelve a estar disponible, **Then** el dashboard se reconecta solo y reanuda las actualizaciones, indicando visualmente el estado de la conexión.

---

### User Story 2 - Inspeccionar un equipo con ficha técnica y gráficas (Priority: P2)

Como operador, hago clic sobre un router en el diagrama y se despliega un panel lateral con su ficha técnica (nombre, IP, rol, estado, latencia, pérdida de paquetes) y dos gráficas deslizantes en tiempo real: ancho de banda (Kbps entrada/salida) y uso de CPU (%).

**Why this priority**: permite diagnosticar la causa de una alerta, pero depende de que la visualización base (P1) funcione.

**Independent Test**: hacer clic en `R1-Core` y comprobar que aparece el panel con datos y que ambas gráficas añaden un punto nuevo cada 3 s.

**Acceptance Scenarios**:

1. **Given** el diagrama visible, **When** el operador hace clic en un router, **Then** se abre el panel lateral con la ficha técnica de ese router.
2. **Given** el panel abierto, **When** llega nueva telemetría, **Then** las gráficas de Kbps In/Out y CPU (%) se desplazan agregando el punto más reciente y descartando el más antiguo de la ventana.
3. **Given** el panel abierto para un nodo, **When** el operador selecciona otro nodo, **Then** el panel y las gráficas se reinician con los datos del nuevo nodo.
4. **Given** el panel abierto, **When** el operador lo cierra, **Then** el diagrama permanece visible y sigue actualizándose.

---

### User Story 3 - Simular fallos y registrar alertas (Priority: P2)

Como operador o desarrollador con `SIMULATION_MODE=true`, uso controles en la interfaz (y los endpoints `POST /api/simulate/toggle-node/{node_id}` y `POST /api/simulate/toggle-link/{edge_id}`) para provocar o restaurar la caída de un nodo o enlace en vivo, y veo cada cambio registrado en un panel de eventos.

**Why this priority**: permite demostrar y probar el sistema sin laboratorio, conforme al principio "Simulador Primero".

**Independent Test**: en modo simulación, accionar el control de `link-r1-r2` y verificar que el enlace se pone rojo, aparece una alerta en el panel de eventos y, al accionarlo de nuevo, se restaura con otra entrada de evento.

**Acceptance Scenarios**:

1. **Given** modo simulación activo, **When** se invoca toggle sobre un nodo operativo, **Then** el nodo pasa a `down`, sus enlaces asociados se reflejan como `down` y se registra un evento de alerta con hora, elemento y nuevo estado.
2. **Given** un enlace caído por simulación, **When** se invoca toggle de nuevo sobre ese enlace, **Then** se restaura y se registra un evento de recuperación.
3. **Given** un identificador de nodo o enlace inexistente, **When** se invoca toggle, **Then** la API responde con error claro de recurso no encontrado y no altera el estado.
4. **Given** `SIMULATION_MODE=false`, **When** se invoca cualquier endpoint de simulación, **Then** la API lo rechaza con error de conflicto y no modifica nada; los controles de simulación no se muestran en la interfaz.
5. **Given** modo simulación, **When** no hay fallos provocados, **Then** la telemetría generada es realista (latencia con variación, tráfico y CPU fluctuantes, contadores coherentes).

---

### User Story 4 - Monitorear equipos reales del laboratorio (Priority: P3)

Como ingeniero de redes con `SIMULATION_MODE=false`, el sistema consulta los equipos reales del laboratorio mediante ping ICMP asíncrono y SNMPv2c, y muestra la misma interfaz y métricas que en modo simulación.

**Why this priority**: aporta el valor de monitoreo real, pero requiere laboratorio; el desarrollo avanza antes con el simulador.

**Independent Test**: con el laboratorio encendido y la comunidad SNMP configurada en `.env`, verificar que las métricas del dashboard corresponden a las de los equipos y que apagar un router lo marca `down` en no más de 2 ciclos.

**Acceptance Scenarios**:

1. **Given** equipos alcanzables, **When** se completa un ciclo de sondeo, **Then** cada nodo reporta latencia promedio, pérdida de paquetes, CPU y tasas de tráfico de entrada/salida.
2. **Given** un equipo que no responde en el tiempo máximo de 1.5 s, **When** termina el ciclo, **Then** ese nodo se marca `down` o `degraded` sin retrasar la actualización de los demás.
3. **Given** un nodo con respuesta ICMP pero SNMP sin respuesta, **When** termina el ciclo, **Then** el nodo se marca `degraded` y se conserva la latencia.
4. **Given** una interfaz con estado operativo distinto de activo, **When** termina el ciclo, **Then** el enlace asociado se marca `down`.

---

### Edge Cases

- ¿Qué ocurre cuando el contador de octetos de una interfaz se reinicia (*wrap* de 32 bits)? La tasa MUST calcularse correctamente, sin valores negativos.
- ¿Qué ocurre en la primera lectura, sin muestra previa? La tasa se reporta como 0 hasta tener dos muestras.
- ¿Qué ocurre si el intervalo de tiempo entre muestras es cero o negativo? Se descarta el cálculo sin error.
- ¿Qué ocurre si un cliente cierra el navegador abruptamente? El servidor lo elimina sin errores y los demás clientes siguen recibiendo datos.
- ¿Qué ocurre si no hay clientes conectados? El servidor sigue operativo sin errores.
- ¿Qué ocurre cuando un nodo cae? Sus enlaces asociados se reflejan como `down`.
- ¿Qué ocurre si falta o es inválida la configuración (`.env`, comunidad SNMP)? El sistema informa el error de configuración con claridad y no expone secretos.
- ¿Qué ocurre si varios operadores accionan la simulación a la vez? Todos los clientes ven el mismo estado consistente.
- ¿Qué ocurre con el historial de gráficas si se cambia de nodo o se reconecta? La ventana se reinicia o rellena sin errores visuales.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: El sistema MUST gestionar una topología con tres nodos: `R1-Core` (`192.168.76.10`), `R2-Dist` (`10.0.12.2`) y `R3-Edge` (`10.0.13.2`), y dos enlaces: `link-r1-r2` (`GigabitEthernet0/1`) y `link-r1-r3` (`GigabitEthernet0/2`).
- **FR-002**: La definición de topología MUST ser estructurada y extensible (nodos de tipo router, switch o servidor; enlaces con interfaces de origen y destino) sin modificar la lógica de sondeo.
- **FR-003**: Cada nodo MUST reportar: estado operativo (`up`, `degraded`, `down`), `latency_ms` (promedio), `packet_loss`, uso de CPU (%), `bw_in_kbps` y `bw_out_kbps`.
- **FR-004**: El sistema MUST determinar el estado de un nodo: `down` si no responde; `degraded` si hay pérdida parcial, latencia elevada o falla parcial de telemetría; `up` en caso contrario. Los umbrales MUST ser configurables y documentados.
- **FR-005**: El sistema MUST determinar el estado de un enlace a partir del estado operativo de su interfaz y de los nodos que conecta.
- **FR-006**: El dashboard MUST mostrar el grafo interactivo con nodos, enlaces, etiquetas de nodo e interfaz, y estado.
- **FR-007**: El dashboard MUST usar los colores verde `#10b981` (`up`), ámbar `#f59e0b` (`degraded`) y rojo `#ef4444` (`down`).
- **FR-008**: El dashboard MUST actualizarse automáticamente cada 3 segundos mediante el canal en tiempo real `/ws/telemetry`, sin recargar la página.
- **FR-009**: El cliente MUST reconectarse automáticamente tras una pérdida de conexión e indicar visualmente el estado de conexión.
- **FR-010**: Al hacer clic en un router o switch, el sistema MUST mostrar un panel lateral con ficha técnica y dos gráficas deslizantes en tiempo real: Kbps entrada/salida y CPU (%).
- **FR-011**: Las gráficas MUST mantener una ventana deslizante de longitud fija de muestras recientes.
- **FR-012**: El cálculo de tasas de tráfico MUST derivarse del delta de octetos por segundo expresado en Kbps, manejando el *wrap* de contadores de 32 bits, la primera muestra y los intervalos no positivos.
- **FR-013**: Con `SIMULATION_MODE=true`, el sistema MUST generar telemetría simulada realista sin requerir laboratorio ni red real.
- **FR-014**: Con `SIMULATION_MODE=true`, el sistema MUST exponer `POST /api/simulate/toggle-node/{node_id}` y `POST /api/simulate/toggle-link/{edge_id}` para provocar o restaurar caídas, devolviendo el nuevo estado.
- **FR-015**: La interfaz MUST ofrecer controles de simulación únicamente en modo simulación.
- **FR-016**: Los endpoints de simulación MUST responder con error de no encontrado ante identificadores inexistentes y con error de conflicto cuando el modo simulación está desactivado.
- **FR-017**: El sistema MUST registrar eventos de alerta y recuperación (hora, elemento, estado anterior y nuevo) y mostrarlos en un panel de eventos en la interfaz.
- **FR-018**: Con `SIMULATION_MODE=false`, el sistema MUST consultar equipos reales mediante ping ICMP asíncrono (latencia y pérdida) y SNMPv2c (estado de interfaz, octetos de entrada/salida y CPU).
- **FR-019**: Todo el sondeo MUST ejecutarse de forma concurrente y asíncrona, con tiempo máximo de 1.5 s por consulta, de modo que un equipo caído no retrase el ciclo de 3 s ni la entrega a los clientes (constitución, Principio I).
- **FR-020**: Las conexiones y desconexiones de clientes en `/ws/telemetry` MUST gestionarse sin excepciones no controladas y sin afectar a los demás clientes (Principio III).
- **FR-021**: Toda la configuración sensible (comunidad SNMP, modo de operación) MUST cargarse desde variables de entorno (`.env`) y no estar en el código fuente (Principio IV).
- **FR-022**: El sistema MUST proveer pruebas automatizadas que no requieran laboratorio, cubriendo cálculo de ancho de banda, cambios de estado de topología y estructura del payload JSON.
- **FR-023**: Cada mensaje de telemetría MUST contener marca de tiempo, indicador de modo simulación y el estado y métricas de todos los nodos y enlaces en una estructura consistente entre ambos modos.
- **FR-024**: Un cliente que se conecta MUST recibir el último estado conocido de inmediato, sin esperar al siguiente ciclo.

### Key Entities

- **Nodo**: equipo de red (router, switch o servidor) con identificador, nombre, dirección IP, tipo y rol jerárquico (core, distribución, borde).
- **Enlace**: conexión entre dos nodos con identificador, interfaz de origen y de destino, y estado.
- **Telemetría de nodo**: estado, latencia, pérdida de paquetes, CPU y tasas de tráfico de entrada/salida en un instante.
- **Telemetría de enlace**: estado operativo de la interfaz y tasas de tráfico asociadas.
- **Evento de alerta**: registro con hora, elemento afectado, tipo (caída, degradación, recuperación) y estado.
- **Instantánea de telemetría**: conjunto completo de métricas de todos los nodos y enlaces en un ciclo, con marca de tiempo y modo.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Un operador ve la topología completa con estado de todos los elementos en menos de 3 segundos tras abrir el dashboard.
- **SC-002**: Un cambio de estado (caída o recuperación) se refleja visualmente en el diagrama en un máximo de 4 segundos desde que ocurre.
- **SC-003**: El dashboard permanece actualizado de forma continua durante al menos 1 hora sin recargas ni intervención manual.
- **SC-004**: Un equipo caído o lento no retrasa la actualización de los demás: el intervalo entre actualizaciones se mantiene en 3 segundos (±1 s) en el 99 % de los ciclos.
- **SC-005**: Tras una pérdida de conexión, el dashboard se recupera automáticamente en menos de 15 segundos una vez disponible el servidor.
- **SC-006**: El 100 % de las caídas y recuperaciones provocadas en simulación quedan registradas en el panel de eventos.
- **SC-007**: Un operador abre la ficha de cualquier equipo y ve sus gráficas con datos en un máximo de 2 clics y 1 segundo.
- **SC-008**: Las tasas de tráfico calculadas son siempre no negativas, incluso ante reinicio de contadores, en el 100 % de los casos de prueba.
- **SC-009**: La suite de pruebas automatizadas se ejecuta completa sin laboratorio ni red real y pasa al 100 %.
- **SC-010**: Soportar al menos 10 clientes simultáneos sin degradación perceptible; la desconexión de uno no interrumpe a los demás.

## Assumptions

- Los usuarios son operadores NOC o ingenieros en un entorno de laboratorio o demostración, con navegador moderno; no se incluye autenticación de usuarios en esta versión.
- La topología inicial es fija (3 routers, 2 enlaces) y se define en configuración; edición gráfica de la topología queda fuera de alcance.
- Se usa SNMPv2c con una comunidad de solo lectura configurada en `.env`; SNMPv3 queda fuera de alcance.
- No se persiste historial: las gráficas y el panel de eventos mantienen solo datos recientes en memoria durante la sesión.
- Umbrales por defecto de `degraded`: pérdida de paquetes parcial (> 0 % y < 100 %) o latencia superior a un valor configurable; los valores se precisarán en el plan.
- El intervalo de actualización es de 3 segundos y el tiempo máximo por consulta de 1.5 s, conforme a la constitución.
- La CPU se obtiene de equipos Cisco IOS; en equipos sin soporte se reporta como no disponible.
- El dashboard se sirve desde la misma aplicación que expone la API y el canal en tiempo real.
- Depende de que el laboratorio (GNS3) esté encendido y alcanzable únicamente en modo real.
