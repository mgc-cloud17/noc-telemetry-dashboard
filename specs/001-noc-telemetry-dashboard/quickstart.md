# Quickstart: validación de extremo a extremo

Guía de ejecución y validación. Contratos: [rest-api.yaml](contracts/rest-api.yaml), [websocket-telemetry.md](contracts/websocket-telemetry.md). Modelo: [data-model.md](data-model.md).

## Prerrequisitos
- Python 3.12
- Navegador moderno con acceso a internet (CDN de Tailwind, Cytoscape.js y Chart.js)
- Modo real: laboratorio GNS3 encendido, SNMPv2c habilitado en los routers y, en Windows, terminal como Administrador (ICMP raw)

## Preparación
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # ajustar valores
```

## Escenario 1 — Pruebas automatizadas (sin laboratorio)
```powershell
pytest -q
```
**Esperado**: todo en verde; cubre `calculate_kbps` (incl. *wrap* 2³²), reglas de estado, propagación en simulación, contrato JSON del payload y WebSocket. (SC-008, SC-009)

## Escenario 2 — Dashboard en modo simulador (US1, US2, US3)
1. En `.env`: `SIMULATION_MODE=true`.
2. `uvicorn main:app --reload` y abrir `http://localhost:8000/`.
3. **Esperado**: 3 nodos y 2 enlaces en verde `#10b981`, etiquetas de interfaz visibles, métricas cambiando cada 3 s sin recargar. (FR-006–FR-008)
4. Clic en `R1-Core`: panel lateral con ficha y dos gráficas deslizantes (Kbps In/Out y CPU %). (FR-010)
5. Pulsar el control de `link-r1-r2` (o `Invoke-RestMethod -Method Post http://localhost:8000/api/simulate/toggle-link/link-r1-r2`).
   **Esperado**: enlace rojo `#ef4444` ≤ 4 s y evento en el panel; repetir lo restaura. (SC-002, SC-006)
6. `POST /api/simulate/toggle-node/R2-Dist`: el nodo y `link-r1-r2` se ponen en rojo; `R1-Core` sigue verde/ámbar.
7. `POST /api/simulate/toggle-node/NoExiste` → `404`.

## Escenario 3 — Resiliencia WebSocket (FR-009, FR-020)
1. Con el dashboard abierto, detener Uvicorn: el indicador pasa a "desconectado" y el cliente reintenta con *backoff*.
2. Reiniciar Uvicorn: reconexión en < 15 s y se reanudan las actualizaciones. (SC-005)
3. Cerrar pestañas abruptamente: sin trazas de excepciones en la consola del servidor.

## Escenario 4 — Modo laboratorio real (US4)
1. En `.env`: `SIMULATION_MODE=false`, `SNMP_COMMUNITY=<comunidad>`; verificar `ifIndex` en `topology_config.py`.
2. Arrancar la aplicación; **esperado**: métricas reales; botones de simulación ocultos; `POST /api/simulate/*` → `409`. (FR-015, FR-016)
3. Apagar `R2-Dist` en GNS3: **esperado**: `down` en ≤ 2 ciclos (≈ 6 s) sin retrasar a los otros nodos. (SC-004)
4. Quitar el servicio SNMP de un router que sigue respondiendo ping: **esperado**: `degraded`.

## Verificación de contratos
```powershell
pytest -q -k "contract"   # valida el payload contra contracts/telemetry-message.schema.json
```
