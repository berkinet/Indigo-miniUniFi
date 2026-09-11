#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################

import indigo   # noqa
import time
import requests # noqa
import logging
import json

from datetime import datetime

requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)


class ControllerRequestError(Exception):
    """A predictable controller polling failure safe to show in Indigo logs."""


# Indigo really doesn't like dicts with keys that start with a number or symbol...

def safeKey(key):
    if not key[0].isalpha():
        return f'sk{key.strip()}'
    else:
        return key.strip()

# functions for converting lists and dicts into Indigo states

def dict_to_states(prefix, the_dict, states_list):
    for key in the_dict:
        if isinstance(the_dict[key], list):
            list_to_states(f"{prefix}{key}_", the_dict[key], states_list)
        elif isinstance(the_dict[key], dict):
            dict_to_states(f"{prefix}{key}_", the_dict[key], states_list)
        elif the_dict[key]:
            states_list.append({'key': safeKey(f"{prefix}{key.strip()}"), 'value': the_dict[key]})

def list_to_states(prefix, the_list, states_list):
    for i in range(len(the_list)):
        if isinstance(the_list[i], list):
            list_to_states(f"{prefix}{i}_", the_list[i], states_list)
        elif isinstance(the_list[i], dict):
            dict_to_states(f"{prefix}{i}_", the_list[i], states_list)
        else:
            states_list.append({'key': safeKey(f"{prefix}{i}"), 'value': the_list[i]})


UniFiTypes = {
    'uap': 'UniFi Access Point',
    'udm': 'UniFi Dream Machine',
    'ugw': 'UniFi Gateway',
    'usw': 'UniFi Switch',
}

def nameFromClient(data):
    if name := data.get('name'):
        return name
    if name := data.get('hostname'):
        return name
    return f"{data.get('ip')} ({data.get('mac')})"

def nameFromDevice(data):
    return data.get('name', f"{data.get('model')} @ {data.get('ip')}")

################################################################################
class Plugin(indigo.PluginBase):

    ########################################
    #
    # Main Plugin methods
    #
    ########################################
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)

        pfmt = logging.Formatter('%(asctime)s.%(msecs)03d\t[%(levelname)8s] %(name)20s.%(funcName)-25s%(msg)s', datefmt='%Y-%m-%d %H:%M:%S')
        self.plugin_file_handler.setFormatter(pfmt)
        self.logLevel = int(pluginPrefs.get("logLevel", logging.INFO))
        self.indigo_log_handler.setLevel(self.logLevel)
        self.logger.setLevel(self.logLevel)

        self.updateFrequency = float(pluginPrefs.get('updateFrequency', "60"))
        if self.updateFrequency < 30.0:
            self.updateFrequency = 30.0
        self.logger.debug(f"updateFrequency = {self.updateFrequency}")
        self.next_update = time.time()

        self.unifi_controllers = {}  # dict of controller info dicts keyed by DeviceID.
        self.unifi_clients = {}  # dict of device state definitions keyed by DeviceID.
        self.unifi_devices = {}  # dict of device state definitions keyed by DeviceID.
        self.update_needed = False
        self.last_controller = None
        self.last_site = 'default'
        self.outageReminderInterval = float(pluginPrefs.get('outageReminderInterval', 900))

    def startup(self):
        self.logger.info("Starting miniUniFi")

    def runConcurrentThread(self):
        self.logger.debug("Starting runConcurrentThread")
        try:
            while True:
                if (time.time() > self.next_update) or self.update_needed:
                    self.next_update = time.time() + self.updateFrequency
                    self.update_needed = False

                    # update from UniFi Controllers

                    for controllerID in list(self.unifi_controllers):
                        try:
                            self.updateUniFiController(indigo.devices[controllerID])
                        except Exception:
                            # A controller or malformed response must never terminate
                            # Indigo's concurrent plugin thread.
                            self.logger.exception("Unexpected error updating UniFi controller")

                    # now update all the client devices  

                    for clientID in list(self.unifi_clients):
                        self.updateUniFiClient(indigo.devices[clientID])

                    # now update all the UniFi devices  

                    for deviceID in list(self.unifi_devices):
                        try:
                            unifiDevice = indigo.devices[deviceID]
                        except Exception as err:
                            self.logger.error(f"Error retrieving Device ID {deviceID}: {err}")
                        else:
                            self.updateUniFiDevice(unifiDevice)

                self.sleep(1.0)

        except self.StopThread:
            pass

    def deviceStartComm(self, device):

        self.logger.info(f"{device.name}: Starting Device")

        if device.pluginProps.get('sql_logging_exclude', False):
            props = device.sharedProps
            props["sqlLoggerIgnoreStates"] = "*"
            device.replaceSharedPropsOnServer(props)

        if device.deviceTypeId == 'unifiController':
            self.unifi_controllers[device.id] = {
                'name': device.name,
                'controller_type': None,
                'snapshot_authoritative': False,
                'last_success': None,
                'outage_since': None,
                'last_failure_log': None,
                'last_failure_signature': None,
            }
            self.update_needed = True
            if not self.last_controller:
                self.last_controller = str(device.id)

        elif device.deviceTypeId in ['unifiClient', 'unifiWirelessClient']:
            self.unifi_clients[device.id] = None  # discovered states for the device
            self.update_needed = True

        elif device.deviceTypeId in ['unifiDevice', 'unifiAccessPoint']:
            self.unifi_devices[device.id] = None  # discovered states for the device
            self.update_needed = True

        device.stateListOrDisplayStateIdChanged()

    def deviceStopComm(self, device):

        self.logger.info(f"{device.name}: Stopping Device")

        if device.deviceTypeId == 'unifiController':
            self.unifi_controllers.pop(device.id, None)

        elif device.deviceTypeId in ['unifiClient', 'unifiWirelessClient']:
            self.unifi_clients.pop(device.id, None)

        elif device.deviceTypeId in ['unifiDevice', 'unifiAccessPoint']:
            self.unifi_devices.pop(device.id, None)

    ########################################
    #
    # Data Retrieval methods
    #
    ########################################

    def is_unifi_os(self, device):
        """
        check for Unifi OS controller e.g. UDM, UDM Pro.
        HEAD request will return 200 if Unifi OS,
        if this is a Standard controller, we will get 302 (redirect) to /manage
        """
        controller = self.unifi_controllers.setdefault(device.id, {'name': device.name})
        if controller.get('controller_type') == 'os':
            return True
        if controller.get('controller_type') == 'standard':
            return False

        ssl_verify = device.pluginProps.get('ssl_verify', False)
        if ssl_verify is False:
            from requests.packages.urllib3.exceptions import InsecureRequestWarning
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

        try:
            r = requests.head(f'https://{device.pluginProps["address"]}:{device.pluginProps["port"]}', verify=ssl_verify, timeout=5.0)
        except requests.exceptions.RequestException:
            raise
        except Exception as err:
            raise ControllerRequestError(f"controller type check failed: {err}") from err

        if r.status_code == 200:
            self.logger.debug(f'{device.name}: Unifi OS controller detected')
            controller['controller_type'] = 'os'
            return True
        if r.status_code == 302:
            self.logger.debug(f'{device.name}: Unifi Standard controller detected')
            controller['controller_type'] = 'standard'
            return False
        raise ControllerRequestError(
            f"controller type check returned HTTP {r.status_code}")

    def _update_health_states(self, device, available, stale, last_success=None):
        """Publish controller freshness without changing a client's last presence."""
        updates = [
            {'key': 'controllerAvailable', 'value': available},
            {'key': 'dataStale', 'value': stale},
        ]
        if last_success:
            updates.append({'key': 'lastSuccessfulPoll', 'value': last_success})
        device.updateStatesOnServer(updates)

    def _mark_controller_failure(self, device, status, detail):
        controller = self.unifi_controllers[device.id]
        now = time.time()
        if controller.get('outage_since') is None:
            controller['outage_since'] = now
        controller['snapshot_authoritative'] = False
        signature = f"{status}: {detail}"
        last_log = controller.get('last_failure_log')
        should_log = (
            signature != controller.get('last_failure_signature') or
            last_log is None or
            now - last_log >= self.outageReminderInterval
        )
        if should_log:
            self.logger.error(f"{device.name}: {signature}")
            controller['last_failure_log'] = now
            controller['last_failure_signature'] = signature
        device.updateStateOnServer(key='status', value=status)
        device.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)
        self._update_health_states(
            device, False, bool(controller.get('sites')), controller.get('last_success'))

    def _mark_controller_success(self, device, sites):
        controller = self.unifi_controllers[device.id]
        now = time.time()
        last_success = datetime.fromtimestamp(now).isoformat(timespec='seconds')
        outage_since = controller.get('outage_since')
        controller.update({
            'sites': sites,
            'snapshot_authoritative': True,
            'last_success': last_success,
            'outage_since': None,
            'last_failure_log': None,
            'last_failure_signature': None,
        })
        if outage_since is not None:
            self.logger.info(
                f"{device.name}: Controller recovered after {int(now - outage_since)} seconds")
        device.updateStateOnServer(key='status', value='Online')
        device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)
        self._update_health_states(device, True, False, last_success)

    def _controller_snapshot_is_authoritative(self, controller_id):
        controller = self.unifi_controllers.get(controller_id, {})
        return bool(controller.get('snapshot_authoritative'))

    def _mark_dependent_stale(self, device, controller_id):
        controller = self.unifi_controllers.get(controller_id, {})
        self._update_health_states(device, False, True, controller.get('last_success'))
        last_value = device.states.get('onOffState')
        if last_value is not None:
            label = 'Online' if last_value else 'Offline'
            device.updateStateOnServer(
                key='onOffState', value=last_value, uiValue=f'{label} (stale)')

    @staticmethod
    def _json_payload(response, operation):
        if response.status_code != requests.codes.ok:
            raise ControllerRequestError(f"{operation} returned HTTP {response.status_code}")
        content_type = response.headers.get('Content-Type', '').lower()
        if content_type and 'json' not in content_type:
            raise ControllerRequestError(f"{operation} returned non-JSON content")
        if not getattr(response, 'content', b'') and not getattr(response, 'text', ''):
            raise ControllerRequestError(f"{operation} returned an empty response")
        try:
            payload = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError) as err:
            raise ControllerRequestError(f"{operation} returned malformed JSON") from err
        if not isinstance(payload, dict):
            raise ControllerRequestError(f"{operation} returned an unexpected JSON shape")
        return payload

    @classmethod
    def _json_data(cls, response, operation):
        payload = cls._json_payload(response, operation)
        if not isinstance(payload.get('data'), list):
            raise ControllerRequestError(f"{operation} returned an unexpected JSON shape")
        return payload['data']

    def updateUniFiController(self, device):
        self.logger.debug(f"{device.name}: Updating controller")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        login_headers = {"Accept": "application/json", "Content-Type": "application/json", "referer": "/login"}
        base_url = f"https://{device.pluginProps['address']}:{device.pluginProps['port']}/"
        login_body = {"username": device.pluginProps['username'], "password": device.pluginProps['password'], 'strict': True}
        ssl_verify = device.pluginProps.get('ssl_verify', False)

        try:
            unifi_os = self.is_unifi_os(device)
            if unifi_os:
                login_url, status_url = "{}api/auth/login", "{}proxy/network/status"
                sites_url = "{}proxy/network/api/self/sites"
                active_url = "{}proxy/network/api/s/{}/stat/sta"
                device_url = "{}proxy/network/api/s/{}/stat/device"
            else:
                login_url, status_url = "{}api/login", "{}status"
                sites_url, active_url = "{}api/self/sites", "{}api/s/{}/stat/sta"
                device_url = "{}api/s/{}/stat/device"

            with requests.Session() as session:
                response = session.post(login_url.format(base_url), headers=login_headers,
                                        json=login_body, verify=ssl_verify, timeout=5.0)
                if response.status_code != requests.codes.ok:
                    raise ControllerRequestError(f"login returned HTTP {response.status_code}")

                cookies_dict = requests.utils.dict_from_cookiejar(session.cookies)
                if unifi_os:
                    cookies = {"TOKEN": cookies_dict.get('TOKEN')}
                else:
                    cookies = {"unifises": cookies_dict.get('unifises'),
                               "csrf_token": cookies_dict.get('csrf_token')}

                response = session.get(status_url.format(base_url), headers=headers,
                                       cookies=cookies, verify=ssl_verify, timeout=10.0)
                status_payload = self._json_payload(response, 'controller status')
                version = status_payload.get('meta', {}).get('server_version')
                if version:
                    new_props = device.pluginProps
                    new_props['version'] = version
                    device.replacePluginPropsOnServer(new_props)

                response = session.get(sites_url.format(base_url), headers=headers,
                                       cookies=cookies, verify=ssl_verify, timeout=5.0)
                site_list = self._json_data(response, 'site list')
                sites = {}
                for site in site_list:
                    if not isinstance(site, dict) or not site.get('name'):
                        raise ControllerRequestError('site list contains an invalid site')
                    site_name = site['name']
                    sites[site_name] = {'description': site.get('desc', site_name)}

                    response = session.get(active_url.format(base_url, site_name),
                                           headers=headers, cookies=cookies,
                                           verify=ssl_verify, timeout=5.0)
                    clients = self._json_data(response, f'active clients for {site_name}')
                    sites[site_name]['actives'] = {
                        client.get('mac'): client for client in clients
                        if isinstance(client, dict) and client.get('mac')
                    }

                    response = session.get(device_url.format(base_url, site_name),
                                           headers=headers, cookies=cookies,
                                           verify=ssl_verify, timeout=5.0)
                    devices = self._json_data(response, f'devices for {site_name}')
                    sites[site_name]['devices'] = {
                        item.get('mac'): item for item in devices
                        if isinstance(item, dict) and item.get('mac')
                    }

            self._mark_controller_success(device, sites)
        except requests.exceptions.Timeout as err:
            self._mark_controller_failure(device, 'Unavailable', f'timeout: {err}')
        except requests.exceptions.RequestException as err:
            self._mark_controller_failure(device, 'Unavailable', f'connection error: {err}')
        except ControllerRequestError as err:
            self._mark_controller_failure(device, 'Degraded', str(err))

    def updateUniFiClient(self, device):

        self.logger.threaddebug(f"{device.name}: Updating UniFi Client: {device.address}")

        controller = int(device.pluginProps['unifi_controller'])
        site = device.pluginProps['unifi_site']
        uClient = device.address
        offline = False
        client_data = {}

        if not self._controller_snapshot_is_authoritative(controller):
            self._mark_dependent_stale(device, controller)
            return

        self._update_health_states(
            device, True, False, self.unifi_controllers[controller].get('last_success'))

        try:
            client_data = self.unifi_controllers[controller]['sites'][site]['actives'][uClient]
        except (Exception,):
            self.logger.debug(f"{device.name}: client_data not found")
            offline = True

        if not offline:
            self.logger.threaddebug(f"client_data =\n{json.dumps(client_data, indent=4, sort_keys=True)}")

            states_list = []
            if client_data:
                dict_to_states("", client_data, states_list)

            self.unifi_clients[device.id] = states_list
            device.stateListOrDisplayStateIdChanged()

            try:
                device.updateStatesOnServer(states_list)
            except TypeError as _err:
                self.logger.error(f"{device.name}: invalid state type in states_list: {states_list}")

        if device.deviceTypeId == "unifiClient":
            if offline:
                self.logger.debug(u"{}: Offline".format(device.name))
                device.updateStateOnServer(key="onOffState", value=False, uiValue=u"Offline")
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)

            else:
                self.logger.debug(u"{}: Online".format(device.name))
                device.updateStateOnServer(key="onOffState", value=True, uiValue=u"Online")
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif device.deviceTypeId == "unifiWirelessClient":
            essid = client_data.get('essid', None)
            if offline or not essid:
                device.updateStateOnServer(key="onOffState", value=False, uiValue=u"Offline")
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)
                last_seen = device.states.get('last_seen', None)
                if last_seen:
                    offline_seconds = int((datetime.now() - datetime.fromtimestamp(last_seen)).total_seconds())
                    minutes, seconds = divmod(offline_seconds, 60)
                    hours, minutes = divmod(minutes, 60)
                    status = f"Offline {int(hours):02}:{int(minutes):02}:{int(seconds):02}"
                else:
                    status = "Offline"
                    offline_seconds = 0
                device.updateStateOnServer(key="onOffState", value=False, uiValue=status)
                device.updateStateOnServer(key='offline_seconds', value=offline_seconds)
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)
                self.logger.debug(f"{device.name}: {status} for {offline_seconds} seconds")

            else:
                self.logger.debug(f"{device.name}: Online @ {essid}")
                device.updateStateOnServer(key="onOffState", value=True, uiValue=u"Online @ {}".format(essid))
                device.updateStateOnServer(key='offline_seconds', value=0)
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        else:
            self.logger.debug(f"{device.name}: Unknown Device Type: {device.deviceTypeId}")

    def updateUniFiDevice(self, device):

        self.logger.threaddebug(f"{device.name}: Updating UniFi Device: {device.address}")

        controller = int(device.pluginProps['unifi_controller'])
        site = device.pluginProps['unifi_site']
        uDevice = device.address
        offline = False
        device_data = {}

        if not self._controller_snapshot_is_authoritative(controller):
            self._mark_dependent_stale(device, controller)
            return

        self._update_health_states(
            device, True, False, self.unifi_controllers[controller].get('last_success'))

        try:
            device_data = self.unifi_controllers[controller]['sites'][site]['devices'][uDevice]
        except (Exception,):
            self.logger.debug(f"{device.name}: device_data not found")
            offline = True

        if not offline:
            self.logger.threaddebug(f"device_data =\n{json.dumps(device_data, indent=4, sort_keys=True)}")

            newProps = device.pluginProps
            newProps['version'] = device_data['version']
            device.replacePluginPropsOnServer(newProps)

            device.model = UniFiTypes.get(device_data['type'], 'Unknown')
            device.subModel = device_data['model']
            device.replaceOnServer()

            states_list = []
            if device_data:
                dict_to_states(u"", device_data, states_list)

            self.unifi_devices[device.id] = states_list
            device.stateListOrDisplayStateIdChanged()
            try:
                device.updateStatesOnServer(states_list)
            except TypeError as _err:
                self.logger.error(f"{device.name}: invalid state type in states_list: {states_list}")

        if device.deviceTypeId == "unifiDevice":

            uptime = device_data.get('_uptime', None)
            if offline or not uptime:
                self.logger.debug(f"{device.name}: Offline")
                device.updateStateOnServer(key="onOffState", value=False, uiValue=u"Offline")
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)

            else:
                minutes, seconds = divmod(uptime, 60)
                hours, minutes = divmod(minutes, 60)
                days, hours = divmod(hours, 24)
                status = f"Uptime: {int(days):02}:{int(hours):02}:{int(minutes):02}:{int(seconds):02}"
                self.logger.debug(f"{device.name}: Online")
                device.updateStateOnServer(key="onOffState", value=True, uiValue=status)
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif device.deviceTypeId == "unifiAccessPoint":
            status_display = device.pluginProps.get('status_display', 'uptime')

            uptime = device_data.get('_uptime', None)
            if offline or not uptime:
                self.logger.debug(f"{device.name}: Offline")
                device.updateStateOnServer(key="onOffState", value=False, uiValue=u"Offline")
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)

            elif status_display == 'uptime':
                minutes, seconds = divmod(uptime, 60)
                hours, minutes = divmod(minutes, 60)
                days, hours = divmod(hours, 24)
                status = f"Uptime: {int(days):02}:{int(hours):02}:{int(minutes):02}:{int(seconds):02}"
                self.logger.debug(u"{}: Online".format(device.name))
                device.updateStateOnServer(key="onOffState", value=True, uiValue=status)
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

            elif status_display == 'wifi':
                status = "Wifi: "
                first = True
                for radio in device_data["radio_table_stats"]:
                    clients = radio.get("user-num_sta", 0)
                    channel = radio.get("channel", 0)
                    if not first:
                        status = status + u" / "
                    first = False
                    status = status + f"{channel} ({clients})"
                device.updateStateOnServer(key="onOffState", value=True, uiValue=status)
                device.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

            else:
                self.logger.error(f"{device.name}: invalid status display type: {status_display}")

        else:
            self.logger.error(f"{device.name}: deviceTypeId: {device.deviceTypeId}")

    ################################################################################
    #
    # callback for state list changes, called from stateListOrDisplayStateIdChanged()
    #
    ################################################################################

    def getDeviceStateList(self, device):
        state_list = indigo.PluginBase.getDeviceStateList(self, device)
        self.logger.threaddebug(f"{device.name}: getDeviceStateList, base state_list = {state_list}")

        if device.id in self.unifi_clients and self.unifi_clients[device.id]:
            self.extract_device_states(device, state_list)

        elif device.id in self.unifi_devices and self.unifi_devices[device.id]:
            self.extract_device_states(device, state_list)

        self.logger.threaddebug(f"{device.name}: getDeviceStateList, final state_list = {state_list}")
        return state_list

    def extract_device_states(self, device, state_list):
        for item in self.unifi_clients[device.id]:
            key = item['key']
            value = item['value']
            if isinstance(value, bool):
                dynamic_state = self.getDeviceStateDictForBoolTrueFalseType(str(key), str(key), str(key))
                self.logger.threaddebug(f"{device.name}: getDeviceStateList, adding Bool state {key}, value {value}")
            elif isinstance(value, (float, int)):
                dynamic_state = self.getDeviceStateDictForNumberType(str(key), str(key), str(key))
                self.logger.threaddebug(f"{device.name}: getDeviceStateList, adding Number state {key}, value {value}")
            elif isinstance(value, str):
                dynamic_state = self.getDeviceStateDictForStringType(str(key), str(key), str(key))
                self.logger.threaddebug(f"{device.name}: getDeviceStateList, adding String state {key}, value {value}")
            else:
                self.logger.debug(f"{device.name}: getDeviceStateList, unknown type for key = {key}, value {value}")
                continue

            state_list.append(dynamic_state)

    ########################################
    #
    # device UI methods
    #
    ########################################

    def get_controller_list(self, _filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.debug(f"get_controller_list: typeId = {typeId}, targetId = {targetId}, valuesDict = {valuesDict}")
        controller_list = [
            (devID, indigo.devices[devID].name)
            for devID in self.unifi_controllers
        ]
        controller_list.sort(key=lambda tup: tup[1])
        self.logger.threaddebug(f"get_controller_list: controller_list = {controller_list}")
        return controller_list

    def get_site_list(self, _filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.debug(f"get_site_list: typeId = {typeId}, targetId = {targetId}, valuesDict = {valuesDict}")

        try:
            controller = self.unifi_controllers[int(valuesDict["unifi_controller"])]
        except (Exception,):
            self.logger.debug(u"get_site_list: controller not found, returning empty list")
            return []

        self.logger.debug(f"get_site_list: using controller {controller['name']}")
        self.last_controller = valuesDict["unifi_controller"]

        site_list = [
            (name, controller['sites'][name]['description'])
            for name in controller['sites']
        ]

        site_list.sort(key=lambda tup: tup[1])
        self.logger.threaddebug(f"get_site_list: site_list = {site_list}")
        return site_list

    def get_client_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.debug(f"get_client_list: typeId = {typeId}, targetId = {targetId}, filter = {filter}, valuesDict = {valuesDict}")

        try:
            site = self.unifi_controllers[int(valuesDict["unifi_controller"])]['sites'][valuesDict["unifi_site"]]
        except (Exception,):
            self.logger.debug("get_client_list: no site specified, returning empty list")
            return []

        self.logger.debug(f"get_client_list: using site {valuesDict['unifi_site']} ({site['description']})")
        self.last_site = valuesDict["unifi_site"]

        wired = (filter == "Wired")
        client_list = [
            (mac, nameFromClient(data) if nameFromClient(data) else mac)
            for mac, data in site['actives'].items() if data.get('is_wired', False) == wired
        ]
        client_list.sort(key=lambda tup: tup[1])

        if targetId:
            try:
                dev = indigo.devices[targetId]
                client_list.insert(0, (dev.pluginProps["address"], dev.pluginProps.get("UniFiName", dev.name)))
            except (Exception,):
                pass

        self.logger.threaddebug(f"get_client_list: client_list for {typeId} ({filter}) = {client_list}")
        return client_list

    def get_device_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.debug(f"get_device_list: typeId = {typeId}, targetId = {targetId}, filter = {filter}, valuesDict = {valuesDict}")

        try:
            site = self.unifi_controllers[int(valuesDict["unifi_controller"])]['sites'][valuesDict["unifi_site"]]
        except (Exception,):
            self.logger.debug("get_device_list: no site specified, returning empty list")
            return []

        self.logger.debug(f"get_device_list: using site {valuesDict['unifi_site']} ({site['description']})")
        self.last_site = valuesDict["unifi_site"]

        device_list = [
            (mac, nameFromDevice(data))
            for mac, data in site['devices'].items()
        ]
        device_list.sort(key=lambda tup: tup[1])

        if targetId:
            try:
                dev = indigo.devices[targetId]
            except (Exception,):
                pass
            else:
                name = dev.pluginProps.get("UniFiName", None)
                if name and len(name):
                    device_list.insert(0, (dev.pluginProps["address"], name))

        self.logger.threaddebug(f"get_device_list: device_list for {typeId} ({filter}) = {device_list}")
        return device_list

        # doesn't do anything, just needed to force other menus to dynamically refresh

    def menuChanged(self, valuesDict=None, typeId=None, devId=None):    # noqa
        return valuesDict

    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    # This routine returns the UI values for the device configuration screen prior to it
    # being shown to the user; it is sometimes used to setup default values at runtime
    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    def getDeviceConfigUiValues(self, pluginProps, typeId, devId):
        valuesDict = indigo.Dict(pluginProps)
        errorsDict = indigo.Dict()
        self.logger.debug(f"getDeviceConfigUiValues: devId = {devId}, typeId = {typeId}, pluginProps =\n{pluginProps}")

        if typeId == 'unifiController':
            pass

        else:
            valuesDict["unifi_controller"] = self.last_controller
            valuesDict["unifi_site"] = self.last_site

        return valuesDict, errorsDict

    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    # This routine will validate the device configuration dialog when the user attempts
    # to save the data
    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        self.logger.debug(f"validateDeviceConfigUi: devId = {devId}, typeId = {typeId}, valuesDict =\n{valuesDict}")
        if typeId in ['unifiClient', 'unifiWirelessClient']:
            controller = int(valuesDict['unifi_controller'])
            site = valuesDict['unifi_site']
            uClient = valuesDict['address']
            _client_data = {}

            try:
                _client_data = self.unifi_controllers[controller]['sites'][site]['actives'][uClient]
            except (Exception,):
                self.logger.debug("validateDeviceConfigUi: client_data not found")
            else:
                valuesDict['UniFiName'] = nameFromClient(client_data)

        elif typeId == 'unifiDevice':
            controller = int(valuesDict['unifi_controller'])
            site = valuesDict['unifi_site']
            uClient = valuesDict['address']
            _device_data = {}

            try:
                _device_data = self.unifi_controllers[controller]['sites'][site]['devices'][uClient]
            except (Exception,):
                pass
            else:
                valuesDict['UniFiName'] = nameFromDevice(device_data)
                valuesDict['Version'] = device_data.get('version', None)
        return True, valuesDict

    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    # This routine will be called whenever the user has closed the device config dialog
    # either by save or cancel.  This routine cannot change anything (read only).
    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    def closedDeviceConfigUi(self, valuesDict, userCancelled, typeId, devId):
        self.logger.debug(f"closedDeviceConfigUi: devId = {devId}, typeId = {typeId}, userCancelled = {userCancelled}, valuesDict =\n{valuesDict}")

    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    # This routine returns the UI values for the configuration dialog; the default is to
    # simply return the self.pluginPrefs dictionary. It can be used to dynamically set
    # defaults at run time
    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    def getPrefsConfigUiValues(self):
        self.logger.debug("getPrefsConfigUiValues:")
        return super(Plugin, self).getPrefsConfigUiValues()

    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    # This routine is called in order to validate the inputs within the plugin config
    # dialog box. Return is a (True|False = isOk, valuesDict = values to save, errorMsgDict
    # = errors to display (if necessary))
    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    def validatePrefsConfigUi(self, valuesDict):
        self.logger.debug(f"validatePrefsConfigUi: valuesDict = {valuesDict}")

        # possible to do real validation and return an error if it fails, such as:      
        # errorMsgDict = indigo.Dict()
        # errorMsgDict[u"requiredFieldChk"] = u"You must check this box to continue"
        # return (False, valuesDict, errorMsgDict)

        # if no errors, return True and the values as a tuple
        return True, valuesDict

    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    # This routine is called once the user has exited the preferences dialog
    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        self.logger.debug(f"closedPrefsConfigUi: userCancelled = {userCancelled}, valuesDict= {valuesDict}")
        if not userCancelled:
            self.logLevel = int(valuesDict.get("logLevel", logging.INFO))
            self.indigo_log_handler.setLevel(self.logLevel)
            self.logger.setLevel(self.logLevel)

            try:
                self.updateFrequency = float(valuesDict["updateFrequency"])
                if self.updateFrequency < 30.0:
                    self.updateFrequency = 30.0
            except (Exception,):
                self.updateFrequency = 60.0

    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    # Plugin Menu routines
    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    def menuDumpControllers(self):
        self.logger.debug("menuDumpControllers")

        for controllerID in self.unifi_controllers:
            self.logger.info(json.dumps(self.unifi_controllers[controllerID]['sites'], sort_keys=True, indent=4, separators=(',', ': ')))

        return True

    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-
    # Plugin Action routines
    # -=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

    def restart_device_action(self, _action, device):
        self.logger.debug(f"{device.name}: restart_device_action")
        params = {'cmd': "restart", 'mac':device.address}
        self.command_unifi_controller(device, params)

    def power_cycle_port_action(self, plugin_action, device, _callerWaitingForResult):
        self.logger.debug(f"{device.name}: power_cycle_port_action, props = {plugin_action.props}")
        params = {'cmd': "power-cycle", 'mac':device.address, 'port_idx': int(plugin_action.props['port'])}
        self.command_unifi_controller(device, params)

    def command_unifi_controller(self, device, params):

        self.logger.debug(f"{device.name}: Sending command to controller with params: {params}")

        unifi_controller = indigo.devices[int(device.pluginProps['unifi_controller'])]
        login_headers = {"Accept": "application/json", "Content-Type": "application/json", "referer": "/login"}
        login_params = {"username": unifi_controller.pluginProps['username'], "password": unifi_controller.pluginProps['password']}
        headers = {"Accept": "*/*", "Content-Type": "application/x-www-form-urlencoded"}
        ssl_verify = unifi_controller.pluginProps.get('ssl_verify', False)
        site = device.pluginProps['unifi_site']

        with requests.Session() as session:

            # set up URL templates based on controller type
            unifi_os = self.is_unifi_os(unifi_controller)
            if unifi_os:
                base_url = f"https://{unifi_controller.pluginProps['address']}"
                login_url  = f"{base_url}/api/auth/login"
                cmd_url    = "{}/proxy/network/api/s/{}/cmd/devmgr"
            else:
                base_url = f"https://{unifi_controller.pluginProps['address']}:{unifi_controller.pluginProps['port']}"
                login_url  = f"{base_url}/api/login"
                cmd_url    = "{}/api/s/{}/cmd/devmgr"

            try:
                response = session.post(login_url, headers=login_headers, json=login_params, verify=ssl_verify, timeout=5.0)
            except Exception as err:
                self.logger.error(f"UniFi Controller Login Connection Error: {err}")
                unifi_controller.updateStateOnServer(key='status', value="Connection Error")
                unifi_controller.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)
                return

            if response.status_code != requests.codes.ok:
                unifi_controller.updateStateOnServer(key='status', value="Login Error")
                unifi_controller.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)
                return

            unifi_controller.updateStateOnServer(key='status', value="Login OK")
            unifi_controller.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

            if 'X-CSRF-Token' in response.headers:
                csrf_token = response.headers['X-CSRF-Token']
                headers['X-CSRF-Token'] = csrf_token

            cookies_dict = requests.utils.dict_from_cookiejar(session.cookies)
            if unifi_os:
                cookies = {"TOKEN": cookies_dict.get('TOKEN')}
            else:
                cookies = {"unifises": cookies_dict.get('unifises'), "csrf_token": cookies_dict.get('csrf_token')}

            url = cmd_url.format(base_url, site)
            self.logger.threaddebug(f"{device.name}: Post cmd url: {url}")
            self.logger.threaddebug(f"{device.name}: Post cmd params: {params}")
            try:
                response = session.post(url, headers=headers, cookies=cookies, json=params, verify=ssl_verify, timeout=5.0)
            except Exception as err:
                self.logger.error(f"UniFi Controller Post Error: {err}")
                unifi_controller.updateStateOnServer(key='status', value="Post Error")
                unifi_controller.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)
                return

            if response.status_code != requests.codes.ok:
                self.logger.error(f"UniFi Controller Post Error: {response.status_code}")
                unifi_controller.updateStateOnServer(key='status', value="Post Error")
                unifi_controller.updateStateImageOnServer(indigo.kStateImageSel.SensorTripped)

            self.logger.threaddebug(
                f"{device.name}: Controller command completed with HTTP {response.status_code}")
