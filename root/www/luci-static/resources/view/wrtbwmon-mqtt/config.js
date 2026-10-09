'use strict';
'require view';
'require form';
'require uci';

return view.extend({
	load: function() {
		return uci.load('wrtbwmon-mqtt');
	},

	render: function() {
		var map = new form.Map(
			'wrtbwmon-mqtt',
			_('wrtbwmon for Home Assistant'),
			_('Expose selected wrtbwmon devices to Home Assistant through MQTT Discovery. Each device receives download rate, upload rate, downloaded data, and uploaded data sensors.')
		);

		var section = map.section(
			form.NamedSection,
			'main',
			'mqtt',
			_('Home Assistant MQTT connection'),
			_('Use the same MQTT broker configured in Home Assistant. The connection uses TCP port 1883.')
		);
		section.anonymous = true;
		section.addremove = false;

		var option = section.option(
			form.Value,
			'host',
			_('MQTT broker host'),
			_('Enter a hostname or IP address. Do not include a protocol or port.')
		);
		option.datatype = 'host';
		option.placeholder = 'mqtt.example.net';
		option.rmempty = false;

		option = section.option(
			form.Value,
			'username',
			_('MQTT username'),
			_('Leave empty when the broker does not require authentication.')
		);
		option.rmempty = true;

		option = section.option(
			form.Value,
			'password',
			_('MQTT password'),
			_('The password is stored in the protected UCI configuration file.')
		);
		option.password = true;
		option.rmempty = true;

		option = section.option(
			form.DynamicList,
			'mac',
			_('Devices to expose'),
			_('Add one MAC address for each device to create in Home Assistant. Addresses are normalized and duplicates are removed when saved.')
		);
		option.datatype = 'macaddr';
		option.placeholder = '02:00:00:00:00:01';
		option.rmempty = true;
		option.write = function(sectionId, value) {
			var entries = Array.isArray(value) ? value : (value ? [ value ] : []);
			var seen = Object.create(null);
			var normalized = entries.map(function(address) {
				return address.toLowerCase();
			}).filter(function(address) {
				if (seen[address])
					return false;
				seen[address] = true;
				return true;
			});

			return uci.set('wrtbwmon-mqtt', sectionId, 'mac', normalized);
		};

		return map.render();
	}
});
