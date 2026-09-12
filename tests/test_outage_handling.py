import importlib.util
import logging
import pathlib
import sys
import types
import unittest
from unittest import mock

import requests


PLUGIN_PATH = (
    pathlib.Path(__file__).parents[1]
    / 'miniUniFi.indigoPlugin/Contents/Server Plugin/plugin.py'
)
sys.path.insert(0, str(PLUGIN_PATH.parent))


class FakePluginBase:
    class StopThread(Exception):
        pass

    def getDeviceStateList(self, _device):
        return [{'key': 'controllerAvailable'}]

    def getDeviceStateDictForBoolTrueFalseType(self, key, *_labels):
        return {'key': key, 'type': 'bool'}

    def getDeviceStateDictForNumberType(self, key, *_labels):
        return {'key': key, 'type': 'number'}

    def getDeviceStateDictForStringType(self, key, *_labels):
        return {'key': key, 'type': 'string'}


fake_indigo = types.SimpleNamespace(
    PluginBase=FakePluginBase,
    kStateImageSel=types.SimpleNamespace(SensorTripped='tripped', SensorOn='on'),
    devices={},
)
sys.modules.setdefault('indigo', fake_indigo)
spec = importlib.util.spec_from_file_location('miniunifi_plugin', PLUGIN_PATH)
plugin_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_module)


class RecordingLogger:
    def __init__(self):
        self.messages = []

    def _record(self, level, message):
        self.messages.append((level, str(message)))

    def debug(self, message):
        self._record(logging.DEBUG, message)

    def threaddebug(self, message):
        self._record(logging.DEBUG, message)

    def info(self, message):
        self._record(logging.INFO, message)

    def warning(self, message):
        self._record(logging.WARNING, message)

    def error(self, message):
        self._record(logging.ERROR, message)

    def exception(self, message):
        self._record(logging.ERROR, message)


class FakeDevice:
    def __init__(self, device_id=1, device_type='unifiController'):
        self.id = device_id
        self.name = 'Test Controller' if device_type == 'unifiController' else 'Phone'
        self.deviceTypeId = device_type
        self.address = 'aa:bb:cc:dd:ee:ff'
        self.pluginProps = {
            'address': '192.0.2.1',
            'port': '443',
            'username': 'user',
            'password': 'secret',
            'ssl_verify': False,
            'unifi_controller': '1',
            'unifi_site': 'default',
        }
        self.states = {}
        self.state_history = []
        self.images = []
        self.plugin_props_replacements = 0

    def updateStateOnServer(self, key, value, uiValue=None):
        self.states[key] = value
        self.state_history.append((key, value))
        if uiValue is not None:
            self.states[f'{key}.ui'] = uiValue

    def updateStatesOnServer(self, updates):
        for update in updates:
            self.states[update['key']] = update['value']

    def updateStateImageOnServer(self, image):
        self.images.append(image)

    def replacePluginPropsOnServer(self, props):
        self.pluginProps = props
        self.plugin_props_replacements += 1


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None, content_type='application/json'):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else ('json' if payload is not None else '')
        self.content = self.text.encode()
        self.headers = {'Content-Type': content_type} if content_type else {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, post_result, get_results=()):
        self.post_result = post_result
        self.get_results = iter(get_results)
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set('TOKEN', 'must-not-be-logged')

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, *_args, **_kwargs):
        if isinstance(self.post_result, Exception):
            raise self.post_result
        return self.post_result

    def get(self, *_args, **_kwargs):
        result = next(self.get_results)
        if isinstance(result, Exception):
            raise result
        return result


def healthy_session():
    return FakeSession(
        FakeResponse(payload={}),
        [
            FakeResponse(payload={'meta': {'server_version': '9.0'}}),
            FakeResponse(payload={'data': [{'name': 'default', 'desc': 'Default'}]}),
            FakeResponse(payload={'data': [{
                'mac': 'aa:bb:cc:dd:ee:ff', 'is_wired': False, 'essid': 'wifi'
            }]}),
            FakeResponse(payload={'data': []}),
        ],
    )


class OutageHandlingTests(unittest.TestCase):
    def setUp(self):
        self.plugin = object.__new__(plugin_module.Plugin)
        self.plugin.logger = RecordingLogger()
        self.plugin.outageReminderInterval = 900
        self.plugin.unifi_controllers = {
            1: {
                'name': 'Test Controller', 'controller_type': 'os',
                'snapshot_authoritative': False, 'last_success': None,
                'outage_since': None, 'last_failure_log': None,
                'last_failure_signature': None,
            }
        }
        self.plugin.unifi_clients = {}
        self.plugin.unifi_devices = {}
        self.controller = FakeDevice()

    def poll_with(self, session):
        with mock.patch.object(plugin_module.requests, 'Session', return_value=session):
            self.plugin.updateUniFiController(self.controller)

    def test_recorded_outage_sequence_survives_and_recovers(self):
        sessions = [
            healthy_session(),
            FakeSession(FakeResponse(payload={}), [FakeResponse(status=502)]),
            FakeSession(requests.exceptions.Timeout('timed out')),
            FakeSession(requests.exceptions.ConnectionError('refused')),
            FakeSession(FakeResponse(payload={}), [
                FakeResponse(payload={'meta': {}}), FakeResponse(payload=None),
            ]),
            FakeSession(FakeResponse(payload={}), [
                FakeResponse(payload={'meta': {}}),
                FakeResponse(payload=None, text='<html>recovering</html>', content_type='text/html'),
            ]),
            healthy_session(),
        ]

        for session in sessions:
            self.poll_with(session)

        record = self.plugin.unifi_controllers[1]
        self.assertTrue(record['snapshot_authoritative'])
        self.assertEqual(record['controller_type'], 'os')
        self.assertEqual(self.controller.states['status'], 'Online')
        self.assertTrue(self.controller.states['controllerAvailable'])
        self.assertFalse(self.controller.states['dataStale'])
        self.assertIn('lastSuccessfulPoll', self.controller.states)
        self.assertTrue(any('recovered after' in msg for _, msg in self.plugin.logger.messages))
        self.assertEqual(
            self.controller.plugin_props_replacements, 1,
            'an unchanged controller version must not restart Indigo device communication',
        )
        combined_logs = '\n'.join(msg for _, msg in self.plugin.logger.messages)
        self.assertNotIn('must-not-be-logged', combined_logs)
        self.assertNotIn('secret', combined_logs)
        statuses = [value for key, value in self.controller.state_history if key == 'status']
        self.assertIn('Network App Temporarily Unavailable', statuses)
        self.assertIn('Console Unavailable', statuses)
        self.assertIn('Recovering', statuses)

    def test_http_failure_distinguishes_console_from_network_app(self):
        self.plugin.unifi_controllers[1]['sites'] = {'default': {}}
        self.poll_with(FakeSession(FakeResponse(payload={}), [FakeResponse(status=502)]))
        self.assertEqual(
            self.controller.states['status'], 'Network App Temporarily Unavailable')
        self.assertTrue(self.controller.states['consoleAvailable'])
        self.assertFalse(self.controller.states['networkAppAvailable'])
        self.assertFalse(self.controller.states['controllerAvailable'])
        self.assertTrue(self.controller.states['dataStale'])

    def test_failed_detection_does_not_guess_or_cache_controller_type(self):
        self.plugin.unifi_controllers[1]['controller_type'] = None
        with mock.patch.object(
            plugin_module.requests, 'head',
            side_effect=requests.exceptions.Timeout('timed out'),
        ):
            self.plugin.updateUniFiController(self.controller)
        self.assertIsNone(self.plugin.unifi_controllers[1]['controller_type'])
        self.assertFalse(self.plugin.unifi_controllers[1]['snapshot_authoritative'])

    def test_unchanged_controller_version_does_not_replace_plugin_properties(self):
        self.controller.pluginProps['version'] = '9.0'
        self.poll_with(healthy_session())
        self.assertEqual(self.controller.plugin_props_replacements, 0)

    def test_dependent_retains_value_but_is_explicitly_stale(self):
        self.plugin.unifi_controllers[1].update({
            'sites': {'default': {'actives': {}}},
            'snapshot_authoritative': False,
            'last_success': '2026-09-11T03:12:00',
        })
        client = FakeDevice(2, 'unifiWirelessClient')
        client.states['onOffState'] = True

        self.plugin.updateUniFiClient(client)

        self.assertTrue(client.states['onOffState'])
        self.assertEqual(client.states['onOffState.ui'], 'Online (stale)')
        self.assertFalse(client.states['controllerAvailable'])
        self.assertTrue(client.states['dataStale'])
        self.assertEqual(client.states['lastSuccessfulPoll'], '2026-09-11T03:12:00')

    def test_repeated_identical_failure_is_deduplicated(self):
        with mock.patch.object(plugin_module.time, 'time', side_effect=[1000, 1001, 1010]):
            self.plugin._mark_controller_failure(self.controller, 'Unavailable', 'timeout')
            self.plugin._mark_controller_failure(self.controller, 'Unavailable', 'timeout')
        errors = [msg for level, msg in self.plugin.logger.messages if level == logging.ERROR]
        self.assertEqual(len(errors), 1)

    def test_infrastructure_dynamic_states_use_device_collection(self):
        switch = FakeDevice(463468270, 'unifiDevice')
        self.plugin.unifi_devices[switch.id] = [
            {'key': 'uptime', 'value': 1234},
            {'key': 'adopted', 'value': True},
        ]

        states = self.plugin.getDeviceStateList(switch)

        keys = [state['key'] for state in states]
        self.assertIn('controllerAvailable', keys)
        self.assertIn('uptime', keys)
        self.assertIn('adopted', keys)

    def test_client_configuration_validation_uses_selected_client(self):
        self.plugin.unifi_controllers[1]['sites'] = {
            'default': {'actives': {
                'aa:bb:cc:dd:ee:ff': {'name': 'Rick Phone', 'mac': 'aa:bb:cc:dd:ee:ff'}
            }}
        }
        values = {
            'unifi_controller': '1', 'unifi_site': 'default',
            'address': 'aa:bb:cc:dd:ee:ff',
        }

        valid, result = self.plugin.validateDeviceConfigUi(
            values, 'unifiWirelessClient', 2)

        self.assertTrue(valid)
        self.assertEqual(result['UniFiName'], 'Rick Phone')

    def test_access_point_configuration_validation_uses_selected_device(self):
        self.plugin.unifi_controllers[1]['sites'] = {
            'default': {'devices': {
                'aa:bb:cc:dd:ee:ff': {
                    'name': 'Pool AP', 'mac': 'aa:bb:cc:dd:ee:ff',
                    'version': '7.1.0',
                }
            }}
        }
        values = {
            'unifi_controller': '1', 'unifi_site': 'default',
            'address': 'aa:bb:cc:dd:ee:ff',
        }

        valid, result = self.plugin.validateDeviceConfigUi(
            values, 'unifiAccessPoint', 3)

        self.assertTrue(valid)
        self.assertEqual(result['UniFiName'], 'Pool AP')
        self.assertEqual(result['Version'], '7.1.0')


if __name__ == '__main__':
    unittest.main()
