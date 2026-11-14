/*
 * tens-panel — the 10s Devices panel, the `<tens-panel>` custom element that panel_custom mounts.
 *
 * Reads the 10s Dashboard sensor (found by its platform, not its entity id, which the user may rename) and
 * `GET /api/tens/device_slots`, and drives 10s through the admin-only lifecycle services. It renders what is
 * served and never differences readings or derives the accounting from a status.
 */
(function () {
  const SERVICE = {
    withdraw: 'withdraw_device',
    re_register: 're_register_device',
    remove: 'remove_device',
    color: 'set_device_color',
  };
  const STATUS_LABEL = {
    registered: 'Registered',
    registering: 'Registering…',
    withdrawing: 'Withdrawing…',
    withdrawn: 'Withdrawn',
  };
  // A copy of the brand wordmark with its #28303d paint moved to currentColor: an <img> cannot inherit the theme's
  // colour, and an async fetch would race _render(), which rebuilds innerHTML synchronously on every push.
  const WORDMARK_SVG = '<svg class="tens-wordmark" role="img" aria-label="10s"'
    + ' viewBox="0 0 1080 701.22" xmlns="http://www.w3.org/2000/svg">'
    + '<path fill="currentColor" d="M108.27,51.44h115.85v598.34h-77.63V129.06h-78.22l40-77.62Z"/>'
    + '<path fill="currentColor" d="M895.44,224.27c-1.04-25.47-78.31-13.47-82.54-54.05-2.94-28.93,25.9-41.1,53.88-39.98,34.54,1.3,49.13,23.4,49.13,23.4l-16.15,14.16c-6.82-9.41-18.04-15.8-31.6-16.84-17.87-1.38-32.21,3.45-32.03,14.42.17,24.95,81.94,8.29,82.37,60.09.17,23.74-25.82,41.1-54.31,39.8-29.01-1.29-44.21-14.07-55.09-29.01l16.84-14.42c8.38,13.47,22.36,22.71,39.98,23.05,17.7.34,29.96-9.07,29.53-20.64Z"/>'
    + '<path fill="currentColor" d="M863.48,342.93c-.07,0-.13,0-.19,0,.09,2.56.19,5.11.19,7.69,0,122.36-99.2,221.55-221.55,221.55s-221.55-99.19-221.55-221.55,99.19-221.55,221.55-221.55c30.43,0,59.42,6.14,85.81,17.24,9.63-25.39,26.18-47.37,47.35-63.65-40.1-19.97-85.31-31.21-133.16-31.21-165.23,0-299.17,133.94-299.17,299.17s133.94,299.17,299.17,299.17,299.17-133.94,299.17-299.17c0-9.88-.49-19.65-1.43-29.29-22.15,13.69-48.24,21.61-76.19,21.61Z"/>'
    + '<circle fill="none" stroke="currentColor" stroke-width="4" stroke-miterlimit="10"'
    + ' cx="863.8" cy="197.76" r="142.76"/></svg>';
  // One archive slot (15 minutes); the endpoint keys each energy by its opening mark.
  const SLOT_MS = 15 * 60 * 1000;
  // How 10s accounts a slot, as served beside each energy; the order is the stacking order, bottom first.
  // An unknown value draws as 'outside': "not known to be accounted for" is the safe reading.
  const ACCOUNTING = ['accounted', 'owed', 'outside'];
  const ACCOUNTING_ALPHA = { accounted: 0.6, owed: 0.35, outside: 0.15 };
  const ACCOUNTING_LABEL = {
    accounted: 'accounted to 10s',
    owed: 'to be sent to 10s',
    outside: 'not accounted',
  };
  const CHART_DEBOUNCE_MS = 400;
  // A failed read is not an empty archive: saying "no readings yet" for one would suggest a metering fault.
  const CHART_UNREADABLE = '<div class="tens-chart-note">Could not read the device archive'
    + ' from Home Assistant — the next coordinator cycle tries again.</div>';
  const CHART_NO_READINGS = '<div class="tens-chart-note">No metered readings yet — the archive'
    + ' fills one 15-minute slot at a time and needs two readings before it can show'
    + ' the energy between them.</div>';
  // The caption states what a reader would otherwise misread: an empty run of columns is not a load that
  // stopped, and both halves share one scale.
  const CHART_CAPTION = 'Energy in each 15-minute slot, in three shades: solid is accounted to 10s;'
    + ' lighter is inside the device\'s 10s window but not confirmed yet — it goes at the next contact;'
    + ' faint is outside that window — before the registration took effect, after the withdrawal did,'
    + ' or a device not registered — and the load did not stop.'
    + ' Returned energy is mirrored below the line. A gap is a slot with no measurable value'
    + ' (a missed reading or a counter reset), exactly as 10s receives it, so a column sums only'
    + ' the devices that reported: the slot\'s own title names the ones it is short of.';
  const CHART_CAPTION_SCALE = 'Both halves share one scale, so a small return is drawn small'
    + ' rather than enlarged, and the zero line is not the middle of the plot — read each half'
    + ' against the axis, not against the other. Right of the dashed line is today, which has'
    + ' not happened yet; the gap before it is the slot still filling.';

  // A pending status's window edge in local time; the date matters, as 10s's edge is one to two days out.
  function fmtTransition(iso) {
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return iso;
    return 'until ' + at.toLocaleString([], {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    });
  }

  function esc(text) {
    return String(text ?? '').replace(/[&<>"']/g, (ch) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
  }

  function localMidnight(offsetDays) {
    const at = new Date();
    at.setHours(0, 0, 0, 0);
    at.setDate(at.getDate() + offsetDays);
    return at.getTime();
  }

  // The smallest 1, 2, 2.5 or 5 times a power of ten that is at least `value`, so axis labels read as round values.
  function niceStep(value) {
    if (!(value > 0)) return 1;
    const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    for (const step of [1, 2, 2.5, 5, 10]) {
      if (step * magnitude >= value) return step * magnitude;
    }
    return magnitude * 10;
  }

  /**
   * Return one slot's fill opacity; the mirrored half is lighter, which tells the two halves apart.
   *
   * @param {string} accounting `'accounted'`, `'owed'` or `'outside'`, as served.
   * @param {string} side `'c'` above the axis, `'r'` mirrored below it.
   */
  function slotAlpha(accounting, side) {
    const base = ACCOUNTING_ALPHA[accounting] ?? ACCOUNTING_ALPHA.outside;
    return side === 'c' ? base : base * 0.65;
  }

  class TensPanel extends HTMLElement {
    constructor() {
      super();
      // Device id → the colour just picked, until the sensor reports it back.
      this._colorPicks = new Map();
    }

    set hass(hass) {
      this._hass = hass;
      this._render();
      this._maybeRefreshChart();
    }

    get hass() {
      return this._hass;
    }

    set panel(panel) {
      // Home Assistant assigns `element.panel` on every custom panel; without a setter the assignment throws.
      this._render();
    }

    connectedCallback() {
      this._render();
    }

    /**
     * Drop the chart's debounce timer and resize observer; the timer would fire against a detached element
     * and a reconnect cannot take either back.
     */
    disconnectedCallback() {
      clearTimeout(this._chartTimer);
      if (this._chartObserver) {
        this._chartObserver.disconnect();
        this._chartObserver = null;
      }
    }

    _dashboardEntity() {
      if (!this._hass) return null;
      const entities = this._hass.entities || {};
      for (const state of Object.values(this._hass.states || {})) {
        // One 10s entry per Home Assistant: one tens-platform entity carries the device list.
        const isTens = entities[state?.entity_id]?.platform === 'tens';
        if (isTens && Array.isArray(state.attributes?.metered_devices)) {
          return state;
        }
      }
      return null;
    }

    async _call(service, deviceId) {
      if (!this._hass) return;
      try {
        await this._hass.callService('tens', service, { metered_device_id: deviceId });
      } catch (err) {
        this._error = err && err.message ? err.message : String(err);
      }
      // The mirror updates on the next coordinator cycle; render now so a service error banner shows.
      this._render();
    }

    /**
     * Return the device rows with any just-picked colour laid over the served one.
     *
     * The served colour lags the service by a coordinator push; a pick is dropped once the sensor reports it.
     */
    _withPickedColors(devices) {
      return devices.map((dev) => {
        const pick = this._colorPicks.get(dev.id);
        if (pick === undefined) return dev;
        if (pick === dev.color) {
          this._colorPicks.delete(dev.id);
          return dev;
        }
        return { ...dev, color: pick };
      });
    }

    /**
     * Show a picked colour at once and send it to the set_device_color service.
     *
     * The colour is display-only, so a failure drops the pick and shows the error banner.
     */
    async _setColor(deviceId, color) {
      if (!this._hass) return;
      this._colorPicks.set(deviceId, color);
      // The picker has closed, so the held rebuild can go ahead.
      this._pickingColor = false;
      this._render();
      this._drawDeviceChart();
      try {
        await this._hass.callService('tens', SERVICE.color, { metered_device_id: deviceId, color });
      } catch (err) {
        this._colorPicks.delete(deviceId);
        this._error = err && err.message ? err.message : String(err);
        this._render();
        this._drawDeviceChart();
      }
    }

    /**
     * Return why Register is greyed out for a device, or '' when it is not.
     *
     * `eligible` is null until 10s has been asked; then the service call itself is the check.
     */
    _ineligibleReason(dev, accepted) {
      if (dev.eligible !== false) return '';
      const kinds = Array.isArray(accepted) && accepted.length
        ? `10s accepts: ${accepted.join(', ')}.`
        : '10s accepts no device kinds for this login yet.';
      return `10s does not accept ${(dev.category_label || 'this kind of').toLowerCase()} devices`
        + ` for registration under this login. ${kinds}`;
    }

    /**
     * Return whether the signed-in user may act on devices.
     *
     * The tens.* services are admin-only, and a button that is always refused is noise.
     * `hass.user` arrives with the first push; until then nobody is admin.
     */
    _isAdmin() {
      return !!(this._hass && this._hass.user && this._hass.user.is_admin);
    }

    _deviceActions(dev, accepted) {
      const actions = [];
      const s = dev.status;
      if (s === 'withdrawn') {
        const refused = this._ineligibleReason(dev, accepted);
        actions.push({
          key: 're_register',
          label: 'Register',
          className: 'action',
          disabled: !!refused,
          title: refused,
        });
        actions.push({
          key: 'remove',
          label: 'Remove',
          className: 'danger',
        });
      } else if (s === 'registered' || s === 'registering') {
        actions.push({
          key: 'withdraw',
          label: 'Withdraw',
          className: 'action',
        });
      }
      return actions;
    }

    _render() {
      if (!this.isConnected) return;
      // Every hass push rebuilds innerHTML, which would close an open colour picker; hold the rebuild until it is done.
      if (this._pickingColor) return;
      const entity = this._dashboardEntity();
      if (!entity) {
        this.innerHTML = '<div class="tens-root"><div class="tens-card">' +
          '<div class="tens-muted">10s dashboard sensor not found yet.</div></div></div>';
        return;
      }

      const attrs = entity.attributes || {};
      const linked = attrs.partner_linked;
      const account = attrs.partner_account;
      const devices = this._withPickedColors(Array.isArray(attrs.metered_devices) ? attrs.metered_devices : []);
      const canAct = this._isAdmin();
      const rows = devices.map((dev) => {
        const actions = canAct ? this._deviceActions(dev, attrs.partner_eligible) : [];
        const actionButtons = actions
          .map((a) => `<button data-act="${a.key}" data-device-id="${esc(dev.id)}" class="${a.className}"`
            + `${a.disabled ? ' disabled' : ''}${a.title ? ` title="${esc(a.title)}"` : ''}>${a.label}</button>`)
          .join('');
        const refused = dev.status === 'withdrawn' ? this._ineligibleReason(dev, attrs.partner_eligible) : '';
        return `
          <li class="tens-device">
            <input type="color" class="tens-swatch" data-color="1" data-device-id="${esc(dev.id)}"
              value="${esc(dev.color)}"${canAct ? '' : ' disabled'}
              title="${canAct ? 'Chart colour' : 'Chart colour (an administrator can change it)'}" />
            <div class="tens-device-body">
              <div class="tens-device-title">
                <span class="tens-device-name">${esc(dev.name)}</span>
                <span class="tens-badge">${esc(STATUS_LABEL[dev.status] || dev.status || '—')}</span>
              </div>
              <div class="tens-device-meta">${esc(dev.category_label || '')}${dev.transition_at ? ' · ' + esc(fmtTransition(dev.transition_at)) : ''}</div>
              ${refused ? `<div class="tens-device-meta tens-ineligible">${esc(refused)}</div>` : ''}
            </div>
            <div class="tens-device-actions">${actionButtons}</div>
          </li>`;
      }).join('');

      // The environment rides on the account line so a dev installation is never taken for production.
      const environment = attrs.partner_environment ? ` · 10s ${esc(attrs.partner_environment)} environment` : '';
      const accountHtml = linked && account
        ? `<div class="tens-muted">Logged in as <b>${esc(account.label)}</b>${environment}</div>`
        : `<div class="tens-muted">Not logged in — open the integration options to log in.${environment}</div>`;

      // Drawn only with an account and devices to report on. Its last drawing rides along in the rebuild:
      // a state push must not blank a chart the coordinator has not refreshed yet.
      const chartHtml = linked && devices.length
        ? `<div class="tens-chart" id="tens-chart">${this._chartHtml || ''}</div>`
        : '';

      const banner = this._error
        ? `<div class="tens-banner">${esc(this._error)}${this._dismiss()}</div>`
        : '';

      // A notice above a working panel, not an error in place of it: metering goes on while 10s refuses the login.
      const authNotice = attrs.auth_failed
        ? '<div class="tens-notice">10s no longer accepts the login. Readings are still being collected'
          + ' and will be sent after you log in again (Home Assistant shows the re-authentication prompt).</div>'
        : '';

      this.innerHTML = `
        <style>
          .tens-root { padding: 16px; }
          .tens-card .ha-card { padding: 16px; }
          .tens-header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
          .tens-title { font-weight: 600; }
          .tens-wordmark { height: 20px; width: auto; flex: none; color: var(--primary-text-color, #111); }
          .tens-banner { background: var(--error-color, #db4437); color: white; border-radius: 6px;
            padding: 8px 12px; margin-bottom: 8px; display: flex; align-items: center;
            justify-content: space-between; }
          .tens-notice { background: var(--warning-color, #ffa600); color: white; border-radius: 6px;
            padding: 8px 12px; margin-bottom: 8px; }
          .tens-list { list-style: none; margin: 8px 0 0; padding: 0; }
          .tens-device { display: flex; align-items: center; gap: 12px; padding: 10px 0;
            border-bottom: 1px solid var(--divider-color, rgba(128,128,128,.2)); }
          .tens-swatch { width: 24px; height: 24px; padding: 0; border: 0; background: none; cursor: pointer; flex: none; }
          .tens-device-body { flex: 1; }
          .tens-device-title { display: flex; align-items: center; gap: 8px; }
          .tens-device-name { font-weight: 500; }
          .tens-badge { font-size: 12px; border-radius: 10px; padding: 2px 8px;
            background: var(--secondary-background-color, #e0e0e0); }
          .tens-device-meta, .tens-muted { color: var(--secondary-text-color, #777); font-size: 13px; }
          .tens-device-actions { display: flex; gap: 6px; flex: none; }
          .tens-chart { margin-top: 12px; }
          .tens-chart svg { display: block; width: 100%; height: auto; }
          .tens-chart-note, .tens-chart-caption { color: var(--secondary-text-color, #777);
            font-size: 12px; margin: 8px 0 2px; }
          .tens-chart-legend { font-size: 11px; fill: var(--primary-text-color, #111); }
          .tens-chart-tick, .tens-chart-unit { font-size: 10px; fill: var(--secondary-text-color, #777); }
          .tens-chart-grid { stroke: var(--divider-color, rgba(128,128,128,.2)); stroke-width: 1; }
          .tens-chart-axis { stroke: var(--divider-color, rgba(128,128,128,.5)); stroke-width: 1; }
          .tens-chart-now { stroke: var(--primary-text-color, #111); stroke-width: 1;
            stroke-dasharray: 4 3; stroke-opacity: .6; }
          button.action, button.danger, button.dismiss { border: none; border-radius: 6px;
            padding: 6px 12px; cursor: pointer; color: var(--primary-text-color, #111); }
          button.action { background: var(--primary-color, #03a9f4); color: white; }
          button[disabled] { opacity: .45; cursor: not-allowed; }
          .tens-ineligible { font-style: italic; }
          button.danger { background: var(--error-color, #db4437); color: white; }
          button.dismiss { background: transparent; color: inherit; padding: 2px 6px; }
        </style>
        <div class="tens-root">
          <div class="tens-card">
            <div class="ha-card">
              <div class="tens-header">
                ${WORDMARK_SVG}
                <span class="tens-title">metered devices</span>
                <span class="tens-muted">${devices.length} linked</span>
              </div>
              ${banner}
              ${authNotice}
              ${accountHtml}
              <ul class="tens-list">${rows || '<li class="tens-muted">No devices linked yet.</li>'}</ul>
              ${chartHtml}
              ${chartHtml ? `<div class="tens-chart-caption">${CHART_CAPTION}</div>`
                + `<div class="tens-chart-caption">${CHART_CAPTION_SCALE}</div>` : ''}
            </div>
          </div>
        </div>`;
      this._bindActions();
      this._watchChartWidth(this.querySelector('#tens-chart'));
    }

    _dismiss() {
      return '<button class="action dismiss" data-dismiss="1">✕</button>';
    }

    _bindActions() {
      this.querySelectorAll('button[data-act]').forEach((btn) => {
        btn.addEventListener('click', () => {
          // The device is named on the button: after a rebuild its row is not where a position lookup would find it.
          const deviceId = btn.dataset.deviceId;
          const act = btn.dataset.act;
          if (act === 'withdraw') this._call(SERVICE.withdraw, deviceId);
          else if (act === 're_register') this._call(SERVICE.re_register, deviceId);
          else if (act === 'remove') this._call(SERVICE.remove, deviceId);
        });
      });
      // "change" fires when the picker closes; "input" would call the service on every drag step.
      this.querySelectorAll('input[data-color]').forEach((input) => {
        input.addEventListener('focus', () => { this._pickingColor = true; });
        input.addEventListener('blur', () => {
          // A native picker dialog takes the window's focus and blurs the input while still open; rebuilding then
          // would replace the input the dialog answers to.
          if (!document.hasFocus()) return;
          this._pickingColor = false;
          this._render();
        });
        input.addEventListener('change', () => this._setColor(input.dataset.deviceId, input.value));
      });
      this.querySelectorAll('button[data-dismiss]').forEach((btn) => {
        btn.addEventListener('click', () => { this._error = null; this._render(); });
      });
    }

    // --- The 72-hour device chart ---

    /**
     * Fetch the archive and redraw once per coordinator push.
     *
     * `set hass` fires on every state change; the dashboard sensor's last_updated advances only when the
     * coordinator rewrites it, so gating on it fetches once per cycle, and the debounce absorbs unrelated pushes.
     */
    _maybeRefreshChart() {
      const entity = this._dashboardEntity();
      const stamp = entity ? (entity.last_updated || entity.last_changed) : null;
      if (!stamp || stamp === this._chartStamp) return;
      this._chartStamp = stamp;
      clearTimeout(this._chartTimer);
      this._chartTimer = setTimeout(() => this._renderDeviceChart(), CHART_DEBOUNCE_MS);
    }

    /**
     * Read the 72-hour device archive from the integration's endpoint.
     *
     * @returns {Promise<Object|null>} device id → slot energies, or null when the endpoint could not be read,
     *   which is not the same as an empty archive.
     */
    async _fetchDeviceSlots() {
      const path = 'tens/device_slots';
      try {
        const payload = await this._hass.callApi('get', path);
        return payload?.devices || {};
      } catch {
        return null;
      }
    }

    /**
     * Index one device's served slot energies by slot start; a slot with neither side is a gap and gets no cell.
     *
     * A side is null after a missed reading or across a counter reset; the returned side is null for a kind
     * that measures none.
     *
     * @param {Array} rows `[start_ms, consumed|null, returned|null, accounting]`, earliest first.
     * @returns {Map<number, {c: number|null, r: number|null, a: string}>} slot start → kWh and accounting.
     */
    _slotCells(rows) {
      const out = new Map();
      for (const [t, c, r, a] of rows) {
        if (c == null && r == null) continue;
        out.set(t, { c, r, a });
      }
      return out;
    }

    /**
     * Read the archive and draw it, if there is somewhere to draw it.
     *
     * A rebuild during the fetch throws the target out of the tree, and the fresh one has nothing drawn in it.
     */
    async _renderDeviceChart() {
      if (!this.querySelector('#tens-chart')) return;
      const archive = await this._fetchDeviceSlots();
      if (!this.querySelector('#tens-chart')) return;
      this._chartArchive = archive;
      this._drawDeviceChart();
    }

    /** Draw the fetched archive, or the note that says why nothing is drawn; a resize redraws from the last fetch. */
    _drawDeviceChart() {
      const target = this.querySelector('#tens-chart');
      // No fetch has landed: the card already carries the last drawing, if there was one.
      if (!target || this._chartArchive === undefined) return;
      const width = target.clientWidth;
      if (width) this._chartWidth = width;
      this._chartHtml = this._chartMarkup(width || 640);
      target.innerHTML = this._chartHtml;
    }

    /** Return the chart's markup for the given width: the drawing, or a note. */
    _chartMarkup(width) {
      const archive = this._chartArchive;
      if (archive === null) return CHART_UNREADABLE;
      const entity = this._dashboardEntity();
      const rows = this._withPickedColors(
        Array.isArray(entity?.attributes?.metered_devices) ? entity.attributes.metered_devices : [],
      );
      const bands = [];
      for (const row of rows) {
        const cells = this._slotCells(archive[row.id] || []);
        if (cells.size) {
          bands.push({
            row,
            cells,
            // Lets a slot's title say "not read" about a lost return and stay silent about a kind that has none.
            measuresReturn: (row.meters || []).some((meter) => meter.role === 'returned'),
          });
        }
      }
      if (!bands.length) return CHART_NO_READINGS;
      return this._chartSvg(bands, width);
    }

    /** Redraw on a real width change, so the chart is neither stretched by a resize nor drawn at a guessed width. */
    _watchChartWidth(target) {
      if (this._chartObserver) this._chartObserver.disconnect();
      this._chartObserver = null;
      if (!target || typeof ResizeObserver === 'undefined') return;
      this._chartObserver = new ResizeObserver(() => {
        const width = target.clientWidth;
        if (width && Math.abs(width - this._chartWidth) > 8) this._drawDeviceChart();
      });
      this._chartObserver.observe(target);
    }

    /**
     * Build the stacked consumption chart as an inline `<svg>`: one band per device above the axis, returned
     * energy mirrored below it, every band broken where its readings are.
     *
     * @param {Array} bands `{row, cells, measuresReturn}` per device with a band, in the device list's order.
     * @param {number} width the container's width in CSS pixels.
     */
    _chartSvg(bands, width) {
      const PAD = { left: 44, right: 10, top: 4, bottom: 20 };
      const height = width < 480 ? 240 : 300;
      // Today and the two days before it, edge to edge, not clamped to the archive's floor: a rolling left edge
      // would fall inside a day and the day labels would stop naming their columns. On the day the clocks go
      // back it asks for up to 45 min more than the 72 h the archive keeps; those columns draw as gaps, never zero.
      const start = localMidnight(-2);
      const end = localMidnight(1);

      // --- Legend ---
      // Wraps between items, never inside one: a name wider than the container is cut with an ellipsis (the device
      // row and the slot's title carry it in full). The shades key starts a line of its own.
      const SWATCH = 10;
      const LEGEND_ROW = 16;
      const CHAR_PX = 6;
      const fitChars = Math.max(4, Math.floor((width - SWATCH - 6 - 16) / CHAR_PX));
      const fitName = (name) => (name.length > fitChars ? `${name.slice(0, fitChars - 1)}…` : name);
      const itemWidth = (item) => SWATCH + 6 + Math.round(item.label.length * CHAR_PX) + 16;
      const wrap = (items) => {
        const lines = [];
        let line = [];
        let used = 0;
        for (const item of items) {
          const w = itemWidth(item);
          if (line.length && used + w > width) {
            lines.push(line);
            line = [];
            used = 0;
          }
          line.push({ item, x: used });
          used += w;
        }
        if (line.length) lines.push(line);
        return lines;
      };
      const lines = [
        ...wrap(bands.map((band) => ({
          label: fitName(band.row.name || band.row.id),
          color: band.row.color || '#90a4ae',
          opacity: slotAlpha('accounted', 'c'),
        }))),
        // The key's swatches take the text colour, so they read in a dark theme too.
        ...wrap(ACCOUNTING.map((accounting) => ({
          label: ACCOUNTING_LABEL[accounting],
          color: 'currentColor',
          opacity: slotAlpha(accounting, 'c'),
        }))),
      ];

      let legend = '';
      let y = 0;
      for (const row of lines) {
        for (const entry of row) {
          legend += `<rect x="${entry.x}" y="${y}" width="${SWATCH}" height="${SWATCH}" rx="2"`
            + ` fill="${esc(entry.item.color)}" fill-opacity="${entry.item.opacity}" />`
            + `<text class="tens-chart-legend" x="${entry.x + SWATCH + 6}" y="${y + 9}">`
            + `${esc(entry.item.label)}</text>`;
        }
        y += LEGEND_ROW;
      }

      // --- The plot box and its scale ---
      const plotLeft = PAD.left;
      const plotRight = Math.max(plotLeft + 1, width - PAD.right);
      const plotWidth = plotRight - plotLeft;
      const plotTop = y + PAD.top;
      const plotBottom = height - PAD.bottom;
      const plotHeight = plotBottom - plotTop;
      const xOf = (t) => plotLeft + ((t - start) / (end - start)) * plotWidth;

      // One column per slot any device has a reading for, from the served slots, so a window that does not
      // start on a slot mark still finds its columns.
      const columns = new Map();
      for (const band of bands) {
        for (const [t, cell] of band.cells) {
          let column = columns.get(t);
          if (!column) {
            column = { consumed: 0, returned: 0 };
            columns.set(t, column);
          }
          if (cell.c != null) column.consumed += cell.c;
          if (cell.r != null) column.returned += cell.r;
        }
      }
      let posMax = 0;
      let negMax = 0;
      for (const column of columns.values()) {
        posMax = Math.max(posMax, column.consumed);
        negMax = Math.max(negMax, column.returned);
      }
      // One scale for both halves, range from zero, signed labels: a symmetric axis would squash a 0.5 kWh return
      // into a hairline under a 12 kWh consumption. An unknown return sets no maximum.
      const spread = posMax + negMax;
      // With every cell zero there is no range to divide by and NaN reaches every y coordinate; one step either
      // side of zero keeps a labelled frame.
      const step = spread > 0 ? niceStep(spread / 4) : 1;
      const axisMax = spread > 0 ? Math.ceil(posMax / step) * step : step;
      const axisMin = spread > 0 ? -Math.ceil(negMax / step) * step : -step;
      const decimals = step >= 1 ? 1 : (step >= 0.1 ? 2 : 3);
      const yOf = (kwh) => plotTop + ((axisMax - kwh) / (axisMax - axisMin)) * plotHeight;

      // --- Gridlines, the axis, and their labels ---
      // Stepped by index: `value += step` drifts off zero, and the axis is picked by comparing against it.
      const epsilon = step / 1000;
      let frame = '';
      for (let i = 0; ; i += 1) {
        const value = axisMin + i * step;
        if (value > axisMax + epsilon) break;
        const at = yOf(value).toFixed(1);
        frame += `<line class="${Math.abs(value) < epsilon ? 'tens-chart-axis' : 'tens-chart-grid'}"`
          + ` x1="${plotLeft}" y1="${at}" x2="${plotRight}" y2="${at}" />`
          + `<text class="tens-chart-tick" x="${plotLeft - 6}" y="${at}" dy="3.5"`
          + ` text-anchor="end">${value.toFixed(decimals)}</text>`;
      }
      // Each day's own midnight, not start + n×24 h: on a DST day those are an hour apart.
      for (let day = 0; day <= 2; day++) {
        const at = xOf(localMidnight(day - 2)).toFixed(1);
        frame += `<line class="tens-chart-grid" x1="${at}" y1="${plotTop}" x2="${at}" y2="${plotBottom}" />`
          + `<text class="tens-chart-tick" x="${at}" y="${plotBottom + 14}" text-anchor="middle">`
          + `${new Date(localMidnight(day - 2)).toLocaleDateString([], { day: '2-digit', month: '2-digit' })}</text>`;
      }
      // Rotated about x=11, not the edge: the glyphs rise left of the baseline, so a pivot at the edge clips the label.
      frame += `<text class="tens-chart-unit" x="11" y="${(plotTop + plotHeight / 2).toFixed(1)}"`
        + ` text-anchor="middle" transform="rotate(-90 11 ${(plotTop + plotHeight / 2).toFixed(1)})">kWh / 15 min</text>`;

      // --- The bands ---
      // Stacked by accounting first, device order second: accounted against the axis, owed above, unaccounted on
      // top, so a lighter band reads as "not accounted" rather than "smaller". One path per shade, so a device
      // whose window opens or closes inside the three days changes shade at that mark only.
      const rank = (accounting) => {
        const at = ACCOUNTING.indexOf(accounting);
        return at < 0 ? ACCOUNTING.length : at;
      };
      const rect = (x0, x1, top, bottom) =>
        `M${x0.toFixed(1)},${top.toFixed(1)}H${x1.toFixed(1)}V${bottom.toFixed(1)}H${x0.toFixed(1)}Z`;
      const stack = (side) => {
        // band → accounting → path data.
        const paths = new Map();
        for (const t of columns.keys()) {
          const x0 = Math.max(plotLeft, xOf(t));
          const x1 = Math.min(plotRight, xOf(t + SLOT_MS));
          const here = bands
            .filter((band) => band.cells.has(t))
            .sort((p, q) => rank(p.cells.get(t).a) - rank(q.cells.get(t).a));
          let base = 0;
          for (const band of here) {
            const cell = band.cells.get(t);
            // An unknown return is left out of the mirrored stack, not stacked as zero; the slot's title says so.
            if (side === 'r' && cell.r == null) continue;
            if (side === 'c' && cell.c == null) continue;
            // Returned energy is negative, so the same scale puts it below the line.
            const value = side === 'c' ? cell.c : -cell.r;
            if (value !== 0 && x1 > x0) {
              if (!paths.has(band)) paths.set(band, new Map());
              const shades = paths.get(band);
              shades.set(cell.a, (shades.get(cell.a) || '') + rect(x0, x1, yOf(base + value), yOf(base)));
            }
            base += value;
          }
        }
        return paths;
      };
      let areas = '';
      for (const side of ['c', 'r']) {
        for (const [band, shades] of stack(side)) {
          for (const [accounting, d] of shades) {
            areas += `<path d="${d}" fill="${esc(band.row.color || '#90a4ae')}"`
              + ` fill-opacity="${slotAlpha(accounting, side)}" />`;
          }
        }
      }

      // Only what the archive holds is drawn: the rest of today is empty, not zero, and the run before this line is
      // the slot still filling.
      const nowX = xOf(Date.now());
      if (nowX > plotLeft && nowX < plotRight) {
        frame += `<line class="tens-chart-now" x1="${nowX.toFixed(1)}" y1="${plotTop}"`
          + ` x2="${nowX.toFixed(1)}" y2="${plotBottom}" />`;
      }

      // --- Hover titles ---
      // One transparent column per slot naming every band, zeros and unreadable or missing values included: the
      // column sums only bands that reported, so a title listing only the movers could not tell a shortfall from
      // a drop in load.
      let hover = '';
      for (const t of columns.keys()) {
        const x0 = Math.max(plotLeft, xOf(t));
        const x1 = Math.min(plotRight, xOf(t + SLOT_MS));
        if (x1 <= x0) continue;
        const linesOut = [new Date(t).toLocaleString([], {
          day: '2-digit', month: '2-digit',
          hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
        })];
        for (const band of bands) {
          const name = band.row.name || band.row.id;
          const cell = band.cells.get(t);
          if (!cell) {
            linesOut.push(`${name}: no value in this slot (a missed reading or a counter reset)`);
            continue;
          }
          let text = cell.c == null
            ? `${name}: consumed not measurable in this slot`
            : `${name}: ${cell.c.toFixed(3)} kWh`;
          if (cell.r == null) {
            if (band.measuresReturn) text += ', returned not read in this slot';
          } else if (cell.r) {
            text += `, returned ${cell.r.toFixed(3)} kWh`;
          }
          if (cell.a !== 'accounted') text += ` (${ACCOUNTING_LABEL[cell.a] || ACCOUNTING_LABEL.outside})`;
          linesOut.push(text);
        }
        hover += `<rect x="${x0.toFixed(1)}" y="${plotTop}" width="${(x1 - x0).toFixed(1)}"`
          + ` height="${plotHeight}" fill="transparent"><title>${esc(linesOut.join('\n'))}</title></rect>`;
      }

      return `<svg viewBox="0 0 ${width} ${height}" role="img">`
        + '<title>Energy per 15-minute slot over the last three days, one band per device</title>'
        + legend + frame + areas + hover + '</svg>';
    }
  }

  customElements.define('tens-panel', TensPanel);
})();