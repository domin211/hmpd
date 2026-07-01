from hmpd_bridge.config import TempRange
from hmpd_bridge.models import Zone
from hmpd_bridge.topics import Topics, zone_unique_id


def make_zone(**overrides) -> Zone:
    defaults = dict(
        controller_name="usb0",
        controller_key="usb0_ttyusb0",
        controller_dev="/dev/ttyUSB0",
        zone_index=5,
        zone_name="Room 5",
        unique_id="usb0_ttyusb0_5",
    )
    defaults.update(overrides)
    return Zone(**defaults)


def test_zone_unique_id():
    assert zone_unique_id("usb0_ttyusb0", 5) == "usb0_ttyusb0_5"


def test_topic_names_match_existing_home_assistant_contract():
    topics = Topics(discovery_prefix="homeassistant", base_topic="hmpd")
    assert topics.state("z1") == "hmpd/z1/state"
    assert topics.command("z1") == "hmpd/z1/set_target"
    assert topics.discovery("z1") == "homeassistant/climate/hmpd_z1/config"
    assert topics.bridge_status() == "hmpd/bridge/status"
    assert topics.bridge_resync() == "hmpd/bridge/resync"
    assert topics.set_target_subscription() == "hmpd/+/set_target"


def test_discovery_payload_shape():
    topics = Topics("homeassistant", "hmpd")
    zone = make_zone()
    payload = topics.discovery_payload(zone, TempRange())

    assert payload["unique_id"] == "hmpd_usb0_ttyusb0_5"
    assert payload["temperature_command_topic"] == "hmpd/usb0_ttyusb0_5/set_target"
    assert payload["modes"] == ["off", "heat"]
    assert payload["min_temp"] == 16.0
    assert payload["max_temp"] == 32.0


def test_state_payload_reports_mode_and_falls_back_to_minimum():
    topics = Topics("homeassistant", "hmpd")
    zone = make_zone(current_temp=None, target_temp=None)
    payload = topics.state_payload(zone, TempRange(), mode="off")

    assert payload == {"current_temp": 16.0, "target_temp": 16.0, "mode": "off"}
