# RULES — Reglas de Gobernanza del Proyecto

## 1. Asincronía y Event Loop
- **Nunca** bloquear el event loop de FastAPI con llamadas síncronas (`time.sleep`, `requests`, `subprocess` bloqueante, SNMP síncrono).
- Todo el sondeo de red debe ser asíncrono y concurrente mediante `asyncio.gather`.
- Cada sondeo (ICMP o SNMP) debe tener un **timeout estricto de 1.5 s** (`asyncio.wait_for`). Un dispositivo caído nunca debe retrasar el ciclo de 3 s.
- Usar `asyncio.gather(..., return_exceptions=True)` y tratar los fallos como estado `DOWN`/`DEGRADED`, no como excepciones propagadas.
- Si hay trabajo inevitablemente bloqueante, delegarlo con `asyncio.to_thread`.

## 2. WebSockets
- Manejo limpio de desconexiones: capturar `WebSocketDisconnect` y errores de envío; **sin excepciones no controladas**.
- El `ConnectionManager` debe eliminar clientes muertos durante el broadcast sin interrumpir a los demás.
- El cliente JS debe reconectarse automáticamente con backoff.

## 3. Seguridad y Configuración
- Credenciales y comunidades SNMP **solo** en `.env`; jamás en el código fuente ni en commits.
- Se versiona únicamente `.env.example` con valores ficticios.
- `.gitignore` debe ignorar como mínimo `.env`, `venv/` y `__pycache__/`.

## 4. Calidad de Código
- Código limpio, con **type hints** en todas las funciones y métodos.
- Comentarios y docstrings en **español técnico**.
- Funciones pequeñas y de responsabilidad única; sin números mágicos (usar constantes).
- Pruebas unitarias para cálculo de ancho de banda, cambios de estado y payloads JSON antes de integrar cambios.
