import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "order-service": (8001, "order_service"),
    "payment-service": (8002, "payment_service"),
    "user-service": (8003, "user_service"),
    "inventory-service": (8004, "inventory_service"),
}


def _load_yaml(relative_path):
    return yaml.safe_load((ROOT / relative_path).read_text())


def test_compose_defines_local_observability_stack_and_health_checks():
    compose = _load_yaml("docker-compose.yml")
    services = compose["services"]

    assert set(services) == {
        *TARGETS,
        "prometheus",
        "grafana",
        "loki",
        "alloy",
    }
    assert compose["networks"]["default"]["driver"] == "bridge"

    for name, (port, directory) in TARGETS.items():
        service = services[name]
        assert service["build"] == f"./target_apps/{directory}"
        assert f"127.0.0.1:{port}:{port}" in service["ports"]
        assert service["healthcheck"]["test"][0] == "CMD"

    assert "127.0.0.1:9090:9090" in services["prometheus"]["ports"]
    assert "127.0.0.1:3000:3000" in services["grafana"]["ports"]
    assert "ports" not in services["loki"]
    assert services["alloy"]["ports"] == ["127.0.0.1:1516:1515/tcp"]

    for name in services:
        assert "healthcheck" in services[name]

    healthchecks = {
        name: service["healthcheck"]["test"]
        for name, service in services.items()
    }
    for name, (port, _) in TARGETS.items():
        assert healthchecks[name] == [
            "CMD",
            "python",
            "-c",
            (
                "from urllib.request import urlopen; "
                f"urlopen('http://127.0.0.1:{port}/health', timeout=2)"
            ),
        ]

    assert healthchecks["prometheus"] == [
        "CMD", "wget", "-q", "-O", "/dev/null",
        "http://127.0.0.1:9090/-/ready",
    ]
    assert healthchecks["grafana"] == [
        "CMD", "curl", "--fail", "--silent", "--show-error",
        "http://127.0.0.1:3000/api/health",
    ]
    assert healthchecks["loki"] == [
        "CMD", "wget", "-q", "-O", "/dev/null",
        "http://127.0.0.1:3100/ready",
    ]
    assert healthchecks["alloy"] == [
        "CMD-SHELL",
        (
            "/usr/bin/bash -c 'exec 3<>/dev/tcp/127.0.0.1/12345; "
            "printf \"GET /-/ready HTTP/1.0\\r\\n\\r\\n\" >&3; "
            "IFS= read -r status <&3; [[ \"$$status\" == *200* ]]'"
        ),
    ]

    alloy = services["alloy"]
    assert alloy["image"] == "grafana/alloy:v1.21.1"
    assert alloy["command"] == [
        "run", "--storage.path=/tmp/alloy-data",
        "/etc/alloy/config.alloy",
    ]
    assert alloy["volumes"] == [
        "./monitoring/alloy/config.alloy:/etc/alloy/config.alloy:ro"
    ]
    assert alloy["depends_on"]["loki"]["condition"] == "service_healthy"

    grafana_password = services["grafana"]["environment"][
        "GF_SECURITY_ADMIN_PASSWORD"
    ]
    assert grafana_password == (
        "${GRAFANA_ADMIN_PASSWORD:?Set GRAFANA_ADMIN_PASSWORD in .env}"
    )

    for volume in (
        "prometheus-data:/prometheus",
        "grafana-data:/var/lib/grafana",
        "loki-data:/loki",
    ):
        assert any(
            volume in service.get("volumes", [])
            for service in services.values()
        )


def test_prometheus_scrape_targets_match_target_service_dns_and_ports():
    compose = _load_yaml("docker-compose.yml")
    prometheus = _load_yaml("monitoring/prometheus/prometheus.yml")
    scrape_jobs = {
        job["job_name"]: job
        for job in prometheus["scrape_configs"]
    }

    assert set(scrape_jobs) == set(TARGETS)
    for service, (port, _) in TARGETS.items():
        job = scrape_jobs[service]
        assert job["metrics_path"] == "/metrics"
        assert job["static_configs"][0]["targets"] == [f"{service}:{port}"]
        assert compose["services"]["prometheus"]["depends_on"][service][
            "condition"
        ] == "service_healthy"

        target = ROOT / "target_apps" / TARGETS[service][1]
        requirements = (target / "requirements.txt").read_text().splitlines()
        application = (target / "main.py").read_text()
        dockerfile = (target / "Dockerfile").read_text()
        assert "prometheus-fastapi-instrumentator" in requirements
        assert "COPY requirements.txt ." in dockerfile
        assert "pip install --no-cache-dir -r requirements.txt" in dockerfile
        assert 'endpoint="/metrics"' in application


def test_grafana_provisions_prometheus_loki_and_dashboard():
    compose = _load_yaml("docker-compose.yml")
    datasources = _load_yaml("monitoring/grafana/provisioning/datasources.yml")
    providers = _load_yaml("monitoring/grafana/provisioning/dashboards.yml")
    dashboard = json.loads(
        (ROOT / "monitoring/grafana/dashboards/kybernaut-overview.json")
        .read_text()
    )

    configured = {
        datasource["name"]: datasource
        for datasource in datasources["datasources"]
    }
    assert configured["prometheus"]["url"] == "http://prometheus:9090"
    assert configured["prometheus"]["uid"] == "eg0se2l2z6p6of"
    assert configured["Loki"]["url"] == "http://loki:3100"
    assert providers["providers"][0]["options"]["path"] == (
        "/var/lib/grafana/dashboards"
    )
    assert dashboard["uid"] == "kybernaut-overview"
    assert len(dashboard["panels"]) >= 4
    assert all(
        panel["datasource"]["uid"] == configured["prometheus"]["uid"]
        for panel in dashboard["panels"]
    )

    grafana_mounts = compose["services"]["grafana"]["volumes"]
    assert any("datasources.yml:/etc/grafana/provisioning/datasources/" in m
               for m in grafana_mounts)
    assert any("dashboards.yml:/etc/grafana/provisioning/dashboards/" in m
               for m in grafana_mounts)


def test_compose_logs_use_socket_free_alloy_syslog_pipeline():
    compose = _load_yaml("docker-compose.yml")
    services = compose["services"]

    for service, tag in (
        ("order-service", "kybernaut-order"),
        ("payment-service", "kybernaut-payment"),
        ("user-service", "kybernaut-user"),
        ("inventory-service", "kybernaut-inventory"),
    ):
        application = services[service]
        assert application["logging"]["driver"] == "syslog"
        assert application["logging"]["options"] == {
            "syslog-address": "tcp://127.0.0.1:1516",
            "syslog-format": "rfc5424micro",
            "tag": tag,
            "mode": "non-blocking",
            "max-buffer-size": "4m",
        }
        assert application["depends_on"]["alloy"]["condition"] == (
            "service_healthy"
        )

    for compose_path in (
        "docker-compose.yml",
        "docker-compose.observability.yml",
    ):
        parsed = _load_yaml(compose_path)
        assert "promtail" not in parsed["services"]
        for service in parsed["services"].values():
            for mount in service.get("volumes", []):
                assert "/var/run/docker.sock" not in str(mount)
                assert "/var/lib/docker/containers" not in str(mount)

    alloy_config = (ROOT / "monitoring" / "alloy" / "config.alloy").read_text()
    assert 'address                = "0.0.0.0:1515"' in alloy_config
    assert "use_incoming_timestamp = true" in alloy_config
    assert (
        'regex         = "^kybernaut-(order|payment|user|inventory)$"'
        in alloy_config
    )
    assert 'url = "http://loki:3100/loki/api/v1/push"' in alloy_config


def test_grafana_password_is_required_and_not_supplied_in_example():
    example = (ROOT / ".env.example").read_text().splitlines()
    password_values = [
        line.partition("=")[2]
        for line in example
        if line.startswith("GRAFANA_ADMIN_PASSWORD=")
    ]

    assert len(password_values) == 1
    assert not password_values[0]
    readme = (ROOT / "README.md").read_text()
    assert "GRAFANA_ADMIN_PASSWORD" in readme
    assert "unset or blank" in readme
    assert "does not" in readme and "already-initialized" in readme
    assert "admin` / `admin" not in readme


def test_all_host_run_prometheus_consumers_use_published_endpoint():
    expected_consumers = {
        "incident_detector.py",
        "incident_diagnoser.py",
        "prometheus_features.py",
        "remediation_service.py",
    }
    consumers = {
        path.name
        for path in (ROOT / "app" / "services").glob("*.py")
        if "http://localhost:9090" in path.read_text()
    }

    assert consumers == expected_consumers


def test_isolated_observability_compose_defines_all_services():
    compose = _load_yaml("docker-compose.observability.yml")

    assert compose["name"] == "kybernaut-observability"
    assert set(compose["services"]) == {"loki", "alloy"}
    loki = compose["services"]["loki"]
    alloy = compose["services"]["alloy"]

    assert loki["container_name"] == (
        "kybernaut-observability-loki"
    )
    assert alloy["container_name"] == (
        "kybernaut-observability-alloy"
    )
    assert loki["image"] == "grafana/loki:3.0.0"
    assert alloy["image"] == "grafana/alloy:v1.21.1"
    assert "ports" not in loki
    assert alloy["ports"] == [
        "127.0.0.1:1515:1515/tcp"
    ]
    assert loki["networks"] == {
        "observability": {"aliases": ["loki"]}
    }
    assert alloy["networks"] == ["observability"]
    assert "kybernaut_default" not in compose["networks"]
    assert compose["networks"]["observability"]["name"] == (
        "kybernaut-observability_backend"
    )
    assert compose["volumes"]["loki-data"]["name"] == (
        "kybernaut-observability_loki-data"
    )
    assert alloy["volumes"] == [
        "./monitoring/alloy/config.alloy:/etc/alloy/config.alloy:ro"
    ]
    assert loki["healthcheck"]["test"] == [
        "CMD", "wget", "-q", "-O", "/dev/null",
        "http://127.0.0.1:3100/ready",
    ]
    assert alloy["healthcheck"]["test"] == [
        "CMD-SHELL",
        (
            "/usr/bin/bash -c 'exec 3<>/dev/tcp/127.0.0.1/12345; "
            "printf \"GET /-/ready HTTP/1.0\\r\\n\\r\\n\" >&3; "
            "IFS= read -r status <&3; [[ \"$$status\" == *200* ]]'"
        ),
    ]
    assert alloy["depends_on"]["loki"]["condition"] == "service_healthy"
    assert not any(
        "docker.sock" in volume or "/var/lib/docker" in volume
        for service in (loki, alloy)
        for volume in service.get("volumes", [])
    )
    assert "--storage.path=/tmp/alloy-data" in alloy["command"]


def test_alloy_syslog_pipeline_allowlists_fixed_service_tags():
    alloy = (ROOT / "monitoring" / "alloy" / "config.alloy").read_text()

    assert 'loki.source.syslog "docker"' in alloy
    assert 'protocol               = "tcp"' in alloy
    assert 'syslog_format          = "rfc5424"' in alloy
    assert "use_incoming_timestamp = true" in alloy
    assert "max_message_length     = 8192" in alloy
    assert "relabel_rules = loki.relabel.docker_tags.rules" in alloy
    assert "loki.write.kybernaut.receiver" in alloy
    assert 'url = "http://loki:3100/loki/api/v1/push"' in alloy
    assert 'source_labels = ["__syslog_message_app_name"]' in alloy
    assert (
        'regex         = "^kybernaut-(order|payment|user|inventory)$"'
        in alloy
    )
    for tag, service in (
        ("kybernaut-order", "order"),
        ("kybernaut-payment", "payment"),
        ("kybernaut-user", "user"),
        ("kybernaut-inventory", "inventory"),
    ):
        assert f'regex         = "^{tag}$"' in alloy
        assert f'replacement   = "{service}"' in alloy

    assert "container_id" not in alloy
    assert "docker.sock" not in alloy
    assert "wal {" not in alloy


def test_loki_filesystem_tsdb_retention_paths_are_persistent():
    loki = _load_yaml("monitoring/loki/loki-config.yml")

    assert loki["common"]["storage"]["filesystem"] == {
        "chunks_directory": "/loki/chunks",
        "rules_directory": "/loki/rules",
    }
    schema = loki["schema_config"]["configs"][0]
    assert schema["store"] == "tsdb"
    assert schema["object_store"] == "filesystem"
    assert schema["schema"] == "v13"
    assert schema["index"]["period"] == "24h"
    assert loki["storage_config"]["tsdb_shipper"] == {
        "active_index_directory": "/loki/tsdb-index",
        "cache_location": "/loki/tsdb-cache",
    }
    assert loki["compactor"] == {
        "working_directory": "/loki/compactor",
        "compaction_interval": "10m",
        "retention_enabled": True,
        "retention_delete_delay": "2h",
        "delete_request_store": "filesystem",
    }
    assert loki["limits_config"]["retention_period"] == "168h"
    assert loki["limits_config"]["max_query_lookback"] == "168h"
