// Run with node tests/test_v2_memory.js. No browser, backend or SMU required.
function testV2Memory(source, html) {
    let checks = 0;
    const assert = (condition, message) => {
        if (!condition) throw new Error(message);
        checks++;
    };
    const ids = [...html.matchAll(/\bid=["']([^"']+)["']/g)].map(m => m[1]);
    assert(new Set(ids).size === ids.length, 'HTML IDs must be unique');
    const scripts = [...html.matchAll(/<script\b[^>]*\bsrc="([^"?]+)(?:\?[^" ]*)?"/g)].map(m => m[1]);
    assert(scripts.includes('memory.js') && scripts.indexOf('memory.js') < scripts.indexOf('app.js'), 'load memory renderer before the app');
    const elements = new Map(ids.map(id => [id, {
        textContent: '', dataset: {}, classes: new Set(), attributes: {}, style: {},
        getAttribute(name) { return this.attributes[name]; },
        setAttribute(name, value) { this.attributes[name] = value; },
        classList: { toggle(name, on) { elements.get(id).classes[on ? 'add' : 'delete'](name); } },
    }]));
    const document = { getElementById(id) {
        assert(elements.has(id), `missing HTML element: ${id}`);
        return elements.get(id);
    } };
    const memory = new Function('document', source + '\nreturn BC250Memory;')(document);
    const text = id => elements.get(id).textContent;
    const good = { valid: true, chips_c: [36, 34, 44, 36, 36, 42, 42, 38], age_ms: 100 };

    memory.render(good, 100);
    assert(elements.get('v2-memory-panel').hidden === false, 'panel is visible for a live reading');
    assert(text('v2-memory-average') === '38.5', 'average is shown');
    assert(text('v2-memory-hotspot') === '44', 'hotspot is shown');
    assert(text('v2-memory-chip-2') === '44', 'chip value is shown');
    assert(elements.get('v2-memory-cell-2').classes.has('is-hottest'), 'highlight the hottest chip');
    assert(elements.get('v2-memory-bar-0').style.width === '30.0%', '36 degrees fills 30% of temperature bar');
    assert(elements.get('v2-memory-bar-2').style.backgroundColor === '#fb923c', 'hotspot bar is orange');
    const attr = (id, name) => elements.get(id).getAttribute(name);
    assert(attr('bc-memory-2', 'opacity') === '1', 'show fresh board halos');
    assert(elements.get('bc-memory-2').classes.has('is-hottest'), 'board hotspot matches memory panel');
    assert(text('bc-memory-title-2') === 'Chip 2 · U31: 44 °C · hotspot', 'physical chip tooltip');
    assert(Number(attr('bc-memory-glow-2', 'opacity')) > Number(attr('bc-memory-glow-1', 'opacity')), 'hotspot has a brighter halo');

    for (const status of ['stale', 'error', 'stopped', 'starting', 'unavailable', 'invalid_reading']) {
        memory.render({ valid: false, status }, 0);
        // 'unavailable' (collector never installed/enabled) hides the whole
        // panel instead of showing a dashed-out card; every other status is
        // a transient hiccup on an actually-installed collector, so it stays
        // visible with dashes — a real problem shouldn't go unnoticed.
        const shouldHide = status === 'unavailable';
        assert(elements.get('v2-memory-panel').hidden === shouldHide, `${status}: panel hidden=${shouldHide}`);
        if (shouldHide) continue; // content underneath is stale but never shown
        assert(text('v2-memory-hotspot') === '—', `${status}: clear the old hotspot`);
        assert(text('v2-memory-chip-2') === '—', `${status}: clear old chip data`);
        assert(!elements.get('v2-memory-cell-2').classes.has('is-hottest'), `${status}: clear old highlighting`);
        for (let i = 0; i < 8; ++i) {
            assert(elements.get(`v2-memory-bar-${i}`).style.width === '0%', `${status}: clear chip ${i} bar`);
            assert(attr(`bc-memory-${i}`, 'opacity') === '0', `${status}: hide chip ${i} halo`);
            assert(!elements.get(`bc-memory-${i}`).classes.has('is-hottest'), `${status}: clear chip ${i} hotspot`);
        }
    }
    assert(!memory.view(undefined, 0).valid, 'older API without memory is supported');
    assert(memory.view(undefined, null).label === 'WAITING', 'initial loading state');
    assert(memory.view(undefined, null, true).label === 'OFFLINE', 'initial network failure');
    assert(!memory.view(good, 15001).valid, 'lost connection cannot retain live values indefinitely');
    assert(memory.view({ ...good, age_ms: 14900 }, 101).status === 'stale', 'account for local sample aging');
    assert(!memory.view({ ...good, chips_c: [null, ...good.chips_c.slice(1)] }, 0).valid, 'null is not zero degrees');
    assert(!memory.view({ ...good, age_ms: NaN }, 0).valid, 'reject invalid sample age');
    assert(!memory.view({ ...good, chips_c: [1, 2] }, 0).valid, 'reject incomplete chip arrays');

    memory.render({ ...good, chips_c: [-40, 0, 120, 120, 36, 42, 42, 38] }, 0);
    assert(text('v2-memory-chip-0') === '-40', 'negative temperatures are valid');
    assert(elements.get('v2-memory-bar-0').style.width === '0.0%', 'negative temperature bar is clamped');
    assert(elements.get('v2-memory-bar-2').style.width === '100.0%', 'saturation fills temperature bar');
    assert(elements.get('v2-memory-bar-2').style.backgroundColor === '#fb7185', 'saturation bar uses sensor limit color');
    assert(text('v2-memory-chip-2') === '≥120', 'saturated chip is a lower bound');
    assert(text('v2-memory-hotspot') === '≥120', 'saturated hotspot is a lower bound');
    assert(text('v2-memory-average').startsWith('≥'), 'saturated average is a lower bound');
    assert([2, 3].every(i => elements.get(`v2-memory-cell-${i}`).classes.has('is-hottest')), 'highlight tied hottest chips');
    assert([2, 3].every(i => elements.get(`bc-memory-${i}`).classes.has('is-hottest')), 'highlight tied board hotspots');
    assert(elements.get('bc-memory-2').classes.has('is-saturated'), 'saturated board outline');
    assert(text('bc-memory-title-2').includes('≥120 °C'), 'board tooltip preserves sensor limit');
    assert(attr('bc-memory-gradient-0', 'color') === 'rgb(56, 189, 248)', 'clamp cold halo color');
    assert(attr('bc-memory-gradient-2', 'color') === 'rgb(251, 113, 133)', 'sensor limit halo color');
    memory.render({ ...good, chips_c: [20, 40, 60, 70, 80, 100, 110, 118] }, 0);
    assert(attr('bc-memory-gradient-3', 'color') === 'rgb(251, 191, 36)', 'middle of temperature scale');
    assert(!elements.get('bc-memory-2').classes.has('is-hottest') && elements.get('bc-memory-7').classes.has('is-hottest'), 'board hotspot moves with new samples');
    assert(Number(attr('bc-memory-glow-6', 'opacity')) > Number(attr('bc-memory-glow-1', 'opacity')), 'warmer ordinary chips glow more brightly');
    memory.render(good, 15001);
    assert(attr('bc-memory-7', 'opacity') === '0', 'connection loss clears halos');
    memory.render(good, 0);
    assert(!elements.get('v2-memory-cell-2').classes.has('is-saturated'), 'clear saturation after recovery');
    assert(!elements.get('bc-memory-2').classes.has('is-saturated'), 'clear board saturation after recovery');
    assert(attr('bc-memory-2', 'opacity') === '1', 'restore board halo after recovery');
    return checks;
}

if (typeof require === 'function' && require.main === module) {
    const fs = require('fs');
    const path = require('path');
    const root = path.join(__dirname, '..', 'web', 'v2');
    const checks = testV2Memory(fs.readFileSync(path.join(root, 'memory.js'), 'utf8'),
        fs.readFileSync(path.join(root, 'index.html'), 'utf8'));
    console.log(`v2 memory checks passed (${checks} assertions)`);
}
