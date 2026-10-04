"""API + persistence tests for /api/v1/unifi/*."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.unifi import _find_existing, _persist_devices
from app.core.config import Settings, settings
from app.db.models import InventoryDevice, ScanRun


@pytest.fixture(autouse=True)
def _clear_unifi_env():
    host = settings.unifi_host
    url = settings.unifi_url
    user = settings.unifi_username
    pw = settings.unifi_password
    settings.unifi_host = ""
    settings.unifi_url = ""
    settings.unifi_username = ""
    settings.unifi_password = ""
    yield
    settings.unifi_host = host
    settings.unifi_url = url
    settings.unifi_username = user
    settings.unifi_password = pw
# --- auth ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_requires_auth_sync_now(client: AsyncClient) -> None:
    res = await client.post("/api/v1/unifi/sync-now")
    assert res.status_code == 401
@pytest.mark.asyncio
async def test_requires_auth_config_get(client: AsyncClient) -> None:
    res = await client.get("/api/v1/unifi/config")
    assert res.status_code == 401
# --- config endpoint -------------------------------------------------------

@pytest.mark.asyncio
async def test_config_omits_credentials(client: AsyncClient, headers: dict) -> None:
    settings.unifi_host = "unifi.local"
    settings.unifi_username = "admin"
    settings.unifi_password = "supersecret"
    res = await client.get("/api/v1/unifi/config", headers=headers)
    assert res.status_code == 200
    body = res.text
    assert "supersecret" not in body
    assert res.json()["credentials_configured"] is True
@pytest.mark.asyncio
async def test_config_credentials_configured_false_when_empty(client: AsyncClient, headers: dict) -> None:
    res = await client.get("/api/v1/unifi/config", headers=headers)
    assert res.status_code == 200
    assert res.json()["credentials_configured"] is False
# --- sync-now --------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_now_rejected_without_credentials(client: AsyncClient, headers: dict) -> None:
    res = await client.post("/api/v1/unifi/sync-now", headers=headers)
    assert res.status_code == 400
@pytest.mark.asyncio
async def test_enable_sync_without_credentials_rejected(client: AsyncClient, headers: dict) -> None:
    res = await client.post(
        "/api/v1/unifi/config",
        json={"sync_enabled": True, "sync_interval": 3600},
        headers=headers,
    )
    assert res.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["environment", "dotenv"])
@pytest.mark.parametrize("user_key,password_key", [
    ("UNIFI_USER", "UNIFI_PASS"),
    ("UNIFI_USERNAME", "UNIFI_PASSWORD"),
])
async def test_env_config_survives_stale_overrides_and_enables_sync(
    client: AsyncClient, headers: dict, monkeypatch, tmp_path, source, user_key, password_key
) -> None:
    """Load real env settings, not assigned attributes, through the UI endpoints."""
    values = {
        "UNIFI_HOST": "192.168.1.1",
        "UNIFI_PORT": "443",
        user_key: "inventory-reader",
        password_key: "test-only-unifi-password",
    }
    for key in (
        "UNIFI_HOST", "UNIFI_URL", "UNIFI_PORT",
        "UNIFI_USER", "UNIFI_PASS", "UNIFI_USERNAME", "UNIFI_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    env_file = None
    if source == "dotenv":
        env_file = tmp_path / ".env"
        env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    else:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    configured = Settings(_env_file=env_file, sqlite_path=str(tmp_path / "homelab.db"))
    configured._override_path().write_text(json.dumps({
        "unifi_host": "",
        "unifi_url": "",
        "unifi_port": 8443,
        "unifi_username": "",
        "unifi_password": "",
        "unifi_sync_enabled": False,
    }))
    configured.load_overrides()
    monkeypatch.setattr("app.api.routes.unifi.settings", configured)
    enable_sync = Mock()
    reschedule = Mock()
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.routes.unifi.set_unifi_sync_enabled", enable_sync)
    monkeypatch.setattr("app.api.routes.unifi.reschedule_unifi_sync", reschedule)
    monkeypatch.setattr("app.api.routes.unifi.fetch_unifi_inventory", fetch)

    res = await client.get("/api/v1/unifi/config", headers=headers)
    assert res.status_code == 200
    assert res.json()["host"] == "192.168.1.1"
    assert res.json()["port"] == 443
    assert res.json()["credentials_configured"] is True
    assert values[password_key] not in res.text

    res = await client.post(
        "/api/v1/unifi/config",
        json={"sync_enabled": True, "sync_interval": 600},
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["sync_enabled"] is True
    enable_sync.assert_called_once_with(True)
    reschedule.assert_called_once_with(600)
    persisted = json.loads(configured._override_path().read_text())
    assert persisted["unifi_sync_enabled"] is True
    for key in ("host", "url", "port", "username", "password", "user", "pass"):
        assert f"unifi_{key}" not in persisted

    res = await client.post("/api/v1/unifi/sync-now", headers=headers)
    assert res.status_code == 200
    fetch.assert_awaited_once()
    assert fetch.call_args.kwargs["host"] == values["UNIFI_HOST"]
    assert fetch.call_args.kwargs["port"] == 443
    assert fetch.call_args.kwargs["username"] == values[user_key]
    assert fetch.call_args.kwargs["password"] == values[password_key]

# --- _find_existing --------------------------------------------------------

@pytest.mark.asyncio
async def test_find_existing_by_ieee(db_session: AsyncSession) -> None:
    row = InventoryDevice(
        ieee_address="unifi-aa:bb:cc:dd:ee:ff",
        mac="aa:bb:cc:dd:ee:ff",
        status="pending",
        discovery_source="unifi",
    )
    db_session.add(row)
    await db_session.commit()

    found = await _find_existing(db_session, "unifi-aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff")
    assert found is not None
    assert found.ieee_address == "unifi-aa:bb:cc:dd:ee:ff"
@pytest.mark.asyncio
async def test_find_existing_matches_a_mac_on_a_claimed_row(db_session: AsyncSession) -> None:
    """A MAC is an identity, so it reaches a row another source already owns.

    This asserted the opposite until the duplicate it caused showed up: the
    Proxmox import gives every guest a `pve-…` ieee_address, so skipping rows
    that have one filed each such machine a second time when the controller
    reported it as a client. The mesh imports never set `mac`, so no Zigbee or
    Z-Wave row is reachable this way.
    """
    row = InventoryDevice(
        ieee_address="scan-device-xyz",
        mac="11:22:33:44:55:66",
        status="approved",
        discovery_source="scan",
    )
    db_session.add(row)
    await db_session.commit()

    found = await _find_existing(db_session, "unifi-11:22:33:44:55:66", "11:22:33:44:55:66")
    assert found is not None
    assert found.ieee_address == "scan-device-xyz"
# --- approved status preserved ---------------------------------------------

@pytest.mark.asyncio
async def test_approved_device_stays_approved(db_session: AsyncSession) -> None:
    ieee = "unifi-aa:00:11:22:33:44"
    row = InventoryDevice(
        ieee_address=ieee,
        mac="aa:00:11:22:33:44",
        status="approved",
        discovery_source="unifi",
        discovery_sources=["unifi"],
    )
    db_session.add(row)
    await db_session.commit()

    devices = [{
        "ieee_address": ieee,
        "mac": "aa:00:11:22:33:44",
        "ip": "10.1.1.5",
        "hostname": "ap-living",
        "label": "ap-living",
        "type": "device",
        "vendor": "Ubiquiti",
        "model": "UAP-AC-PRO",
        "properties": [],
    }]
    await _persist_devices(db_session, devices)

    updated = (
        await db_session.execute(
            select(InventoryDevice).where(InventoryDevice.ieee_address == ieee)
        )
    ).scalars().first()
    assert updated is not None
    assert updated.status == "approved", (
        f"Status changed to '{updated.status}'; auto-sync must not un-approve devices"
    )
# --- import modes ----------------------------------------------------------

@pytest.mark.asyncio
async def test_config_exposes_the_import_modes(client: AsyncClient, headers: dict) -> None:
    res = await client.get("/api/v1/unifi/config", headers=headers)
    assert res.status_code == 200
    modes = res.json()["modes"]
    # Infrastructure only by default: list/user is long and IP-less.
    assert modes == {
        "infrastructure": True,
        "known_clients": False,
        "active_clients": False,
    }


@pytest.mark.asyncio
async def test_import_with_no_source_selected_is_rejected(
    client: AsyncClient, headers: dict
) -> None:
    res = await client.post(
        "/api/v1/unifi/import-pending",
        headers=headers,
        json={
            "host": "unifi.local",
            "username": "admin",
            "password": "pw",
            "modes": {
                "infrastructure": False,
                "known_clients": False,
                "active_clients": False,
            },
        },
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_clients_persist_under_their_own_source(db_session: AsyncSession) -> None:
    """A client keeps discovery_source "unifi-client", infra keeps "unifi"."""
    devices = [
        {
            "ieee_address": "unifi-00:27:22:e0:00:02",
            "mac": "00:27:22:e0:00:02",
            "ip": "192.168.1.101",
            "hostname": "USW Ultra",
            "label": "USW Ultra",
            "type": "switch",
            "vendor": "Ubiquiti",
            "properties": [],
            "source": "unifi",
        },
        {
            "ieee_address": "unifi-bc:24:11:8d:26:ed",
            "mac": "bc:24:11:8d:26:ed",
            "ip": None,
            "hostname": "Paperless",
            "label": "Paperless",
            "type": "computer",
            "vendor": "Proxmox Server Solutions GmbH",
            "properties": [],
            "source": "unifi-client",
        },
    ]
    result = await _persist_devices(db_session, devices)
    assert (result.infra_count, result.client_count) == (1, 1)
    assert result.pending_created == 2

    rows = (await db_session.execute(select(InventoryDevice))).scalars().all()
    by_mac = {r.mac: r for r in rows}
    assert by_mac["00:27:22:e0:00:02"].discovery_sources == ["unifi"]
    assert by_mac["bc:24:11:8d:26:ed"].discovery_sources == ["unifi-client"]
    # Clients are approvable like any other discovery: they land pending with a
    # suggested type.
    assert by_mac["bc:24:11:8d:26:ed"].status == "pending"
    assert by_mac["bc:24:11:8d:26:ed"].suggested_type == "computer"


@pytest.mark.asyncio
async def test_an_ip_scan_row_gains_the_unifi_client_source(
    db_session: AsyncSession,
) -> None:
    """The same machine found by nmap and by UniFi stays one inventory row."""
    row = InventoryDevice(
        ip="192.168.1.50",
        mac="bc:24:11:8d:26:ed",
        status="pending",
        discovery_source="arp",
        discovery_sources=["arp"],
    )
    db_session.add(row)
    await db_session.commit()

    await _persist_devices(db_session, [{
        "ieee_address": "unifi-bc:24:11:8d:26:ed",
        "mac": "bc:24:11:8d:26:ed",
        "ip": "192.168.1.50",
        "hostname": "paperless",
        "label": "paperless",
        "type": "computer",
        "vendor": "Proxmox Server Solutions GmbH",
        "properties": [],
        "source": "unifi-client",
    }])

    rows = (await db_session.execute(select(InventoryDevice))).scalars().all()
    assert len(rows) == 1
    assert rows[0].discovery_sources == ["arp", "unifi-client"]


# --- dedup against rows another source already owns -------------------------

@pytest.mark.asyncio
async def test_merges_into_a_proxmox_row_with_the_same_mac(
    db_session: AsyncSession,
) -> None:
    """Regression: a Proxmox LXC reported as a UniFi client landed twice.

    The MAC fallback skipped rows that had an ieee_address, and every Proxmox
    guest has one (`pve-…`), so the controller's view of the same machine
    became a second inventory entry.
    """
    row = InventoryDevice(
        ieee_address="pve-proxmox-129",
        ip="192.168.1.20",
        mac="bc:24:11:8d:26:ed",
        hostname="paperless-ngx",
        friendly_name="Paperless",
        suggested_type="lxc",
        vendor="Proxmox VE",
        model="LXC",
        status="approved",
        discovery_source="proxmox",
        discovery_sources=["arp", "proxmox"],
    )
    db_session.add(row)
    await db_session.commit()

    await _persist_devices(db_session, [{
        "ieee_address": "unifi-bc:24:11:8d:26:ed",
        "mac": "bc:24:11:8d:26:ed",
        "ip": None,
        "hostname": "Paperless",
        "label": "Paperless",
        "type": "computer",
        "vendor": "Proxmox Server Solutions GmbH",
        "properties": [],
        "source": "unifi-client",
    }])

    rows = (await db_session.execute(select(InventoryDevice))).scalars().all()
    assert len(rows) == 1, "the UniFi client must merge, not create a second row"
    merged = rows[0]
    assert merged.discovery_sources == ["arp", "proxmox", "unifi-client"]
    # The guest keeps its identity and its richer description.
    assert merged.ieee_address == "pve-proxmox-129"
    assert merged.suggested_type == "lxc"
    assert merged.hostname == "paperless-ngx"
    assert merged.ip == "192.168.1.20"
    assert merged.status == "approved"


@pytest.mark.asyncio
async def test_merging_twice_changes_nothing_more(db_session: AsyncSession) -> None:
    row = InventoryDevice(
        ieee_address="pve-proxmox-129",
        mac="bc:24:11:8d:26:ed",
        suggested_type="lxc",
        status="pending",
        discovery_source="proxmox",
        discovery_sources=["proxmox"],
    )
    db_session.add(row)
    await db_session.commit()

    dev = {
        "ieee_address": "unifi-bc:24:11:8d:26:ed",
        "mac": "bc:24:11:8d:26:ed",
        "ip": "192.168.1.20",
        "hostname": "Paperless",
        "label": "Paperless",
        "type": "computer",
        "vendor": "Ubiquiti",
        "properties": [],
        "source": "unifi-client",
    }
    first = await _persist_devices(db_session, [dev])
    second = await _persist_devices(db_session, [dev])

    assert (first.pending_created, second.pending_created) == (0, 0)
    assert (first.pending_updated, second.pending_updated) == (1, 1)
    rows = (await db_session.execute(select(InventoryDevice))).scalars().all()
    assert len(rows) == 1
    assert rows[0].discovery_sources == ["proxmox", "unifi-client"]


@pytest.mark.asyncio
async def test_a_unifi_owned_row_is_still_refreshed(db_session: AsyncSession) -> None:
    """A re-sync must keep updating rows UniFi created — a rename, a new model."""
    row = InventoryDevice(
        ieee_address="unifi-00:27:22:e0:00:02",
        mac="00:27:22:e0:00:02",
        hostname="USW Ultra",
        friendly_name="USW Ultra",
        suggested_type="switch",
        model="USM8P",
        status="pending",
        discovery_source="unifi",
        discovery_sources=["unifi"],
    )
    db_session.add(row)
    await db_session.commit()

    await _persist_devices(db_session, [{
        "ieee_address": "unifi-00:27:22:e0:00:02",
        "mac": "00:27:22:e0:00:02",
        "ip": "192.168.1.101",
        "hostname": "Switch Garage",
        "label": "Switch Garage",
        "type": "switch",
        "vendor": "Ubiquiti",
        "model": "USM8P",
        "properties": [],
        "source": "unifi",
    }])

    updated = (await db_session.execute(select(InventoryDevice))).scalars().first()
    assert updated is not None
    assert updated.friendly_name == "Switch Garage"
    assert updated.ip == "192.168.1.101"


@pytest.mark.asyncio
async def test_the_oldest_row_wins_when_two_share_a_mac(
    db_session: AsyncSession,
) -> None:
    """Without an order the database picked either row, so a re-import drifted."""
    from datetime import datetime, timezone

    older = InventoryDevice(
        ieee_address="pve-proxmox-1",
        mac="bc:24:11:8d:26:ed",
        status="pending",
        discovery_sources=["proxmox"],
        discovered_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    newer = InventoryDevice(
        ieee_address="pve-proxmox-2",
        mac="bc:24:11:8d:26:ed",
        status="pending",
        discovery_sources=["proxmox"],
        discovered_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    db_session.add_all([newer, older])
    await db_session.commit()

    found = await _find_existing(db_session, "unifi-bc:24:11:8d:26:ed", "bc:24:11:8d:26:ed")
    assert found is not None
    assert found.ieee_address == "pve-proxmox-1"


# --- properties on re-sync --------------------------------------------------

def _prop(key: str, value: str, visible: bool = False) -> dict:
    return {"key": key, "value": value, "icon": None, "visible": visible}


@pytest.mark.asyncio
async def test_resync_refreshes_values_and_keeps_the_users_choices(
    db_session: AsyncSession,
) -> None:
    """Properties were written only when a row was created, so a re-sync left
    the firmware and uptime frozen at whatever the first import saw."""
    row = InventoryDevice(
        ieee_address="unifi-00:27:22:e0:00:01",
        mac="00:27:22:e0:00:01",
        status="pending",
        discovery_source="unifi",
        discovery_sources=["unifi"],
        properties=[
            _prop("Firmware", "8.6.11.18870", visible=True),
            _prop("Rack unit", "U12"),  # added by hand from the right panel
        ],
    )
    db_session.add(row)
    await db_session.commit()

    await _persist_devices(db_session, [{
        "ieee_address": "unifi-00:27:22:e0:00:01",
        "mac": "00:27:22:e0:00:01",
        "hostname": "U7 Pro",
        "label": "U7 Pro",
        "type": "ap",
        "properties": [
            _prop("Firmware", "8.7.0.99999"),
            _prop("Uptime (s)", "4200"),
        ],
        "source": "unifi",
    }])

    updated = (await db_session.execute(select(InventoryDevice))).scalars().first()
    assert updated is not None
    by_key = {p["key"]: p for p in updated.properties}
    assert by_key["Firmware"]["value"] == "8.7.0.99999"
    # The user turned Firmware on; a re-sync must not turn it back off.
    assert by_key["Firmware"]["visible"] is True
    assert by_key["Uptime (s)"]["value"] == "4200"
    # Their own property survives untouched.
    assert by_key["Rack unit"]["value"] == "U12"


@pytest.mark.asyncio
async def test_a_merged_row_gains_the_controller_properties(
    db_session: AsyncSession,
) -> None:
    row = InventoryDevice(
        ieee_address="pve-proxmox-129",
        mac="bc:24:11:8d:26:ed",
        status="approved",
        discovery_source="proxmox",
        discovery_sources=["proxmox"],
        properties=[_prop("VMID", "129"), _prop("Kind", "LXC")],
    )
    db_session.add(row)
    await db_session.commit()

    await _persist_devices(db_session, [{
        "ieee_address": "unifi-bc:24:11:8d:26:ed",
        "mac": "bc:24:11:8d:26:ed",
        "hostname": "Paperless",
        "label": "Paperless",
        "type": "computer",
        "properties": [_prop("Connection", "wired"), _prop("Switch port", "7")],
        "source": "unifi-client",
    }])

    merged = (await db_session.execute(select(InventoryDevice))).scalars().first()
    assert merged is not None
    by_key = {p["key"]: p["value"] for p in merged.properties}
    # Proxmox's facts stay, the controller's are added alongside.
    assert by_key == {
        "VMID": "129", "Kind": "LXC", "Connection": "wired", "Switch port": "7",
    }


# --- scan history -----------------------------------------------------------
# The manual import runs inline, so nothing else writes its run row. Without one
# a finished import left no trace in Scan History at all.

_IMPORT_BODY = {
    "host": "unifi.local",
    "port": 8443,
    "username": "admin",
    "password": "pw",
    "modes": {"infrastructure": True, "known_clients": False, "active_clients": False},
}

_ONE_DEVICE = [
    {
        "ieee_address": "unifi-00:27:22:e0:00:09",
        "mac": "00:27:22:e0:00:09",
        "ip": "192.168.1.109",
        "hostname": "USW Lite",
        "label": "USW Lite",
        "type": "switch",
        "vendor": "Ubiquiti",
        "properties": [],
        "source": "unifi",
    }
]


@pytest.mark.asyncio
async def test_import_pending_records_a_scan_run(
    client: AsyncClient, headers: dict, db_session: AsyncSession, monkeypatch
) -> None:
    async def _fake_fetch(**_kwargs):
        return _ONE_DEVICE

    monkeypatch.setattr("app.api.routes.unifi.fetch_unifi_inventory", _fake_fetch)

    res = await client.post("/api/v1/unifi/import-pending", headers=headers, json=_IMPORT_BODY)
    assert res.status_code == 200

    runs = (await db_session.execute(select(ScanRun))).scalars().all()
    assert len(runs) == 1
    run = runs[0]
    assert run.kind == "unifi"
    assert run.status == "done"
    assert run.devices_found == 1
    assert run.ranges == ["unifi.local:8443"]
    # Written already terminal — there is no background job to finish it later.
    assert run.finished_at is not None
    assert run.error is None


@pytest.mark.asyncio
async def test_a_failed_import_is_recorded_as_an_error_run(
    client: AsyncClient, headers: dict, db_session: AsyncSession, monkeypatch
) -> None:
    async def _fake_fetch(**_kwargs):
        raise ConnectionError("controller unreachable")

    monkeypatch.setattr("app.api.routes.unifi.fetch_unifi_inventory", _fake_fetch)

    res = await client.post("/api/v1/unifi/import-pending", headers=headers, json=_IMPORT_BODY)
    assert res.status_code == 502

    runs = (await db_session.execute(select(ScanRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == "error"
    assert runs[0].error == "controller unreachable"
    assert runs[0].devices_found == 0


@pytest.mark.asyncio
async def test_sync_now_records_a_scan_run(
    client: AsyncClient, headers: dict, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "unifi_host", "10.0.0.5")
    monkeypatch.setattr(settings, "unifi_username", "admin")
    monkeypatch.setattr(settings, "unifi_password", "pw")

    async def _fake_fetch(**_kwargs):
        return _ONE_DEVICE

    monkeypatch.setattr("app.api.routes.unifi.fetch_unifi_inventory", _fake_fetch)

    res = await client.post("/api/v1/unifi/sync-now", headers=headers)
    assert res.status_code == 200

    runs = (await db_session.execute(select(ScanRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].kind == "unifi"
    assert runs[0].status == "done"
    assert runs[0].devices_found == 1
