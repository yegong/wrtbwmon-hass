#!/bin/sh

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

python3 -m py_compile src/wrtbwmon_mqtt.py
python3 -m unittest discover -s tests -v
python3 -m json.tool \
	root/usr/share/luci/menu.d/luci-app-wrtbwmon-mqtt.json >/dev/null
python3 -m json.tool \
	root/usr/share/rpcd/acl.d/luci-app-wrtbwmon-mqtt.json >/dev/null

sh -n \
	build.sh \
	check.sh \
	root/etc/init.d/wrtbwmon-mqtt \
	packaging/postinst \
	packaging/prerm \
	packaging/postrm

if command -v node >/dev/null 2>&1; then
	node --check root/www/luci-static/resources/view/wrtbwmon-mqtt/config.js
else
	echo "Skipping JavaScript syntax check because Node.js is unavailable"
fi

./build.sh
