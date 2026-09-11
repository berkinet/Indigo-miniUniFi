# miniUniFi review and outage-analysis handoff

## Reviewed baseline

- Upstream: <https://github.com/FlyingDiver/Indigo-miniUniFi>
- Branch/commit: `main` at `09d2c0903b562d0fe14f23071654d70718d35aed`
- Tag/plugin version: `v2026.0.1` / `2026.0.1`
- Main code: `miniUniFi.indigoPlugin/Contents/Server Plugin/plugin.py` (~885 lines)
- Declared dependency: `unifi-official-api` (apparently unused)
- Live Indigo installation reviewed: version `2022.1.3`, Server API `3.1`
- Installed at `/Library/Application Support/Perceptive Automation/Indigo 2025.2/Plugins/miniUniFi.indigoPlugin`
- No implementation changes were made during the review.

The plugin is compact and intentionally narrow: controller health, selected client
presence, selected equipment status, and a few actions. It is a good fork candidate,
but should not yet be trusted for important presence decisions during controller
outages.

## Highest-priority findings

1. **Stale online presence during controller outages.** Failed controller polls do
   not invalidate cached `sites` data. Dependent clients can therefore remain
   reported online from an old snapshot.
2. **Infrastructure dynamic states are broken.** `getDeviceStateList()` recognizes
   `self.unifi_devices`, but `extract_device_states()` always reads
   `self.unifi_clients[device.id]`.
3. **Configuration validation uses undefined variables.** It assigns
   `_client_data`/`_device_data`, then reads `client_data`/`device_data`. AP validation
   is also omitted.
4. **AP shutdown leaves stale runtime data.** Startup handles `unifiDevice` and
   `unifiAccessPoint`; shutdown removes only `unifiDevice`.
5. **Falsy API values disappear.** `dict_to_states()` keeps only truthy scalars, so
   `False`, `0`, `0.0`, and empty strings are omitted.
6. **Dynamic state keys and schemas are unsafe.** Keys are incompletely sanitized,
   empty keys can fail, collisions are not handled, and arbitrary API responses
   become unstable Indigo state definitions.
7. **Detailed logging leaks credentials.** Cookies, login headers, and raw login
   responses can expose `TOKEN`, `unifises`, CSRF tokens, and network/user data.
8. **TLS verification defaults off and is read from the wrong device for commands.**
   `command_unifi_controller()` reads `ssl_verify` from the target device instead of
   the controller device.
9. **Packaging is broken.** Release workflows still reference
   `UKTrains.indigoPlugin`; there is no test CI. The declared
   `unifi-official-api` dependency is not imported by the implementation.

## Overnight incident: 11 September 2026

Observed Indigo sequence:

```text
03:12:54  controller status HTTP 502
03:13:30  OS-check connection timeout
03:14:00  /api/login connection timeout (30 seconds)
03:14:06  OS-check connection timeout
03:14:36  /api/login connection timeout (30 seconds)
03:14:38+ connection refused on port 443
03:18:41+ response.json() failure at plugin.py line 290
```

The JSON failure repeated around 03:18:41, 03:19:12, 03:19:42, and 03:20:13.
It escaped `updateUniFiController()`, terminated `runConcurrentThread()`, and Indigo
restarted the thread ten seconds later.

### Diagnosis

The initial outage was real and external to miniUniFi: a 502 suggests a reachable
front-end with a failing backend, followed by an unresponsive controller and then a
reachable host with nothing listening on 443. This fits a reboot, service restart,
update, or crash. During recovery, the controller apparently returned HTTP success
with empty, HTML, or otherwise non-JSON content.

The plugin amplified the incident in four ways:

- A failed `/` OS probe returns `False`, conflating “confirmed standard controller”
  with “controller unreachable.” The code then incorrectly tries `/api/login`.
- JSON decoding assumes HTTP 200 implies valid JSON with a correctly typed `data`
  member. A malformed recovery response kills the polling thread.
- Cached controller snapshots retain authority, allowing stale online presence.
- Repeated failures flood logs; there is no deduplicated outage lifecycle or single
  recovery message.

The installed `2022.1.3` has a 30-second login timeout. Upstream `2026.0.1` reduces
this to five seconds and catches more request exceptions, improving responsiveness
but not resolving controller-type fallback, JSON validation, stale presence,
deduplication, recovery reporting, secret logging, or the functional bugs above.

## Required outage semantics

Model controller polling explicitly as `healthy`, `degraded`, `unavailable`, or
`recovering`. Distinguish an authoritative current snapshot from authentication
failure, HTTP/proxy failure, timeout/refusal, malformed response, and partial site
failure.

- Log the first outage concisely and deduplicate repeats.
- Optionally log reminders only after a configured interval.
- Continue retrying and emit one recovery message with outage duration.
- Preserve the last successful observation time and optionally expose snapshot age.
- Preserve a successfully detected controller type across outages; do not guess on
  failed detection. Allow an explicit override if useful.
- Mark dependent presence as unknown/controller-unavailable (or retain the last
  value with an explicit stale marker). Never present stale data as freshly online,
  and never claim offline merely because no authoritative list could be obtained.

## Test fixture for the recorded failure

Mock this exact response sequence:

```text
healthy JSON
-> HTTP 502
-> connect timeout
-> connection refused
-> HTTP 200 with empty body
-> HTTP 200 with HTML body
-> valid JSON recovery
```

Assert that the polling thread survives; controller type does not change; the wrong
login endpoint is not called; the initial outage is logged once; reminders are
interval-controlled; one recovery is logged; controller availability is exposed;
dependent clients are not freshly online; snapshot age is tracked; secrets never
appear in logs; recovery restores authoritative client states; one site's partial
failure does not corrupt others; and malformed JSON produces a concise diagnostic,
not a traceback.

Also test dynamic states for client/device/AP, falsy and `None` values, state-key
sanitization/collisions, validation, AP lifecycle, TLS propagation, action-port
validation, preference parsing, controller deletion with dependents, and repeated
reconfiguration while polling.

## Implementation order

### Phase 1: reliability and security

1. Add a request/JSON helper validating status, content type/body, JSON shape, and
   required keys.
2. Cache controller type after successful detection and stop the poll when type is
   unknown rather than guessing.
3. Track snapshot freshness/authority and remove stale-online semantics.
4. Contain all exceptions within the polling loop.
5. Deduplicate outage logs and report recovery once.
6. Remove cookies, tokens, login headers, raw login responses, and full payloads from
   logs.
7. Read TLS configuration from the controller device with correct Boolean parsing.
8. Implement the recorded outage-sequence tests.

### Phase 2: functional defects

Fix validation variable names; include AP validation and shutdown; select the correct
dynamic-state source; preserve falsy scalars; use defensive dictionary removal; and
validate controller, site, MAC, port, and action inputs.

### Phase 3: maintainability

Choose a curated stable state schema if practical. Split HTTP/controller behavior,
state mapping, Indigo integration, and configuration helpers. Add a fake-Indigo and
fake-session test harness, plus concise presence-limitations documentation.

### Phase 4: packaging

Replace copied UKTrains workflow paths, add test/XML/plist/compile CI, remove or
justify and pin `unifi-official-api`, add changelog/contribution/security docs, define
supported Indigo/UniFi versions, and test the intended installation path.

## Design questions to resolve

- Is `192.168.5.5` a UDM/UDM Pro, CloudKey, standalone Network controller, or other
  UniFi OS appliance, and was there a scheduled event around 03:12?
- During controller unavailability, should clients become `unknown`, retain their
  last state plus a stale marker, or expose availability separately?
- Are token/API-key auth and 2FA compatibility required?
- Which dynamic UniFi states and restart/PoE actions are actually used?
- Should controller type be automatic, explicit, or both?
- What repeat-error reminder interval is appropriate?
- Is verified TLS practical with the installed controller certificate?

## Fork scope recommendation

Keep the plugin minimal. Prioritize trustworthy presence semantics, graceful
outages, secret-safe logging, deterministic controller detection, and regression
tests. Avoid absorbing a comprehensive UniFi management feature set unless that is
an explicit future goal.
