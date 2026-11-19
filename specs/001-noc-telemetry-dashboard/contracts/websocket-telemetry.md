# Contrato WebSocket: `/ws/telemetry`

## Conexión
- URL: `ws://<host>:<port>/ws/telemetry` (`wss://` si hay TLS).
- Sin autenticación ni mensajes del cliente (los mensajes entrantes se ignoran). Solo el servidor emite.

## Comportamiento del servidor
1. Al aceptar la conexión envía de inmediato el último snapshot (si existe).
2. Cada 3 s (`POLL_INTERVAL_SECONDS`) difunde un nuevo snapshot a todos los clientes.
3. Desconexión del cliente (cierre, error de red, `WebSocketDisconnect`): se elimina del registro sin excepciones no controladas y sin afectar a los demás.
4. Si un envío falla, ese cliente se descarta; el resto continúa.

## Mensaje `telemetry` (servidor → cliente)
Esquema: [telemetry-message.schema.json](telemetry-message.schema.json).

```json
{
  "type": "telemetry",
  "timestamp": "2026-11-18T18:51:30Z",
  "simulation": true,
  "nodes": [
    { "id": "R1-Core", "status": "up", "latency_ms": 2.4, "packet_loss": 0.0,
      "cpu_percent": 17.0, "bw_in_kbps": 1530.2, "bw_out_kbps": 980.7 },
    { "id": "R2-Dist", "status": "down", "latency_ms": null, "packet_loss": 100.0,
      "cpu_percent": null, "bw_in_kbps": 0.0, "bw_out_kbps": 0.0 }
  ],
  "edges": [
    { "id": "link-r1-r2", "status": "down", "oper_status": null,
      "bw_in_kbps": 0.0, "bw_out_kbps": 0.0 }
  ],
  "events": [
    { "id": 12, "timestamp": "2026-11-18T18:51:30Z", "element_type": "node",
      "element_id": "R2-Dist", "kind": "down", "previous": "up", "current": "down",
      "message": "R2-Dist no responde (pérdida 100 %)" }
  ]
}
```

## Reglas para el cliente (`noc_dashboard.js`)
- Colores: `up` → `#10b981`, `degraded` → `#f59e0b`, `down` → `#ef4444`.
- Reconexión automática con *backoff* exponencial (1 s → máx. 10 s); indicador visible de estado de conexión.
- `events` se anexan al panel de eventos (más reciente primero, máximo 100 visibles).
- Campos `null` se muestran como "N/D".
- Un mensaje inválido o con `type` desconocido se ignora sin romper el cliente.
