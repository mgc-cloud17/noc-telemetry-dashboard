"""Suite de pruebas unitarias e integración para NOC Telemetry Dashboard.

Cubre cálculo de Kbps (incluyendo wrap de 32 bits), validación de topología,
reglas de estado operativo, motor simulado, motor real asíncrono (con mocks),
validación de contrato JSON Schema, endpoints REST y difusión WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import jsonschema
import pytest
from starlette.testclient import TestClient

from main import create_app
from telemetry_engine import (
    COUNTER32_MODULO,
    CPU_5SEC_REV,
    IF_IN_OCTETS,
    IF_OPER_STATUS,
    IF_OUT_OCTETS,
    EventKind,
    EventTracker,
    RealPoller,
    Settings,
    SimulatedPoller,
    Status,
    calculate_kbps,
    evaluate_edge_status,
    evaluate_node_status,
    load_settings,
    snmp_get,
)
from topology_config import (
    EDGES,
    NODES,
    Edge,
    Interface,
    Node,
    NodeType,
    load_topology_from_json,
    topology_as_dict,
    validate_topology,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "specs"
    / "001-noc-telemetry-dashboard"
    / "contracts"
    / "telemetry-message.schema.json"
)


# ---------------------------------------------------------------------------
# Pruebas fundacionales: calculate_kbps y topology_config
# ---------------------------------------------------------------------------


def test_calculate_kbps_caso_normal() -> None:
    """Verifica el cálculo estándar de delta de octetos a Kbps."""
    # 3000 bytes * 8 bits = 24000 bits / 3.0 s / 1000 = 8.0 Kbps
    kbps = calculate_kbps(prev_octets=1000, curr_octets=4000, delta_seconds=3.0)
    assert kbps == pytest.approx(8.0)


def test_calculate_kbps_wrap_32_bits() -> None:
    """Verifica el manejo correcto del desbordamiento (wrap) de 32 bits."""
    prev = COUNTER32_MODULO - 1000
    curr = 500
    # Delta real = 1000 + 500 = 1500 bytes -> 1500 * 8 / 3.0 / 1000 = 4.0 Kbps
    kbps = calculate_kbps(prev_octets=prev, curr_octets=curr, delta_seconds=3.0)
    assert kbps == pytest.approx(4.0)
    assert kbps >= 0.0


def test_calculate_kbps_primera_muestra_y_deltas_invalidos() -> None:
    """Primera muestra o intervalos <= 0 deben devolver 0.0 sin excepciones."""
    assert calculate_kbps(prev_octets=None, curr_octets=1000, delta_seconds=3.0) == 0.0
    assert calculate_kbps(prev_octets=1000, curr_octets=None, delta_seconds=3.0) == 0.0
    assert calculate_kbps(prev_octets=1000, curr_octets=2000, delta_seconds=0.0) == 0.0
    assert calculate_kbps(prev_octets=1000, curr_octets=2000, delta_seconds=-2.5) == 0.0


def test_calculate_kbps_descarta_pico_sobre_max_kbps() -> None:
    """Descarta lecturas espurias que superen el techo físico del enlace."""
    # Un salto gigante en 0.001 s excede 1_000_000 Kbps -> se descarta como 0.0
    kbps = calculate_kbps(
        prev_octets=0,
        curr_octets=500_000_000,
        delta_seconds=0.001,
        max_kbps=1_000_000.0,
    )
    assert kbps == 0.0


def test_topology_default_valida_y_serializa() -> None:
    """La topología inicial contiene 3 routers y 2 enlaces válidos."""
    validate_topology(NODES, EDGES)
    payload = topology_as_dict()
    assert len(payload["nodes"]) == 3
    assert len(payload["edges"]) == 2
    assert {n["id"] for n in payload["nodes"]} == {"R1-Core", "R2-Dist", "R3-Edge"}
    assert {e["id"] for e in payload["edges"]} == {"link-r1-r2", "link-r1-r3"}


def test_topology_validation_rechaza_errores() -> None:
    """Detecta IDs duplicados, IPs inválidas, auto-enlaces y referencias rotas."""
    n1 = Node("R1", "R1", "192.168.1.1", NodeType.ROUTER, "core", (0, 0))
    n1_dup = Node("R1", "R1-b", "192.168.1.2", NodeType.ROUTER, "edge", (10, 10))
    n_bad_ip = Node("R2", "R2", "999.999.1.1", NodeType.ROUTER, "edge", (10, 10))

    with pytest.raises(ValueError):
        validate_topology([], [])
    with pytest.raises(ValueError):
        validate_topology([n1, n1_dup], [])
    with pytest.raises(ValueError):
        validate_topology([n1, n_bad_ip], [])

    n2 = Node("R2", "R2", "10.0.0.2", NodeType.ROUTER, "edge", (10, 10))
    self_edge = Edge("e1", "R1", "R1", Interface("Gi0/1", 2), Interface("Gi0/2", 3))
    missing_edge = Edge("e2", "R1", "NO-EXISTE", Interface("Gi0/1", 2), Interface("Gi0/1", 2))
    dup_edge_a = Edge("e3", "R1", "R2", Interface("Gi0/1", 2), Interface("Gi0/1", 2))
    dup_edge_b = Edge("e4", "R2", "R1", Interface("Gi0/1", 2), Interface("Gi0/1", 2))

    with pytest.raises(ValueError):
        validate_topology([n1, n2], [self_edge])
    with pytest.raises(ValueError):
        validate_topology([n1, n2], [missing_edge])
    with pytest.raises(ValueError):
        validate_topology([n1, n2], [dup_edge_a, dup_edge_b])


def test_load_topology_from_json(tmp_path: Path) -> None:
    """Permite cargar y validar una topología desde disco en formato JSON."""
    topo_file = tmp_path / "topo.json"
    topo_file.write_text(json.dumps(topology_as_dict()), encoding="utf-8")
    nodes, edges = load_topology_from_json(topo_file)
    assert len(nodes) == 3
    assert len(edges) == 2


def test_load_settings_desde_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carga configuración desde .env sin hardcodear comunidades SNMP."""
    monkeypatch.setattr("os.environ", os.environ.copy())
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SIMULATION_MODE=false\n"
        "SNMP_COMMUNITY=lab-ro-secret\n"
        "SNMP_PORT=1161\n"
        "POLL_TIMEOUT_SECONDS=1.5\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file=env_file, override=True)
    assert settings.simulation_mode is False
    assert settings.snmp_community == "lab-ro-secret"
    assert settings.snmp_port == 1161
    assert settings.poll_timeout_seconds == pytest.approx(1.5)


def test_load_settings_rechaza_valores_invalidos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valida puertos, timeouts y comunidad no vacía sin exponer secretos."""
    monkeypatch.setattr("os.environ", os.environ.copy())
    bad_env = tmp_path / ".env"
    bad_env.write_text("SNMP_COMMUNITY=\nSNMP_PORT=161\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(env_file=bad_env, override=True)

    bad_port = tmp_path / ".env.port"
    bad_port.write_text("SNMP_COMMUNITY=mi-secreto-oculto\nSNMP_PORT=99999\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_settings(env_file=bad_port, override=True)
    assert "mi-secreto-oculto" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Pruebas de umbrales de estado operativo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("packet_loss", "latency_ms", "cpu", "if_statuses", "snmp_ok", "expected"),
    [
        # Casos límite de latencia para virtualización QEMU/VMware (149.9 up vs 150.0 degraded)
        (0.0, 149.9, 25.0, [1], True, Status.UP),
        (0.0, 150.0, 25.0, [1], True, Status.DEGRADED),
        # Casos límite de pérdida de paquetes
        (1.0, 10.0, 25.0, [1], True, Status.DEGRADED),
        (50.0, 10.0, 25.0, [1], True, Status.DEGRADED),
        (51.0, 10.0, 25.0, [1], True, Status.DEGRADED),
        (99.0, 10.0, 25.0, [1], True, Status.DEGRADED),
        (100.0, None, None, [], False, Status.DOWN),
        # Casos límite de CPU (84.9 up vs 85.0 degraded)
        (0.0, 10.0, 84.9, [1], True, Status.UP),
        (0.0, 10.0, 85.0, [1], True, Status.DEGRADED),
        # Fallo parcial SNMP
        (0.0, 10.0, None, [], False, Status.DEGRADED),
        # Interfaces en nodo multi-interfaz vs mono-interfaz
        (0.0, 10.0, 20.0, [1, 1], True, Status.UP),
        (0.0, 10.0, 20.0, [1, 2], True, Status.DEGRADED),
        (0.0, 10.0, 20.0, [2, 2], True, Status.DOWN),
        (0.0, 10.0, 20.0, [2], True, Status.DOWN),
        # Prioridad down > degraded > up
        (100.0, 10.0, 95.0, [1], True, Status.DOWN),
    ],
)
def test_evaluate_node_status_umbrales(
    packet_loss: float,
    latency_ms: float | None,
    cpu: float | None,
    if_statuses: list[int | None],
    snmp_ok: bool,
    expected: Status,
) -> None:
    """Valida todos los límites exactos entre up, degraded y down en nodos."""
    status = evaluate_node_status(
        packet_loss=packet_loss,
        latency_ms=latency_ms,
        cpu_percent=cpu,
        if_oper_statuses=if_statuses,
        snmp_ok=snmp_ok,
        latency_threshold_ms=150.0,
        cpu_threshold_percent=85.0,
    )
    assert status == expected


def test_evaluate_edge_status_reglas() -> None:
    """Valida las reglas de estado para enlaces según extremos e ifOperStatus."""
    assert evaluate_edge_status(1, Status.UP, Status.UP, True) == Status.UP
    assert evaluate_edge_status(2, Status.UP, Status.UP, True) == Status.DOWN
    assert evaluate_edge_status(1, Status.DOWN, Status.UP, True) == Status.DOWN
    assert evaluate_edge_status(1, Status.UP, Status.DOWN, True) == Status.DOWN
    assert evaluate_edge_status(1, Status.UP, Status.UP, False) == Status.DEGRADED
    assert evaluate_edge_status(None, Status.UP, Status.UP, False) == Status.DEGRADED


# ---------------------------------------------------------------------------
# Pruebas de SimulatedPoller, contrato JSON Schema y EventTracker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simulated_poller_sano_reproducible_y_wrap() -> None:
    """El poller simulado genera métricas válidas, reproducibles y tolera wrap 2^32."""
    settings = Settings(simulation_mode=True, simulation_seed=42)
    poller_a = SimulatedPoller(settings=settings)
    poller_b = SimulatedPoller(settings=settings)

    snap_a = await poller_a.poll()
    snap_b = await poller_b.poll()

    assert len(snap_a.nodes) == 3
    assert len(snap_a.edges) == 2
    assert all(n.status == Status.UP for n in snap_a.nodes)
    assert all(e.status == Status.UP for e in snap_a.edges)
    assert snap_a.to_dict()["nodes"] == snap_b.to_dict()["nodes"]

    for node in snap_a.nodes:
        assert 0.0 <= node.packet_loss <= 100.0
        assert node.cpu_percent is not None and 0.0 <= node.cpu_percent <= 100.0
        assert node.bw_in_kbps >= 0.0
        assert node.bw_out_kbps >= 0.0

    # Forzar los contadores cerca de 2^32 para ejercitar el wrap real en el ciclo.
    for key in list(poller_a._sim_counters.keys()):
        poller_a._sim_counters[key] = (COUNTER32_MODULO - 500, COUNTER32_MODULO - 500)
    snap_wrap = await poller_a.poll()
    assert all(e.bw_in_kbps >= 0.0 and e.bw_out_kbps >= 0.0 for e in snap_wrap.edges)


@pytest.mark.asyncio
async def test_payload_cumple_json_schema_contract() -> None:
    """Valida el diccionario serializado contra contracts/telemetry-message.schema.json."""
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    poller = SimulatedPoller(settings=Settings(simulation_mode=True, simulation_seed=7))

    # Primer ciclo sano
    snap_up = await poller.poll()
    payload_up = snap_up.to_dict()
    jsonschema.validate(instance=payload_up, schema=schema)
    json.dumps(payload_up)

    # Segundo ciclo con un nodo caído (campos null + evento)
    poller.toggle_node("R2-Dist")
    snap_down = await poller.poll()
    payload_down = snap_down.to_dict()
    jsonschema.validate(instance=payload_down, schema=schema)
    serialized = json.dumps(payload_down)
    assert "null" in serialized
    assert len(payload_down["events"]) >= 1


@pytest.mark.asyncio
async def test_simulated_poller_toggle_y_propagacion() -> None:
    """Tumbado de nodo propaga a sus enlaces sin olvidar enlaces tumbados manualmente."""
    poller = SimulatedPoller(settings=Settings(simulation_mode=True, simulation_seed=1))
    await poller.poll()

    # Tumbar nodo R2-Dist -> link-r1-r2 debe caer, R1-Core y link-r1-r3 siguen arriba
    assert poller.toggle_node("R2-Dist") is True
    snap = await poller.poll()
    nodes_by_id = {n.id: n for n in snap.nodes}
    edges_by_id = {e.id: e for e in snap.edges}

    assert nodes_by_id["R2-Dist"].status == Status.DOWN
    assert nodes_by_id["R2-Dist"].packet_loss == 100.0
    assert nodes_by_id["R2-Dist"].latency_ms is None
    assert nodes_by_id["R2-Dist"].cpu_percent is None
    assert edges_by_id["link-r1-r2"].status == Status.DOWN
    assert nodes_by_id["R1-Core"].status == Status.UP
    assert edges_by_id["link-r1-r3"].status == Status.UP

    # Tumbar explícitamente link-r1-r2 mientras R2-Dist ya estaba caído
    assert poller.toggle_link("link-r1-r2") is True
    # Restaurar R2-Dist: link-r1-r2 debe seguir down porque se tumbó manualmente
    assert poller.toggle_node("R2-Dist") is False
    snap_restored_node = await poller.poll()
    nodes_2 = {n.id: n for n in snap_restored_node.nodes}
    edges_2 = {e.id: e for e in snap_restored_node.edges}
    assert nodes_2["R2-Dist"].status == Status.UP
    assert edges_2["link-r1-r2"].status == Status.DOWN

    # Restaurar el enlace manualmente
    assert poller.toggle_link("link-r1-r2") is False
    snap_final = await poller.poll()
    assert {e.id: e for e in snap_final.edges}["link-r1-r2"].status == Status.UP

    with pytest.raises(KeyError):
        poller.toggle_node("NO-EXISTE")
    with pytest.raises(KeyError):
        poller.toggle_link("NO-EXISTE")


def test_event_tracker_transiciones_y_limite() -> None:
    """Genera eventos solo en transiciones y respeta el límite de 100 elementos."""
    tracker = EventTracker(maxlen=100)
    assert tracker.record("node", "R1-Core", Status.UP, "2026-11-18T12:00:00Z") is None
    assert tracker.record("node", "R1-Core", Status.UP, "2026-11-18T12:00:03Z") is None

    evt_down = tracker.record("node", "R1-Core", Status.DOWN, "2026-11-18T12:00:06Z")
    assert evt_down is not None
    assert evt_down.kind == EventKind.DOWN
    assert evt_down.previous == Status.UP
    assert evt_down.current == Status.DOWN

    evt_rec = tracker.record("node", "R1-Core", Status.UP, "2026-11-18T12:00:09Z")
    assert evt_rec is not None
    assert evt_rec.kind == EventKind.RECOVERED

    # Llenar más de 100 eventos alternando estado
    for i in range(110):
        st = Status.DEGRADED if i % 2 == 0 else Status.UP
        tracker.record("edge", "link-r1-r2", st, f"2026-11-18T12:01:{i % 60:02d}Z")

    recent = tracker.get_recent_events()
    assert len(recent) == 100
    assert recent[0].id > recent[-1].id


# ---------------------------------------------------------------------------
# Pruebas de RealPoller (asíncrono con mocks de ICMP y SNMPv2c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_poller_con_mocks_y_concurrencia() -> None:
    """Valida RealPoller: up, 100% pérdida omite SNMP, timeout SNMP degraded, wrap y concurrencia."""
    settings = Settings(
        simulation_mode=False,
        poll_timeout_seconds=1.5,
        snmp_community="test-ro",
    )
    poller = RealPoller(settings=settings)
    snmp_calls: list[str] = []

    async def fake_ping(ip: str, _settings: Settings) -> tuple[float | None, float]:
        if ip == "192.168.76.10":  # R1-Core: sano
            return (4.5, 0.0)
        if ip == "10.0.12.2":  # R2-Dist: 100% pérdida (no debe llamar SNMP)
            return (None, 100.0)
        if ip == "10.0.13.2":  # R3-Edge: responde ICMP, luego SNMP fallará por timeout
            return (12.0, 0.0)
        return (None, 100.0)

    async def fake_snmp(
        ip: str, oids: list[str], _settings: Settings
    ) -> dict[str, int] | None:
        snmp_calls.append(ip)
        if ip == "192.168.76.10":
            return {
                f"{IF_OPER_STATUS}.2": 1,
                f"{IF_IN_OCTETS}.2": COUNTER32_MODULO - 1000,
                f"{IF_OUT_OCTETS}.2": 2000,
                f"{IF_OPER_STATUS}.3": 1,
                f"{IF_IN_OCTETS}.3": 5000,
                f"{IF_OUT_OCTETS}.3": 6000,
                CPU_5SEC_REV: 22,
            }
        # R3-Edge timeout SNMP (simula demora acotada)
        await asyncio.sleep(0.05)
        return None

    with (
        patch("telemetry_engine.ping_node", side_effect=fake_ping),
        patch("telemetry_engine.snmp_get", side_effect=fake_snmp),
    ):
        snap1 = await poller.poll()
        nodes1 = {n.id: n for n in snap1.nodes}
        edges1 = {e.id: e for e in snap1.edges}

        assert "10.0.12.2" not in snmp_calls
        assert nodes1["R1-Core"].status == Status.UP
        assert nodes1["R1-Core"].cpu_percent == 22.0
        assert nodes1["R2-Dist"].status == Status.DOWN
        assert nodes1["R3-Edge"].status == Status.DEGRADED
        assert nodes1["R3-Edge"].latency_ms == 12.0
        assert edges1["link-r1-r2"].status == Status.DOWN
        assert edges1["link-r1-r3"].status == Status.DEGRADED

        # Segundo ciclo con wrap de 32 bits en R1-Core Gi0/1 y caída de ifOperStatus en Gi0/2
        async def fake_snmp_wrap(
            ip: str, oids: list[str], _settings: Settings
        ) -> dict[str, int] | None:
            if ip == "192.168.76.10":
                return {
                    f"{IF_OPER_STATUS}.2": 1,
                    f"{IF_IN_OCTETS}.2": 500,  # wrap desde 2^32 - 1000
                    f"{IF_OUT_OCTETS}.2": 8000,
                    f"{IF_OPER_STATUS}.3": 2,  # Gi0/2 caída operativamente
                    f"{IF_IN_OCTETS}.3": 5000,
                    f"{IF_OUT_OCTETS}.3": 6000,
                    CPU_5SEC_REV: 30,
                }
            return None

        with patch("telemetry_engine.snmp_get", side_effect=fake_snmp_wrap):
            snap2 = await poller.poll()
            nodes2 = {n.id: n for n in snap2.nodes}
            edges2 = {e.id: e for e in snap2.edges}
            assert edges2["link-r1-r2"].bw_in_kbps >= 0.0
            assert edges2["link-r1-r3"].status == Status.DOWN
            assert nodes2["R1-Core"].status == Status.DEGRADED


@pytest.mark.asyncio
async def test_real_poller_nodo_lento_no_retrasa_ciclo() -> None:
    """Principio I: un nodo lento/colgado queda acotado a ~1.5 s por concurrencia."""
    settings = Settings(simulation_mode=False, poll_timeout_seconds=0.25)
    poller = RealPoller(settings=settings)

    async def slow_icmplib(*args: Any, **kwargs: Any) -> Any:
        ip = args[0] if args else kwargs.get("address")
        if ip == "10.0.12.2":
            await asyncio.sleep(2.0)  # excede el timeout de 0.25 s

        class _Host:
            avg_rtt = 5.0
            packet_loss = 0.0

        return _Host()

    with (
        patch("telemetry_engine.async_ping", side_effect=slow_icmplib),
        patch(
            "telemetry_engine.snmp_get",
            new=AsyncMock(
                return_value={
                    f"{IF_OPER_STATUS}.2": 1,
                    f"{IF_IN_OCTETS}.2": 100,
                    f"{IF_OUT_OCTETS}.2": 100,
                    f"{IF_OPER_STATUS}.3": 1,
                    f"{IF_IN_OCTETS}.3": 100,
                    f"{IF_OUT_OCTETS}.3": 100,
                    CPU_5SEC_REV: 10,
                }
            ),
        ),
    ):
        t0 = time.monotonic()
        snap = await poller.poll()
        elapsed = time.monotonic() - t0

    assert elapsed < 0.7
    nodes = {n.id: n for n in snap.nodes}
    assert nodes["R2-Dist"].status == Status.DOWN
    assert nodes["R1-Core"].status == Status.UP


# ---------------------------------------------------------------------------
# Pruebas de integración FastAPI REST y WebSocket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_endpoints_simulacion_y_ui() -> None:
    """Verifica /, static, /api/topology, /api/config, /api/telemetry/latest y toggles."""
    settings = Settings(simulation_mode=True, simulation_seed=10, poll_interval_seconds=60.0)
    app = create_app(settings=settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            res_html = await client.get("/")
            assert res_html.status_code == 200
            for dom_id in ("cy", "node-panel", "chart-bw", "chart-cpu", "sim-controls", "events-log"):
                assert dom_id in res_html.text

            res_js = await client.get("/static/js/noc_dashboard.js")
            assert res_js.status_code == 200

            res_cfg = await client.get("/api/config")
            assert res_cfg.status_code == 200
            assert res_cfg.json()["simulation"] is True

            res_topo = await client.get("/api/topology")
            assert res_topo.status_code == 200
            assert len(res_topo.json()["nodes"]) == 3

            res_latest = await client.get("/api/telemetry/latest")
            assert res_latest.status_code == 200
            assert res_latest.json()["type"] == "telemetry"

            # Toggle de nodo válido
            res_toggle = await client.post("/api/simulate/toggle-node/R2-Dist")
            assert res_toggle.status_code == 200
            body = res_toggle.json()
            assert body == {"id": "R2-Dist", "forced_down": True, "status": "down"}

            # Toggle de enlace válido
            res_link = await client.post("/api/simulate/toggle-link/link-r1-r3")
            assert res_link.status_code == 200
            assert res_link.json()["status"] == "down"

            # ID inexistente -> 404
            res_404 = await client.post("/api/simulate/toggle-node/NO-EXISTE")
            assert res_404.status_code == 404

            # Eventos recientes ordenados del más nuevo al más antiguo
            res_events = await client.get("/api/events")
            assert res_events.status_code == 200
            events = res_events.json()
            assert len(events) >= 2
            assert events[0]["id"] >= events[-1]["id"]


@pytest.mark.asyncio
async def test_simulate_endpoints_retornan_409_si_modo_real() -> None:
    """Con SIMULATION_MODE=false, los toggles deben responder 409 Conflict."""
    settings = Settings(simulation_mode=False, poll_interval_seconds=60.0)
    mock_poller = RealPoller(settings=settings)

    with (
        patch("telemetry_engine.ping_node", new=AsyncMock(return_value=(3.0, 0.0))),
        patch("telemetry_engine.snmp_get", new=AsyncMock(return_value=None)),
    ):
        app = create_app(settings=settings, poller=mock_poller)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/api/config")).json()["simulation"] is False
                res_node = await client.post("/api/simulate/toggle-node/R1-Core")
                res_link = await client.post("/api/simulate/toggle-link/link-r1-r2")
                assert res_node.status_code == 409
                assert res_link.status_code == 409


def test_websocket_broadcast_y_desconexion_limpia() -> None:
    """Verifica snapshot inicial, difusión a múltiples clientes y desconexión sin fallos."""
    settings = Settings(simulation_mode=True, simulation_seed=5, poll_interval_seconds=60.0)
    app = create_app(settings=settings)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/telemetry") as ws1:
            initial = ws1.receive_json()
            assert initial["type"] == "telemetry"
            assert len(initial["nodes"]) == 3

            # Segundo cliente conectado simultáneamente; luego se desconecta abruptamente
            with client.websocket_connect("/ws/telemetry") as ws2:
                initial2 = ws2.receive_json()
                assert initial2["type"] == "telemetry"

                # Un toggle dispara broadcast inmediato hacia ambos clientes
                resp = client.post("/api/simulate/toggle-link/link-r1-r2")
                assert resp.status_code == 200

                msg1 = ws1.receive_json()
                msg2 = ws2.receive_json()
                assert any(e["id"] == "link-r1-r2" and e["status"] == "down" for e in msg1["edges"])
                assert any(e["id"] == "link-r1-r2" and e["status"] == "down" for e in msg2["edges"])
                assert len(msg1["events"]) >= 1

            # ws2 ya cerró; ws1 debe seguir recibiendo sin excepciones en el servidor
            resp2 = client.post("/api/simulate/toggle-link/link-r1-r2")
            assert resp2.status_code == 200
            msg1_after = ws1.receive_json()
            assert any(e["id"] == "link-r1-r2" and e["status"] == "up" for e in msg1_after["edges"])


@pytest.mark.asyncio
async def test_telemetry_latest_503_y_link_404() -> None:
    """Sin snapshot previo devuelve 503 y toggle-link inexistente devuelve 404."""
    settings = Settings(simulation_mode=True, simulation_seed=3)
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res_503 = await client.get("/api/telemetry/latest")
        assert res_503.status_code == 503

        res_404 = await client.post("/api/simulate/toggle-link/enlace-fantasma")
        assert res_404.status_code == 404


@pytest.mark.asyncio
async def test_snmp_get_parseo_y_timeout_con_mock() -> None:
    """Verifica snmp_get contra la firma real de pysnmp.hlapi.asyncio.getCmd."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        import pysnmp.hlapi.asyncio as hlapi

    settings = Settings(simulation_mode=False, poll_timeout_seconds=0.3, snmp_community="ro-test")

    async def fake_get_cmd(*args: Any, **kwargs: Any) -> tuple[Any, int, int, list[tuple[str, Any]]]:
        return (None, 0, 0, [(CPU_5SEC_REV, 19), (f"{IF_OPER_STATUS}.2", 1)])

    with patch.object(hlapi, "getCmd", side_effect=fake_get_cmd):
        res = await snmp_get("192.168.76.10", [CPU_5SEC_REV, f"{IF_OPER_STATUS}.2"], settings)
    assert res == {CPU_5SEC_REV: 19, f"{IF_OPER_STATUS}.2": 1}

    async def fake_timeout(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(1.0)
        return (None, 0, 0, [])

    with patch.object(hlapi, "getCmd", side_effect=fake_timeout):
        res_timeout = await snmp_get("192.168.76.10", [CPU_5SEC_REV], settings)
    assert res_timeout is None


@pytest.mark.asyncio
async def test_real_poller_ciclo_de_gracia_snmp_transitorio() -> None:
    """Conserva métricas SNMP válidas durante 4 ciclos de gracia ante timeouts transitorios."""
    settings = Settings(simulation_mode=False, snmp_timeout_seconds=3.0, snmp_retries=1)
    poller = RealPoller(settings=settings)
    assert poller._snmp_semaphore._value == 4
    assert poller.SNMP_GRACE_CYCLES == 4

    valid_snmp = {
        f"{IF_OPER_STATUS}.2": 1,
        f"{IF_IN_OCTETS}.2": 1000,
        f"{IF_OUT_OCTETS}.2": 2000,
        f"{IF_OPER_STATUS}.3": 1,
        f"{IF_IN_OCTETS}.3": 3000,
        f"{IF_OUT_OCTETS}.3": 4000,
        CPU_5SEC_REV: 25,
    }

    with (
        patch("telemetry_engine.ping_node", new=AsyncMock(return_value=(30.0, 0.0))),
        patch("telemetry_engine.snmp_get", new=AsyncMock(return_value=valid_snmp)),
    ):
        snap1 = await poller.poll()
        assert {n.id: n for n in snap1.nodes}["R1-Core"].status == Status.UP

    # 4 timeouts transitorios consecutivos con ping 0% pérdida y latencia < 200 ms -> mantienen UP
    with (
        patch("telemetry_engine.ping_node", new=AsyncMock(return_value=(42.0, 0.0))),
        patch("telemetry_engine.snmp_get", new=AsyncMock(return_value=None)),
    ):
        for _ in range(4):
            snap_grace = await poller.poll()
            r1_grace = {n.id: n for n in snap_grace.nodes}["R1-Core"]
            assert r1_grace.status == Status.UP
            assert r1_grace.cpu_percent == 25.0
            assert r1_grace.latency_ms == 42.0
            assert all(e.status != Status.DOWN for e in snap_grace.edges)

        # Quinto fallo SNMP consecutivo -> agota los 4 ciclos de gracia y marca DEGRADED
        snap_exhausted = await poller.poll()
        r1_exhausted = {n.id: n for n in snap_exhausted.nodes}["R1-Core"]
        assert r1_exhausted.status == Status.DEGRADED
        assert r1_exhausted.cpu_percent is None

    # Caída de interfaz (ifOperStatus != 1) o pérdida ICMP 100% dispara DOWN de inmediato (0 ciclos de gracia)
    down_if_snmp = {**valid_snmp, f"{IF_OPER_STATUS}.2": 2, f"{IF_OPER_STATUS}.3": 2}
    with (
        patch("telemetry_engine.ping_node", new=AsyncMock(return_value=(15.0, 0.0))),
        patch("telemetry_engine.snmp_get", new=AsyncMock(return_value=down_if_snmp)),
    ):
        snap_if_down = await poller.poll()
        assert {n.id: n for n in snap_if_down.nodes}["R1-Core"].status == Status.DOWN



