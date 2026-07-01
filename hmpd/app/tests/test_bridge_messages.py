from hmpd_bridge.bridge import HMPDBridge
from hmpd_bridge.config import Controller, Options, TempRange


def make_bridge() -> HMPDBridge:
    options = Options(
        controllers=(Controller(name="usb0", dev="/dev/ttyUSB0", baud=4800, expected_regs=1),),
        temp_range=TempRange(),
    )
    bridge = HMPDBridge(options)
    bridge.published = []  # type: ignore[attr-defined]

    def fake_publish(topic, payload="", qos=0, retain=False):
        bridge.published.append((topic, payload, retain))

    bridge.mqtt.publish = fake_publish  # type: ignore[method-assign]
    return bridge


REGS_LINES = [
    "0 | Room A | cur: 21.0 | tgt: 22.0 | EN",
    "1 | Room B | cur: 19.5 | tgt: 18.0 | EN",
]
TEMPS_LINES = ["0: 21.4", "1: 19.9"]


def test_apply_regs_creates_zones_and_publishes_discovery_and_state():
    bridge = make_bridge()
    controller = bridge.controllers[0]

    bridge._apply_regs(controller, REGS_LINES)

    assert set(bridge.zones.keys()) == {f"{controller.key}_0", f"{controller.key}_1"}
    zone_a = bridge.zones[f"{controller.key}_0"]
    assert zone_a.zone_name == "Room A"
    assert zone_a.target_temp == 22.0
    assert zone_a.discovered is True

    discovery_topics = [topic for topic, _, _ in bridge.published if "/config" in topic]
    assert any(f"hmpd_{controller.key}_0" in topic for topic in discovery_topics)


def test_apply_regs_removes_stale_zones_not_present_in_latest_scan():
    bridge = make_bridge()
    controller = bridge.controllers[0]

    bridge._apply_regs(controller, REGS_LINES)
    assert len(bridge.zones) == 2

    bridge._apply_regs(controller, [REGS_LINES[0]])  # zone 1 no longer reported

    assert set(bridge.zones.keys()) == {f"{controller.key}_0"}


def test_apply_temps_updates_current_temp_for_known_zones():
    bridge = make_bridge()
    controller = bridge.controllers[0]
    bridge._apply_regs(controller, REGS_LINES)

    bridge._apply_temps(controller, TEMPS_LINES)

    assert bridge.zones[f"{controller.key}_0"].current_temp == 21.4
    assert bridge.zones[f"{controller.key}_1"].current_temp == 19.9


def test_handle_set_target_snaps_value_and_enqueues_job():
    bridge = make_bridge()
    controller = bridge.controllers[0]
    bridge._apply_regs(controller, REGS_LINES)
    zone_key = f"{controller.key}_0"

    bridge._handle_set_target(zone_key, "23.6")

    assert bridge.zones[zone_key].target_temp == 24.0
    assert bridge.queues[controller.key].pending_set_count() == 1


def test_handle_set_target_ignores_unknown_zone_and_bad_payload():
    bridge = make_bridge()
    controller = bridge.controllers[0]
    bridge._apply_regs(controller, REGS_LINES)

    bridge._handle_set_target("does-not-exist", "20.0")
    bridge._handle_set_target(f"{controller.key}_0", "not-a-number")

    assert bridge.queues[controller.key].pending_set_count() == 0


def test_off_mode_reflected_in_state_payload():
    bridge = make_bridge()
    controller = bridge.controllers[0]
    bridge._apply_regs(controller, REGS_LINES)

    zone_b = bridge.zones[f"{controller.key}_1"]  # target_temp 18.0 <= off threshold
    assert zone_b.is_off_mode(18.0) is True
