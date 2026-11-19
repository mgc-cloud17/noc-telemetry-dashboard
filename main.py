"""Aplicación FastAPI principal para el NOC Topology & Real-Time Telemetry Dashboard.

Expone la interfaz web zero-build, los endpoints REST de topología/simulación,
el gestor de conexiones WebSocket `/ws/telemetry` y el ciclo `asyncio` periódico.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from telemetry_engine import (
    BasePoller,
    RealPoller,
    Settings,
    SimulatedPoller,
    TelemetrySnapshot,
    load_settings,
)
from topology_config import EDGES, NODES, topology_as_dict, validate_topology

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("noc.main")

BASE_DIR = Path(__file__).resolve().parent


class ConnectionManager:
    """Administra clientes WebSocket activos y difunde instantáneas sin bloquear."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        """Acepta y registra una nueva conexión WebSocket."""
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        """Elimina un cliente desconectado sin lanzar errores si ya no existe."""
        self._connections.discard(websocket)

    async def _send_safe(self, websocket: WebSocket, payload: dict[str, Any]) -> bool:
        """Envía JSON a un cliente individual capturando errores de red."""
        try:
            await websocket.send_json(payload)
            return True
        except (WebSocketDisconnect, RuntimeError, OSError):
            return False
        except Exception as exc:
            logger.debug("Error inesperado al enviar por WebSocket: %s", exc)
            return False

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Difunde un mensaje a todos los clientes y purga los desconectados."""
        if not self._connections:
            return

        clients = list(self._connections)
        results = await asyncio.gather(
            *(self._send_safe(ws, payload) for ws in clients),
            return_exceptions=True,
        )
        for ws, res in zip(clients, results, strict=True):
            if res is not True:
                self.disconnect(ws)


def create_app(
    settings: Settings | None = None,
    poller: BasePoller | None = None,
) -> FastAPI:
    """Fábrica de la aplicación FastAPI con inyección opcional para pruebas."""
    active_settings = settings if settings is not None else load_settings()
    validate_topology(NODES, EDGES)

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    ws_manager = ConnectionManager()
    poll_lock = asyncio.Lock()

    if poller is not None:
        active_poller: BasePoller = poller
    elif active_settings.simulation_mode:
        active_poller = SimulatedPoller(settings=active_settings)
    else:
        active_poller = RealPoller(settings=active_settings)

    async def run_poll_cycle(app_instance: FastAPI) -> TelemetrySnapshot:
        """Ejecuta un ciclo protegido por lock, guarda snapshot y difunde por WS."""
        async with poll_lock:
            snapshot = await asyncio.wait_for(
                active_poller.poll(),
                timeout=max(
                    active_settings.poll_timeout_seconds
                    + active_settings.snmp_timeout_seconds
                    + 1.0,
                    5.0,
                ),
            )
            app_instance.state.latest_snapshot = snapshot
        await ws_manager.broadcast(snapshot.to_dict())
        return snapshot

    async def telemetry_loop(app_instance: FastAPI) -> None:
        """Tarea en segundo plano que ejecuta ciclos cada `poll_interval_seconds`."""
        interval = active_settings.poll_interval_seconds
        while True:
            cycle_start = time.monotonic()
            try:
                await run_poll_cycle(app_instance)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Fallo en ciclo de telemetría: %s", exc)

            elapsed = time.monotonic() - cycle_start
            await asyncio.sleep(max(0.0, interval - elapsed))

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        """Inicializa el primer sondeo, arranca el bucle y limpia al apagar."""
        mode_name = "SIMULADOR" if active_settings.simulation_mode else "LABORATORIO REAL"
        logger.info("Iniciando NOC Telemetry Dashboard en modo %s.", mode_name)
        if not active_settings.simulation_mode:
            logger.info(
                "Nota ICMP: en Windows el ping raw puede requerir ejecutar como Administrador "
                "(ICMP_PRIVILEGED=%s).",
                active_settings.icmp_privileged,
            )

        try:
            await run_poll_cycle(app_instance)
        except Exception as exc:
            logger.warning("Primer ciclo inicial no completado aún: %s", exc)

        bg_task = asyncio.create_task(telemetry_loop(app_instance))
        app_instance.state.telemetry_task = bg_task
        try:
            yield
        finally:
            bg_task.cancel()
            try:
                await bg_task
            except asyncio.CancelledError:
                pass
            logger.info("Bucle de telemetría detenido limpiamente.")

    application = FastAPI(
        title="NOC Topology & Real-Time Telemetry Dashboard",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.latest_snapshot = None
    application.state.settings = active_settings
    application.state.poller = active_poller
    application.state.ws_manager = ws_manager

    static_dir = BASE_DIR / "static"
    if static_dir.exists():
        application.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @application.get("/", response_class=HTMLResponse)
    async def get_dashboard(request: Request) -> HTMLResponse:
        """Renderiza la interfaz HTML del Centro de Comando NOC."""
        return templates.TemplateResponse(request, "dashboard.html")

    @application.get("/api/topology")
    async def get_topology() -> dict[str, Any]:
        """Retorna la definición estática de nodos y enlaces."""
        return topology_as_dict()

    @application.get("/api/config")
    async def get_public_config() -> dict[str, Any]:
        """Retorna configuración pública de UI sin exponer secretos."""
        return {
            "simulation": active_settings.simulation_mode,
            "poll_interval_seconds": active_settings.poll_interval_seconds,
        }

    @application.get("/api/telemetry/latest")
    async def get_latest_telemetry() -> dict[str, Any]:
        """Retorna el último snapshot recolectado o 503 si aún no hay datos."""
        snapshot: TelemetrySnapshot | None = application.state.latest_snapshot
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Telemetría aún no disponible; esperando primer ciclo.",
            )
        return snapshot.to_dict()

    @application.get("/api/events")
    async def get_events() -> list[dict[str, Any]]:
        """Retorna los últimos eventos registrados (más recientes primero)."""
        return [evt.to_dict() for evt in active_poller.get_recent_events()]

    @application.post("/api/simulate/toggle-node/{node_id}")
    async def simulate_toggle_node(node_id: str) -> dict[str, Any]:
        """Alterna caída/restauración simulada de un nodo y difunde inmediatamente."""
        if not active_settings.simulation_mode or not isinstance(
            active_poller, SimulatedPoller
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Simulación desactivada (SIMULATION_MODE=false).",
            )
        try:
            forced_down = active_poller.toggle_node(node_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Nodo no encontrado: '{node_id}'.",
            ) from exc

        snapshot = await run_poll_cycle(application)
        node_status = next(
            (n.status.value for n in snapshot.nodes if n.id == node_id),
            "down" if forced_down else "up",
        )
        return {
            "id": node_id,
            "forced_down": forced_down,
            "status": node_status,
        }

    @application.post("/api/simulate/toggle-link/{edge_id}")
    async def simulate_toggle_link(edge_id: str) -> dict[str, Any]:
        """Alterna caída/restauración simulada de un enlace y difunde inmediatamente."""
        if not active_settings.simulation_mode or not isinstance(
            active_poller, SimulatedPoller
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Simulación desactivada (SIMULATION_MODE=false).",
            )
        try:
            forced_down = active_poller.toggle_link(edge_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Enlace no encontrado: '{edge_id}'.",
            ) from exc

        snapshot = await run_poll_cycle(application)
        edge_status = next(
            (e.status.value for e in snapshot.edges if e.id == edge_id),
            "down" if forced_down else "up",
        )
        return {
            "id": edge_id,
            "forced_down": forced_down,
            "status": edge_status,
        }

    @application.websocket("/ws/telemetry")
    async def websocket_telemetry(websocket: WebSocket) -> None:
        """Canal WebSocket en tiempo real; envía último snapshot al conectar."""
        await ws_manager.connect(websocket)
        try:
            snapshot: TelemetrySnapshot | None = application.state.latest_snapshot
            if snapshot is not None:
                sent = await ws_manager._send_safe(websocket, snapshot.to_dict())
                if not sent:
                    return

            while True:
                # Consumimos mensajes entrantes sin bloquear para detectar cierres limpios.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.debug("Conexión WebSocket cerrada por error controlado: %s", exc)
        finally:
            ws_manager.disconnect(websocket)

    return application


app = create_app()
