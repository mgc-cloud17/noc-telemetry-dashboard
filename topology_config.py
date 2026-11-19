"""Configuración y validación de la topología de red gestionada por el NOC.

Define los modelos estructurales de nodos, interfaces y enlaces, la topología
inicial del laboratorio y utilidades para validarla o cargarla desde JSON.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class NodeType(str, Enum):
    """Tipos de dispositivos de red soportados en la topología."""

    ROUTER = "router"
    SWITCH = "switch"
    SERVER = "server"


@dataclass(frozen=True)
class Interface:
    """Representa una interfaz física o lógica monitoreada vía SNMP."""

    name: str
    if_index: int


@dataclass(frozen=True)
class Node:
    """Representa un dispositivo gestionado dentro del diagrama de topología."""

    id: str
    name: str
    ip: str
    type: NodeType
    role: str
    position: tuple[int, int]
    snmp_cpu_index: int = 1


@dataclass(frozen=True)
class Edge:
    """Representa un enlace punto a punto entre dos nodos de la topología."""

    id: str
    source: str
    target: str
    source_if: Interface
    target_if: Interface


# Topología gestionada por defecto (laboratorio Cisco IOSv en GNS3 + VMware).
# Índices SNMP (ifIndex) verificados con `show snmp mib ifmib ifindex`:
# GigabitEthernet0/0 -> ifIndex=1, GigabitEthernet0/1 -> ifIndex=2, GigabitEthernet0/2 -> ifIndex=3.
NODES: tuple[Node, ...] = (
    Node(
        id="R1-Core",
        name="R1-Core",
        ip="192.168.76.10",
        type=NodeType.ROUTER,
        role="core",
        position=(400, 130),
        snmp_cpu_index=1,
    ),
    Node(
        id="R2-Dist",
        name="R2-Dist",
        ip="10.0.12.2",
        type=NodeType.ROUTER,
        role="distribution",
        position=(190, 340),
        snmp_cpu_index=1,
    ),
    Node(
        id="R3-Edge",
        name="R3-Edge",
        ip="10.0.13.2",
        type=NodeType.ROUTER,
        role="edge",
        position=(610, 340),
        snmp_cpu_index=1,
    ),
)

EDGES: tuple[Edge, ...] = (
    Edge(
        id="link-r1-r2",
        source="R1-Core",
        target="R2-Dist",
        source_if=Interface(name="GigabitEthernet0/1", if_index=2),
        target_if=Interface(name="GigabitEthernet0/1", if_index=2),
    ),
    Edge(
        id="link-r1-r3",
        source="R1-Core",
        target="R3-Edge",
        source_if=Interface(name="GigabitEthernet0/2", if_index=3),
        target_if=Interface(name="GigabitEthernet0/1", if_index=2),
    ),
)


def validate_topology(
    nodes: Sequence[Node] = NODES,
    edges: Sequence[Edge] = EDGES,
) -> None:
    """Valida la integridad referencial y las restricciones de la topología.

    Lanza ``ValueError`` si existen identificadores duplicados o vacíos,
    direcciones IPv4 inválidas, referencias a nodos inexistentes, auto-enlaces,
    índices SNMP no positivos o enlaces duplicados sobre las mismas interfaces.
    """
    if not nodes:
        raise ValueError("La topología debe definir al menos un nodo.")

    seen_node_ids: set[str] = set()
    for node in nodes:
        if not node.id or not node.id.strip():
            raise ValueError("Todos los nodos deben tener un identificador no vacío.")
        if node.id in seen_node_ids:
            raise ValueError(f"Identificador de nodo duplicado: '{node.id}'.")
        seen_node_ids.add(node.id)

        if not node.name or not node.name.strip():
            raise ValueError(f"El nodo '{node.id}' debe tener un nombre válido.")

        try:
            ipaddress.IPv4Address(node.ip)
        except ipaddress.AddressValueError as exc:
            raise ValueError(
                f"El nodo '{node.id}' tiene una dirección IPv4 inválida: '{node.ip}'."
            ) from exc

        if node.snmp_cpu_index < 1:
            raise ValueError(
                f"El nodo '{node.id}' requiere snmp_cpu_index >= 1."
            )

    seen_edge_ids: set[str] = set()
    seen_interface_pairs: set[tuple[str, str, str, str]] = set()
    for edge in edges:
        if not edge.id or not edge.id.strip():
            raise ValueError("Todos los enlaces deben tener un identificador no vacío.")
        if edge.id in seen_edge_ids:
            raise ValueError(f"Identificador de enlace duplicado: '{edge.id}'.")
        seen_edge_ids.add(edge.id)

        if edge.source not in seen_node_ids:
            raise ValueError(
                f"El enlace '{edge.id}' referencia un origen inexistente: '{edge.source}'."
            )
        if edge.target not in seen_node_ids:
            raise ValueError(
                f"El enlace '{edge.id}' referencia un destino inexistente: '{edge.target}'."
            )
        if edge.source == edge.target:
            raise ValueError(
                f"El enlace '{edge.id}' forma un auto-enlace no permitido en '{edge.source}'."
            )

        if edge.source_if.if_index < 1 or edge.target_if.if_index < 1:
            raise ValueError(
                f"El enlace '{edge.id}' debe tener índices SNMP if_index >= 1."
            )
        if not edge.source_if.name.strip() or not edge.target_if.name.strip():
            raise ValueError(
                f"El enlace '{edge.id}' debe especificar nombres de interfaz válidos."
            )

        # Normalizamos el par extremo-interfaz para detectar enlaces duplicados bidireccionales.
        endpoint_a = (edge.source, edge.source_if.name)
        endpoint_b = (edge.target, edge.target_if.name)
        ordered = (
            (endpoint_a[0], endpoint_a[1], endpoint_b[0], endpoint_b[1])
            if endpoint_a <= endpoint_b
            else (endpoint_b[0], endpoint_b[1], endpoint_a[0], endpoint_a[1])
        )
        if ordered in seen_interface_pairs:
            raise ValueError(
                f"Enlace duplicado detectado entre '{edge.source}:{edge.source_if.name}' "
                f"y '{edge.target}:{edge.target_if.name}'."
            )
        seen_interface_pairs.add(ordered)


def topology_as_dict(
    nodes: Sequence[Node] = NODES,
    edges: Sequence[Edge] = EDGES,
) -> dict[str, Any]:
    """Serializa la topología al formato JSON esperado por ``GET /api/topology``."""
    return {
        "nodes": [
            {
                "id": node.id,
                "name": node.name,
                "ip": node.ip,
                "type": node.type.value,
                "role": node.role,
                "position": {"x": node.position[0], "y": node.position[1]},
            }
            for node in nodes
        ],
        "edges": [
            {
                "id": edge.id,
                "source": edge.source,
                "target": edge.target,
                "source_if": {
                    "name": edge.source_if.name,
                    "if_index": edge.source_if.if_index,
                },
                "target_if": {
                    "name": edge.target_if.name,
                    "if_index": edge.target_if.if_index,
                },
            }
            for edge in edges
        ],
    }


def load_topology_from_json(
    path: str | Path,
) -> tuple[tuple[Node, ...], tuple[Edge, ...]]:
    """Carga y valida una topología estructurada desde un archivo JSON."""
    raw_text = Path(path).read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw_text)

    nodes = tuple(
        Node(
            id=str(item["id"]),
            name=str(item["name"]),
            ip=str(item["ip"]),
            type=NodeType(item["type"]),
            role=str(item["role"]),
            position=(
                int(item["position"]["x"]),
                int(item["position"]["y"]),
            ),
            snmp_cpu_index=int(item.get("snmp_cpu_index", 1)),
        )
        for item in data.get("nodes", [])
    )

    edges = tuple(
        Edge(
            id=str(item["id"]),
            source=str(item["source"]),
            target=str(item["target"]),
            source_if=Interface(
                name=str(item["source_if"]["name"]),
                if_index=int(item["source_if"]["if_index"]),
            ),
            target_if=Interface(
                name=str(item["target_if"]["name"]),
                if_index=int(item["target_if"]["if_index"]),
            ),
        )
        for item in data.get("edges", [])
    )

    validate_topology(nodes, edges)
    return nodes, edges


# Validación temprana al importar el módulo para detectar errores de definición.
validate_topology(NODES, EDGES)
