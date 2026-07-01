from hmpd_bridge.config import Controller, TempRange, build_options


def test_controller_key_normalizes_name_and_device():
    controller = Controller(name="usb0", dev="/dev/ttyUSB0", baud=4800)
    assert controller.key == "usb0_ttyusb0"


def test_controller_key_strips_accents_and_symbols():
    controller = Controller(name="Kotelna #1", dev="/dev/ttyÚSB0", baud=4800)
    assert controller.key == controller.key.lower()
    assert all(c.isalnum() or c == "_" for c in controller.key)


def test_temp_range_snaps_to_nearest_step():
    temp_range = TempRange(minimum=16.0, maximum=32.0, step=1.0)
    assert temp_range.snap(21.4) == 21.0
    assert temp_range.snap(21.6) == 22.0


def test_temp_range_clamps_out_of_bounds():
    temp_range = TempRange(minimum=16.0, maximum=32.0, step=1.0)
    assert temp_range.snap(5.0) == 16.0
    assert temp_range.snap(100.0) == 32.0


def test_temp_range_valid_current_temp_bounds():
    temp_range = TempRange()
    assert temp_range.valid_current_temp(21.0) is True
    assert temp_range.valid_current_temp(-40.0) is False
    assert temp_range.valid_current_temp(200.0) is False


def test_build_options_defaults_when_empty():
    options = build_options({})
    assert options.debug is False
    assert options.mqtt_host == "core-mosquitto"
    assert len(options.controllers) == 2
    assert {c.name for c in options.controllers} == {"usb0", "usb1"}


def test_build_options_uses_configured_controllers():
    raw = {
        "debug": True,
        "mqtt_host": "broker.local",
        "controllers": [{"name": "usb2", "dev": "/dev/ttyUSB2", "baud": 9600, "expected_regs": 10}],
    }
    options = build_options(raw)
    assert options.debug is True
    assert options.mqtt_host == "broker.local"
    assert len(options.controllers) == 1
    assert options.controllers[0].expected_regs == 10
