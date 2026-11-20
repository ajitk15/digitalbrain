# Hostinger VPS infrastructure

Open **hostinger-vps.drawio** in draw.io / diagrams.net. It contains native editable shapes, groups and connectors, with three pages:

1. **Default VPS demo** — GitHub deployment through Coolify; public reverse proxy; one app container with web serving, nine worker lanes, AI SDKs, OCR, antivirus and demo test tools; PostgreSQL, Redis, bootstrap jobs, all ten named volumes and external integrations.
2. **Optional scaled services** — separate migration, web, worker, sandbox and ClamAV services, sharing the backing services and persistent workspace.
3. **Optional monitoring** — collector certificates, OpenTelemetry Collector, Prometheus, persistent metrics and restricted operator access.

The default app is the hosted demonstration: reset, approved local writes and tests are enabled while production transport and access controls remain configured.

**Autoscaling is not configured.** The default all-in-one app must remain one container. With the optional scale overlay, web replicas are set manually with `DIGITAL_BRAIN_WEB_REPLICAS` (default 1); worker and sandbox replicas can also be increased manually on a compatible platform. PostgreSQL, Redis and ClamAV remain single instances in this configuration. No automatic scaling controller, load thresholds or replica policies are declared. Monitoring supplies metrics and alerts, not automatic scaling. These are configuration findings; live VPS settings remain unverified.

The diagrams describe committed configuration at **df446fc61362ea143d462b9e64325426da18800a**. The live VPS container inventory, active Coolify overlays, external credentials and actual deployment outcome were not inspected. Optional pages describe available overlays, not a claim that these services are currently running. Coolify is shown as a deployment/controller and proxy role; its own management dependencies are outside the repository's declared stack.

## Previews

Each page has a matching vector SVG and rendered PNG:

- `hostinger-vps-default.svg` / `hostinger-vps-default.png`
- `hostinger-vps-scaled.svg` / `hostinger-vps-scaled.png`
- `hostinger-vps-monitoring.svg` / `hostinger-vps-monitoring.png`

Shapes and preview exports were generated from the same cell definitions. XML parsing, unique identifiers, parent/connector references and the default volume inventory were checked. All three final PNGs were visually inspected. Import into the native draw.io editor was not exercised.

The revised diagrams use short labels and embedded SVG icons. Technology marks come from the bundled Simple Icons catalogue and the OpenAI mark recorded in `icons.json`; infrastructure concepts use locally drawn symbols. Icons are embedded in the draw.io file and SVG previews, so viewing does not need an icon server. Detailed original component descriptions are retained in `component-details.json`.

Regenerate the editable file and SVG previews with `python docs/infrastructure/generate_diagrams.py`. The existing PNGs are browser-rendered previews of those SVGs; regenerating SVGs does not refresh PNGs automatically.

## Evidence

Primary sources: `../../docker-compose.yaml`, `../../docker-compose.scale.yaml`, `../../docker-compose.observability.yaml`, `../../docker-compose.observability.scale.yaml`, `../../Dockerfile`, `../../deploy/README.md` and `../../deploy/observability/collector.yaml`.

`validation.json` records hashes for the editable file and all six preview exports, XML and connection checks, embedded-icon decoding checks, and visual review of the three PNG pages. Live VPS inventory and native draw.io editor import remain unverified.
