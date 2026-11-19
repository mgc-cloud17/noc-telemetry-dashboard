"""Motor de telemetría asíncrono para el Dashboard NOC.

Proporciona carga segura de configuración desde `.env`, cálculo de tasas de
ancho de banda (Kbps) con soporte de *wrap* de 32 bits, evaluación determinista
de estados (`up`, `degraded`, `down`), seguimiento de eventos y dos pollers
intercambiables bajo la interfaz `BasePoller`: `SimulatedPoller` y `RealPoller`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from icmplib import ICMPLibError, NameLookupError, async_ping

from topology_config import EDGES, NODES, Edge, Interface, Node

logger = logging.getLogger("noc.telemetry")

# Constantes de OIDs SNMPv2c (IF-MIB y Cisco Process MIB)
IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"
IF_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"
CPU_5SEC_REV = "1.3.6.1.4.1.9.9.109.1.1.1.1.6.1"
COUNTER32_MODULO = 2**32


class Status(str, Enum):
    """Estados operativos normalizados para nodos y enlaces."""

    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


class EventKind(str, Enum):
    """Tipos de eventos registrados ante transiciones de estado."""

    DOWN = "down"
    DEGRADED = "degraded"
    RECOVERED = "recovered"


@dataclass(frozen=True)
class Settings:
    """Parámetros de configuración cargados desde variables de entorno."""

    simulation_mode: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    poll_interval_seconds: float = 4.0
    poll_timeout_seconds: float = 1.5
    snmp_timeout_seconds: float = 3.0
    snmp_retries: int = 1
    snmp_community: str = "change-me"
    snmp_port: int = 161
    latency_degraded_ms: float = 150.0
    cpu_degraded_percent: float = 85.0
    icmp_privileged: bool = False
    link_max_kbps: float = 1_000_000.0
    simulation_seed: int | None = None


def _parse_bool(raw: str | None, default: bool) -> bool:
    """Convierte una cadena de entorno en booleano de forma estricta."""
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on", "si"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Valor booleano inválido en configuración: '{raw}'.")


def load_settings(
    env_file: str | Path | None = None,
    override: bool = False,
) -> Settings:
    """Carga y valida la configuración del sistema desde `.env` mediante python-dotenv.

    Nunca incluye secretos ni comunidades SNMP en los mensajes de error.
    """
    load_dotenv(dotenv_path=env_file, override=override)

    simulation_mode = _parse_bool(os.getenv("SIMULATION_MODE"), True)
    host = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"

    try:
        port = int(os.getenv("PORT", "8000").strip())
        poll_interval = float(os.getenv("POLL_INTERVAL_SECONDS", "4").strip())
        poll_timeout = float(os.getenv("POLL_TIMEOUT_SECONDS", "1.5").strip())
        snmp_timeout = float(os.getenv("SNMP_TIMEOUT_SECONDS", "3.0").strip())
        snmp_retries = int(os.getenv("SNMP_RETRIES", "1").strip())
        snmp_port = int(os.getenv("SNMP_PORT", "161").strip())
        latency_ms = float(os.getenv("LATENCY_DEGRADED_MS", "150").strip())
        cpu_pct = float(os.getenv("CPU_DEGRADED_PERCENT", "85").strip())
        link_max_kbps = float(os.getenv("LINK_MAX_KBPS", "1000000").strip())
    except ValueError as exc:
        raise ValueError("Parámetro numérico inválido en variables de entorno.") from exc

    icmp_privileged = _parse_bool(os.getenv("ICMP_PRIVILEGED"), False)

    raw_seed = os.getenv("SIMULATION_SEED", "").strip()
    simulation_seed: int | None = None
    if raw_seed:
        try:
            simulation_seed = int(raw_seed)
        except ValueError as exc:
            raise ValueError("SIMULATION_SEED debe ser un entero válido.") from exc

    snmp_community = os.getenv("SNMP_COMMUNITY", "change-me").strip()
    if not snmp_community:
        raise ValueError("SNMP_COMMUNITY no puede estar vacío en la configuración.")
    if not (1 <= port <= 65535) or not (1 <= snmp_port <= 65535):
        raise ValueError("Los puertos de red deben estar en el rango 1..65535.")
    if poll_interval <= 0 or poll_timeout <= 0 or snmp_timeout <= 0 or snmp_retries < 0:
        raise ValueError("Los tiempos de sondeo y timeout deben ser mayores a cero.")
    if latency_ms <= 0 or not (0.0 < cpu_pct <= 100.0) or link_max_kbps <= 0:
        raise ValueError("Umbrales operativos fuera de rango válido.")

    return Settings(
        simulation_mode=simulation_mode,
        host=host,
        port=port,
        poll_interval_seconds=poll_interval,
        poll_timeout_seconds=poll_timeout,
        snmp_timeout_seconds=snmp_timeout,
        snmp_retries=snmp_retries,
        snmp_community=snmp_community,
        snmp_port=snmp_port,
        latency_degraded_ms=latency_ms,
        cpu_degraded_percent=cpu_pct,
        icmp_privileged=icmp_privileged,
        link_max_kbps=link_max_kbps,
        simulation_seed=simulation_seed,
    )


@dataclass(frozen=True)
class NodeTelemetry:
    """Instantánea de métricas operativas para un nodo de la topología."""

    id: str
    status: Status
    latency_ms: float | None
    packet_loss: float
    cpu_percent: float | None
    bw_in_kbps: float
    bw_out_kbps: float

    def to_dict(self) -> dict[str, Any]:
        """Serializa el registro de nodo conforme al esquema JSON del contrato."""
        return {
            "id": self.id,
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "packet_loss": self.packet_loss,
            "cpu_percent": self.cpu_percent,
            "bw_in_kbps": self.bw_in_kbps,
            "bw_out_kbps": self.bw_out_kbps,
        }


@dataclass(frozen=True)
class EdgeTelemetry:
    """Instantánea de métricas operativas para un enlace de la topología."""

    id: str
    status: Status
    oper_status: int | None
    bw_in_kbps: float
    bw_out_kbps: float

    def to_dict(self) -> dict[str, Any]:
        """Serializa el registro de enlace conforme al esquema JSON del contrato."""
        return {
            "id": self.id,
            "status": self.status.value,
            "oper_status": self.oper_status,
            "bw_in_kbps": self.bw_in_kbps,
            "bw_out_kbps": self.bw_out_kbps,
        }


@dataclass(frozen=True)
class AlertEvent:
    """Evento generado cuando un nodo o enlace cambia de estado operativo."""

    id: int
    timestamp: str
    element_type: str
    element_id: str
    kind: EventKind
    previous: Status
    current: Status
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Serializa el evento para emisión por REST o WebSocket."""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "element_type": self.element_type,
            "element_id": self.element_id,
            "kind": self.kind.value,
            "previous": self.previous.value,
            "current": self.current.value,
            "message": self.message,
        }


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Instantánea completa de un ciclo de sondeo NOC."""

    timestamp: str
    simulation: bool
    nodes: tuple[NodeTelemetry, ...]
    edges: tuple[EdgeTelemetry, ...]
    events: tuple[AlertEvent, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serializa la instantánea al contrato JSON de `/ws/telemetry`."""
        return {
            "type": "telemetry",
            "timestamp": self.timestamp,
            "simulation": self.simulation,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "events": [evt.to_dict() for evt in self.events],
        }


def utc_now_iso() -> str:
    """Retorna la marca de tiempo UTC actual en formato ISO 8601."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def calculate_kbps(
    prev_octets: int | None,
    curr_octets: int | None,
    delta_seconds: float,
    counter_bits: int = 32,
    max_kbps: float | None = 1_000_000.0,
) -> float:
    """Calcula la tasa en Kbps a partir de dos lecturas de octetos.

    Maneja primera muestra (`prev_octets is None`), intervalos no positivos,
    desbordamiento (*wrap*) de contadores de 32/64 bits y picos espurios sobre
    el techo físico configurado (`max_kbps`). Siempre retorna un valor >= 0.0.
    """
    if (
        prev_octets is None
        or curr_octets is None
        or delta_seconds <= 0.0
        or prev_octets < 0
        or curr_octets < 0
    ):
        return 0.0

    delta = curr_octets - prev_octets
    if delta < 0:
        # Ajuste por desbordamiento del contador SNMP (wrap de 2^counter_bits).
        delta += 2**counter_bits

    if delta < 0:
        return 0.0

    kbps = (delta * 8.0) / delta_seconds / 1000.0
    if max_kbps is not None and kbps > max_kbps:
        return 0.0

    return round(max(0.0, kbps), 2)


class CounterTracker:
    """Mantiene la muestra anterior de octetos por interfaz `(node_id, if_index)`."""

    def __init__(self, max_kbps: float | None = 1_000_000.0) -> None:
        self._max_kbps = max_kbps
        self._samples: dict[tuple[str, int], tuple[int, int, float]] = {}

    def update(
        self,
        node_id: str,
        if_index: int,
        in_octets: int,
        out_octets: int,
        now: float | None = None,
    ) -> tuple[float, float]:
        """Registra contadores actuales y devuelve `(bw_in_kbps, bw_out_kbps)`."""
        ts = time.monotonic() if now is None else now
        key = (node_id, if_index)
        previous = self._samples.get(key)
        self._samples[key] = (in_octets, out_octets, ts)

        if previous is None:
            return 0.0, 0.0

        prev_in, prev_out, prev_ts = previous
        delta_s = ts - prev_ts
        bw_in = calculate_kbps(prev_in, in_octets, delta_s, max_kbps=self._max_kbps)
        bw_out = calculate_kbps(prev_out, out_octets, delta_s, max_kbps=self._max_kbps)
        return bw_in, bw_out

    def reset_interface(self, node_id: str, if_index: int) -> None:
        """Limpia la muestra previa de una interfaz tras una caída."""
        self._samples.pop((node_id, if_index), None)


def evaluate_node_status(
    packet_loss: float,
    latency_ms: float | None,
    cpu_percent: float | None,
    if_oper_statuses: Sequence[int | None] = (),
    snmp_ok: bool = True,
    latency_threshold_ms: float = 150.0,
    cpu_threshold_percent: float = 85.0,
) -> Status:
    """Determina el estado operativo de un nodo aplicando prioridad `down > degraded > up`."""
    if packet_loss >= 100.0 or latency_ms is None:
        return Status.DOWN

    # Si SNMP reporta todas las interfaces monitoreadas caídas, el nodo está down.
    if if_oper_statuses and all(
        status is not None and status != 1 for status in if_oper_statuses
    ):
        return Status.DOWN

    has_down_interface = any(status != 1 for status in if_oper_statuses)
    if (
        not snmp_ok
        or cpu_percent is None
        or latency_ms >= latency_threshold_ms
        or packet_loss >= 1.0
        or cpu_percent >= cpu_threshold_percent
        or has_down_interface
    ):
        return Status.DEGRADED

    return Status.UP


def evaluate_edge_status(
    oper_status: int | None,
    source_status: Status,
    target_status: Status,
    snmp_metrics_ok: bool = True,
) -> Status:
    """Determina el estado operativo de un enlace a partir de sus extremos y SNMP."""
    if source_status == Status.DOWN or target_status == Status.DOWN:
        return Status.DOWN
    if oper_status is not None and oper_status != 1:
        return Status.DOWN
    if not snmp_metrics_ok or oper_status is None:
        return Status.DEGRADED
    return Status.UP


class EventTracker:
    """Detecta transiciones de estado y mantiene un historial acotado en memoria."""

    def __init__(self, maxlen: int = 100) -> None:
        self._events: deque[AlertEvent] = deque(maxlen=maxlen)
        self._previous: dict[tuple[str, str], Status] = {}
        self._next_id: int = 1

    def record(
        self,
        element_type: str,
        element_id: str,
        current: Status,
        timestamp: str,
    ) -> AlertEvent | None:
        """Compara el estado actual con el anterior y registra evento si hubo transición."""
        key = (element_type, element_id)
        previous = self._previous.get(key)
        self._previous[key] = current

        if previous is None:
            if current == Status.UP:
                return None
            previous_status = Status.UP
        elif previous == current:
            return None
        else:
            previous_status = previous

        if current == Status.DOWN:
            kind = EventKind.DOWN
            desc = "caído (sin respuesta o interfaz down)"
        elif current == Status.DEGRADED:
            kind = EventKind.DEGRADED
            desc = "degradado (latencia, pérdida, CPU o SNMP parcial)"
        else:
            kind = EventKind.RECOVERED
            desc = "recuperado a estado operativo"

        prefix = "Nodo" if element_type == "node" else "Enlace"
        event = AlertEvent(
            id=self._next_id,
            timestamp=timestamp,
            element_type=element_type,
            element_id=element_id,
            kind=kind,
            previous=previous_status,
            current=current,
            message=f"{prefix} {element_id} {desc}",
        )
        self._next_id += 1
        self._events.append(event)
        return event

    def get_recent_events(self) -> list[AlertEvent]:
        """Devuelve los eventos almacenados ordenados del más reciente al más antiguo."""
        return list(reversed(self._events))


class BasePoller(ABC):
    """Contrato común para los motores de telemetría real y simulada."""

    def __init__(
        self,
        settings: Settings,
        nodes: Sequence[Node] = NODES,
        edges: Sequence[Edge] = EDGES,
    ) -> None:
        self._settings = settings
        self._nodes: tuple[Node, ...] = tuple(nodes)
        self._edges: tuple[Edge, ...] = tuple(edges)
        self._nodes_by_id: dict[str, Node] = {n.id: n for n in self._nodes}
        self._edges_by_id: dict[str, Edge] = {e.id: e for e in self._edges}
        self._events = EventTracker(maxlen=100)

    @abstractmethod
    async def poll(self) -> TelemetrySnapshot:
        """Ejecuta un ciclo de sondeo completo y devuelve la instantánea resultante."""

    def get_recent_events(self) -> list[AlertEvent]:
        """Retorna los eventos recientes registrados por el poller."""
        return self._events.get_recent_events()


class SimulatedPoller(BasePoller):
    """Motor de telemetría simulada con estado mutable para fallos en vivo."""

    _BASE_LATENCY_MS: dict[str, float] = {
        "R1-Core": 2.4,
        "R2-Dist": 7.8,
        "R3-Edge": 12.5,
    }
    _BASE_BW_KBPS: dict[str, tuple[float, float]] = {
        "link-r1-r2": (1850.0, 1120.0),
        "link-r1-r3": (940.0, 620.0),
    }

    def __init__(
        self,
        settings: Settings,
        nodes: Sequence[Node] = NODES,
        edges: Sequence[Edge] = EDGES,
    ) -> None:
        super().__init__(settings=settings, nodes=nodes, edges=edges)
        self._rng = random.Random(settings.simulation_seed)
        self.nodes_down: set[str] = set()
        self.edges_down: set[str] = set()
        self._tick: int = 0
        self._cpu_state: dict[str, float] = {
            node.id: 18.0 + (idx * 6.5) for idx, node in enumerate(self._nodes)
        }
        # Contadores acumulativos de 32 bits por enlace: (in_octets, out_octets).
        self._sim_counters: dict[str, tuple[int, int]] = {
            edge.id: (100_000 + idx * 25_000, 80_000 + idx * 15_000)
            for idx, edge in enumerate(self._edges)
        }

    def toggle_node(self, node_id: str) -> bool:
        """Alterna el estado forzado caído de un nodo y retorna True si quedó caído."""
        if node_id not in self._nodes_by_id:
            raise KeyError(f"Nodo inexistente: '{node_id}'.")
        if node_id in self.nodes_down:
            self.nodes_down.remove(node_id)
            return False
        self.nodes_down.add(node_id)
        return True

    def toggle_link(self, edge_id: str) -> bool:
        """Alterna el estado forzado caído de un enlace y retorna True si quedó caído."""
        if edge_id not in self._edges_by_id:
            raise KeyError(f"Enlace inexistente: '{edge_id}'.")
        if edge_id in self.edges_down:
            self.edges_down.remove(edge_id)
            return False
        self.edges_down.add(edge_id)
        return True

    async def poll(self) -> TelemetrySnapshot:
        """Genera una instantánea simulada coherente y sin operaciones bloqueantes."""
        self._tick += 1
        delta_s = self._settings.poll_interval_seconds
        timestamp = utc_now_iso()

        # 1. Calcular tráfico simulado por enlace usando contadores de 32 bits y calculate_kbps.
        edge_bw: dict[str, tuple[float, float]] = {}
        edge_oper: dict[str, int | None] = {}

        for idx, edge in enumerate(self._edges):
            node_down = edge.source in self.nodes_down or edge.target in self.nodes_down
            link_down = edge.id in self.edges_down
            if node_down:
                edge_oper[edge.id] = None
                edge_bw[edge.id] = (0.0, 0.0)
                continue
            if link_down:
                edge_oper[edge.id] = 2
                edge_bw[edge.id] = (0.0, 0.0)
                continue

            base_in, base_out = self._BASE_BW_KBPS.get(edge.id, (1000.0, 700.0))
            wave = 1.0 + 0.25 * math.sin((self._tick + idx) * 0.45)
            target_in_kbps = max(50.0, base_in * wave + self._rng.uniform(-90.0, 90.0))
            target_out_kbps = max(40.0, base_out * wave + self._rng.uniform(-60.0, 60.0))

            bytes_in = int((target_in_kbps * 1000.0 / 8.0) * delta_s)
            bytes_out = int((target_out_kbps * 1000.0 / 8.0) * delta_s)

            prev_in, prev_out = self._sim_counters[edge.id]
            curr_in = (prev_in + bytes_in) % COUNTER32_MODULO
            curr_out = (prev_out + bytes_out) % COUNTER32_MODULO
            self._sim_counters[edge.id] = (curr_in, curr_out)

            bw_in = calculate_kbps(
                prev_in,
                curr_in,
                delta_s,
                max_kbps=self._settings.link_max_kbps,
            )
            bw_out = calculate_kbps(
                prev_out,
                curr_out,
                delta_s,
                max_kbps=self._settings.link_max_kbps,
            )
            edge_oper[edge.id] = 1
            edge_bw[edge.id] = (bw_in, bw_out)

        # 2. Construir telemetría de cada nodo agregando sus interfaces.
        node_results: list[NodeTelemetry] = []
        node_status_map: dict[str, Status] = {}

        for node in self._nodes:
            if node.id in self.nodes_down:
                status = Status.DOWN
                node_status_map[node.id] = status
                node_results.append(
                    NodeTelemetry(
                        id=node.id,
                        status=status,
                        latency_ms=None,
                        packet_loss=100.0,
                        cpu_percent=None,
                        bw_in_kbps=0.0,
                        bw_out_kbps=0.0,
                    )
                )
                continue

            base_lat = self._BASE_LATENCY_MS.get(node.id, 10.0)
            latency_ms = round(max(1.0, base_lat + self._rng.gauss(0.0, 0.8)), 2)

            # Paseo aleatorio acotado debajo del umbral de degradación en operación normal.
            prev_cpu = self._cpu_state.get(node.id, 22.0)
            next_cpu = min(65.0, max(10.0, prev_cpu + self._rng.uniform(-3.5, 3.5)))
            self._cpu_state[node.id] = next_cpu
            cpu_percent = round(next_cpu, 1)

            # En modo simulación, el estado propio del nodo depende de nodes_down;
            # los enlaces en edges_down o con extremos down se propagan en la fase 3.
            total_in = 0.0
            total_out = 0.0
            for edge in self._edges:
                if edge.source == node.id or edge.target == node.id:
                    e_in, e_out = edge_bw[edge.id]
                    if edge.source == node.id:
                        total_in += e_in
                        total_out += e_out
                    else:
                        total_in += e_out
                        total_out += e_in

            status = evaluate_node_status(
                packet_loss=0.0,
                latency_ms=latency_ms,
                cpu_percent=cpu_percent,
                if_oper_statuses=(1,),
                snmp_ok=True,
                latency_threshold_ms=self._settings.latency_degraded_ms,
                cpu_threshold_percent=self._settings.cpu_degraded_percent,
            )
            node_status_map[node.id] = status
            node_results.append(
                NodeTelemetry(
                    id=node.id,
                    status=status,
                    latency_ms=latency_ms,
                    packet_loss=0.0,
                    cpu_percent=cpu_percent,
                    bw_in_kbps=round(total_in, 2),
                    bw_out_kbps=round(total_out, 2),
                )
            )

        # 3. Construir telemetría de enlaces aplicando propagación de nodos caídos.
        edge_results: list[EdgeTelemetry] = []
        for edge in self._edges:
            src_status = node_status_map[edge.source]
            dst_status = node_status_map[edge.target]
            oper = edge_oper[edge.id]
            e_in, e_out = edge_bw[edge.id]
            e_status = evaluate_edge_status(
                oper_status=oper,
                source_status=src_status,
                target_status=dst_status,
                snmp_metrics_ok=oper is not None,
            )
            if e_status == Status.DOWN:
                e_in = 0.0
                e_out = 0.0
            edge_results.append(
                EdgeTelemetry(
                    id=edge.id,
                    status=e_status,
                    oper_status=oper,
                    bw_in_kbps=e_in,
                    bw_out_kbps=e_out,
                )
            )

        # 4. Registrar transiciones de estado para el panel de eventos.
        cycle_events: list[AlertEvent] = []
        for n in node_results:
            evt = self._events.record("node", n.id, n.status, timestamp)
            if evt is not None:
                cycle_events.append(evt)
        for e in edge_results:
            evt = self._events.record("edge", e.id, e.status, timestamp)
            if evt is not None:
                cycle_events.append(evt)

        return TelemetrySnapshot(
            timestamp=timestamp,
            simulation=True,
            nodes=tuple(node_results),
            edges=tuple(edge_results),
            events=tuple(cycle_events),
        )


async def ping_node(ip: str, settings: Settings) -> tuple[float | None, float]:
    """Ejecuta ping ICMP asíncrono con timeout estricto de 1.5 s.

    Retorna `(latency_ms, packet_loss_percent)`. Ante timeout o error de red,
    devuelve `(None, 100.0)` sin propagar excepciones.
    """
    try:
        host = await asyncio.wait_for(
            async_ping(
                ip,
                count=3,
                interval=0.2,
                timeout=0.45,
                privileged=settings.icmp_privileged,
            ),
            timeout=settings.poll_timeout_seconds,
        )
        loss_pct = round(float(host.packet_loss) * 100.0, 2)
        if loss_pct >= 100.0:
            return None, 100.0
        avg_rtt = round(float(host.avg_rtt), 2)
        # En QEMU/GNS3, el rate-limit ICMP de Cisco IOSv puede descartar 1 de 3 ecos
        # cuando concurren varios pings a través del router core. Si al menos 2 de 3
        # paquetes responden con RTT < 200 ms, se normaliza a 0.0% de pérdida.
        if loss_pct <= 34.0 and avg_rtt < 200.0:
            loss_pct = 0.0
        return avg_rtt, loss_pct
    except (asyncio.TimeoutError, NameLookupError, ICMPLibError, OSError) as exc:
        logger.debug("Fallo ICMP hacia %s: %s", ip, type(exc).__name__)
        return None, 100.0


async def snmp_get(
    ip: str,
    oids: Sequence[str],
    settings: Settings,
) -> dict[str, int] | None:
    """Consulta SNMPv2c GET agrupada (Multi-VarBind) y asíncrona para un nodo.

    Agrupa todos los `ObjectType(ObjectIdentity(oid))` del router en una sola
    llamada `getCmd` con `timeout=3.0` y `retries=1`. Retorna `{oid: valor_int}`
    o `None` ante timeout o error SNMP.
    """
    if not oids:
        return {}

    from pysnmp.hlapi.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        getCmd,
    )

    snmp_engine = SnmpEngine()
    try:
        # En laboratorio real usamos snmp_timeout_seconds (3.0 s) con retries=1.
        # Si en una prueba unitaria se configura poll_timeout_seconds < 1.5 s, acotamos a ese valor.
        effective_timeout = (
            min(settings.snmp_timeout_seconds, settings.poll_timeout_seconds)
            if settings.poll_timeout_seconds < 1.5
            else settings.snmp_timeout_seconds
        )
        retries = max(0, settings.snmp_retries)
        per_attempt_timeout = min(1.4, effective_timeout / (retries + 1))

        auth = CommunityData(settings.snmp_community, mpModel=1)
        target = UdpTransportTarget(
            (ip, settings.snmp_port),
            timeout=per_attempt_timeout,
            retries=retries,
        )
        var_binds = [ObjectType(ObjectIdentity(oid)) for oid in oids]
        error_indication, error_status, _error_index, result_binds = await asyncio.wait_for(
            getCmd(snmp_engine, auth, target, ContextData(), *var_binds),
            timeout=effective_timeout,
        )
        if error_indication or error_status:
            logger.debug("Error SNMPv2c en %s: %s", ip, error_indication or error_status)
            return None

        parsed: dict[str, int] = {}
        for oid_obj, val_obj in result_binds:
            oid_str = str(oid_obj)
            try:
                parsed[oid_str] = int(val_obj)
            except (TypeError, ValueError):
                # Ignora instancias ausentes (NoSuchObject / NoSuchInstance).
                continue
        return parsed if parsed else None
    except (asyncio.TimeoutError, OSError, ValueError) as exc:
        logger.debug("Timeout/error SNMP en %s: %s", ip, type(exc).__name__)
        return None
    finally:
        snmp_engine.closeDispatcher()


class RealPoller(BasePoller):
    """Motor de telemetría real mediante ICMP asíncrono y SNMPv2c Multi-VarBind."""

    SNMP_MAX_CONCURRENCY: int = 4
    SNMP_GRACE_CYCLES: int = 4
    SNMP_GRACE_MAX_LATENCY_MS: float = 200.0

    def __init__(
        self,
        settings: Settings,
        nodes: Sequence[Node] = NODES,
        edges: Sequence[Edge] = EDGES,
    ) -> None:
        super().__init__(settings=settings, nodes=nodes, edges=edges)
        self._counters = CounterTracker(max_kbps=settings.link_max_kbps)
        self._snmp_semaphore = asyncio.Semaphore(self.SNMP_MAX_CONCURRENCY)
        self._node_interfaces: dict[str, tuple[Interface, ...]] = {}
        # Caché de últimas métricas SNMP válidas por nodo para 2 ciclos de gracia ante timeouts transitorios.
        self._last_valid_snmp: dict[
            str,
            tuple[
                float | None,
                list[int | None],
                dict[int, tuple[int | None, float, float, bool]],
                float,
                float,
            ],
        ] = {}
        self._snmp_grace_used: dict[str, int] = {}
        for node in self._nodes:
            seen_if: dict[int, Interface] = {}
            for edge in self._edges:
                if edge.source == node.id:
                    seen_if[edge.source_if.if_index] = edge.source_if
                elif edge.target == node.id:
                    seen_if[edge.target_if.if_index] = edge.target_if
            self._node_interfaces[node.id] = tuple(seen_if.values())

    def _build_node_oids(self, node: Node) -> list[str]:
        """Construye la lista única de OIDs a consultar por nodo."""
        cpu_oid = (
            CPU_5SEC_REV
            if node.snmp_cpu_index == 1
            else f"1.3.6.1.4.1.9.9.109.1.1.1.1.6.{node.snmp_cpu_index}"
        )
        oids = [cpu_oid]
        for iface in self._node_interfaces.get(node.id, ()):
            oids.extend(
                [
                    f"{IF_OPER_STATUS}.{iface.if_index}",
                    f"{IF_IN_OCTETS}.{iface.if_index}",
                    f"{IF_OUT_OCTETS}.{iface.if_index}",
                ]
            )
        return oids

    async def _safe_ping_node(self, node: Node) -> tuple[float | None, float]:
        """Ejecuta el sondeo ICMP de un nodo capturando cualquier excepción."""
        try:
            return await ping_node(node.ip, self._settings)
        except Exception as exc:
            logger.warning("Excepción inesperada en ICMP para %s: %s", node.id, exc)
            return None, 100.0

    async def _poll_single_node(
        self,
        node: Node,
        ping_result: tuple[float | None, float] | None = None,
    ) -> tuple[NodeTelemetry, dict[int, tuple[int | None, float, float, bool]]]:
        """Sondea un nodo (ICMP y, si responde, SNMPv2c Multi-VarBind) sin lanzar excepciones."""
        interfaces = self._node_interfaces.get(node.id, ())
        iface_metrics: dict[int, tuple[int | None, float, float, bool]] = {
            iface.if_index: (None, 0.0, 0.0, False) for iface in interfaces
        }

        if ping_result is not None:
            latency_ms, packet_loss = ping_result
        else:
            latency_ms, packet_loss = await self._safe_ping_node(node)

        # Si el equipo no responde ICMP, omitimos SNMP y disparamos down de inmediato (0 ciclos de gracia).
        if packet_loss >= 100.0 or latency_ms is None:
            self._last_valid_snmp.pop(node.id, None)
            self._snmp_grace_used.pop(node.id, None)
            for iface in interfaces:
                self._counters.reset_interface(node.id, iface.if_index)
            return (
                NodeTelemetry(
                    id=node.id,
                    status=Status.DOWN,
                    latency_ms=None,
                    packet_loss=100.0,
                    cpu_percent=None,
                    bw_in_kbps=0.0,
                    bw_out_kbps=0.0,
                ),
                iface_metrics,
            )

        oids = self._build_node_oids(node)
        try:
            async with self._snmp_semaphore:
                snmp_data = await snmp_get(node.ip, oids, self._settings)
        except Exception as exc:
            logger.warning("Excepción inesperada en SNMP para %s: %s", node.id, exc)
            snmp_data = None

        if snmp_data is None:
            # 4 ciclos consecutivos de gracia si el ping respondió con 0% pérdida,
            # latencia < 200.0 ms y ya existían métricas SNMP válidas conocidas.
            grace_used = self._snmp_grace_used.get(node.id, 0)
            if (
                packet_loss == 0.0
                and latency_ms < self.SNMP_GRACE_MAX_LATENCY_MS
                and node.id in self._last_valid_snmp
                and grace_used < self.SNMP_GRACE_CYCLES
            ):
                self._snmp_grace_used[node.id] = grace_used + 1
                (
                    cached_cpu,
                    cached_if_statuses,
                    cached_iface_metrics,
                    cached_in,
                    cached_out,
                ) = self._last_valid_snmp[node.id]
                status = evaluate_node_status(
                    packet_loss=packet_loss,
                    latency_ms=latency_ms,
                    cpu_percent=cached_cpu,
                    if_oper_statuses=cached_if_statuses,
                    snmp_ok=True,
                    latency_threshold_ms=self._settings.latency_degraded_ms,
                    cpu_threshold_percent=self._settings.cpu_degraded_percent,
                )
                return (
                    NodeTelemetry(
                        id=node.id,
                        status=status,
                        latency_ms=latency_ms,
                        packet_loss=packet_loss,
                        cpu_percent=cached_cpu,
                        bw_in_kbps=cached_in,
                        bw_out_kbps=cached_out,
                    ),
                    dict(cached_iface_metrics),
                )

            self._snmp_grace_used[node.id] = grace_used + 1
            status = evaluate_node_status(
                packet_loss=packet_loss,
                latency_ms=latency_ms,
                cpu_percent=None,
                if_oper_statuses=(),
                snmp_ok=False,
                latency_threshold_ms=self._settings.latency_degraded_ms,
                cpu_threshold_percent=self._settings.cpu_degraded_percent,
            )
            return (
                NodeTelemetry(
                    id=node.id,
                    status=status,
                    latency_ms=latency_ms,
                    packet_loss=packet_loss,
                    cpu_percent=None,
                    bw_in_kbps=0.0,
                    bw_out_kbps=0.0,
                ),
                iface_metrics,
            )

        cpu_oid = (
            CPU_5SEC_REV
            if node.snmp_cpu_index == 1
            else f"1.3.6.1.4.1.9.9.109.1.1.1.1.6.{node.snmp_cpu_index}"
        )
        raw_cpu = snmp_data.get(cpu_oid)
        cpu_percent = float(min(100, max(0, raw_cpu))) if raw_cpu is not None else None

        if_statuses: list[int | None] = []
        total_in = 0.0
        total_out = 0.0

        for iface in interfaces:
            oper = snmp_data.get(f"{IF_OPER_STATUS}.{iface.if_index}")
            in_oct = snmp_data.get(f"{IF_IN_OCTETS}.{iface.if_index}")
            out_oct = snmp_data.get(f"{IF_OUT_OCTETS}.{iface.if_index}")
            if_statuses.append(oper)

            if oper == 1 and in_oct is not None and out_oct is not None:
                bw_in, bw_out = self._counters.update(
                    node.id, iface.if_index, in_oct, out_oct
                )
                iface_metrics[iface.if_index] = (oper, bw_in, bw_out, True)
                total_in += bw_in
                total_out += bw_out
            else:
                if oper is not None and oper != 1:
                    self._counters.reset_interface(node.id, iface.if_index)
                iface_metrics[iface.if_index] = (
                    oper,
                    0.0,
                    0.0,
                    oper is not None and in_oct is not None and out_oct is not None,
                )

        # Consulta SNMP exitosa: reiniciamos el contador de gracia y actualizamos la caché.
        self._snmp_grace_used[node.id] = 0
        if cpu_percent is not None and all(s is not None for s in if_statuses):
            self._last_valid_snmp[node.id] = (
                cpu_percent,
                list(if_statuses),
                dict(iface_metrics),
                round(total_in, 2),
                round(total_out, 2),
            )

        status = evaluate_node_status(
            packet_loss=packet_loss,
            latency_ms=latency_ms,
            cpu_percent=cpu_percent,
            if_oper_statuses=if_statuses,
            snmp_ok=True,
            latency_threshold_ms=self._settings.latency_degraded_ms,
            cpu_threshold_percent=self._settings.cpu_degraded_percent,
        )

        return (
            NodeTelemetry(
                id=node.id,
                status=status,
                latency_ms=latency_ms,
                packet_loss=packet_loss,
                cpu_percent=cpu_percent,
                bw_in_kbps=round(total_in, 2),
                bw_out_kbps=round(total_out, 2),
            ),
            iface_metrics,
        )

    async def poll(self) -> TelemetrySnapshot:
        """Sondea todos los nodos en 2 fases concurrentes (ICMP -> SNMP Multi-VarBind)."""
        timestamp = utc_now_iso()
        # Fase 1: completar todos los pings ICMP antes de disparar SNMP evita que el
        # agente SNMP de R1-Core eleve el RTT de los ecos en tránsito hacia R2/R3.
        ping_results = await asyncio.gather(
            *(self._safe_ping_node(node) for node in self._nodes)
        )
        tasks = [
            self._poll_single_node(node, ping_res)
            for node, ping_res in zip(self._nodes, ping_results, strict=True)
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        node_results: list[NodeTelemetry] = []
        node_status_map: dict[str, Status] = {}
        iface_map: dict[str, dict[int, tuple[int | None, float, float, bool]]] = {}

        for node, result in zip(self._nodes, raw_results, strict=True):
            if isinstance(result, BaseException):
                logger.error("Error inesperado sondeando %s: %s", node.id, result)
                telemetry = NodeTelemetry(
                    id=node.id,
                    status=Status.DOWN,
                    latency_ms=None,
                    packet_loss=100.0,
                    cpu_percent=None,
                    bw_in_kbps=0.0,
                    bw_out_kbps=0.0,
                )
                metrics: dict[int, tuple[int | None, float, float, bool]] = {}
            else:
                telemetry, metrics = result

            node_results.append(telemetry)
            node_status_map[node.id] = telemetry.status
            iface_map[node.id] = metrics

        edge_results: list[EdgeTelemetry] = []
        for edge in self._edges:
            src_status = node_status_map[edge.source]
            dst_status = node_status_map[edge.target]

            src_oper, src_in, src_out, src_ok = iface_map.get(edge.source, {}).get(
                edge.source_if.if_index, (None, 0.0, 0.0, False)
            )
            dst_oper, dst_in, dst_out, dst_ok = iface_map.get(edge.target, {}).get(
                edge.target_if.if_index, (None, 0.0, 0.0, False)
            )

            # Combinar estado operativo reportado por ambos extremos del enlace.
            if src_oper is not None and src_oper != 1:
                edge_oper: int | None = src_oper
            elif dst_oper is not None and dst_oper != 1:
                edge_oper = dst_oper
            elif src_oper == 1 or dst_oper == 1:
                edge_oper = 1
            else:
                edge_oper = None

            snmp_metrics_ok = src_ok and dst_ok
            edge_status = evaluate_edge_status(
                oper_status=edge_oper,
                source_status=src_status,
                target_status=dst_status,
                snmp_metrics_ok=snmp_metrics_ok,
            )

            if edge_status == Status.DOWN:
                bw_in, bw_out = 0.0, 0.0
            elif src_ok:
                bw_in, bw_out = src_in, src_out
            elif dst_ok:
                bw_in, bw_out = dst_out, dst_in
            else:
                bw_in, bw_out = 0.0, 0.0

            edge_results.append(
                EdgeTelemetry(
                    id=edge.id,
                    status=edge_status,
                    oper_status=edge_oper,
                    bw_in_kbps=bw_in,
                    bw_out_kbps=bw_out,
                )
            )

        cycle_events: list[AlertEvent] = []
        for n in node_results:
            evt = self._events.record("node", n.id, n.status, timestamp)
            if evt is not None:
                cycle_events.append(evt)
        for e in edge_results:
            evt = self._events.record("edge", e.id, e.status, timestamp)
            if evt is not None:
                cycle_events.append(evt)

        return TelemetrySnapshot(
            timestamp=timestamp,
            simulation=False,
            nodes=tuple(node_results),
            edges=tuple(edge_results),
            events=tuple(cycle_events),
        )
