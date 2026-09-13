// GDDR6 temperatures are discrete samples: display them without interpolation.
// Physical positions follow the board owner's top/X-ray view mapping.
const BC250Memory = (() => {
    const MAX_AGE_MS = 15000;
    const DESIGNATORS = ['U27', 'U29', 'U31', 'U33', 'U43', 'U41', 'U39', 'U37'];

    // Normalized 0–1 position on the visual scale (20 °C → 0, 120 °C → 1),
    // shared by the halo brightness and the bar/halo color.
    const heatLevel = (temperature) => Math.max(0, Math.min(1, (temperature - 20) / 100));

    // A continuous visual scale, not hardware alarm thresholds: blue at 20 °C,
    // amber at 70 °C, pink at the 120 °C sensor limit. Clamp colder samples.
    function heatColor(temperature) {
        const level = heatLevel(temperature);
        const stops = [[56, 189, 248], [251, 191, 36], [251, 113, 133]];
        const segment = level < 0.5 ? 0 : 1;
        const mix = level * 2 - segment;
        return `rgb(${stops[segment].map((v, i) => Math.round(v + (stops[segment + 1][i] - v) * mix)).join(', ')})`;
    }
    const messages = {
        starting: ['STARTING', 'Preparing memory monitoring…'],
        stopped: ['STOPPED', 'Memory monitoring is stopped.'],
        unavailable: ['UNAVAILABLE', 'Memory temperatures are unavailable.'],
        stale: ['STALE', 'Waiting for a fresh memory reading.'],
        error: ['ERROR', 'Memory monitoring reported an error.'],
        invalid_data: ['INVALID DATA', 'The memory reading could not be used.'],
        invalid_reading: ['INVALID DATA', 'The memory reading could not be used.'],
    };

    function view(memory, elapsedMs, fetchFailed = false) {
        const empty = (status, label, message) => ({ valid: false, status, label, message });
        if (elapsedMs == null) {
            return fetchFailed ? empty('error', 'OFFLINE', 'Cannot reach telemetry.')
                : empty('starting', 'WAITING', 'Waiting for telemetry…');
        }
        if (elapsedMs > MAX_AGE_MS) return empty('stale', 'OFFLINE', 'Telemetry connection is unavailable.');
        if (!memory) return empty('unavailable', ...messages.unavailable);
        if (memory.valid !== true) {
            const status = Object.hasOwn(messages, memory.status) ? memory.status : 'invalid_data';
            return empty(status, ...messages[status]);
        }
        const chips = memory.chips_c;
        if (!Array.isArray(chips) || chips.length !== 8
            || !chips.every(t => typeof t === 'number' && Number.isFinite(t) && t >= -40 && t <= 120)
            || !Number.isFinite(memory.age_ms) || memory.age_ms < 0) {
            return empty('invalid_data', ...messages.invalid_data);
        }
        const ageMs = memory.age_ms + Math.max(0, elapsedMs);
        if (ageMs > MAX_AGE_MS) return empty('stale', ...messages.stale);
        const hotspot = Math.max(...chips);
        const saturated = chips.some(t => t === 120);
        return {
            valid: true, status: saturated ? 'saturated' : 'ok',
            label: saturated ? 'SENSOR LIMIT' : 'LIVE', chips,
            average: chips.reduce((sum, t) => sum + t, 0) / chips.length,
            hotspot, hottest: chips.flatMap((t, i) => t === hotspot ? [i] : []), saturated,
            message: saturated ? '≥120 °C: sensor limit reached; summary values are lower bounds.'
                : 'Live GDDR6 temperatures',
        };
    }

    function render(memory, elapsedMs, fetchFailed = false) {
        const data = view(memory, elapsedMs, fetchFailed);
        const panel = document.getElementById('v2-memory-panel');
        if (!panel) return;
        // 'unavailable' means the collector was never installed/enabled (the
        // daemon found no snapshot file at all) — not a transient hiccup like
        // 'stale'/'error'/'starting', which stay visible so a real problem
        // with an actually-installed collector doesn't go unnoticed.
        panel.hidden = data.status === 'unavailable';
        if (panel.hidden) return;
        const text = (id, value) => {
            const el = document.getElementById(id);
            if (el && el.textContent !== value) el.textContent = value;
        };
        panel.dataset.memoryStatus = data.status;
        // The indicator is a dot (.oc-dot), so the state has no visible text:
        // it reads out through the tooltip and the accessible name instead.
        const statusEl = document.getElementById('v2-memory-status');
        if (statusEl) {
            const summary = `${data.label} — ${data.message}`;
            if (statusEl.title !== summary) statusEl.title = summary;
            if (statusEl.getAttribute('aria-label') !== summary) statusEl.setAttribute('aria-label', summary);
        }
        text('v2-memory-average', data.valid ? (data.saturated ? '≥' : '') + data.average.toFixed(1) : '—');
        text('v2-memory-hotspot', data.valid ? (data.saturated ? '≥' : '') + data.hotspot.toFixed(0) : '—');
        for (let i = 0; i < 8; ++i) {
            const hottest = data.valid && data.hottest.includes(i);
            const limit = data.valid && data.chips[i] === 120;
            text(`v2-memory-chip-${i}`, data.valid ? (limit ? '≥' : '') + data.chips[i].toFixed(0) : '—');
            const cell = document.getElementById(`v2-memory-cell-${i}`);
            if (cell) {
                cell.classList.toggle('is-hottest', !!hottest);
                cell.classList.toggle('is-saturated', !!limit);
            }
            const color = data.valid ? (limit ? '#fb7185' : hottest ? '#fb923c' : heatColor(data.chips[i])) : 'transparent';
            const bar = document.getElementById(`v2-memory-bar-${i}`);
            if (bar) {
                // 0–120 °C display scale; negative temperatures remain in the
                // numeric reading and saturation fills the entire track.
                const width = data.valid ? (Math.max(0, Math.min(120, data.chips[i])) / 120 * 100).toFixed(1) + '%' : '0%';
                if (bar.style.width !== width) bar.style.width = width;
                if (bar.style.backgroundColor !== color) bar.style.backgroundColor = color;
            }
            const chip = document.getElementById(`bc-memory-${i}`);
            if (chip) {
                const attr = (el, name, value) => {
                    if (el && el.getAttribute(name) !== value) el.setAttribute(name, value);
                };
                attr(chip, 'opacity', data.valid ? '1' : '0');
                chip.classList.toggle('is-hottest', !!hottest);
                chip.classList.toggle('is-saturated', !!limit);
                const temperature = data.valid ? data.chips[i] : null;
                const heat = data.valid ? heatLevel(temperature) : 0;
                attr(document.getElementById(`bc-memory-gradient-${i}`), 'color', data.valid ? heatColor(temperature) : '#38bdf8');
                attr(document.getElementById(`bc-memory-glow-${i}`), 'opacity', data.valid ? (hottest ? '0.95' : (0.3 + heat * 0.5).toFixed(3)) : '0');
                text(`bc-memory-title-${i}`, `Chip ${i} · ${DESIGNATORS[i]}: `
                    + (data.valid ? `${limit ? '≥' : ''}${temperature} °C${hottest ? ' · hotspot' : ''}` : 'unavailable'));
            }
        }
    }

    return { view, render };
})();
