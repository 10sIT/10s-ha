# 10s

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/tens/brand/dark_logo.png">
  <img src="custom_components/tens/brand/logo.png" alt="10s" width="380">
</picture>

A Home Assistant integration that meters devices and reports their energy to 10s.

**Requires Home Assistant 2024.11.0 or newer** (Python 3.12).

> The integration talks to 10s's **production** environment (`https://api.10senergy.com`) and verifies its
> certificates, which are publicly trusted.

## What it does

10s reads the energy meters of the devices you choose (a heat pump, a boiler, an EV charger, a battery) and sends the
energy used in each 15-minute slot to the 10s service. It meters only: no inverter, prices or forecasts, and it never
controls a device. How it works is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Setup

1. Add this repository to Home Assistant:

   [![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=10sIT&repository=10s-ha&category=integration)

   Or by hand:
   - **Home Assistant 2026.11 or newer:** **Settings → Marketplace → Add from link**, repository
     `https://github.com/10sIT/10s-ha`, type **Integration**. Home Assistant asks once to connect a GitHub account (any
     free account); it uses it to read the repository and check for updates.
   - **Older versions, with [HACS](https://hacs.xyz):** **HACS → ⋮ → Custom repositories**, repository
     `https://github.com/10sIT/10s-ha`, type **Integration**, then **Add**.
2. Open **10s**, choose **Download** (or **Install**), then **restart Home Assistant**. Restart again after each update:
   until then Home Assistant keeps running the previous version.
3. **Settings → Devices & services → Add integration → 10s.**
4. Open **10s account**, enter the registration token you received from 10s, and confirm. The token works once; 10s
   answers with this installation's credentials, which are stored at once. The login identifies this Home Assistant to
   10s by its installation id, which is the same on every login.
5. Under the account, open **Metered devices** and link each device: pick the Home Assistant device and its energy
   sensors. Go back to the menu and choose **Check and save**. Later changes are under **Configure**.

## Using it

- The **10s** sidebar panel lists every linked device with its status (registering, registered, withdrawing,
  withdrawn), the 15-minute chart (solid where 10s accounts a slot, lighter where it is still to be sent, faint outside
  the device's 10s window), and buttons to withdraw, register again, remove or recolour a device. Only categories 10s
  accepts for your account can be registered; the panel greys out the rest.
- Each device also gets a 15-minute energy sensor, and one **10s Dashboard** sensor feeds the panel.
- Services: `tens.withdraw_device`, `tens.re_register_device`, `tens.remove_device`, `tens.set_device_color`. They are
  for administrators: Home Assistant refuses a call from a non-admin user, and the panel shows a non-admin the devices
  and the chart without the buttons. They take the metered device id from the 10s Dashboard sensor's `metered_devices`
  attribute.
- A registered device takes effect at 10s at midnight Brussels time, two days after the day it was registered; the
  panel shows the date. Nothing waits for that date: right after 10s accepts the registration, the device's recorded
  history goes to 10s from Home Assistant's recorder statistics (whatever the recorder holds, at most one year; nothing
  warns about a short one), and its 15-minute readings go from the registration on, at each contact. A history that
  does not get through goes at the next contact.
- Home Assistant contacts 10s when it starts and then every 8 hours, and each contact sends everything owed since the
  last one. A contact that fails is not retried sooner: the next one sends what it missed.
- The meters of every linked device are read every 5 minutes, whatever its status. Readings are kept for 72 hours, so an
  outage of 10s shorter than that loses nothing; after a longer one, older slots are not delivered and nothing is made
  up for them.
- A withdrawal leaves no gap: the slots up to the moment it takes effect go at the next contact, even though the device
  then reads *Withdrawn*; nothing after that moment is sent.

## Consent and your data at 10s

Consent to send the readings, where 10s stores them and how they are deleted are settled between you and the provider
(10s) by your contract with them. This integration is only the transport: it shows no consent step and makes no
statement about 10s's storage or deletion.

## Logging in, logging out, and when 10s stops accepting the login

- **If 10s refuses a login**, the form says so (token not accepted, already registered, refused, or 10s unreachable)
  and nothing is stored. Checking the token is up to you; anything else is for 10s to resolve.
- **Logging out** (Configure → 10s account → Log out, allowed once every device is withdrawn) removes the account,
  the devices and their readings from this Home Assistant only. 10s keeps this installation's registration, so
  logging in again from the same Home Assistant can be answered *already registered* until 10s resolves it.
- **When 10s stops accepting the login** — while running or when Home Assistant starts — the integration keeps
  running: it keeps taking the 15-minute readings (up to 72 hours are kept), and only sending them waits. Home
  Assistant asks for a new registration token (*Re-authentication required*). After you enter one, the readings it
  kept are sent. Your devices stay linked, and keep their 10s registration as long as 10s accepts the new token for
  this same installation; a device 10s no longer lists is logged as a warning and shows as not registered.
- While the login is refused, the 10s panel keeps working and shows a notice that 10s no longer accepts the login
  and that the readings will be sent after you log in again; the 10s sensors stay available. Registering or
  withdrawing a device waits for the new login (the panel shows the error).

## Removing the integration

Withdraw every device in the 10s panel before deleting the integration. 10s offers no way to revoke a registration from
here, so a device still registered when the integration is deleted stays registered at 10s, with nothing left to send
its readings. Deleting removes this installation's stored registrations and its 72-hour archive from Home Assistant.

## Repairs and troubleshooting

| Repair | Meaning | What to do |
|---|---|---|
| *<device> has stopped reporting* | A device 10s holds (registering, registered or withdrawing) has had no meter reading for 2 hours | Check its energy sensor is available and the device is online. Clears itself. |
| *<device>'s readings are not reaching 10s* | Uploads waiting for more than 20 hours (two missed contacts) | Check the internet connection; a refusal by 10s is logged as a warning. Clears itself. |
| *10s will not take <device>'s recorded history* | 10s refused the recorded history four times (the offer at registration and three retries, one per contact); it is not offered again | Live readings are unaffected. To resend it: fix the cause (the refusal is logged as a warning), withdraw the device and, once it reads withdrawn, register it again — a new registration offers its history afresh. Clears when the device is withdrawn or removed. |
| Re-authentication required | 10s no longer accepts the stored credentials | Follow the prompt and enter a new registration token. Readings are still being collected, and those still within the 72 hours kept are sent once the new login works; devices stay linked and keep their 10s registration as long as 10s accepts the token for this installation. |

**Diagnostics:** Settings → Devices & services → 10s → ⋮ → *Download diagnostics*. The file lists the stored
configuration, the account and the devices as the panel sees them. Credentials and every identifier of your 10s account
and contract (account id, contract id and reference, client id, app instance id, tokens) are redacted. Attach it when
reporting an issue; if 10s needs your contract reference, it asks you privately.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): how it is built, what runs when, where state lives.

## License

Copyright 2026 10s-Energy. Licensed under the [Apache License 2.0](LICENSE).
