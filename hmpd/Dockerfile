name: HMPD Thermostat Bridge
slug: hmpd
version: "1.2.0"
description: Expose HMPD thermostat zones to Home Assistant as MQTT climate entities
arch:
  - amd64
  - aarch64
  - armv7
  - armhf
  - i386
startup: services
boot: auto
init: false
uart: true
map:
  - type: homeassistant_config
    read_only: true
    path: /ha_config
  - type: addon_config
    read_only: false
    path: /config

options:
  mqtt_host: core-mosquitto
  mqtt_port: 1883
  mqtt_username: ""
  mqtt_password: ""
  mqtt_discovery_prefix: homeassistant
  mqtt_base_topic: hmpd
  mqtt_client_id: hmpd_bridge
  mqtt_keepalive: 60
  mqtt_retry_seconds: 10
  controllers:
    - name: usb0
      dev: /dev/ttyUSB0
      baud: 4800
  poll_interval: 10
  reg_refresh_interval: 300
  command_timeout: 15
  temp_min: 10.0
  temp_max: 35.5
  temp_step: 0.1
  hmpd_path: ""
  retain_discovery: true
  retain_state: true
  log_level: info
  debug_raw_output: false
  debug_save_log_file: true
  debug_log_file: /config/hmpd_bridge.log
  debug_dump_parsed_data: false

schema:
  mqtt_host: str
  mqtt_port: port
  mqtt_username: str?
  mqtt_password: password?
  mqtt_discovery_prefix: str
  mqtt_base_topic: str
  mqtt_client_id: str
  mqtt_keepalive: int(5,600)
  mqtt_retry_seconds: int(1,600)
  controllers:
    - name: str
      dev: str
      baud: int
  poll_interval: int(1,3600)
  reg_refresh_interval: int(5,86400)
  command_timeout: int(1,300)
  temp_min: float
  temp_max: float
  temp_step: float
  hmpd_path: str?
  retain_discovery: bool
  retain_state: bool
  log_level: list(trace|debug|info|notice|warning|error|fatal)
  debug_raw_output: bool
  debug_save_log_file: bool
  debug_log_file: str
  debug_dump_parsed_data: bool
