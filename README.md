# HMPD Home Assistant Add-ons

This repository contains a Home Assistant add-on that exposes HMPD thermostat zones to Home Assistant through MQTT discovery.

## Install

1. Create a new GitHub repository.
2. Upload the contents of this folder.
3. Edit `repository.yaml` and replace the placeholder URL and maintainer.
4. In Home Assistant, open **Settings -> Add-ons -> Add-on Store -> menu -> Repositories**.
5. Add your GitHub repository URL.
6. Install the **HMPD Thermostat Bridge** add-on.

## Notes

- Put your `hmpd` binary next to `configuration.yaml` on the Home Assistant host, so the add-on can read it as `/homeassistant/hmpd`.
- The add-on publishes MQTT discovery `climate` entities.
