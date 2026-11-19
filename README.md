# NOC Topology & Real-Time Telemetry Dashboard

Dashboard de centro de comando NOC en tiempo real para monitorear una topología de red jerárquica (`R1-Core`, `R2-Dist`, `R3-Edge`), latencia ICMP, pérdida de paquetes, estado de interfaces, uso de CPU y tasas de tráfico en Kbps.

## Arquitectura y Reglas
- Especificación funcional: [spec.md](specs/001-noc-telemetry-dashboard/spec.md)
- Arquitectura técnica: [ARCHITECTURE.md](ARCHITECTURE.md)
- Reglas de gobernanza: [RULES.md](RULES.md)
- Guía de validación completa: [quickstart.md](specs/001-noc-telemetry-dashboard/quickstart.md)

## Requisitos
- Python 3.12
- Navegador moderno (Tailwind CSS, Cytoscape.js y Chart.js se cargan vía CDN sin proceso de build)
- Laboratorio GNS3 solo si `SIMULATION_MODE=false`

## Arranque Rápido (Modo Simulador)
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn main:app --host 0.0.0.0 --port 8000
```

Abrir `http://localhost:8000/` en el navegador.

## Pruebas Automatizadas
```powershell
pytest -v
```
