# 10s — architecture

What the integration is, how it is built, what runs when, and where every piece of state lives.

## 1. What it is

10s (domain `tens`, display name *10s*) is a Home Assistant integration that **meters** devices. It reads the energy
counters of the devices the user links (a heat pump, a boiler, an EV charger, a battery: 10s's four categories), keeps
the readings, and sends the energy used in each 15-minute slot to the 10s service. It never controls a device, and it
has no inverter, prices or forecasts.

It is two repositories:

| Repository | Role |
|---|---|
| `tens` (this one) | The **host shell**: everything that is Home Assistant wiring: entry setup, config flow, coordinator, entities, panel, services, repairs, diagnostics. |
| `tenskit` | The **shared core**, vendored as a git submodule at `custom_components/tens/_tenskit/`: the metering logic, the 10s client, the setup-flow steps. Host-agnostic: it never imports a host, and a host reaches it only through **seams**. |

## 2. Layers and ownership

**Host (`custom_components/tens/`)**

| File | Owns |
|---|---|
| `__init__.py` | Entry set-up and tear-down; the optional `tens:` block of `configuration.yaml` (one key, `environment: dev` or `production`), read once at start-up; the four `tens.*` services (admin-only); the static path and the one sidebar panel (`/tens`); the update listener that reloads the entry on every options write. One loaded coordinator per Home Assistant (`single_config_entry`). |
| `config_flow.py` | The root menu, the verify screen's gate and its Finish; composes the kit's flow steps. On first set-up the flow stays open after the login (the account is parked in a store of its own) and *Check and save* creates the entry. |
| `environment.py` | Which 10s the installation talks to and the one place its client is built, on Home Assistant's shared session, which verifies certificates. A stored account names the environment it was issued in and keeps it; a new login uses the one `tens: environment:` names in `configuration.yaml` (dev), else 10s production. Nothing of it is stored in the entry. |
| `coordinator.py` | The runtime's two stores, the meter reads (each quarter-hour mark and every 5 minutes), the contact with 10s (at set-up, then every 8 hours), the repair issues, the entry writer. |
| `sensor.py` | `10s Dashboard` (the panel's data) and one `Energy (15 min)` sensor per device. |
| `device_slots_view.py` | `/api/tens/device_slots`: the archive the panel fetches, per slot the energy and how 10s accounts it (`accounted`, `owed`, `outside`). |
| `diagnostics.py` | The diagnostics download, credentials redacted. |
| `www/tens-panel.js` | The panel. Host code, not shared; it renders served values, does no arithmetic and derives no accounting from a status. |

**Kit (`_tenskit/`)**: five packages in three layers. A package imports only from the layers below its own: never from a
package beside it, and never from the host.

| Layer | Package | Holds |
|---|---|---|
| top | `orchestration/` | `PartnerRuntime` (reconcile, upload, the actions), `SlotCollector` and `slot_log` (the 72-hour archive and its watermark), `registrations` (the registration mirror and its codec), the `Store` protocol. |
| top | `setup_flow/` | The step definitions for the 10s account, the metered devices and the verify screen, on a `FlowBase` the host subclasses. |
| middle | `inbound/` | The Home Assistant edge: which entities qualify as meters, reading a device's counters, reading the recorder's statistics, building the registration dump. |
| middle | `outbound/partners/` | The `PartnerClient` protocol and its errors, the real 10s client (`tens.py`), the measurement-block encoder (`measurements.py`). |
| bottom | `contract/` | Value types and constants: `MeteredDevice` and its per-kind meter profiles, `PartnerAccount`, `RegistrationState` (and `status_from_window`), the slot grid and `DeviceSlot`. No Home Assistant, no I/O. |

Each kit package declares the names it offers in its `__init__.py` (re-exports and `__all__`), its public header: code
outside a package imports from the package, never from its files. Inside a package, files import their siblings
directly. The kit's tests are white-box and may import any file. import-linter enforces the layers, the headers and the
no-host rule (`pyproject.toml`, `[tool.importlinter]`).

### The seams a host implements

`FlowBase` (`setup_flow/core.py`) is the contract. The host supplies: where to finish (`_async_finish`), the root menu,
the 10s client the login redeems through, how a freshly redeemed account is persisted, how credentials are rewritten
after a reauth, which devices are currently held, how to log out, which platforms never count as meters, and what 10s
accepts for the verify screen. Persistence is the `Store` protocol (`orchestration/ha_store.py`), implemented by the
host's two stores. Nothing in the kit knows it runs in `tens`.

## 3. What runs when

| Trigger | What happens |
|---|---|
| **Entry set-up** | The coordinator loads both stores; the platforms, the two meter-read timers and the panel are set up; then the first contact with 10s starts **in the background** as the entry's own task, eagerly, so this mark's reading is taken inside the set-up. The set-up never waits for 10s: one that hangs, fails or refuses the login does not delay it. The entry loads and samples, and a refused login opens Home Assistant's reauth prompt through the coordinator, as on every later refresh. |
| **Every quarter-hour mark** (`:00 :15 :30 :45`) **and every 5 minutes** | A timer reads the meters into the archive (`SlotCollector`) and refreshes the repairs and the entities. Independent of 10s: nothing is sent. |
| **At set-up, then 8 hours after each attempt** (coordinator refresh) | One *contact* with 10s. First the meters are read, before the contact waits on its lock or on 10s, so the reading is filed under the slot of the instant the contact began. Then, under the lock: drop mirror rows of unlinked devices; `GET /devices` and record what 10s reports; refresh the eligible categories; save; run the **upload pass**. An unreachable or refusing 10s leaves the mirror as it was. |
| **Upload pass** | For each device 10s takes data for (`registered` or `withdrawing`, with a known 10s id): the history dump if it is owed, then the live slots whose totals are final, both sides in one request per window; the watermark moves on `2xx`. A `withdrawn` device is still sent the slots it owes up to its `effectiveTo`, without the history. |
| **Panel / service action** | Register, withdraw, remove, recolour, serialised with the reconcile by one lock. Register and withdraw call 10s; remove and recolour are local. Afterwards the repairs and the entities are refreshed locally; the next contact is not brought forward. |
| **Options write** | Every write reloads the entry (account, device bindings; a colour lives in the registration store and does not reload). The sidebar panel is **not** unregistered by a reload, since a browser sitting on a panel that vanishes is sent to the default dashboard. It is replaced only when its JS changed, and removed when the entry is disabled or deleted. |
| **Auth failure** | `PartnerAuthError` becomes `ConfigEntryAuthFailed`: Home Assistant's coordinator opens the reauth flow (the first reconcile included); collection keeps going; only sending waits. |

### Concurrency

One lock serialises every change to the registration mirror (the reconcile, register, withdraw, remove, recolour,
logout). Each change awaits 10s between reading the mirror and saving it, so without the lock a register or a logout
landing inside a reconcile would be overwritten by the reconcile's older answer. The lock is released before the action
asks the host to refresh: a host whose refresh reconciles would otherwise wait on itself.

The archive needs no lock: the collector has no `await` between reading its log and recording into it, and it is the only
writer. The meter reads on the timers and the contact's first step both rely on that.

## 4. State and where it lives

| State | Where | Written by | Notes |
|---|---|---|---|
| The account (`PartnerAccount`: ids, the contract's id and reference, **client secret**, `appInstanceId`) | Config entry **options** (`tens_account`) | The login step, the reauth finish, logout | `entry.data` is empty by design. Redacted in diagnostics. |
| The linked devices (`MeteredDevice` rows: id, name, kind, meter bindings) | Config entry options (`metered_devices`) | The options flow, `remove_device`, logout | The list is the installation's; 10s only registers devices that exist. |
| **Registration mirror**: per device the status 10s last reported, its 10s id, its effective window (`effectiveFrom`, `effectiveTo`), the owed-history marker and refusal count, `registered_at`, the cached eligible categories, the chart colour | Store `tens_<entry>_device_lifecycle` | `PartnerRuntime`, under the lock | 10s is the authority; the mirror records what it said, so the panel can show the last known state while 10s is unreachable. |
| **Slot archive**: per device one total per 15-minute slot (72 hours) and one watermark | Store `tens_<entry>_device_slots` | `SlotCollector` |  |
| Upload hold-backs, stall and silence clocks, the bearer token cache, the remembered 10s device ids, `auth_failed` | Memory only | The runtime, the client, the coordinator | A restart or reload resets them on purpose (a fresh grace period). |
| History and live counters | Home Assistant (recorder statistics, entity states) | n/a | Read only. |

Stored shapes are not migrated while the integration is in development: when a shape changes, the old data is dropped
and the integration is set up fresh. Logout wipes the account, the devices and both stores; deleting the entry removes
both stores and sends nothing to 10s.

## 5. What Home Assistant sees

### Setup flow

*10s setup* menu: *10s account* and *Check and save*. *10s account* is the login; once logged in it offers *Metered
devices* (add, edit, remove) and *Log out*. First set-up stays in the flow after the login: the account is parked at
once in `tens_pending_account` (10s shows the secret once), and the entry is created, and the parked copy deleted, by
*Check and save*.

**Which meters are offered.** A sensor qualifies as the Energy dashboard would accept it: `sensor` domain,
`device_class: energy`, `state_class` `total` or `total_increasing`, enabled, not hidden, not a `utility_meter` helper
and not one of the integration's own sensors. Power sensors never qualify. A device with no candidate is refused with a
reason: `device_meters_linked`, `device_meter_not_counter`, `device_meter_disabled`, `device_power_only` or
`device_no_meter`.

**Candidates are ranked, never filtered**, by name hints, best first: a whole-device counter, then neutral names, then
counters split by circuit, tariff, phase or source, then time-scoped ones (today, monthly, per session), then non-electric
ones (heat), then anything that looks like export. The tokens come from the entity id, its names, its `translation_key`
and its `unique_id`, the last two because they stay English on an installation whose names are translated. Split counters
add up to a device's whole consumption only within one axis (L1 + L2 + L3, but not L1 + T1), which is why the axis is
kept.

**What is pre-picked.** Only the consumed side is guessed. If the best counter is one of several splits along one axis
and the role sums its counters, the whole axis is picked. A device whose every counter looks like export gets nothing. A
returned-side role opens empty, since a wrong guess would send the wrong counter as export, and a battery's required
discharge counter is refused empty until the installer picks it.

**The verify screen** (*Check and save*) is read-only: the account and, per device, whether it receives data and whether
10s accepts its kind. It exists because the installer leaves after Finish: the screen shows what 10s will receive while
it can still be fixed. In the options flow, Finish refuses a change that removes or edits a device 10s holds.

### Entities

- `10s Dashboard`: state `ok` or `auth_failed`; attributes: the environment, the account label, the eligible kinds, and
  one row per device (status, colour, meters). Its device list and timestamp are kept out of the recorder, because
  they would write a large new state row on every refresh. Per device `Energy (15 min)`: kWh of the latest ended slot,
  with the slot's bounds, the returned energy, the latest reading and the last confirmed mark. Both stay available while
  the login is refused.
- The sensors register no Home Assistant device: a device in the registry makes the frontend end the setup flow with its
  area page.

### Panel

Sidebar *10s* (`/tens`): the devices, their statuses, a stacked chart of slot energies and the buttons. The chart
fetches `/api/tens/device_slots`, which carries energies and accounting only: names, colours and statuses come from the
dashboard sensor alone, since a second copy read at another instant could disagree with it.

- **Axis.** Today and the two days before it, midnight to midnight, not clamped to the archive: a rolling left edge
  would fall inside a day and the day labels would stop naming their columns. On a day the clocks go back, the axis asks
  for up to 45 minutes more than the 72 hours kept; those columns draw as gaps, never zero. Right of the dashed line is
  today, which has not happened yet, and the caption says so.
- **Scale.** One scale for both halves (consumed above the zero line, returned mirrored below), so a small return is
  drawn small.
- **Stacking.** By accounting first, device order second: accounted against the axis, owed above it, not accounted on
  top, so a lighter band reads as "not accounted" rather than "smaller". A gap is a slot with no measurable value, as 10s
  receives it.

### Services and repairs

- **Services.** `tens.withdraw_device`, `tens.re_register_device`, `tens.remove_device`, `tens.set_device_color`,
  registered admin-only (`async_register_admin_service`): Home Assistant refuses a non-admin call before the handler
  runs, and the panel shows a non-admin the devices and the chart without buttons or colour picker. A refusal by 10s
  is a validation error; an unreachable 10s or a lost login is a Home Assistant error.
- **Repairs** (non-fixable warnings, per device): silent meter, stalled uploads, history given up; plus Home Assistant's
  own *re-authentication required*. They are level-triggered: after every meter read and contact the coordinator asks the
  kit's predicates (`silent_devices`, `stalled_devices`, `given_up_histories`) which devices have the problem now, opens
  an issue for each new one and closes the rest. A recovered, withdrawn or removed device therefore clears with no
  wiring of its own.
- **Diagnostics.** Entry options, the account, the devices as the panel sees them, the last refresh's success and
  exception.

## 6. The 10s client

`TensClient` speaks 10s's [live API description](https://api.dev.10senergy.com/v3/api-docs/external) on Home Assistant's
shared `aiohttp` session, to the servers of a `TensEnvironment`: production (`TENS_PROD`: `api.10senergy.com`, token endpoint
`auth.10senergy.com`, realm `10senergy-apps`), the default, or dev (`TENS_DEV`: `api.dev.10senergy.com`, same realm
and paths). The certificates of both are publicly trusted, and the session verifies them. An account works only in the
environment that issued it, so a stored account keeps its environment, for the coordinator and for a reauth. A new
login redeems in the environment `environment: dev` under `tens:` in `configuration.yaml` names, read once at
start-up (a change needs a restart), else in production; the login form and every refusal name the environment tried.
To move an installation, log out and log in again. The client gets a Keycloak client-credentials
token (cached, refreshed 30 s before it expires, one fresh grant on a `401`), then makes six calls: redeem a
registration token, list devices, list eligible categories, register, deregister, push measurements. Errors are
mapped by status only. It is the only client: nothing simulates 10s.

## 7. Checks

| Check | What it covers |
|---|---|
| Host suite (`tests/`) | The flows, the coordinator, the sensors, the view, the services, the reauth, the repairs, with a stub `homeassistant` module installed by the root `conftest.py`, so it runs without Home Assistant. |
| Kit suite (`_tenskit/tests/`) | Every kit layer. The real client is tested against `fake_tens.py`, an in-memory 10s and Keycloak behind a fake `ClientSession`; where 10s's API description is silent, its module docstring lists every default the fake applies. |
| `ruff`, `mypy`, `lint-imports` | `py312`, line length 120, the import-linter contracts. |
| CI | The same checks on every push (`.github/workflows/ci.yml`), plus an import of every module against real Home Assistant (at the `hacs.json` floor and latest), Home Assistant's `hassfest` and the HACS validation. |

`scripts/checks.sh` runs the lint, type, import and test checks with the arguments CI uses.
