# wrtbwmon for Home Assistant

`wrtbwmon-hass` exposes per-device traffic statistics from `wrtbwmon` to
Home Assistant through MQTT Discovery. It runs as a small `procd` service on
OpenWrt and includes a LuCI page for broker credentials and device selection.

For each configured MAC address, Home Assistant receives four sensors:

| Sensor | Source field | Published unit |
| --- | --- | --- |
| Download rate | `speed_in` | bit/s |
| Upload rate | `speed_out` | bit/s |
| Downloaded data | `in` | bytes |
| Uploaded data | `out` | bytes |

The rate fields are averages over the latest `wrtbwmon` sampling interval, not
instantaneous measurements. The service is read-only: it does not alter
`wrtbwmon`, firewall rules, the MQTT broker, or Home Assistant configuration.

## Features

- Home Assistant MQTT Device Discovery with stable, MAC-based identifiers.
- One Home Assistant device and four sensors per selected network device.
- A persistent MQTT connection with reconnect handling and an LWT.
- Separate availability for the MQTT bridge and the `wrtbwmon` data source.
- Retained discovery and state messages for restart recovery.
- Cleanup of retained discovery when a MAC address is removed.
- Recovery after Home Assistant or MQTT broker restarts.
- Tolerant CSV parsing with complete-snapshot and freshness checks.
- A LuCI JavaScript form with no legacy Lua CBI dependency.
- A reproducible, architecture-independent IPK build.

## Requirements and compatibility

The generated IPK targets OpenWrt-derived systems that use `opkg` and the IPK
package format. It declares these dependencies:

- `wrtbwmon`;
- `python3` 3.10 or newer;
- `python3-paho-mqtt`;
- `luci-base`.

The MQTT integration in Home Assistant must be configured and MQTT Discovery
must be enabled.

The current release intentionally uses the following fixed settings:

- MQTT over plain TCP on port `1883`;
- QoS `0`;
- Home Assistant discovery prefix `homeassistant`;
- MQTT topic prefix `wrtbwmon`;
- a five-second file polling interval;
- a three-minute data-source freshness limit.

TLS, custom ports, and custom topic prefixes are not currently supported. Use
the service only with a trusted broker on a trusted network.

## Installation

Build the package as described in [Building from source](#building-from-source),
or obtain an IPK produced from this repository.

Copy the IPK to the router and install it:

```sh
opkg install /tmp/wrtbwmon-hass.ipk
```

`opkg` installs missing dependencies when they are available from configured
package feeds. The service is enabled at boot during installation.

### Configure with LuCI

Open **Services → wrtbwmon for Home Assistant**, then enter:

1. the MQTT broker hostname or IP address;
2. the MQTT username, if required;
3. the MQTT password, if required;
4. one or more MAC addresses to expose to Home Assistant.

Save and apply the form. MAC addresses are normalized to lowercase and
duplicates are removed.

### Configure with UCI

The equivalent command-line configuration is:

```sh
uci set wrtbwmon-mqtt.main.host='mqtt.example.net'
uci set wrtbwmon-mqtt.main.username='ha-mqtt'
uci set wrtbwmon-mqtt.main.password='change-me'
uci -q delete wrtbwmon-mqtt.main.mac
uci add_list wrtbwmon-mqtt.main.mac='02:00:00:00:00:01'
uci commit wrtbwmon-mqtt
/etc/init.d/wrtbwmon-mqtt restart
```

The values above are synthetic examples. Be aware that entering a password on
the command line can store it in shell history; LuCI is preferable for normal
configuration.

The service reads the traffic database path from
`wrtbwmon.general.path`. If the option is absent, it falls back to
`/tmp/usage.db`. This project never changes the `wrtbwmon` path.

## Home Assistant behavior

Discovery is published to:

```text
homeassistant/device/wrtbwmon_<normalized_mac>/config
```

State is published to:

```text
wrtbwmon/device/<normalized_mac>/state
```

The MAC address determines the Home Assistant device and entity identifiers.
Changing a device IP address therefore does not create new entities.

Two retained availability topics are used:

- `wrtbwmon/bridge/availability` reports the MQTT connection and uses an LWT;
- `wrtbwmon/source/availability` reports whether a complete, recent
  `wrtbwmon` snapshot has been processed.

Home Assistant requires both to be online. These topics describe the bridge
and its data source; they do not claim that an individual network client is
online. If a configured MAC is absent from a snapshot, the service does not
publish fabricated traffic values.

The service also subscribes to `homeassistant/status`. When Home Assistant
announces startup, discovery and cached state are republished. Discovery is
retained as an additional recovery mechanism.

## Service operation

Common commands are:

```sh
/etc/init.d/wrtbwmon-mqtt status
/etc/init.d/wrtbwmon-mqtt start
/etc/init.d/wrtbwmon-mqtt stop
/etc/init.d/wrtbwmon-mqtt restart
```

View recent logs:

```sh
logread -e wrtbwmon-mqtt
```

Follow logs in real time:

```sh
logread -f -e wrtbwmon-mqtt
```

The service does not log the MQTT password.

## Upgrading

Install a newer IPK over the existing package:

```sh
opkg install /tmp/wrtbwmon-hass.ipk
```

`/etc/config/wrtbwmon-mqtt` is a protected conffile. If it has been edited,
`opkg` preserves it and may write the package default to
`/etc/config/wrtbwmon-mqtt-opkg`. This is expected; do not replace the active
configuration with the empty package default.

## Uninstalling

Remove the package with:

```sh
opkg remove wrtbwmon-hass
```

Before removal, the package stops the service and attempts to delete retained
discovery and state messages. The MQTT broker must be reachable for this
cleanup to succeed.

To perform the cleanup manually, stop the service first so it cannot republish
the messages:

```sh
/etc/init.d/wrtbwmon-mqtt stop
/usr/bin/wrtbwmon-mqtt --cleanup
```

## Troubleshooting

### The service is running but no device appears

- Confirm that Home Assistant's MQTT integration is connected and Discovery is
  enabled.
- Check the broker host, username, and password in LuCI.
- Confirm that the configured MAC address exists in the current `wrtbwmon`
  CSV file.
- Inspect the service log for connection or snapshot errors.

### Entities are unavailable

Check both the service log and the age of the data file:

```sh
usage_path=$(uci -q get wrtbwmon.general.path || echo /tmp/usage.db)
ls -l "$usage_path"
```

The source becomes unavailable when no complete, fresh snapshot has been read
for three minutes. This prevents a persisted old rate from being presented as
current data.

### Verify installed dependencies

Some `opkg` versions accept only one package name per `status` invocation:

```sh
for package in wrtbwmon python3 python3-paho-mqtt luci-base; do
	opkg status "$package" | sed -n '/^Package:/p; /^Version:/p; /^Status:/p'
done
```

Verify that Paho MQTT can be imported:

```sh
python3 -c 'import paho.mqtt; print(getattr(paho.mqtt, "__version__", "unknown"))'
```

## Building from source

The build requires a POSIX shell, `tar`, `gzip`, and standard Unix file tools.
No OpenWrt SDK is required because the package contains only Python, shell, and
LuCI JavaScript files.

Build the IPK:

```sh
./build.sh
```

The package is written to `dist/` using this naming scheme:

```text
wrtbwmon-hass_<version>-r<release>_all.ipk
```

The build produces the gzip-compressed tar form of IPK used by OpenWrt `opkg`
and validates the outer archive and both inner archives. Set
`SOURCE_DATE_EPOCH` for an explicit reproducible timestamp or `RELEASE` to
override the package release number:

```sh
SOURCE_DATE_EPOCH=0 RELEASE=1 ./build.sh
```

## Development and testing

Run all local checks and build the package:

```sh
./check.sh
```

The check script runs:

- Python syntax checks and unit tests;
- JSON validation for LuCI menu and ACL files;
- shell syntax checks;
- a JavaScript syntax check when Node.js is installed;
- the complete IPK build and archive validation.

Tests use only synthetic addresses and do not require a running MQTT broker.
The same check script runs in GitHub Actions.

## Security and privacy

- The packaged UCI configuration is installed with mode `0600`.
- MQTT passwords are never included in logs or generated packages.
- The retained-device registry is stored locally under
  `/etc/wrtbwmon-mqtt/` with mode `0600`.
- Real credentials, MAC addresses, IP addresses, hostnames, captured traffic,
  and development-machine paths must not be committed to the repository.
- MQTT traffic is unencrypted in the current release. Do not expose the broker
  port to an untrusted network.

## Contributing

Keep changes focused and include tests for behavior changes. Project code,
comments, documentation, examples, and commit messages should be written in
English. Use synthetic or standards-reserved values in tests and documentation.

## License

This project is licensed under GPL-3.0. See [LICENSE](LICENSE).
