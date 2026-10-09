#!/bin/sh

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PACKAGE_NAME=wrtbwmon-hass
VERSION=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' "$PROJECT_DIR/src/wrtbwmon_mqtt.py")
RELEASE=${RELEASE:-1}
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}

[ -n "$VERSION" ] || { echo "Cannot determine the package version" >&2; exit 1; }
case "$VERSION" in
	*[!0-9A-Za-z.+~-]*) echo "Invalid VERSION: $VERSION" >&2; exit 2 ;;
esac
case "$RELEASE" in
	*[!0-9A-Za-z.+~-]*) echo "Invalid RELEASE: $RELEASE" >&2; exit 2 ;;
esac
case "$SOURCE_DATE_EPOCH" in
	''|*[!0-9]*) echo "Invalid SOURCE_DATE_EPOCH: $SOURCE_DATE_EPOCH" >&2; exit 2 ;;
esac

for command_name in cp gzip install mkdir rm sed tar wc; do
	command -v "$command_name" >/dev/null 2>&1 || {
		echo "Required build command is missing: $command_name" >&2
		exit 1
	}
done

BUILD_DIR="$PROJECT_DIR/build/ipk"
PACKAGE_ROOT="$BUILD_DIR/root"
CONTROL_ROOT="$BUILD_DIR/control"
ARCHIVE_ROOT="$BUILD_DIR/archive"
OUTPUT_DIR="$PROJECT_DIR/dist"
OUTPUT_FILE="$OUTPUT_DIR/${PACKAGE_NAME}_${VERSION}-r${RELEASE}_all.ipk"

rm -rf "$BUILD_DIR"
mkdir -p "$PACKAGE_ROOT" "$CONTROL_ROOT" "$ARCHIVE_ROOT" "$OUTPUT_DIR"

cp -a "$PROJECT_DIR/root/." "$PACKAGE_ROOT/"
install -D -m 0755 "$PROJECT_DIR/src/wrtbwmon_mqtt.py" \
	"$PACKAGE_ROOT/usr/bin/wrtbwmon-mqtt"
chmod 0755 "$PACKAGE_ROOT/etc/init.d/wrtbwmon-mqtt"
chmod 0600 "$PACKAGE_ROOT/etc/config/wrtbwmon-mqtt"

tar_common_options="--sort=name --format=gnu --numeric-owner --owner=0 --group=0 --mtime=@$SOURCE_DATE_EPOCH"
(cd "$PACKAGE_ROOT" && tar $tar_common_options -cf - .) | gzip -n -9 >"$ARCHIVE_ROOT/data.tar.gz"
INSTALLED_SIZE=$(gzip -dc "$ARCHIVE_ROOT/data.tar.gz" | wc -c)

sed \
	-e "s/@VERSION@/$VERSION/g" \
	-e "s/@RELEASE@/$RELEASE/g" \
	-e "s/@INSTALLED_SIZE@/$INSTALLED_SIZE/g" \
	"$PROJECT_DIR/packaging/control" >"$CONTROL_ROOT/control"
install -m 0644 "$PROJECT_DIR/packaging/conffiles" "$CONTROL_ROOT/conffiles"
install -m 0755 "$PROJECT_DIR/packaging/postinst" "$CONTROL_ROOT/postinst"
install -m 0755 "$PROJECT_DIR/packaging/prerm" "$CONTROL_ROOT/prerm"
install -m 0755 "$PROJECT_DIR/packaging/postrm" "$CONTROL_ROOT/postrm"

printf '2.0\n' >"$ARCHIVE_ROOT/debian-binary"

(cd "$CONTROL_ROOT" && tar $tar_common_options -cf - .) | gzip -n -9 >"$ARCHIVE_ROOT/control.tar.gz"

rm -f "$OUTPUT_FILE"
(cd "$ARCHIVE_ROOT" && \
	tar $tar_common_options -cf - ./debian-binary ./data.tar.gz ./control.tar.gz) \
	| gzip -n -9 >"$OUTPUT_FILE"

EXPECTED_MEMBERS='./debian-binary
./data.tar.gz
./control.tar.gz'
ACTUAL_MEMBERS=$(tar -tzf "$OUTPUT_FILE")
[ "$ACTUAL_MEMBERS" = "$EXPECTED_MEMBERS" ] || {
	echo "Generated IPK has an invalid outer archive layout" >&2
	exit 1
}
[ "$(tar -xOzf "$OUTPUT_FILE" ./debian-binary)" = "2.0" ] || {
	echo "Generated IPK has an invalid format marker" >&2
	exit 1
}
tar -xOzf "$OUTPUT_FILE" ./control.tar.gz | gzip -t
tar -xOzf "$OUTPUT_FILE" ./data.tar.gz | gzip -t

echo "Built $OUTPUT_FILE"
