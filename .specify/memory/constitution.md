<!--
Sync Impact Report
- Version change: (plantilla sin ratificar) → 1.0.0
- Principios modificados: ninguno renombrado (se definieron los 4 principios iniciales)
- Secciones añadidas: Principios Fundamentales (4), Restricciones Técnicas, Flujo de Desarrollo y Calidad, Gobernanza
- Secciones eliminadas: principio 5 de la plantilla (no requerido)
- TODOs pendientes: ninguno
- Fuentes sincronizadas: ARCHITECTURE.md, RULES.md
-->
# NOC Topology & Real-Time Telemetry Dashboard Constitution

## Core Principles

### I. Asincronía y Rendimiento NOC (NON-NEGOTIABLE)
Todo el sondeo de red (ICMP con `icmplib` y SNMPv2c con `pysnmp`/`pysnmp-lextudio`) MUST ser 100% asíncrono y ejecutarse de forma concurrente con `asyncio.gather`. Cada sondeo MUST tener un timeout estricto de 1.5 segundos (`asyncio.wait_for`). Está prohibido bloquear el *event loop* de FastAPI con llamadas síncronas (`time.sleep`, `requests`, SNMP síncrono); el trabajo inevitablemente bloqueante MUST delegarse con `asyncio.to_thread`. Los fallos de un dispositivo MUST traducirse en estado `DOWN`/`DEGRADED` (`return_exceptions=True`) y nunca retrasar el ciclo de 3 s ni la entrega por WebSocket.
*Rationale*: un NOC solo es útil si un nodo caído no degrada la visibilidad del resto.

### II. Modo Simulador Primero (Desacoplamiento del Laboratorio)
El sistema MUST soportar `SIMULATION_MODE=true` en `.env` para generar telemetría realista y permitir simular caídas de nodos y enlaces en vivo (`up` / `degraded` / `down`) mediante la API (`POST /api/simulate/toggle-link/{edge_id}` y `POST /api/simulate/toggle-node/{node_id}`), sin requerir GNS3 encendido durante el desarrollo o las pruebas. El poller real y el simulado MUST compartir la misma interfaz y producir el mismo contrato de payload JSON. Las pruebas automatizadas MUST ejecutarse sin dependencias de laboratorio ni red real.
*Rationale*: permite desarrollar y probar el frontend de forma determinista y reproducible.

### III. Resiliencia en Tiempo Real
Las conexiones y desconexiones del endpoint `/ws/telemetry` MUST gestionarse de forma limpia: capturar `WebSocketDisconnect` y errores de envío, eliminar clientes muertos durante el *broadcast* sin interrumpir a los demás, y no propagar excepciones no controladas. El cliente MUST reconectarse automáticamente con *backoff*. El cálculo de ancho de banda en Kbps (delta de octetos por segundo) MUST manejar el *wrap* de contadores SNMP de 32 bits ($2^{32}$) y deltas de tiempo no positivos sin producir valores negativos ni excepciones.
*Rationale*: los contadores SNMP se reinician y las redes fallan; el dashboard debe mantenerse estable.

### IV. Seguridad y Gobernanza del Código
No MUST existir secretos ni comunidades SNMP hardcodeadas: toda configuración sensible se carga desde `.env` mediante `python-dotenv`. Solo se versiona `.env.example` con valores ficticios, y `.gitignore` MUST ignorar `.env`, `venv/` y `__pycache__/`. Todo el código Python MUST usar *type hints* y estar comentado en español técnico. Toda lógica de cálculo de ancho de banda, cambios de estado de topología y emisión de payloads JSON MUST estar cubierta por pruebas unitarias automatizadas (`pytest`).
*Rationale*: protege credenciales y mantiene el proyecto mantenible y verificable.

## Restricciones Técnicas

- Backend: Python 3.12, FastAPI, Uvicorn, WebSockets nativos y `asyncio`.
- Frontend Zero-Build: HTML5 semántico, Tailwind CSS, Cytoscape.js y Chart.js por CDN; sin bundlers ni `npm`.
- Estados de topología: `UP` (verde), `DEGRADED` (ámbar), `DOWN` (rojo).
- Estructura y responsabilidades de archivos definidas en `ARCHITECTURE.md`; reglas operativas en `RULES.md`.

## Flujo de Desarrollo y Calidad

- Toda funcionalidad nueva MUST seguir el flujo Spec Kit (specify → plan → tasks → implement).
- Antes de integrar cambios, las pruebas unitarias MUST pasar localmente.
- Las revisiones MUST verificar el cumplimiento de los cuatro principios, en especial la ausencia de llamadas bloqueantes y de secretos en el código.
- Funciones pequeñas y de responsabilidad única; constantes en lugar de números mágicos.

## Governance

Esta constitución prevalece sobre otras prácticas del proyecto; `ARCHITECTURE.md` y `RULES.md` MUST mantenerse consistentes con ella. Las enmiendas requieren documentar el cambio y su justificación, actualizar los documentos afectados y registrar la nueva versión. Versionado semántico: MAJOR por eliminación o redefinición incompatible de principios; MINOR por nuevos principios o ampliación material; PATCH por aclaraciones y correcciones de redacción. Toda revisión de código o PR MUST verificar el cumplimiento; cualquier complejidad adicional MUST justificarse.

**Version**: 1.0.0 | **Ratified**: 2026-11-18 | **Last Amended**: 2026-11-18
