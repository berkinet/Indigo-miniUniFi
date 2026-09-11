# Indigo-miniUniFi
Minimalistic UniFi plugin for Indigo

This maintained fork uses four-part release numbers such as `2026.0.1.1`: the first
three parts identify the FlyingDiver upstream version and the fourth identifies the
fork revision. See [VERSIONING.md](VERSIONING.md).

Allows for monitoring the on/off-line state of network clients on a UniFi based system.  Primary use is presence detection using mobile devices.

| Requirement            |                     |
|------------------------|---------------------|
| Minimum Indigo Version | 2022.1              |
| Python Library (API)   | Unofficial          |
| Requires Local Network | Yes                 |
| Requires Internet      | No                  |
| Hardware Interface     | None                |

1. Create a "UniFi Controller" device, configured with host and login information for your UniFi controller.  This can be a software controller, a cloud key, or a UniFi Dream Machine (or Pro).
2. Create a "UniFi Client" device for network clients you want to monitor, specifying the controller, site managed by that controller, and device (by name).
3. Create a "UniFi Device" device if you want o monitor status of UniFi equipment (APs, switches, gateways, etc).
3. Create triggers based on the on/off status of the client device.  The Wireless Client devices also have an "offline_seconds" state that can be used for delayed triggering.

Does not work with controllers that have 2FA enabled.

## Controller outages and presence freshness

miniUniFi retains a client's last known online/offline value when its controller is
temporarily unavailable. The retained value is explicitly marked as stale rather
than being presented as a fresh observation. Controller and dependent devices expose:

- `consoleAvailable`: whether the CloudKey/UniFi OS console responded
- `networkAppAvailable`: whether Network produced a complete authoritative snapshot
- `controllerAvailable`: legacy alias for `networkAppAvailable`
- `dataStale`: whether the displayed data comes from the last successful snapshot
- `lastSuccessfulPoll`: local timestamp of the last authoritative snapshot

Repeated identical outage errors are suppressed for 15 minutes by default, and one
recovery message reports the outage duration when polling succeeds again.

The plugin uses the `requests` library supplied by Indigo and has no separately
installed Python package requirements.
