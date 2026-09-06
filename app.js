/* MiyeePDF - UI layer.
 *
 * All PDF work happens in pdf_engine.py, so this file only handles chrome:
 * loading files, drawing pages, collecting options and shuttling them across
 * the engine bridge.
 */

/* ------------------------------------------------------------------ */
/* engine bridge                                                       */
/* ------------------------------------------------------------------ */

// Keep in step with the ?v= query on the script/style tags in index.html so a
// redeploy never leaves a browser running a stale mix of old and new assets.
const APP_VERSION = '4.25.0';
const PYODIDE_VERSION = '314.0.5';
const PYODIDE_INDEX = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const PYMUPDF_WHEEL = 'vendor/pymupdf-1.28.2-cp313-abi3-pyemscripten_2025_0_wasm32.whl';

class PyEngine {
    constructor() {
        this.pyodide = null;
        this.module = null;
        this.ready = false;
        this.error = null;
        this._promise = null;
        this.onStep = null;
        this.lastMessage = 'Getting things ready…';
        this.lastPct = 0;
    }

    /** Boot the engine. Safe to await from anywhere; only runs once.
     *
     * Progress is reported through `onStep`, which can be attached after boot
     * has already started - that lets the download begin the moment this file
     * parses, rather than waiting for DOMContentLoaded and the UI wiring.
     */
    boot() {
        if (!this._promise) this._promise = this._boot((msg, pct) => this._report(msg, pct));
        return this._promise;
    }

    _report(message, pct) {
        this.lastMessage = message;
        this.lastPct = pct;
        if (this.onStep) this.onStep(message, pct);
    }

    /** Approximate the wheel download for the progress bar.
     *
     * loadPackage() owns the actual fetch - it is the only thing that reliably
     * installs the package, and prefetching separately made the browser
     * download all 17 MB twice (loadPackage treats a path as a URL, so it
     * cannot be handed bytes, and HTTP-cache dedupe is not dependable). So the
     * bar is advanced on a timer sized to the payload instead, and snaps to
     * completion when the install actually returns.
     */
    _fakeProgress(from, to, expectedMs, onStep, label) {
        const started = Date.now();
        const timer = setInterval(() => {
            const frac = Math.min(0.97, (Date.now() - started) / expectedMs);
            onStep(label, from + (to - from) * frac);
        }, 250);
        return () => clearInterval(timer);
    }

    /** Say honestly how the one big download is going.
     *
     * Three things were tried for a real byte count and none of them work
     * here, so the reasoning is worth keeping rather than rediscovering:
     *
     *  - Fetching the wheel separately and handing it over transfers it
     *    twice. Measured: 35.1 MB moved for a 17.5 MB file, because nothing
     *    dedupes the second request.
     *  - loadPackage will not take a blob URL; it derives the package name
     *    from the filename and a blob URL has none.
     *  - Wrapping window.fetch sees nothing: Pyodide does not fetch the
     *    wheel through it, as a probe of every URL that passes through
     *    window.fetch confirmed.
     *
     * So the size is not knowable from here. What is knowable is how long it
     * has been going, and that is what gets shown - with the bar left
     * indeterminate rather than creeping to 97% and sitting there, which is
     * what made a stalled download look like an almost-finished one.
     */
    _watchDownload(onStep) {
        const started = Date.now();
        const tick = setInterval(() => {
            const secs = Math.round((Date.now() - started) / 1000);
            if (secs < 20) return;
            const mins = Math.floor(secs / 60);
            const elapsed = mins ? `${mins} min ${secs % 60}s` : `${secs}s`;
            if (secs < 90) {
                onStep(`Downloading PDF tools (17.5 MB, one time) - ${elapsed} so far`, -1);
            } else if (secs < 240) {
                onStep(`Still downloading (17.5 MB, one time) - ${elapsed} so far. ` +
                       'A slow connection can take a few minutes.', -1);
            } else {
                onStep(`This is taking unusually long - ${elapsed} so far. ` +
                       'The download may have stalled; you can wait or try again.', -1);
            }
        }, 1000);
        return () => clearInterval(tick);
    }

    async _boot(onStep = () => {}) {
        try {
            onStep('Setting up your workspace…', 5);
            let stop = this._fakeProgress(5, 45, 12000, onStep, 'Setting up your workspace…');
            this.pyodide = await loadPyodide({ indexURL: PYODIDE_INDEX });
            stop();

            onStep('Downloading PDF tools (17.5 MB, one time)…', -1);
            const unwatch = this._watchDownload(onStep);
            try {
                await this.pyodide.loadPackage(PYMUPDF_WHEEL);
            } finally {
                unwatch();
            }

            onStep('Almost ready…', 90);
            const source = await (await fetch(`pdf_engine.py?v=${APP_VERSION}`)).text();
            this.pyodide.FS.writeFile('/pdf_engine.py', source);
            this.pyodide.runPython('import sys\nif "/" not in sys.path: sys.path.insert(0, "/")');
            this.module = this.pyodide.pyimport('pdf_engine');

            onStep('Ready', 100);
            this.ready = true;
            return this;
        } catch (err) {
            this.error = err;
            console.error('Engine failed to start:', err);
            throw err;
        }
    }

    /** Call a function in pdf_engine.py. Engine exceptions surface as JS errors. */
    async call(fn, ...args) {
        if (!this.ready) await this.boot();
        const target = this.module[fn];
        if (!target) throw new Error(`pdf_engine has no function '${fn}'`);
        const result = target(...args);
        return this._unwrap(result);
    }

    /** Same as call(), but parses a JSON string result. */
    async callJSON(fn, ...args) {
        const raw = await this.call(fn, ...args);
        return typeof raw === 'string' ? JSON.parse(raw) : raw;
    }

    /** Open a PDF, asking for the password if the file turns out to be
     * encrypted. Every tool loads documents through here, so a protected file
     * behaves the same way wherever it is opened rather than failing with a
     * raw engine error in fourteen of the fifteen tabs.
     *
     * Returns the same metadata as open_doc. Throws CANCELLED if the person
     * dismisses the prompt, which the busy-wrapper treats as "no error".
     */
    async openDoc(docId, bytes, fileName = '') {
        let password = '';
        let retry = false;
        for (;;) {
            try {
                const info = await this.callJSON('open_doc', docId, bytes, password);
                // Kept only in memory, for the length of a batch, so the same
                // password is not asked for twenty times over.
                this.lastPassword = password;
                return info;
            } catch (err) {
                if (!String(err.message || err).includes('PASSWORD_REQUIRED')) throw err;
                password = await UI.askPassword(fileName, retry);
                if (password === null) throw new Error('CANCELLED');
                retry = true;
            }
        }
    }

    _unwrap(value) {
        if (value && typeof value.toJs === 'function') {
            const converted = value.toJs({ create_pyproxies: false });
            value.destroy();
            return converted;
        }
        if (value && typeof value.getBuffer === 'function') {
            const buf = value.getBuffer();
            const copy = new Uint8Array(buf.data);
            buf.release();
            value.destroy();
            return copy;
        }
        return value;
    }
}

const engine = new PyEngine();

/* ------------------------------------------------------------------ */
/* small helpers                                                       */
/* ------------------------------------------------------------------ */

const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function download(data, filename, mime = 'application/pdf') {
    const blob = data instanceof Blob ? data : new Blob([data], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

/** Which open documents carry changes that have not been downloaded yet.
 *
 * Every tool here works on a copy in memory and hands the result back as a
 * download, so closing the tab is the only way to lose work - and until now
 * it did so in silence. A change is recorded where the undo snapshot is
 * taken, which is exactly the set of operations that alter the document,
 * and cleared when the file is saved or a different one is opened.
 */
const Dirty = {
    slots: new Set(),
    names: {},

    touch(docId) {
        this.slots.add(docId);
        this.show(docId);
        Session.remember(docId, this.names[docId] || '');
    },
    clear(docId) {
        this.slots.delete(docId);
        this.show(docId);
        Session.forget(docId);
    },
    has() { return this.slots.size > 0; },

    /** Mark the tab's Save button, so "I have not saved this" is visible
     *  rather than something you have to remember. */
    show(docId) {
        const dirty = this.slots.has(docId);
        const btn = $(`${docId}-save`);
        if (btn) btn.classList.toggle('btn--unsaved', dirty);
        const note = $(`${docId}-unsaved`);
        if (note) note.classList.toggle('hidden', !dirty);
    },
};

// The browser shows its own wording here; all a page can do is ask for the
// prompt at all. Nothing is asked when there is nothing to lose.
window.addEventListener('beforeunload', (e) => {
    if (!Dirty.has()) return;
    e.preventDefault();
    e.returnValue = '';
});

/** Keeps the document you are working on across a reload.
 *
 * Everything here lives in memory, so a stray Back, a refresh or a phone
 * killing the tab used to throw away an afternoon's work with no way to get
 * it back. The working copy is written to IndexedDB on the device - the same
 * promise as the rest of the app, nothing leaves it - and offered back the
 * next time the page opens.
 *
 * Only the document being worked on is kept, not a history of files: this is
 * a way to survive an accident, not an archive of everything you have opened.
 */
const Session = {
    DB: 'miyee-session',
    STORE: 'documents',
    MAX_AGE_MS: 7 * 24 * 60 * 60 * 1000,
    // A save costs a serialise of the whole document, so edits are allowed to
    // settle rather than writing on every keystroke.
    DEBOUNCE_MS: 2500,

    _timers: {},

    open() {
        if (this._db) return this._db;
        this._db = new Promise((resolve, reject) => {
            const req = indexedDB.open(this.DB, 1);
            req.onupgradeneeded = () => {
                if (!req.result.objectStoreNames.contains(this.STORE)) {
                    req.result.createObjectStore(this.STORE);
                }
            };
            req.onsuccess = () => resolve(req.result);
            req.onerror = () => reject(req.error);
        }).catch((err) => {
            // Private windows and locked-down browsers refuse storage. That
            // is a lost convenience, never a reason to break the app.
            console.warn('Session storage unavailable', err);
            return null;
        });
        return this._db;
    },

    async _tx(mode, run) {
        const db = await this.open();
        if (!db) return null;
        return new Promise((resolve, reject) => {
            const tx = db.transaction(this.STORE, mode);
            const req = run(tx.objectStore(this.STORE));
            tx.oncomplete = () => resolve(req && req.result);
            tx.onerror = () => reject(tx.error);
        }).catch((err) => { console.warn('Session write failed', err); return null; });
    },

    /** Remember the current state of one tab's document, after a pause. */
    remember(docId, fileName) {
        clearTimeout(this._timers[docId]);
        this._timers[docId] = setTimeout(async () => {
            try {
                const bytes = await engine.call('save', docId);
                await this._tx('readwrite', (store) => store.put({
                    docId, fileName, at: Date.now(),
                    bytes: bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes),
                }, docId));
            } catch (err) {
                console.warn('Could not keep the session', err);
            }
        }, this.DEBOUNCE_MS);
    },

    forget(docId) {
        clearTimeout(this._timers[docId]);
        return this._tx('readwrite', (store) => store.delete(docId));
    },

    async saved() {
        const db = await this.open();
        if (!db) return [];
        const all = await new Promise((resolve) => {
            const out = [];
            const tx = db.transaction(this.STORE, 'readonly');
            const req = tx.objectStore(this.STORE).openCursor();
            req.onsuccess = () => {
                const cur = req.result;
                if (!cur) return resolve(out);
                out.push(cur.value);
                cur.continue();
            };
            req.onerror = () => resolve(out);
        });
        const fresh = all.filter((e) => Date.now() - e.at < this.MAX_AGE_MS);
        // Anything older than a week is stale enough that offering it back
        // would be a puzzle rather than a rescue.
        all.filter((e) => !fresh.includes(e)).forEach((e) => this.forget(e.docId));
        return fresh;
    },
};

/** A Uint8Array as the binary string forge works in. Chunked, because
 *  String.fromCharCode over a multi-megabyte array at once overflows the
 *  argument stack. */
function bytesToBinary(bytes) {
    const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    let out = '';
    for (let i = 0; i < arr.length; i += 0x8000) {
        out += String.fromCharCode.apply(null, arr.subarray(i, i + 0x8000));
    }
    return out;
}

function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
}

/** PDF colour integer (0xRRGGBB) as a CSS colour. */
function intToCss(value) {
    return `#${(Number(value) >>> 0 & 0xffffff).toString(16).padStart(6, '0')}`;
}

/* PyMuPDF span flags: 2 = italic, 4 = serifed, 8 = monospaced, 16 = bold. */
const FLAG_ITALIC = 2, FLAG_SERIF = 4, FLAG_MONO = 8, FLAG_BOLD = 16;

/** A widely installed stack that resembles a PDF font the browser cannot load.
 *
 * Used when a font is not embedded, or is embedded in a format (bare CFF,
 * Type 1) that browsers refuse - the on-screen text still reads as the same
 * kind of face as the page around it.
 */
function lookalikeFont(name, flags = 0) {
    const n = String(name || '').split('+').pop().toLowerCase();
    const bold = (flags & FLAG_BOLD) !== 0 || /bold|black|heavy|semibold|\bbd\b/.test(n);
    const italic = (flags & FLAG_ITALIC) !== 0 || /italic|oblique/.test(n);
    let family = '"Helvetica Neue", Helvetica, Arial, sans-serif';
    if ((flags & FLAG_MONO) !== 0 || /mono|courier|consol|menlo/.test(n)) {
        family = '"Courier New", Courier, monospace';
    } else if (/times|serif|georgia|garamond|cambria|roman|minion|book(?!man old)/.test(n)
               || ((flags & FLAG_SERIF) !== 0 && !/sans|arial|helvet|roboto|calibri/.test(n))) {
        family = 'Georgia, "Times New Roman", Times, serif';
    }
    return { family, weight: bold ? '700' : '400', style: italic ? 'italic' : 'normal' };
}

async function fileToBytes(file) {
    if (file.size > 200 * 1024 * 1024) throw new Error('File too large (max 200 MB)');
    return new Uint8Array(await file.arrayBuffer());
}

/* ------------------------------------------------------------------ */
/* application shell                                                   */
/* ------------------------------------------------------------------ */

const UI = {
    theme: 'light',

    init() {
        this.setupTabs();
        this.setupTheme();
        this.setupPickers();
        // Wire every tool straight away. These only attach listeners and render
        // markup, so the app is fully browsable while the engine downloads -
        // waiting for it here is what made the page look frozen on first visit.
        Tools.forEach((t) => { if (t.init) t.init(); });
        this.bootEngine();
        this.offerRestore();
    },

    /** Offer back a document that a reload would otherwise have discarded. */
    async offerRestore() {
        let saved = [];
        try {
            saved = await Session.saved();
        } catch (err) {
            return;                       // storage refused; nothing to offer
        }
        if (!saved.length) return;

        const banner = $('restore-banner');
        const which = saved.map((e) => e.fileName || 'a document').join(', ');
        $('restore-what').textContent = saved.length === 1
            ? `You were working on ${which}.`
            : `You were working on ${saved.length} documents: ${which}.`;
        banner.classList.remove('hidden');

        $('restore-yes').onclick = async () => {
            banner.classList.add('hidden');
            await this.run('Restoring your work…', async () => {
                for (const entry of saved) {
                    const tool = Tools.find((t) => t.view && t.view.docId === entry.docId);
                    await engine.call('open_doc', entry.docId, entry.bytes);
                    Dirty.names[entry.docId] = entry.fileName;
                    if (tool) {
                        // Put the tab back on screen showing the document,
                        // and mark it unsaved, because it still is.
                        tool.view.info = await engine.callJSON('doc_info', entry.docId);
                        tool.view.pages = tool.view.info.pages;
                        tool.view.page = 0;
                        $(`${tool.view.key}-workspace`).classList.remove('hidden');
                        tool.view.buildThumbs();
                        await tool.view.render();
                        if (tool.history) await tool.history.refresh();
                    }
                    Dirty.slots.add(entry.docId);
                    Dirty.show(entry.docId);
                }
                const first = Tools.find((t) => t.view && t.view.docId === saved[0].docId);
                if (first) this.showTab(first.view.key);
                this.toast('Your work is back - it was never uploaded anywhere', 'success');
            });
        };

        $('restore-no').onclick = async () => {
            banner.classList.add('hidden');
            for (const entry of saved) await Session.forget(entry.docId);
        };
    },

    setupTabs() {
        $$('.tab-btn').forEach((btn) => {
            btn.addEventListener('click', () => this.showTab(btn.dataset.tab));
        });
    },

    /** Single entry point for switching tools, shared by the nav and the dashboard. */
    showTab(name) {
        const target = `${name}-tab`;
        if (!$(target)) return;
        $$('.tab-btn').forEach((b) => {
            const on = b.dataset.tab === name;
            b.classList.toggle('active', on);
            b.setAttribute('aria-selected', String(on));
            // On a phone the strip scrolls; keep the chosen tab in view.
            if (on && b.scrollIntoView) {
                b.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            }
        });
        $$('.tab-content').forEach((c) => c.classList.toggle('active', c.id === target));
        window.scrollTo({ top: 0, behavior: 'smooth' });
    },

    setupTheme() {
        const toggle = $('theme-toggle');
        toggle.addEventListener('click', () => {
            this.theme = this.theme === 'light' ? 'dark' : 'light';
            document.documentElement.setAttribute('data-color-scheme', this.theme);
            toggle.textContent = this.theme === 'dark' ? '☀️' : '🌙';
        });
        document.documentElement.setAttribute('data-color-scheme', this.theme);
    },

    /** Wire every "Select PDF" button and drop zone declaratively. */
    setupPickers() {
        $$('[data-pick]').forEach((btn) => {
            btn.addEventListener('click', () => $(btn.dataset.pick).click());
        });
        // Picking the same file twice fires no change event, because the
        // input's value has not changed - so after redacting a document and
        // choosing it again to check the result, nothing would happen.
        // Clearing the value on the way into the dialog makes the second pick
        // count as a new one.
        $$('input[type="file"]').forEach((input) => {
            input.addEventListener('click', () => { input.value = ''; });
        });
        $$('[data-drop]').forEach((zone) => {
            const input = $(`${zone.dataset.drop}-file`);
            zone.addEventListener('click', (e) => {
                if (e.target === zone || e.target.tagName === 'P') input.click();
            });
            // The zone is reachable by keyboard, so it has to respond to one.
            zone.addEventListener('keydown', (e) => {
                if (e.target !== zone) return;
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
            });
            ['dragenter', 'dragover'].forEach((ev) =>
                zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('drag-over'); }));
            ['dragleave', 'drop'].forEach((ev) =>
                zone.addEventListener(ev, () => zone.classList.remove('drag-over')));
            zone.addEventListener('drop', (e) => {
                e.preventDefault();
                const files = Array.from(e.dataTransfer.files).filter((f) => f.name.toLowerCase().endsWith('.pdf'));
                if (files.length) {
                    const dt = new DataTransfer();
                    files.forEach((f) => dt.items.add(f));
                    input.files = dt.files;
                    input.dispatchEvent(new Event('change'));
                }
            });
        });
    },

    /** Latest boot message, so a tool waiting on the engine can show progress. */
    bootMessage: 'Getting things ready…',

    async bootEngine() {
        const step = $('engine-loading-step');
        const fill = $('engine-progress-fill');
        const badge = $('engine-badge');
        const badgeText = $('engine-badge-text');

        const paint = (message, pct) => {
            this.bootMessage = message;
            step.textContent = message;
            // A negative percentage means the length is genuinely unknown.
            // Showing a moving stripe says "working" without claiming a
            // position the code cannot know.
            const unknown = pct < 0;
            fill.parentElement.classList.toggle('progress-bar--unknown', unknown);
            if (!unknown) fill.style.width = `${pct}%`;
            badgeText.textContent = unknown ? 'Downloading…'
                : message.length > 34 ? `${Math.round(pct)}%` : message;
            // Keep a waiting operation's spinner in step with the download.
            if (!$('status-container').classList.contains('hidden')) {
                $('status-text').textContent = message;
            }
        };

        try {
            // Boot already began at parse time; attach to it and catch up.
            engine.onStep = paint;
            paint(engine.lastMessage, engine.lastPct);
            await engine.boot();
            $('engine-loading').classList.add('hidden');
            badge.className = 'engine-badge engine-badge--ready';
            badgeText.textContent = 'Ready';
        } catch (err) {
            // Say which part failed and offer another go, rather than asking
            // for a page reload that starts the whole download again.
            const reason = String(err && (err.message || err));
            this.bootMessage = 'Setup failed';
            step.innerHTML = (reason.includes('OFFLINE')
                ? 'Could not reach the download. Check your connection, then try again.'
                : reason.startsWith('HTTP_')
                    ? `The download was refused (${reason.replace('HTTP_', 'error ')}). Try again in a moment.`
                    : 'Could not finish setting up.') +
                ' <button class="btn btn--sm" id="engine-retry">Try again</button>';
            fill.style.width = '100%';
            fill.style.background = 'var(--color-error)';
            badge.className = 'engine-badge engine-badge--error';
            badgeText.textContent = 'Setup failed';
            const retry = $('engine-retry');
            if (retry) {
                retry.onclick = () => {
                    fill.style.background = '';
                    fill.style.width = '0%';
                    badge.className = 'engine-badge engine-badge--loading';
                    engine.ready = false;
                    engine.error = null;
                    engine._booting = null;
                    this.bootEngine();
                };
            }
        }
    },

    busy(message) {
        $('status-text').textContent = message;
        $('status-container').classList.remove('hidden');
    },

    idle() {
        $('status-container').classList.add('hidden');
    },

    toast(message, type = 'info') {
        $$('.toast').forEach((t) => t.remove());
        const el = document.createElement('div');
        el.className = `toast toast--${type}`;
        el.textContent = message;
        document.body.appendChild(el);
        requestAnimationFrame(() => el.classList.add('show'));
        setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); }, 3600);
    },

    /** Ask for a PDF password. Resolves to the text typed, or null if the
     * person cancelled. Uses a real dialog rather than window.prompt(), which
     * mobile browsers render badly and some contexts block outright. */
    askPassword(fileName = '', retry = false) {
        const modal = $('pw-modal');
        const input = $('pw-input');
        const error = $('pw-error');
        $('pw-file').textContent = fileName ? `${fileName} needs a password to open.` : '';
        error.classList.toggle('hidden', !retry);
        input.value = '';
        modal.classList.remove('hidden');
        setTimeout(() => input.focus(), 50);

        return new Promise((resolve) => {
            const finish = (value) => {
                modal.classList.add('hidden');
                $('pw-submit').removeEventListener('click', onSubmit);
                input.removeEventListener('keydown', onKey);
                $$('[data-pwclose]', modal).forEach((el) => el.removeEventListener('click', onCancel));
                resolve(value);
            };
            const onSubmit = () => { if (input.value) finish(input.value); };
            const onCancel = () => finish(null);
            const onKey = (e) => {
                if (e.key === 'Enter') { e.preventDefault(); onSubmit(); }
                if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
            };
            $('pw-submit').addEventListener('click', onSubmit);
            input.addEventListener('keydown', onKey);
            $$('[data-pwclose]', modal).forEach((el) => el.addEventListener('click', onCancel));
        });
    },

    /** Turn an engine exception into something worth showing a person.
     * Python tracebacks arrive as one long string; the last line is the
     * useful part, but the raw text still reads like a crash report. */
    explain(err) {
        const raw = String(err && err.message || err);
        const last = raw.split('\n').filter(Boolean).pop() || 'Something went wrong';
        const known = [
            [/PASSWORD_REQUIRED/, 'This PDF is password-protected.'],
            [/needs a password|encrypted/i, 'This PDF is password-protected.'],
            [/cannot open broken|no objects found|not a (pdf|textpage)|format error|syntax error/i,
             'This file could not be read as a PDF. It may be damaged or not really a PDF.'],
            [/File too large/i, last],
            [/out of memory|Aborted|RangeError|allocation/i,
             'This file is too big to process in the browser. Try splitting it into smaller parts first.'],
            [/NetworkError|Failed to fetch/i,
             'A download failed. Check your connection and try again.'],
        ];
        for (const [pattern, friendly] of known) if (pattern.test(raw)) return friendly;
        // Strip the "PythonError: ExceptionType: " preamble Pyodide prepends.
        return last.replace(/^\w*(Error|Exception):\s*/, '').trim() || 'Something went wrong';
    },

    /** Run an async job with a spinner and uniform error reporting. */
    async run(message, job) {
        // On first use the engine may still be arriving; say so rather than
        // showing a task message that looks stuck.
        this.busy(engine.ready ? message : this.bootMessage);
        try {
            return await job();
        } catch (err) {
            // Dismissing the password prompt is a choice, not a failure.
            if (String(err.message || err).includes('CANCELLED')) return null;
            console.error(err);
            this.toast(this.explain(err), 'error');
            return null;
        } finally {
            this.idle();
        }
    },
};

/* ------------------------------------------------------------------ */
/* reusable single-document page viewer                                */
/* ------------------------------------------------------------------ */

class DocView {
    /** @param {string} key matches the element id prefix used in index.html */
    constructor(key, { onRender } = {}) {
        this.key = key;
        this.docId = key;
        this.page = 0;
        this.pages = 0;
        this.info = null;
        this.file = null;
        this.onRender = onRender;

        this.img = $(`${key}-page-img`);
        this.holder = $(`${key}-holder`);
        this.overlay = $(`${key}-overlay`);

        // 0 means fit-to-width; anything else is a multiple of that.
        this.zoom = 0;

        $$(`[data-nav="${key}"]`).forEach((btn) => {
            btn.addEventListener('click', () => this.go(this.page + Number(btn.dataset.dir)));
        });
        // Scoped by key, like the page-navigation buttons: an unscoped
        // [data-zoom] selector binds every viewer to one tab's controls, so
        // zooming here would try to render documents that are not open.
        $$(`[data-zoom-for="${key}"]`).forEach((btn) => {
            btn.addEventListener('click', () => this.setZoom(btn.dataset.zoom));
        });

        this.rail = $(`${key}-thumbs`);
        this._thumbUrls = [];
        $$(`[data-thumbs-for="${key}"]`).forEach((btn) => {
            btn.addEventListener('click', () => this.toggleThumbs(btn));
        });

        // Typing a page number is how you move through a hundred-page file;
        // clicking the arrow fifty times is not.
        const jump = $(`${key}-page-jump`);
        if (jump) {
            const goTyped = () => {
                const n = Number(jump.value);
                if (!Number.isFinite(n)) return;
                const target = Math.min(Math.max(Math.round(n), 1), this.pages) - 1;
                jump.value = target + 1;
                if (target !== this.page) this.go(target);
            };
            jump.addEventListener('change', goTyped);
            jump.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); goTyped(); }
            });
        }
    }

    /* --- thumbnail rail ---------------------------------------------------
     *
     * Rendering every page up front stalls a long document - a 200-page file
     * would sit behind 200 engine calls before showing anything - so each
     * thumbnail is drawn only once it scrolls into the rail, and then kept.
     * The draws are queued one at a time so a fast scroll cannot starve the
     * main page render, which shares the engine with them.
     */
    buildThumbs() {
        if (!this.rail) return;
        this._thumbUrls.forEach((u) => URL.revokeObjectURL(u));
        this._thumbUrls = [];
        this._thumbQueue = [];
        this._thumbBusy = false;
        if (this._thumbWatcher) this._thumbWatcher.disconnect();
        this.rail.innerHTML = '';

        this._thumbWatcher = new IntersectionObserver((entries) => {
            entries.forEach((e) => {
                if (e.isIntersecting) this.queueThumb(Number(e.target.dataset.page));
            });
        }, { root: this.rail, rootMargin: '300px 0px' });

        for (let i = 0; i < this.pages; i++) {
            const cell = document.createElement('button');
            cell.type = 'button';
            cell.className = 'thumb';
            cell.dataset.page = String(i);
            cell.setAttribute('aria-label', `Page ${i + 1}`);
            cell.innerHTML = `<span class="thumb__frame"></span><span class="thumb__no">${i + 1}</span>`;
            cell.addEventListener('click', () => this.go(i));
            this.rail.appendChild(cell);
            this._thumbWatcher.observe(cell);
        }
        this.markThumb();
    }

    queueThumb(index, force = false) {
        const cell = this.rail && this.rail.children[index];
        if (!cell) return;
        if (cell.dataset.drawn === '1' && !force) return;
        cell.dataset.drawn = '1';
        this._thumbQueue.push(index);
        this.drainThumbs();
    }

    async drainThumbs() {
        if (this._thumbBusy) return;
        this._thumbBusy = true;
        try {
            while (this._thumbQueue.length) {
                const index = this._thumbQueue.shift();
                const cell = this.rail && this.rail.children[index];
                if (!cell) continue;
                let png;
                try {
                    png = await engine.call('render_thumb', this.docId, index, 132);
                } catch (err) {
                    // The document may have been closed or swapped while this
                    // was queued. Let it be redrawn later rather than leaving
                    // a broken image in the rail.
                    cell.dataset.drawn = '';
                    continue;
                }
                const url = URL.createObjectURL(new Blob([png], { type: 'image/png' }));
                this._thumbUrls.push(url);
                const frame = cell.querySelector('.thumb__frame');
                frame.innerHTML = '';
                const img = document.createElement('img');
                img.alt = '';
                img.src = url;
                frame.appendChild(img);
            }
        } finally {
            this._thumbBusy = false;
        }
    }

    /** Show which page is on screen, and scroll the rail to it. */
    markThumb() {
        if (!this.rail) return;
        Array.from(this.rail.children).forEach((cell, i) => {
            const here = i === this.page;
            cell.classList.toggle('thumb--current', here);
            cell.setAttribute('aria-current', here ? 'page' : 'false');
            // The rail is a column on a wide screen and a strip on a narrow
            // one, so both axes are nudged.
            if (here) cell.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        });
    }

    toggleThumbs(btn) {
        if (!this.rail) return;
        const hidden = this.rail.classList.toggle('hidden');
        btn.setAttribute('aria-pressed', String(!hidden));
        btn.setAttribute('aria-label', hidden ? 'Show page thumbnails' : 'Hide page thumbnails');
    }

    /** Zoom the page. Overlays are positioned in percentages, so they follow
     *  the image without any recalculation; only the render resolution and the
     *  holder width change. */
    async setZoom(action) {
        const steps = [1, 1.25, 1.5, 2, 3, 4];
        const current = this.zoom || 1;
        if (action === 'reset') {
            this.zoom = 0;
        } else if (action === 'in') {
            this.zoom = steps.find((s) => s > current + 0.001) || steps[steps.length - 1];
        } else if (action === 'out') {
            const below = steps.filter((s) => s < current - 0.001);
            this.zoom = below.length ? below[below.length - 1] : 0;
        } else {
            this.zoom = Number(action) || 0;
        }
        this.applyZoom();
        // Re-render sharper when magnified, so zooming shows more detail
        // rather than a bigger blur.
        await this.render();
    }

    applyZoom() {
        if (!this.holder) return;
        if (this.zoom) {
            this.holder.style.width = `${this.zoom * 100}%`;
            this.holder.style.maxWidth = 'none';
        } else {
            this.holder.style.width = '';
            this.holder.style.maxWidth = '';
        }
        const label = $(`${this.key}-zoom-level`);
        if (label) label.textContent = this.zoom ? `${Math.round(this.zoom * 100)}%` : 'Fit';
    }

    async load(file) {
        const bytes = await fileToBytes(file);
        this.file = file;
        this.info = await engine.openDoc(this.docId, bytes, file.name);
        Dirty.names[this.docId] = file.name;
        Dirty.clear(this.docId);
        this.pages = this.info.pages;
        this.page = 0;
        $(`${this.key}-workspace`).classList.remove('hidden');
        const jump = $(`${this.key}-page-jump`);
        if (jump) { jump.max = this.pages; jump.value = 1; }
        this.buildThumbs();
        await this.render();
        return this.info;
    }

    async go(index) {
        if (index < 0 || index >= this.pages) return;
        this.page = index;
        await this.render();
    }

    async render() {
        // Render at the zoom level so magnifying reveals detail instead of
        // enlarging the same pixels. Capped so a 4x zoom cannot ask for a
        // page render big enough to stall the tab.
        const dpi = Math.round(Math.min(110 * Math.max(this.zoom || 1, 1), 300));
        const png = await engine.call('render_page', this.docId, this.page, dpi);
        if (this._url) URL.revokeObjectURL(this._url);
        this._url = URL.createObjectURL(new Blob([png], { type: 'image/png' }));
        await new Promise((resolve) => {
            this.img.onload = resolve;
            this.img.src = this._url;
        });
        const info = $(`${this.key}-page-info`);
        if (info) info.textContent = `${this.page + 1}/${this.pages}`;
        const jump = $(`${this.key}-page-jump`);
        if (jump && Number(jump.value) !== this.page + 1) jump.value = this.page + 1;
        this.markThumb();
        // Every operation that changes the document ends in a re-render, so
        // this is where a stale thumbnail gets caught: redact a page, stamp
        // it or edit its text and the rail shows the change rather than the
        // version from when the file was opened.
        this.queueThumb(this.page, true);
        if (this.overlay) this.overlay.innerHTML = '';
        if (this.onRender) await this.onRender();
    }

    /** Pointer position on the page as 0..1 fractions. */
    fracFromEvent(e) {
        const r = this.overlay.getBoundingClientRect();
        return {
            x: Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1),
            y: Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1),
        };
    }

    async save(filename) {
        const bytes = await engine.call('save', this.docId);
        download(bytes, filename);
        Dirty.clear(this.docId);
    }
}

/* ------------------------------------------------------------------ */
/* 1. Edit text + annotate                                             */
/* ------------------------------------------------------------------ */

const EditTool = {
    mode: 'TEXT',
    spans: [],
    editing: null,
    fontCache: new Map(),

    init() {
        this.view = new DocView('edit', { onRender: () => this.drawSpans() });
        // The Edit tab keeps its own shortcut, which stands aside while an
        // inline editor is open so the browser's undo owns the box.
        this.history = makeHistory('edit', this.view,
                                   { bindKeys: false, onStep: (d) => this.step(d) });
        $('edit-file').addEventListener('change', (e) => this.open(e.target.files[0]));

        $$('[data-editmode]').forEach((btn) => btn.addEventListener('click', () => {
            this.mode = btn.dataset.editmode;
            $$('[data-editmode]').forEach((b) => b.classList.toggle('active', b === btn));
            $('edit-hint').textContent =
                this.mode === 'TEXT' ? 'Tap a highlighted line and type - you edit straight on the page. Clear the box to delete the line.'
              : this.mode === 'BLOCK' ? 'Tap a paragraph and rewrite it in place - the text rewraps to fit.'
              : this.mode === 'ADD' ? 'Tap anywhere on the page to add new text there.'
              : `Drag across the page to add a ${this.mode} annotation.`;
            $('edit-add-options').classList.toggle('hidden', this.mode !== 'ADD');
            this.view.overlay.classList.toggle('overlay--add', this.mode === 'ADD');
            this.drawSpans();
        }));

        $('outline-add').addEventListener('click', () => {
            const title = $('outline-title').value.trim();
            if (!title) return UI.toast('Enter a bookmark title', 'error');
            this.outline = this.outline || [];
            this.outline.push([Number($('outline-level').value) || 1, title,
                               Number($('outline-page').value) || 1]);
            $('outline-title').value = '';
            this.renderOutline();
        });

        $('outline-apply').addEventListener('click', async () => {
            await UI.run('Updating bookmarks…', async () => {
                await this.mark();
                const n = await engine.call('set_outline', this.view.docId,
                                            JSON.stringify(this.outline || []));
                UI.toast(`${n} bookmark(s) applied - save to keep them`, 'success');
            });
        });

        $('edit-save').addEventListener('click', () => this.view.save('edited.pdf'));
        $('edit-search-btn').addEventListener('click', () => this.search());
        $('edit-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') this.search(); });

        $('edit-scan-ocr').addEventListener('click', () => this.ocrCurrent());

        // Ctrl+Z / Ctrl+Shift+Z while the Edit tab is showing and nothing is
        // being typed into - the browser's own undo owns the box while it is.
        document.addEventListener('keydown', (e) => {
            if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 'z') return;
            if (!$('edit-tab').classList.contains('active')) return;
            if (this.editing) return;
            const el = document.activeElement;
            if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
            e.preventDefault();
            this.step(e.shiftKey ? 'redo' : 'undo');
        });

        this.setupDragAnnotate();
        this.setupAddText();
    },

    /* ---- scanned pages ---- */

    /** A scanned page carries no text objects, so the editor has nothing to
     *  offer until the page has been read. Say so plainly and put the fix
     *  right there, rather than leaving an unclickable page and no reason. */
    async checkScanned() {
        const notice = $('edit-scan-notice');
        if (!notice) return;
        let status;
        try {
            status = await engine.callJSON('page_status', this.view.docId, this.view.page);
        } catch (err) {
            notice.classList.add('hidden');
            return;
        }
        this.pageStatus = status;
        // Editing a scanned page has to paint over pixels, not just swap
        // glyphs: the words you see are in the image, and the text layer OCR
        // adds is invisible.
        this.coverPixels = !!status.ocrLayer;

        if (status.scanned) {
            $('edit-scan-title').textContent =
                `Page ${this.view.page + 1} is a scan.`;
            $('edit-scan-detail').textContent =
                'Its words are part of an image, so there is nothing to tap yet. ' +
                'Reading the page finds the words and makes them editable.';
            $('edit-scan-ocr').textContent = 'Make this page editable';
            notice.classList.remove('hidden');
        } else if (status.ocrLayer) {
            $('edit-scan-title').textContent = 'Read from a scan.';
            $('edit-scan-detail').textContent =
                'Editing here paints over the scanned image and writes the new ' +
                'wording in its place, so what you see is what the file says.';
            $('edit-scan-ocr').textContent = 'Read again';
            notice.classList.remove('hidden');
        } else {
            notice.classList.add('hidden');
        }
    },

    async ocrCurrent() {
        const all = $('edit-scan-all').checked;
        const pages = all
            ? Array.from({ length: this.view.pages }, (_, i) => i)
            : [this.view.page];

        const bar = $('edit-scan-bar');
        const status = $('edit-scan-status');
        const btn = $('edit-scan-ocr');
        bar.classList.remove('hidden');
        status.classList.remove('hidden');
        btn.disabled = true;
        const setProgress = (pct, label) => {
            $('edit-scan-fill').style.width = `${pct}%`;
            $('edit-scan-pct').textContent = `${pct}%`;
            if (label) status.textContent = label;
        };

        try {
            await this.mark();
            const res = await recognisePages(this.view.docId, pages,
                                             $('edit-scan-lang').value, setProgress,
                                             $('edit-scan-upright').checked);
            await this.view.render();
            await this.refreshHistory();
            const note = res.uncertain
                ? ` ${res.uncertain} word(s) were hard to read and are marked in amber.`
                : '';
            status.textContent = `${res.pages} page(s) read - tap any line to edit it.${note}`;
            UI.toast(`Read ${res.pages} page(s) - the text is editable now`, 'success');
        } catch (err) {
            console.error(err);
            status.textContent = `Could not read the page: ${UI.explain(err)}`;
            UI.toast('Reading the page failed', 'error');
        } finally {
            btn.disabled = false;
            bar.classList.add('hidden');
        }
    },

    /* ---- undo / redo ---- */

    /** Take a snapshot before a change, so it can be stepped back. */
    mark() { return this.history.mark(); },

    refreshHistory() { return this.history.refresh(); },

    step(direction) {
        // An open inline editor is holding text that the step is about to
        // replace underneath it.
        if (this.editing) this.cancelEdit();
        return this.history.step(direction);
    },

    /* ---- adding new text ---- */

    setupAddText() {
        this.view.overlay.addEventListener('click', (e) => {
            if (this.mode !== 'ADD' || this.editing) return;
            if (e.target !== this.view.overlay) return;   // ignore existing spans
            const at = this.view.fracFromEvent(e);
            this.beginAdd(at.x, at.y);
        });
    },

    /** An empty box placed where the page was clicked, styled to match what
     *  will be written, so adding text looks the same as editing it. */
    beginAdd(xFrac, yFrac) {
        if (this.editing) this.commitEdit();
        const img = this.view.img;
        const pageWidth = this.view.info.sizes[this.view.page].width;
        const scale = img.clientWidth / pageWidth;
        const size = Number($('edit-add-size').value) || 12;

        const el = document.createElement('div');
        el.className = 'inline-edit';
        try { el.contentEditable = 'plaintext-only'; } catch (err) { /* fall through */ }
        if (el.contentEditable !== 'plaintext-only') el.contentEditable = 'true';
        el.spellcheck = false;
        el.dataset.placeholder = 'Type here…';
        el.style.left = `${xFrac * 100}%`;
        el.style.top = `${yFrac * 100}%`;
        el.style.width = `${Math.max(10, (1 - xFrac) * 100 - 2)}%`;
        el.style.fontSize = `${Math.max(size * scale, 7)}px`;
        el.style.lineHeight = '1.25';
        el.style.color = $('edit-add-color').value;
        el.style.fontWeight = $('edit-add-bold').checked ? '700' : '400';
        el.style.fontStyle = $('edit-add-italic').checked ? 'italic' : 'normal';

        const tag = document.createElement('div');
        tag.className = 'inline-edit__tag';
        tag.style.left = `${xFrac * 100}%`;
        tag.style.top = `${yFrac * 100}%`;
        tag.textContent = `New text · ${size}pt - Enter to add, Esc to cancel`;

        el.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') { e.preventDefault(); this.cancelEdit(); return; }
            if (e.key !== 'Enter' || e.shiftKey) return;   // Shift+Enter breaks the line
            e.preventDefault();
            this.commitAdd();
        });
        el.addEventListener('paste', (e) => {
            e.preventDefault();
            const text = (e.clipboardData || window.clipboardData).getData('text');
            document.execCommand('insertText', false, text);
        });
        el.addEventListener('blur', () => this.commitAdd());

        this.editing = { adding: true, el, tag, box: null, done: false, xFrac, yFrac, size };
        this.view.overlay.append(el, tag);
        el.focus({ preventScroll: true });
    },

    async commitAdd() {
        const ctx = this.editing;
        if (!ctx || ctx.done || !ctx.adding) return;
        ctx.done = true;
        const text = ctx.el.innerText.replace(/ /g, ' ').replace(/\s+$/, '');
        const widthFrac = ctx.el.offsetWidth / this.view.overlay.clientWidth;
        this.closeEditor();
        if (!text.trim()) return;

        await UI.run('Adding text…', async () => {
            await this.mark();
            const res = await engine.callJSON(
                'insert_text', this.view.docId, this.view.page, ctx.xFrac, ctx.yFrac,
                text, ctx.size, $('edit-add-color').value,
                $('edit-add-bold').checked, $('edit-add-italic').checked, widthFrac);
            if (!res.ok) return UI.toast(res.reason || 'Could not add the text', 'error');
            await this.view.render();
            await this.refreshHistory();
            UI.toast('Text added to the page', 'success');
        });
    },

    async open(file) {
        if (!file) return;
        this.fontCache = new Map();   // fonts are extracted from the open document
        await UI.run('Opening document…', async () => {
            const info = await this.view.load(file);
            $('edit-doc-info').innerHTML =
                `<strong>${file.name}</strong><br>${info.pages} page(s) · ${formatSize(file.size)}`;
            $('outline-page').max = info.pages;
            this.view.zoom = 0;
            this.view.applyZoom();
            await this.refreshAnnots();
            await this.loadOutline();
            await this.refreshHistory();
        });
    },

    async drawSpans() {
        const overlay = this.view.overlay;
        overlay.innerHTML = '';
        await this.checkScanned();
        if (this.mode !== 'TEXT' && this.mode !== 'BLOCK') return;

        // Line mode edits one span; paragraph mode edits a whole block and reflows.
        const isBlock = this.mode === 'BLOCK';
        this.spans = await engine.callJSON(isBlock ? 'get_blocks' : 'get_spans',
                                           this.view.docId, this.view.page);
        // Words the reader was unsure of, so they can be pointed at rather
        // than sitting on the page looking as settled as everything else.
        let doubts = [];
        try {
            doubts = await engine.callJSON('ocr_doubts', this.view.docId, this.view.page);
        } catch (err) { /* nothing recorded for this page */ }
        this.spans.forEach((item, index) => {
            const el = document.createElement('div');
            el.className = isBlock ? 'span-box span-box--block' : 'span-box';
            // Overlap test against the low-confidence list: a doubtful word
            // marks the span it sits in.
            const shaky = doubts.find((d) =>
                d.xFrac < item.xFrac + item.wFrac && d.xFrac + d.wFrac > item.xFrac &&
                d.yFrac < item.yFrac + item.hFrac && d.yFrac + d.hFrac > item.yFrac);
            if (shaky) el.classList.add('span-box--doubt');
            el.style.cssText = `left:${item.xFrac * 100}%;top:${item.yFrac * 100}%;` +
                               `width:${item.wFrac * 100}%;height:${item.hFrac * 100}%`;
            el.title = shaky
                ? `Read as "${shaky.text}" but only ${shaky.conf}% sure - worth checking`
                : isBlock
                    ? `Paragraph · ${item.lines} line(s) - tap to rewrite in place`
                    : `${item.font} ${item.size}pt - tap to edit in place`;
            el.addEventListener('click', (e) => this.beginEdit(index, el, e));
            overlay.appendChild(el);
        });
        this.drawSearchMarks();
    },

    /* ---- in-place text editing ---- */

    /** The face to draw the editor in, matching the page as closely as possible.
     *
     * The document's own embedded font is used when the browser can render it,
     * so the text you type on screen is the text you get in the PDF. Otherwise
     * a lookalike stack stands in.
     */
    async fontFor(item) {
        const key = `${item.font}|${item.flags}`;
        if (this.fontCache.has(key)) return this.fontCache.get(key);

        const look = lookalikeFont(item.font, item.flags);
        let spec = { ...look, exact: false };
        try {
            const bytes = await engine.call('get_web_font', this.view.docId,
                                            this.view.page, item.font || '');
            if (bytes && bytes.length) {
                const family = `MPDFface${this.fontCache.size}`;
                const face = new FontFace(family, bytes);
                await face.load();
                document.fonts.add(face);
                // An embedded face already carries its own weight and slant,
                // so asking for synthetic bold/italic on top would double it.
                spec = { family: `"${family}", ${look.family}`, weight: '400',
                         style: 'normal', exact: true };
            }
        } catch (err) {
            console.warn('Embedded font unavailable - using a lookalike', err);
        }
        this.fontCache.set(key, spec);
        return spec;
    },

    /** Open an editable box sitting exactly where the text is. */
    async beginEdit(index, box, event) {
        if (this.editing) this.commitEdit();
        const item = this.spans[index];
        const isBlock = this.mode === 'BLOCK';
        const img = this.view.img;
        const scale = img.clientWidth / this.view.info.sizes[this.view.page].width;
        const spec = await this.fontFor(item);

        const el = document.createElement('div');
        el.className = isBlock ? 'inline-edit inline-edit--block' : 'inline-edit';
        // plaintext-only keeps pasted markup out; older browsers get plain
        // contenteditable plus the paste handler below.
        try { el.contentEditable = 'plaintext-only'; } catch (err) { /* fall through */ }
        if (el.contentEditable !== 'plaintext-only') el.contentEditable = 'true';
        el.spellcheck = false;
        el.textContent = item.text;

        el.style.left = `${item.xFrac * 100}%`;
        el.style.top = `${item.yFrac * 100}%`;
        el.style.width = `${item.wFrac * 100}%`;
        el.style.fontFamily = spec.family;
        el.style.fontWeight = spec.weight;
        el.style.fontStyle = spec.style;
        el.style.fontSize = `${Math.max(item.size * scale, 7)}px`;
        el.style.color = intToCss(item.colorInt);
        const boxHeight = item.hFrac * img.clientHeight;
        if (isBlock) {
            el.style.minHeight = `${boxHeight}px`;
            el.style.lineHeight = `${(boxHeight / Math.max(item.lines || 1, 1)).toFixed(2)}px`;
        } else {
            // One line: matching line-height to the box centres it on the original.
            el.style.height = `${boxHeight}px`;
            el.style.lineHeight = `${boxHeight}px`;
        }

        const tag = document.createElement('div');
        tag.className = 'inline-edit__tag';
        tag.style.left = `${item.xFrac * 100}%`;
        tag.style.top = `${item.yFrac * 100}%`;
        tag.textContent = `${item.font} · ${item.size}pt`
            + (spec.exact ? '' : ' · lookalike')
            + (isBlock ? ' - Ctrl+Enter to apply, Esc to cancel'
                       : ' - Enter to apply, Esc to cancel');

        el.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') { e.preventDefault(); this.cancelEdit(); return; }
            if (e.key !== 'Enter') return;
            // A paragraph can hold line breaks, so it commits deliberately.
            if (isBlock && !(e.ctrlKey || e.metaKey)) return;
            e.preventDefault();
            this.commitEdit();
        });
        el.addEventListener('paste', (e) => {
            e.preventDefault();
            const text = (e.clipboardData || window.clipboardData).getData('text');
            document.execCommand('insertText', false, text);
        });
        el.addEventListener('blur', () => this.commitEdit());

        box.style.visibility = 'hidden';
        this.editing = { item, isBlock, el, tag, box, done: false };
        this.view.overlay.append(el, tag);

        el.focus({ preventScroll: true });
        this.placeCaret(el, event);
        el.scrollIntoView({ block: 'center', inline: 'nearest' });
    },

    /** Put the caret where the page was tapped, rather than at the start. */
    placeCaret(el, event) {
        const sel = window.getSelection();
        if (!sel) return;
        let range = null;
        if (event && document.caretRangeFromPoint) {
            range = document.caretRangeFromPoint(event.clientX, event.clientY);
        } else if (event && document.caretPositionFromPoint) {
            const pos = document.caretPositionFromPoint(event.clientX, event.clientY);
            if (pos) {
                range = document.createRange();
                range.setStart(pos.offsetNode, pos.offset);
            }
        }
        if (!range || !el.contains(range.startContainer)) {
            range = document.createRange();
            range.selectNodeContents(el);
        }
        range.collapse(true);
        sel.removeAllRanges();
        sel.addRange(range);
    },

    cancelEdit() {
        if (!this.editing) return;
        this.editing.done = true;
        this.closeEditor();
    },

    closeEditor() {
        const ctx = this.editing;
        this.editing = null;
        if (!ctx) return;
        ctx.el.remove();
        ctx.tag.remove();
        if (ctx.box) ctx.box.style.visibility = '';
    },

    /** Write what was typed back into the PDF itself. */
    async commitEdit() {
        const ctx = this.editing;
        if (!ctx || ctx.done) return;
        if (ctx.adding) return this.commitAdd();
        ctx.done = true;

        const raw = ctx.el.innerText.replace(/\u00a0/g, ' ').replace(/\s+$/, '');
        // A span is a single line, so anything typed across lines is joined.
        const next = ctx.isBlock ? raw : raw.replace(/\s*\n\s*/g, ' ');
        const item = ctx.item;
        const isBlock = ctx.isBlock;
        this.closeEditor();
        if (next === item.text) return;

        // Emptying the box deletes the text. This used to do nothing at all,
        // silently, leaving no way to remove a line. Deletion is worth
        // confirming, because the glyphs really are removed from the file.
        const deleting = !next.trim();
        if (deleting) {
            const what = isBlock ? 'this paragraph' : `"${item.text.slice(0, 60)}"`;
            if (!confirm(`Delete ${what} from the page?`)) {
                await this.view.render();
                return;
            }
        }

        await UI.run(deleting ? 'Deleting text…'
                              : (isBlock ? 'Rewriting paragraph…' : 'Replacing text…'), async () => {
            await this.mark();
            // On a page read from a scan the visible words are pixels, so the
            // region has to be painted over before the new wording is drawn.
            const res = await engine.callJSON(isBlock ? 'edit_block' : 'edit_text',
                                              this.view.docId, this.view.page, item.bbox,
                                              next, item.font, item.size,
                                              item.colorInt, item.flags,
                                              ...(isBlock ? [true, this.coverPixels]
                                                          : [this.coverPixels]));
            await this.view.render();
            await this.refreshHistory();
            if (deleting) return UI.toast('Text deleted from the page', 'success');
            const face = res.exact
                ? `kept ${item.font}`
                : 'matched with a similar font';
            UI.toast(isBlock
                ? (res.grew ? `Paragraph rewritten at ${res.size}pt, box grew to fit - ${face}`
                            : `Paragraph rewrapped at ${res.size}pt - ${face}`)
                : `Text replaced - ${face}`, 'success');
        });
    },

    /* ---- bookmarks / outline ---- */

    async loadOutline() {
        this.outline = await engine.callJSON('get_outline', this.view.docId);
        this.renderOutline();
    },

    renderOutline() {
        const box = $('edit-outline');
        if (!box) return;
        box.innerHTML = this.outline.length
            ? this.outline.map((row, i) => `
                <div class="outline-row" style="padding-left:${(row[0] - 1) * 14}px">
                    <span class="outline-title">${String(row[1]).replace(/[<>&]/g, '')}</span>
                    <span class="muted">p${row[2]}</span>
                    <button class="outline-del" data-out="${i}" title="Remove">✕</button>
                </div>`).join('')
            : '<p class="muted">No bookmarks yet.</p>';
        $$('.outline-del', box).forEach((b) => b.addEventListener('click', () => {
            this.outline.splice(Number(b.dataset.out), 1);
            this.renderOutline();
        }));
    },

    /** Drag a rectangle to place an annotation. */
    setupDragAnnotate() {
        const overlay = this.view.overlay;
        let start = null;
        let ghost = null;

        overlay.addEventListener('mousedown', (e) => {
            if (this.mode === 'TEXT') return;
            start = this.view.fracFromEvent(e);
            ghost = document.createElement('div');
            ghost.className = 'draw-ghost';
            overlay.appendChild(ghost);
        });
        overlay.addEventListener('mousemove', (e) => {
            if (!start || !ghost) return;
            const now = this.view.fracFromEvent(e);
            Object.assign(ghost.style, {
                left: `${Math.min(start.x, now.x) * 100}%`,
                top: `${Math.min(start.y, now.y) * 100}%`,
                width: `${Math.abs(now.x - start.x) * 100}%`,
                height: `${Math.abs(now.y - start.y) * 100}%`,
            });
        });
        window.addEventListener('mouseup', async (e) => {
            if (!start) return;
            const end = this.view.fracFromEvent(e);
            const from = start;
            start = null;
            if (ghost) { ghost.remove(); ghost = null; }
            if (Math.abs(end.x - from.x) < 0.01 || Math.abs(end.y - from.y) < 0.005) return;

            const needsText = this.mode === 'note' || this.mode === 'freetext';
            const text = needsText ? (prompt('Comment text:') || '') : '';
            await UI.run('Adding annotation…', async () => {
                await this.mark();
                await engine.call('annotate', this.view.docId, this.view.page, this.mode,
                                  from.x, from.y, end.x, end.y, text, $('edit-annot-color').value);
                await this.view.render();
                await this.refreshAnnots();
            });
        });
    },

    async refreshAnnots() {
        const list = await engine.callJSON('list_annotations', this.view.docId);
        $('edit-annot-list').innerHTML = list.length
            ? `<h4 class="side-subtitle">Comments (${list.length})</h4>` + list.map((a) =>
                `<div class="annot-item"><strong>${a.type}</strong> · p${a.page + 1}
                 <div class="muted">${a.content || '-'}</div></div>`).join('')
            : '';
    },

    async search() {
        const term = $('edit-search').value.trim();
        if (!term) return;
        const safe = (t) => String(t).replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
        await UI.run('Searching…', async () => {
            this.hits = await engine.callJSON('search_text', this.view.docId, term);
            const box = $('edit-search-results');
            if (!this.hits.length) {
                box.innerHTML = '<span class="muted">No matches</span>';
                this.drawSpans();
                return;
            }
            // Every hit shows the line it sits on, so you can tell which of
            // seven "NEFT" on page one is the one you are after.
            box.innerHTML =
                `<p class="muted search-count">${this.hits.length} match(es)</p>` +
                this.hits.map((h, i) => `
                    <button class="search-hit" data-hit="${i}">
                        <span class="search-hit__page">p${h.page + 1}</span>
                        <span class="search-hit__text">${safe(h.before)}<mark>${safe(h.hit)}</mark>${safe(h.after)}</span>
                    </button>`).join('');
            $$('.search-hit', box).forEach((b) =>
                b.addEventListener('click', () => this.goToHit(Number(b.dataset.hit))));
            this.drawSpans();
        });
    },

    /** Go to a hit and show where it is, rather than only which page. */
    async goToHit(index) {
        const hit = this.hits && this.hits[index];
        if (!hit) return;
        if (hit.page !== this.view.page) await this.view.go(hit.page);
        this.current = index;
        this.drawSpans();
        const mark = $$('#edit-overlay .search-mark')[0];
        if (mark) mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
    },

    /** Boxes over the hits on the page being viewed. */
    drawSearchMarks() {
        if (!this.hits || !this.hits.length) return;
        const overlay = this.view.overlay;
        this.hits.forEach((h, i) => {
            if (h.page !== this.view.page) return;
            const el = document.createElement('div');
            el.className = 'search-mark' + (i === this.current ? ' search-mark--current' : '');
            el.style.cssText = `left:${h.xFrac * 100}%;top:${h.yFrac * 100}%;` +
                               `width:${h.wFrac * 100}%;height:${h.hFrac * 100}%`;
            overlay.appendChild(el);
        });
    },
};

/* ------------------------------------------------------------------ */
/* 2. Pages - merge / split / organise                                 */
/* ------------------------------------------------------------------ */

const PagesTool = {
    order: [],
    rotations: {},
    selected: new Set(),
    past: [],
    futures: [],

    /* Undo here works differently from the other tabs, and cheaply. Nothing
     * is written into the document until Save: deleting, reordering and
     * rotating only change which pages this tab intends to keep and how. So
     * a step back is a copy of that intention, not a copy of the file - no
     * megabytes per step, and no engine call. */
    STEPS: 40,

    /** Record the arrangement before changing it. */
    snap() {
        this.past.push({ order: [...this.order], rotations: { ...this.rotations } });
        if (this.past.length > this.STEPS) this.past.shift();
        this.futures = [];
        Dirty.touch('pages');
        this.refreshSteps();
    },

    refreshSteps() {
        $('pages-undo').disabled = !this.past.length;
        $('pages-redo').disabled = !this.futures.length;
    },

    async step(direction) {
        const from = direction === 'undo' ? this.past : this.futures;
        const to = direction === 'undo' ? this.futures : this.past;
        if (!from.length) {
            return UI.toast(direction === 'undo' ? 'Nothing left to undo' : 'Nothing to redo', 'error');
        }
        to.push({ order: [...this.order], rotations: { ...this.rotations } });
        const state = from.pop();
        this.order = state.order;
        this.rotations = state.rotations;
        // A page brought back by an undo cannot stay selected as if it had
        // never left, so the selection starts clean after a step.
        this.selected.clear();
        this.refreshSteps();
        if (this.past.length) Dirty.touch('pages'); else Dirty.clear('pages');
        await UI.run(direction === 'undo' ? 'Undoing…' : 'Redoing…', () => this.renderGrid());
        UI.toast(direction === 'undo' ? 'Change undone' : 'Change redone', 'success');
    },

    init() {
        $('pages-undo').addEventListener('click', () => this.step('undo'));
        $('pages-redo').addEventListener('click', () => this.step('redo'));
        document.addEventListener('keydown', (e) => {
            if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 'z') return;
            if (!$('pages-tab').classList.contains('active')) return;
            const el = document.activeElement;
            if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
            e.preventDefault();
            this.step(e.shiftKey ? 'redo' : 'undo');
        });
        $('pages-file').addEventListener('change', (e) => this.open(Array.from(e.target.files)));
        $('pages-rotate-left').addEventListener('click', () => this.rotate(-90));
        $('pages-rotate-right').addEventListener('click', () => this.rotate(90));
        $('pages-delete').addEventListener('click', () => this.remove());
        $('pages-select-all').addEventListener('click', () => this.selectAll(true));
        $('pages-deselect').addEventListener('click', () => this.selectAll(false));
        $('pages-save').addEventListener('click', () => this.save());
        $('pages-split-mode').addEventListener('change', (e) => {
            $('pages-split-every').classList.toggle('hidden', e.target.value !== 'every');
            $('pages-split-ranges').classList.toggle('hidden', e.target.value !== 'ranges');
        });
    },

    async open(files) {
        if (!files.length) return;
        await UI.run('Loading pages…', async () => {
            // Multiple files are merged first, then organised as one document.
            const ids = [];
            for (let i = 0; i < files.length; i++) {
                const id = `pages_src_${i}`;
                await engine.openDoc(id, await fileToBytes(files[i]), files[i].name);
                ids.push(id);
            }
            // Merging is used even for a single file: it yields a plain copy of
            // the already-open document, so a protected file is not re-read
            // from its still-encrypted bytes and asked about a second time.
            const merged = await engine.call('merge', JSON.stringify(ids));
            await engine.call('open_doc', 'pages', merged);
            for (const id of ids) await engine.call('close_doc', id);

            const info = await engine.callJSON('doc_info', 'pages');
            this.order = Array.from({ length: info.pages }, (_, i) => i);
            this.rotations = {};
            this.selected.clear();
            this.past = [];
            this.futures = [];
            this.refreshSteps();
            Dirty.clear('pages');
            $('pages-workspace').classList.remove('hidden');
            await this.renderGrid();
            UI.toast(`${info.pages} pages loaded`, 'success');
        });
    },

    async renderGrid() {
        const grid = $('pages-grid');
        grid.innerHTML = '';
        for (const original of this.order) {
            const png = await engine.call('render_page', 'pages', original, 36);
            const url = URL.createObjectURL(new Blob([png], { type: 'image/png' }));
            const card = document.createElement('div');
            card.className = 'page-card' + (this.selected.has(original) ? ' selected' : '');
            card.draggable = true;
            card.dataset.original = original;
            card.innerHTML = `<img src="${url}" style="transform:rotate(${this.rotations[original] || 0}deg)">
                              <div class="page-card__label">Page ${original + 1}</div>`;
            card.addEventListener('click', () => {
                if (this.selected.has(original)) this.selected.delete(original);
                else this.selected.add(original);
                card.classList.toggle('selected');
            });
            card.addEventListener('dragstart', () => { this._drag = original; card.classList.add('dragging'); });
            card.addEventListener('dragend', () => card.classList.remove('dragging'));
            card.addEventListener('dragover', (e) => e.preventDefault());
            card.addEventListener('drop', (e) => {
                e.preventDefault();
                const from = this.order.indexOf(this._drag);
                const to = this.order.indexOf(original);
                if (from < 0 || to < 0 || from === to) return;
                this.snap();
                this.order.splice(to, 0, ...this.order.splice(from, 1));
                this.renderGrid();
            });
            grid.appendChild(card);
        }
    },

    rotate(delta) {
        if (!this.selected.size) return UI.toast('Select pages first', 'error');
        this.snap();
        this.selected.forEach((p) => {
            this.rotations[p] = ((this.rotations[p] || 0) + delta + 360) % 360;
        });
        this.renderGrid();
    },

    remove() {
        if (!this.selected.size) return UI.toast('Select pages first', 'error');
        this.snap();
        this.order = this.order.filter((p) => !this.selected.has(p));
        this.selected.clear();
        this.renderGrid();
    },

    selectAll(on) {
        this.selected = on ? new Set(this.order) : new Set();
        this.renderGrid();
    },

    async save() {
        if (!this.order.length) return UI.toast('No pages left to save', 'error');
        await UI.run('Building PDF…', async () => {
            const bytes = await engine.call('organize', 'pages',
                                            JSON.stringify(this.order), JSON.stringify(this.rotations));
            const mode = $('pages-split-mode').value;
            if (!mode) { download(bytes, 'organized.pdf'); Dirty.clear('pages'); return; }

            await engine.call('open_doc', 'pages_out', bytes);
            const parts = await engine.callJSON('split', 'pages_out', mode,
                                                $('pages-split-ranges').value,
                                                Number($('pages-split-every').value || 1));
            const zip = new JSZip();
            parts.forEach((p) => zip.file(p.name, b64ToBytes(p.b64)));
            download(await zip.generateAsync({ type: 'blob' }), 'split-pages.zip', 'application/zip');
            Dirty.clear('pages');
            UI.toast(`Split into ${parts.length} files`, 'success');
        });
    },
};

/* ------------------------------------------------------------------ */
/* 3. Fill & sign                                                      */
/* ------------------------------------------------------------------ */

const SignTool = {
    tool: 'SIGNATURE',
    items: [],
    signature: null,
    initials: null,
    target: 'SIGNATURE',

    init() {
        this.history = null;
        this.view = new DocView('sign', { onRender: () => this.drawItems() });
        this.history = makeHistory('sign', this.view);
        this.setupDsc();
        $('sign-file').addEventListener('change', (e) => this.open(e.target.files[0]));

        $$('[data-signtool]').forEach((btn) => btn.addEventListener('click', () => {
            const next = btn.dataset.signtool;
            const isSig = next === 'SIGNATURE' || next === 'INITIALS';
            if (isSig && this.tool === next) return this.openModal(next);
            this.tool = next;
            $$('[data-signtool]').forEach((b) => b.classList.toggle('active', b === btn));
        }));

        this.view.overlay.addEventListener('click', (e) => {
            if (e.target === this.view.overlay) this.place(e);
        });
        $('sign-clear').addEventListener('click', () => { this.items = []; this.drawItems(); });
        $('sign-save').addEventListener('click', () => this.save());
        this.setupModal();
    },

    async open(file) {
        if (!file) return;
        await UI.run('Opening document…', async () => {
            await this.view.load(file);
            this.items = [];
            const fields = await engine.callJSON('get_form_fields', 'sign');
            this.fields = fields;
            $('sign-fields').innerHTML = fields.length
                ? fields.map((f, i) => `
                    <div class="form-group">
                        <label class="form-label">${f.name}</label>
                        ${f.kind === 'checkbox'
                            ? `<input type="checkbox" data-field="${i}">`
                            : f.kind === 'select'
                            ? `<select class="form-control" data-field="${i}"><option value=""></option>${
                                f.options.map((o) => `<option>${o}</option>`).join('')}</select>`
                            : `<input type="text" class="form-control" data-field="${i}" value="${f.value || ''}">`}
                    </div>`).join('')
                : '<p class="muted">No fillable fields detected.</p>';
        });
    },

    place(e) {
        const { x, y } = this.view.fracFromEvent(e);
        const size = this.view.info.sizes[this.view.page];
        const add = (item) => { this.items.push(item); this.drawItems(); };

        if (this.tool === 'SIGNATURE' || this.tool === 'INITIALS') {
            const img = this.tool === 'SIGNATURE' ? this.signature : this.initials;
            if (!img) return this.openModal(this.tool);
            const wFrac = Math.min(0.32, 170 / size.width);
            const hFrac = (wFrac * size.width) * (img.h / img.w) / size.height;
            return add({ page: this.view.page, dataUrl: img.dataUrl, text: '',
                         xFrac: x - wFrac / 2, yFrac: y - hFrac / 2, wFrac, hFrac });
        }

        const text = this.tool === 'TEXT' ? (prompt('Enter text:') || '')
                   : this.tool === 'DATE' ? new Date().toLocaleDateString()
                   : this.tool === 'CHECK' ? '✔' : '✗';
        if (!text) return;
        const fontSize = this.tool === 'CHECK' || this.tool === 'CROSS' ? 22 : 14;
        const wFrac = Math.max(0.06, (text.length * fontSize * 0.55) / size.width);
        const hFrac = (fontSize * 1.5) / size.height;
        add({ page: this.view.page, text, size: fontSize, color: '#12123a', dataUrl: '',
              xFrac: x - wFrac / 2, yFrac: y - hFrac / 2, wFrac, hFrac });
    },

    drawItems() {
        const overlay = this.view.overlay;
        overlay.innerHTML = '';
        this.items.filter((i) => i.page === this.view.page).forEach((item) => {
            const el = document.createElement('div');
            el.className = 'placed-item';
            el.style.cssText = `left:${item.xFrac * 100}%;top:${item.yFrac * 100}%;` +
                               `width:${item.wFrac * 100}%;height:${item.hFrac * 100}%`;
            el.innerHTML = item.dataUrl
                ? `<img src="${item.dataUrl}">`
                : `<span style="color:${item.color}">${item.text}</span>`;

            const del = document.createElement('button');
            del.className = 'placed-item__del';
            del.textContent = '✕';
            del.addEventListener('click', (e) => {
                e.stopPropagation();
                this.items = this.items.filter((i) => i !== item);
                this.drawItems();
            });
            el.appendChild(del);

            el.addEventListener('mousedown', (e) => {
                e.preventDefault();
                e.stopPropagation();
                const rect = overlay.getBoundingClientRect();
                const ox = e.clientX; const oy = e.clientY;
                const sx = item.xFrac; const sy = item.yFrac;
                const move = (ev) => {
                    item.xFrac = Math.min(Math.max(sx + (ev.clientX - ox) / rect.width, 0), 1 - item.wFrac);
                    item.yFrac = Math.min(Math.max(sy + (ev.clientY - oy) / rect.height, 0), 1 - item.hFrac);
                    el.style.left = `${item.xFrac * 100}%`;
                    el.style.top = `${item.yFrac * 100}%`;
                };
                const up = () => {
                    window.removeEventListener('mousemove', move);
                    window.removeEventListener('mouseup', up);
                };
                window.addEventListener('mousemove', move);
                window.addEventListener('mouseup', up);
            });
            overlay.appendChild(el);
        });
    },

    setupModal() {
        $$('[data-sigclose]').forEach((el) => el.addEventListener('click', () => $('sig-modal').classList.add('hidden')));
        $$('[data-sigmode]').forEach((btn) => btn.addEventListener('click', () => {
            $$('[data-sigmode]').forEach((b) => b.classList.toggle('active', b === btn));
            $$('[data-sigpanel]').forEach((p) => p.classList.toggle('hidden', p.dataset.sigpanel !== btn.dataset.sigmode));
        }));

        const canvas = $('sig-canvas');
        const ctx = canvas.getContext('2d');
        let drawing = false;
        this.drew = false;
        const pos = (e) => {
            const r = canvas.getBoundingClientRect();
            const cx = e.touches ? e.touches[0].clientX : e.clientX;
            const cy = e.touches ? e.touches[0].clientY : e.clientY;
            return { x: (cx - r.left) * (canvas.width / r.width), y: (cy - r.top) * (canvas.height / r.height) };
        };
        const start = (e) => {
            e.preventDefault();
            drawing = true; this.drew = true;
            const p = pos(e);
            ctx.strokeStyle = $('sig-color').value;
            ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
            ctx.beginPath(); ctx.moveTo(p.x, p.y);
        };
        const move = (e) => { if (!drawing) return; e.preventDefault(); const p = pos(e); ctx.lineTo(p.x, p.y); ctx.stroke(); };
        const end = () => { drawing = false; };
        canvas.addEventListener('mousedown', start);
        canvas.addEventListener('mousemove', move);
        window.addEventListener('mouseup', end);
        canvas.addEventListener('touchstart', start, { passive: false });
        canvas.addEventListener('touchmove', move, { passive: false });
        canvas.addEventListener('touchend', end);
        $('sig-clear').addEventListener('click', () => { ctx.clearRect(0, 0, canvas.width, canvas.height); this.drew = false; });

        const renderTyped = () => {
            const c = $('sig-type-canvas');
            const cx = c.getContext('2d');
            cx.clearRect(0, 0, c.width, c.height);
            const text = $('sig-typed').value.trim();
            if (!text) return;
            cx.fillStyle = $('sig-color').value;
            cx.font = `64px ${$('sig-font').value}`;
            cx.textAlign = 'center'; cx.textBaseline = 'middle';
            cx.fillText(text, c.width / 2, c.height / 2, c.width - 40);
        };
        $('sig-typed').addEventListener('input', renderTyped);
        $('sig-font').addEventListener('change', renderTyped);

        $('sig-upload').addEventListener('change', (e) => {
            const file = e.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = (ev) => {
                this.uploaded = ev.target.result;
                const c = $('sig-upload-canvas');
                const cx = c.getContext('2d');
                const img = new Image();
                img.onload = () => {
                    cx.clearRect(0, 0, c.width, c.height);
                    const s = Math.min(c.width / img.width, c.height / img.height);
                    cx.drawImage(img, (c.width - img.width * s) / 2, (c.height - img.height * s) / 2,
                                 img.width * s, img.height * s);
                };
                img.src = this.uploaded;
            };
            reader.readAsDataURL(file);
        });

        $('sig-use').addEventListener('click', () => this.commit());
    },

    openModal(target) {
        this.target = target;
        $('sig-title').textContent = target === 'INITIALS' ? 'Create Initials' : 'Create Signature';
        const c = $('sig-canvas');
        c.getContext('2d').clearRect(0, 0, c.width, c.height);
        this.drew = false;
        this.uploaded = null;
        $('sig-typed').value = '';
        [$('sig-type-canvas'), $('sig-upload-canvas')].forEach((cv) =>
            cv.getContext('2d').clearRect(0, 0, cv.width, cv.height));
        $('sig-modal').classList.remove('hidden');
    },

    commit() {
        const mode = document.querySelector('[data-sigmode].active').dataset.sigmode;
        let dataUrl = null;
        if (mode === 'draw') {
            if (!this.drew) return UI.toast('Draw your signature first', 'error');
            dataUrl = $('sig-canvas').toDataURL('image/png');
        } else if (mode === 'type') {
            if (!$('sig-typed').value.trim()) return UI.toast('Type your name first', 'error');
            dataUrl = $('sig-type-canvas').toDataURL('image/png');
        } else {
            if (!this.uploaded) return UI.toast('Choose an image first', 'error');
            dataUrl = this.uploaded;
        }
        const img = new Image();
        img.onload = () => {
            const payload = { dataUrl, w: img.naturalWidth || 300, h: img.naturalHeight || 100 };
            if (this.target === 'INITIALS') this.initials = payload; else this.signature = payload;
            this.tool = this.target;
            $$('[data-signtool]').forEach((b) => b.classList.toggle('active', b.dataset.signtool === this.target));
            $('sig-modal').classList.add('hidden');
            UI.toast('Ready - click the page to place it', 'success');
        };
        img.src = dataUrl;
    },

    /* ---- digital signature (DSC) ----------------------------------------
     *
     * A drawn signature is a picture of one. This is the real thing: the
     * document is sealed with a certificate, so a reader can say who signed
     * it and whether a single byte changed afterwards.
     *
     * The private key never reaches the PDF engine. The engine reserves a
     * slot, says which bytes the signature must cover, and this code signs
     * those bytes here in the browser and hands back the result. The
     * certificate file and its password stay on the device throughout.
     */
    setupDsc() {
        $('dsc-file').addEventListener('change', () => this.readCertificate());
        $('dsc-pass').addEventListener('change', () => this.readCertificate());
        $('dsc-sign').addEventListener('click', () => this.signDigitally());
        $('dsc-visible').addEventListener('change', (e) => {
            $('dsc-place').classList.toggle('hidden', !e.target.checked);
        });
    },

    /** Open the certificate so the signer can be named before signing, and
     *  a wrong password is caught here rather than half way through. */
    async readCertificate() {
        const file = $('dsc-file').files[0];
        const box = $('dsc-who');
        if (!file) { box.classList.add('hidden'); this.dsc = null; return; }
        try {
            const bytes = await fileToBytes(file);
            const p12 = forge.pkcs12.pkcs12FromAsn1(
                forge.asn1.fromDer(forge.util.createBuffer(bytesToBinary(bytes))),
                $('dsc-pass').value);
            const keyBags = p12.getBags({ bagType: forge.pki.oids.pkcs8ShroudedKeyBag });
            const plainBags = p12.getBags({ bagType: forge.pki.oids.keyBag });
            const key = (keyBags[forge.pki.oids.pkcs8ShroudedKeyBag] || [])
                .concat(plainBags[forge.pki.oids.keyBag] || [])
                .map((b) => b.key).find(Boolean);
            const certs = (p12.getBags({ bagType: forge.pki.oids.certBag })[forge.pki.oids.certBag] || [])
                .map((b) => b.cert).filter(Boolean);
            if (!key || !certs.length) throw new Error('No key or certificate in that file');
            // The signing certificate is the one this key belongs to; the
            // others are the chain and are included but not signed with.
            const signer = certs.find((c) => c.publicKey && c.publicKey.n &&
                                             c.publicKey.n.equals(key.n)) || certs[0];
            this.dsc = { key, signer, certs };
            const cn = (signer.subject.getField('CN') || {}).value || 'this certificate';
            box.classList.remove('hidden');
            box.innerHTML = `Will sign as <strong>${cn.replace(/[<>&]/g, '')}</strong>, ` +
                            `valid until ${signer.validity.notAfter.toLocaleDateString()}.`;
        } catch (err) {
            this.dsc = null;
            box.classList.remove('hidden');
            box.innerHTML = /[Ii]nvalid password|MAC/.test(String(err.message || err))
                ? '<span class="warn">That password did not open the certificate file.</span>'
                : `<span class="warn">Could not read that certificate: ${UI.explain(err)}</span>`;
        }
    },

    async signDigitally() {
        if (!this.view.info) return UI.toast('Open a PDF first', 'error');
        if (!this.dsc) {
            await this.readCertificate();
            if (!this.dsc) return UI.toast('Choose a certificate file and its password first', 'error');
        }
        const visible = $('dsc-visible').checked;
        // Fractions of the page, so the box lands in the same place whatever
        // the paper size.
        const corners = {
            tl: { x0: 0.06, y0: 0.05, x1: 0.44, y1: 0.15 },
            tr: { x0: 0.56, y0: 0.05, x1: 0.94, y1: 0.15 },
            bl: { x0: 0.06, y0: 0.84, x1: 0.44, y1: 0.94 },
            br: { x0: 0.56, y0: 0.84, x1: 0.94, y1: 0.94 },
        };
        const box = corners[$('dsc-corner').value] || corners.bl;

        await UI.run('Signing…', async () => {
            const cn = (this.dsc.signer.subject.getField('CN') || {}).value || '';
            await engine.callJSON('prepare_signature', 'sign', this.view.page,
                                  box.x0, box.y0, box.x1, box.y1,
                                  cn, $('dsc-reason').value, $('dsc-location').value, '', visible);
            let signed;
            try {
                const payload = await engine.call('signature_payload', 'sign');
                const p7 = forge.pkcs7.createSignedData();
                p7.content = forge.util.createBuffer(bytesToBinary(payload));
                this.dsc.certs.forEach((c) => p7.addCertificate(c));
                p7.addSigner({
                    key: this.dsc.key,
                    certificate: this.dsc.signer,
                    digestAlgorithm: forge.pki.oids.sha256,
                    authenticatedAttributes: [
                        { type: forge.pki.oids.contentType, value: forge.pki.oids.data },
                        { type: forge.pki.oids.messageDigest },
                        { type: forge.pki.oids.signingTime, value: new Date() },
                    ],
                });
                // Detached: the signature covers the document's bytes without
                // carrying a copy of them.
                p7.sign({ detached: true });
                const der = forge.asn1.toDer(p7.toAsn1()).getBytes();
                signed = await engine.call('finish_signature', 'sign', forge.util.bytesToHex(der));
            } catch (err) {
                await engine.call('cancel_signature', 'sign');
                throw err;
            }
            download(signed, 'signed-digitally.pdf');
            const res = $('dsc-result');
            res.classList.remove('hidden');
            res.innerHTML = `Signed and saved. The file is sealed as it stands now: any later change ` +
                            `to it will show up as a broken signature in a reader.`;
            UI.toast('Digitally signed', 'success');
        });
    },

    async save() {
        await UI.run('Saving signed PDF…', async () => {
            const values = {};
            $$('[data-field]').forEach((input) => {
                const field = this.fields[Number(input.dataset.field)];
                values[field.name] = input.type === 'checkbox' ? input.checked : input.value;
            });
            if (Object.keys(values).length) {
                await this.history.mark();
                await engine.call('fill_form', 'sign', JSON.stringify(values), $('sign-flatten').checked);
            }
            if (this.items.length) {
                await this.history.mark();
                await engine.call('place_items', 'sign', JSON.stringify(this.items));
            }
            await this.view.save('signed.pdf');
            UI.toast('Signed PDF saved', 'success');
        });
    },
};

/* ------------------------------------------------------------------ */
/* 4. Watermark / stamps / numbering / Bates                           */
/* ------------------------------------------------------------------ */

const StampTool = {
    stamps: [],
    active: null,

    init() {
        this.view = new DocView('stamp', { onRender: () => this.drawStamps() });
        this.history = makeHistory('stamp', this.view);
        $('stamp-file').addEventListener('change', (e) => this.open(Array.from(e.target.files)));

        $$('[data-stampsec]').forEach((btn) => btn.addEventListener('click', () => {
            $$('[data-stampsec]').forEach((b) => b.classList.toggle('active', b === btn));
            $$('[data-panel]').forEach((p) => p.classList.toggle('hidden', p.dataset.panel !== btn.dataset.stampsec));
        }));

        $('wm-opacity').addEventListener('input', (e) => { $('wm-op-val').textContent = `${e.target.value}%`; });
        $('wm-rotate').addEventListener('input', (e) => { $('wm-rot-val').textContent = `${e.target.value}°`; });

        $('wm-apply').addEventListener('click', () => this.applyWatermark());
        $('stamp-apply').addEventListener('click', () => this.applyStamps());
        $('stamp-clear').addEventListener('click', () => { this.stamps = []; this.drawStamps(); });
        $('pn-apply').addEventListener('click', () => this.applyNumbers());
        $('hf-apply').addEventListener('click', () => this.applyHeaderFooter());
        $('bates-apply').addEventListener('click', () => this.applyBates());
        // A Bates run is a legal artefact and hard to eyeball from four
        // separate fields, so it is spelled out before it is committed.
        ['bates-prefix', 'bates-suffix', 'bates-start', 'bates-digits'].forEach((id) => {
            $(id).addEventListener('input', () => this.previewBates());
        });
        $('stamp-save').addEventListener('click', () => this.view.save('stamped.pdf'));

        $$('.stamp-btn').forEach((btn) => btn.addEventListener('click', () => {
            this.active = btn.dataset.stamp;
            $$('.stamp-btn').forEach((b) => b.classList.toggle('active', b === btn));
        }));
        $('stamp-custom').addEventListener('change', (e) => {
            if (e.target.value.trim()) this.active = e.target.value.trim().toUpperCase();
        });

        this.view.overlay.addEventListener('click', (e) => {
            if (!this.active || e.target !== this.view.overlay) return;
            const { x, y } = this.view.fracFromEvent(e);
            const size = this.view.info.sizes[this.view.page];
            const wFrac = Math.min(0.45, 170 / size.width);
            const hFrac = 38 / size.height;
            this.stamps.push({ page: this.view.page, text: this.active,
                               xFrac: x - wFrac / 2, yFrac: y - hFrac / 2, wFrac, hFrac });
            this.drawStamps();
        });
    },

    async open(files) {
        const list = Array.isArray(files) ? files : [files].filter(Boolean);
        if (!list.length) return;
        this.files = list;
        this.file = list[0];
        this.stamps = [];
        if (list.length > 1) {
            // Stamps are placed by clicking a page, so that one sub-panel
            // cannot work across files. The other four settings can.
            $('stamp-apply').disabled = true;
            $('stamp-save').disabled = true;
            $('stamp-viewer').classList.add('hidden');
            this.previewBates();
            return batchChosen('stamp', list);
        }
        $('stamp-apply').disabled = false;
        $('stamp-save').disabled = false;
        $('stamp-viewer').classList.remove('hidden');
        $('stamp-summary').classList.add('hidden');
        $('stamp-result').classList.add('hidden');
        await UI.run('Opening document…', async () => {
            await this.view.load(this.file);
            await this.history.refresh();
            this.previewBates();
        });
    },

    /** True when the chosen files are to be treated as a batch. */
    get batching() {
        return Boolean(this.files && this.files.length > 1);
    },

    /** Run one stamping operation over every chosen file.
     *  @param label   what to show while it runs
     *  @param suffix  appended to each output name inside the ZIP
     *  @param op      async () => void, the engine call, document already open
     */
    runOverFiles(label, suffix, op) {
        return UI.run(label, () => runBatch({
            files: this.files, docId: 'stamp', suffix,
            apply: async (info, file) => {
                await op(info, file);
                return engine.call('save', 'stamp');
            },
            report: (html) => batchReport('stamp', html),
        }));
    },

    drawStamps() {
        const overlay = this.view.overlay;
        overlay.innerHTML = '';
        this.stamps.filter((s) => s.page === this.view.page).forEach((s) => {
            const el = document.createElement('div');
            el.className = 'stamp-ghost';
            el.style.cssText = `left:${s.xFrac * 100}%;top:${s.yFrac * 100}%;` +
                               `width:${s.wFrac * 100}%;height:${s.hFrac * 100}%`;
            el.textContent = s.text;
            overlay.appendChild(el);
        });
    },

    async applyWatermark() {
        const watermark = () => engine.call('watermark_text', 'stamp', $('wm-text').value || 'CONFIDENTIAL',
                                            $('wm-pages').value, Number($('wm-size').value),
                                            $('wm-color').value, Number($('wm-opacity').value) / 100,
                                            Number($('wm-rotate').value), $('wm-tiled').checked);
        if (this.batching) return this.runOverFiles('Watermarking files…', 'watermarked', watermark);
        await UI.run('Applying watermark…', async () => {
            await this.history.mark();
            await engine.call('watermark_text', 'stamp', $('wm-text').value || 'CONFIDENTIAL',
                              $('wm-pages').value, Number($('wm-size').value),
                              $('wm-color').value, Number($('wm-opacity').value) / 100,
                              Number($('wm-rotate').value), $('wm-tiled').checked);
            await this.view.render();
            await this.history.refresh();
            UI.toast('Watermark applied - press Save to download', 'success');
        });
    },

    async applyStamps() {
        if (!this.stamps.length) return UI.toast('Place a stamp on the page first', 'error');
        await UI.run('Applying stamps…', async () => {
            await this.history.mark();
            await engine.call('stamp', 'stamp', JSON.stringify(this.stamps));
            this.stamps = [];
            await this.view.render();
            await this.history.refresh();
            UI.toast('Stamps applied', 'success');
        });
    },

    async applyNumbers() {
        const number = () => engine.call('page_numbers', 'stamp', $('pn-format').value, $('pn-pos').value,
                                         Number($('pn-start').value || 1));
        if (this.batching) return this.runOverFiles('Numbering files…', 'numbered', number);
        await UI.run('Adding page numbers…', async () => {
            await this.history.mark();
            await engine.call('page_numbers', 'stamp', $('pn-format').value, $('pn-pos').value,
                              Number($('pn-start').value || 1));
            await this.view.render();
            await this.history.refresh();
            UI.toast('Page numbers added', 'success');
        });
    },

    async applyHeaderFooter() {
        const fields = {
            'header-left': $('hf-hl').value, 'header-center': $('hf-hc').value, 'header-right': $('hf-hr').value,
            'footer-left': $('hf-fl').value, 'footer-center': $('hf-fc').value, 'footer-right': $('hf-fr').value,
        };
        if (!Object.values(fields).some((v) => v.trim())) return UI.toast('Fill at least one field', 'error');
        // The document name is one of the fields people substitute into a
        // header, so in a batch each file gets its own rather than the first.
        const stampHF = (info, file) => engine.call('header_footer', 'stamp', JSON.stringify(fields),
                                                    9, '#111111', 28,
                                                    file.name.replace(/\.pdf$/i, ''));
        if (this.batching) return this.runOverFiles('Adding header/footer…', 'headed', stampHF);
        await UI.run('Adding header/footer…', async () => {
            await this.history.mark();
            await engine.call('header_footer', 'stamp', JSON.stringify(fields), 9, '#111111', 28,
                              (this.file && this.file.name.replace(/\.pdf$/i, '')) || 'document');
            await this.view.render();
            await this.history.refresh();
            UI.toast('Header/footer added', 'success');
        });
    },

    /** The first and last number the current settings would produce. */
    previewBates() {
        const box = $('bates-preview');
        if (!box) return;
        const pages = this.batching
            ? null
            : (this.view && this.view.pages) || 0;
        const start = Number($('bates-start').value || 1);
        const digits = Math.max(1, Number($('bates-digits').value || 6));
        const label = (n) => `${$('bates-prefix').value}${String(n).padStart(digits, '0')}${$('bates-suffix').value}`;

        if (this.batching) {
            box.innerHTML = `Starts at <code>${label(start)}</code> and runs on through every ` +
                            `chosen file as one sequence.`;
            return;
        }
        if (!pages) {
            box.textContent = 'Choose a document to see the run this will produce.';
            return;
        }
        const last = start + pages - 1;
        // A number wider than the padding is not wrong, but it is worth
        // saying: the run stops lining up at that point.
        const overflow = String(last).length > digits
            ? ' <span class="warn">The last numbers are wider than the padding, so the run stops lining up.</span>'
            : '';
        box.innerHTML = `<code>${label(start)}</code> to <code>${label(last)}</code> ` +
                        `across <strong>${pages}</strong> page(s).${overflow}`;
    },

    async applyBates() {
        if (this.batching) {
            // Bates numbering is a single unbroken sequence across a bundle,
            // so the count carries from one file into the next rather than
            // restarting - that is the whole point of it in a legal filing.
            let next = Number($('bates-start').value || 1);
            return this.runOverFiles('Applying Bates numbering…', 'bates', async () => {
                const n = await engine.call('bates', 'stamp', $('bates-prefix').value, $('bates-suffix').value,
                                            next, Number($('bates-digits').value || 6), $('bates-pos').value);
                next += n;
            });
        }
        await UI.run('Applying Bates numbering…', async () => {
            await this.history.mark();
            const n = await engine.call('bates', 'stamp', $('bates-prefix').value, $('bates-suffix').value,
                                        Number($('bates-start').value || 1), Number($('bates-digits').value || 6),
                                        $('bates-pos').value);
            await this.view.render();
            await this.history.refresh();
            this.previewBates();
            UI.toast(`Bates numbering applied to ${n} pages`, 'success');
        });
    },
};

/* ------------------------------------------------------------------ */
/* 5. OCR                                                              */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* shared OCR                                                          */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* batch processing                                                    */
/* ------------------------------------------------------------------ */

/** Apply one tool's operation to several files and hand back a ZIP.
 *
 * The tools that qualify are the ones whose settings apply uniformly, with
 * nothing to point at per document: compressing, protecting, watermarking,
 * reading, replacing and sanitising. Editing, redacting and signing are left
 * out on purpose -- each needs you to look at the page in front of you, so a
 * "do this to twenty files" button would be dishonest.
 *
 * @param files    the chosen File objects
 * @param docId    engine slot to load each one into, in turn
 * @param suffix   appended to each output name
 * @param apply    async () => Uint8Array, run once per file with the tool's
 *                 own settings already on screen
 * @param report   (html) => void, for progress and the final summary
 */
/** Show a tool's workspace in batch form: the settings still apply, but
 *  there is no single document to preview, so say what will happen instead. */
function batchChosen(key, files) {
    const workspace = $(`${key}-workspace`);
    if (workspace) workspace.classList.remove('hidden');
    const total = files.reduce((n, f) => n + f.size, 0);
    const summary = $(`${key}-summary`);
    if (summary) {
        summary.classList.remove('hidden');
        summary.innerHTML =
            `<strong>${files.length} files</strong> - ${formatSize(total)} in total.<br>` +
            `<span class="muted">The settings below apply to every one, and you get a ZIP back.</span>`;
    }
    batchReport(key, '');
}

/** Progress and the closing summary, in whichever result box the tool has. */
function batchReport(key, html) {
    const box = $(`${key}-result`) || $(`${key}-status`) || $(`${key}-summary`);
    if (!box) return;
    box.classList.toggle('hidden', !html);
    box.innerHTML = html;
}

async function runBatch({ files, docId, suffix, apply, report = () => {}, sharedPassword = false }) {
    const zip = new JSZip();
    const failed = [];
    let done = 0;
    // A password that opened one file usually opens the rest of the batch;
    // asking for the same one twenty times is the chore, not the unlocking.
    let known = '';

    for (const file of files) {
        report(`Processing <strong>${file.name}</strong> (${done + 1} of ${files.length})…`);
        try {
            let info;
            if (sharedPassword && known) {
                try {
                    info = await engine.callJSON('open_doc', docId, await fileToBytes(file), known);
                } catch {
                    info = null;          // this one differs; fall through and ask
                }
            }
            if (!info) {
                info = await engine.openDoc(docId, await fileToBytes(file), file.name);
                if (sharedPassword) known = engine.lastPassword || known;
            }
            const bytes = await apply(info, file);
            const base = file.name.replace(/\.pdf$/i, '');
            zip.file(`${base}-${suffix}.pdf`, bytes);
            done++;
        } catch (err) {
            // One bad file must not lose the whole run: record why and carry
            // on, so twenty files do not fail because the third is corrupt or
            // its password prompt was dismissed.
            const why = String(err.message || err).includes('CANCELLED')
                ? 'skipped - no password given'
                : UI.explain(err);
            failed.push({ name: file.name, why });
        }
    }

    if (!done) {
        report('<strong>Nothing was produced.</strong><br>' +
               failed.map((f) => `${f.name}: ${f.why}`).join('<br>'));
        return { done: 0, failed };
    }

    report(`Building the ZIP…`);
    const blob = await zip.generateAsync({ type: 'blob' });
    download(blob, `${suffix}-${done}-files.zip`, 'application/zip');

    const note = failed.length
        ? `<br><span class="warn">${failed.length} could not be done:</span><br>` +
          failed.map((f) => `${f.name} - ${f.why}`).join('<br>')
        : '';
    report(`<strong>${done} file(s)</strong> processed and downloaded as a ZIP.${note}`);
    return { done, failed };
}

/** Draw a rendered page into a canvas, lifting contrast on the way.
 *
 * Measured on a real scan: raising contrast on a greyscale copy took a
 * stamp-paper page from 56% confidence to 68%, and 77 to 82 confidently read
 * words, while a clean page was unaffected. Hard binarisation was tried too
 * and was no better than the raw image, so it is not used.
 */
async function preprocessForOcr(pngBytes, rotate = 0) {
    const blob = new Blob([pngBytes], { type: 'image/png' });
    const bmp = await createImageBitmap(blob);
    const swap = rotate === 90 || rotate === 270;
    const canvas = document.createElement('canvas');
    canvas.width = swap ? bmp.height : bmp.width;
    canvas.height = swap ? bmp.width : bmp.height;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.save();
    ctx.translate(canvas.width / 2, canvas.height / 2);
    if (rotate) ctx.rotate((rotate * Math.PI) / 180);
    ctx.drawImage(bmp, -bmp.width / 2, -bmp.height / 2);
    ctx.restore();
    bmp.close?.();

    const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const d = img.data;
    for (let i = 0; i < d.length; i += 4) {
        const grey = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
        const lifted = Math.max(0, Math.min(255, (grey - 128) * 1.6 + 128));
        d[i] = d[i + 1] = d[i + 2] = lifted;
    }
    ctx.putImageData(img, 0, 0);
    const out = await new Promise((r) => canvas.toBlob(r, 'image/png'));
    return { blob: out, width: canvas.width, height: canvas.height };
}

/** Which way up is this page?
 *
 * Tesseract's own orientation detection returns nothing in this build, but
 * confidence separates the cases cleanly on its own: the same page scored 89
 * upright against 45, 36 and 45 at the three wrong turns. The probe runs at
 * low resolution, so picking the orientation costs a fraction of the real
 * read that follows.
 */
async function detectOrientation(worker, pngBytes, onStep = () => {}) {
    let best = { angle: 0, confidence: -1 };
    for (const angle of [0, 90, 180, 270]) {
        onStep(null, `Checking orientation (${angle}\u00b0)…`);
        const { blob } = await preprocessForOcr(pngBytes, angle);
        const url = URL.createObjectURL(blob);
        try {
            const { data } = await worker.recognize(url);
            if ((data.confidence || 0) > best.confidence) {
                best = { angle, confidence: data.confidence || 0 };
            }
        } finally {
            URL.revokeObjectURL(url);
        }
    }
    return best;
}

/** Recognise pages and write an invisible text layer back into the document.
 *
 * Shared by the OCR tab and the Edit tab: a scanned page has no text objects
 * at all, so the editor needs exactly this before it has anything to offer.
 *
 * @param docId    document already open in the engine
 * @param pages    page indices to read
 * @param lang     Tesseract language code(s), e.g. "eng" or "eng+hin"
 * @param onStep   (pct, label) progress callback
 * @param checkOrientation  probe which way up each page is first
 * @returns {{pages: number, uncertain: number}}
 */
async function recognisePages(docId, pages, lang, onStep = () => {},
                              checkOrientation = false) {
    onStep(0, 'Loading the reader…');
    const worker = await Tesseract.createWorker(lang, 1, {
        // The language pack is ~11 MB on first use. Without this the UI sits
        // silent through the download and looks like it has frozen.
        logger: (m) => {
            if (m.status && m.status !== 'recognizing text') {
                onStep(Math.round((m.progress || 0) * 100),
                       `${m.status.charAt(0).toUpperCase()}${m.status.slice(1)}…`);
            }
        },
    });
    try {
        let done = 0;
        let uncertain = 0;
        for (const pno of pages) {
            onStep(Math.round((done / pages.length) * 100), `Reading page ${pno + 1}…`);

            // A sideways page reads as nonsense, so check which way is up
            // before the real pass -- cheaply, at low resolution, and only
            // when asked, since most pages are already upright.
            if (checkOrientation) {
                const probe = await engine.call('render_page', docId, pno, 90);
                const best = await detectOrientation(worker, probe, onStep);
                if (best.angle) {
                    // Rotate the page itself rather than the image: only the
                    // /Rotate entry changes, so the scan is not re-encoded,
                    // and everything after this sees an upright page.
                    //
                    // The angle is added, not subtracted. The probe rotates an
                    // image that already reflects the page's current rotation,
                    // so the turn that made it readable is the turn the page
                    // still needs; negating it lands 180 degrees out, which is
                    // portrait and upside down, and reads as badly as sideways.
                    await engine.call('set_page_rotation', docId, pno, best.angle);
                }
            }

            // Render high: recognition accuracy follows input resolution.
            const png = await engine.call('render_page', docId, pno, 200);
            const { blob, width, height } = await preprocessForOcr(png);
            const url = URL.createObjectURL(blob);
            try {
                const { data } = await worker.recognize(url);
                // Send lines with their baselines, not a flat word list: the
                // baseline is what puts the text layer on the ink, and a line
                // gives the engine enough context to size its words alike.
                const asWord = (w) => ({
                    text: w.text, bbox: w.bbox, baseline: w.baseline,
                    confidence: w.confidence,
                });
                const lines = (data.lines || []).map((ln) => ({
                    baseline: ln.baseline,
                    words: (ln.words || []).map(asWord),
                }));
                const payload = lines.length
                    ? { lines }
                    : { lines: [{ words: (data.words || []).map(asWord) }] };
                const res = await engine.callJSON('insert_ocr_layer', docId, pno,
                                                  JSON.stringify(payload), width, height);
                if (res && typeof res === 'object') uncertain += res.doubts || 0;
            } finally {
                URL.revokeObjectURL(url);
            }
            done++;
            onStep(Math.round((done / pages.length) * 100), `Read ${done} of ${pages.length}`);
        }
        return { pages: done, uncertain };
    } finally {
        await worker.terminate();
    }
}

const OcrTool = {
    init() {
        $('ocr-file').addEventListener('change', (e) => this.open(Array.from(e.target.files)));
        $('ocr-run').addEventListener('click', () => this.run());
        $('ocr-save').addEventListener('click', async () => {
            const bytes = await engine.call('save', 'ocr');
            download(bytes, 'searchable.pdf');
        });
    },

    async open(files) {
        const list = Array.isArray(files) ? files : [files].filter(Boolean);
        if (!list.length) return;
        this.files = list;
        if (list.length > 1) {
            this.pages = 0;
            this.needs = [];
            $('ocr-save').disabled = true;
            return batchChosen('ocr', list);
        }
        const file = list[0];
        await UI.run('Analysing document…', async () => {
            const info = await engine.openDoc('ocr', await fileToBytes(file), file.name);
            this.pages = info.pages;
            const needs = [];
            for (let i = 0; i < info.pages; i++) {
                if (await engine.call('needs_ocr', 'ocr', i)) needs.push(i);
            }
            this.needs = needs;
            $('ocr-workspace').classList.remove('hidden');
            $('ocr-save').disabled = true;
            $('ocr-summary').innerHTML =
                `<strong>${file.name}</strong> - ${info.pages} page(s).<br>` +
                (needs.length
                    ? `<span class="warn">${needs.length} page(s) look scanned and have no searchable text.</span>`
                    : 'Every page already has extractable text - OCR is optional here.');
        });
    },

    async run() {
        const scope = $('ocr-scope').value;
        const lang = $('ocr-lang').value;
        const upright = $('ocr-upright').checked;

        $('ocr-bar').classList.remove('hidden');
        $('ocr-run').disabled = true;
        const setProgress = (pct, label) => {
            $('ocr-fill').style.width = `${pct}%`;
            $('ocr-pct').textContent = `${pct}%`;
            if (label) $('ocr-status').textContent = label;
        };

        if (this.files && this.files.length > 1) {
            // Which pages need reading differs from file to file, so it is
            // worked out per document rather than from the one on screen.
            try {
                await UI.run('Reading files…', () => runBatch({
                    files: this.files, docId: 'ocr', suffix: 'searchable',
                    apply: async (info) => {
                        const pages = [];
                        for (let i = 0; i < info.pages; i++) {
                            if (scope === 'all' || await engine.call('needs_ocr', 'ocr', i)) pages.push(i);
                        }
                        if (pages.length) await recognisePages('ocr', pages, lang, setProgress, upright);
                        return engine.call('save', 'ocr');
                    },
                    report: (html) => batchReport('ocr', html),
                }));
            } finally {
                $('ocr-run').disabled = false;
            }
            return;
        }

        const targets = scope === 'all' ? Array.from({ length: this.pages }, (_, i) => i) : this.needs;
        if (!targets.length) {
            $('ocr-run').disabled = false;
            return UI.toast('Nothing to OCR', 'error');
        }

        try {
            const res = await recognisePages('ocr', targets, lang, setProgress, upright);
            const note = res.uncertain ? ` ${res.uncertain} word(s) were hard to read.` : '';
            $('ocr-status').textContent =
                `Done - ${res.pages} page(s) now carry a searchable text layer.${note}`;
            $('ocr-save').disabled = false;
            UI.toast('OCR complete', 'success');
        } catch (err) {
            console.error(err);
            $('ocr-status').textContent = `OCR failed: ${UI.explain(err)}`;
            UI.toast(`OCR failed: ${UI.explain(err)}`, 'error');
        } finally {
            $('ocr-run').disabled = false;
        }
    },
};

/* ------------------------------------------------------------------ */
/* 6. Compress                                                         */
/* ------------------------------------------------------------------ */

const CompressTool = {
    init() {
        $('compress-file').addEventListener('change', (e) => this.open(Array.from(e.target.files)));
        $('compress-run').addEventListener('click', () => this.run());
    },

    async open(files) {
        if (!files.length) return;
        this.files = files;
        this.file = files[0];
        if (files.length > 1) return batchChosen('compress', files);
        await UI.run('Opening document…', async () => {
            const info = await engine.openDoc('compress', await fileToBytes(this.file), this.file.name);
            $('compress-workspace').classList.remove('hidden');
            $('compress-result').classList.add('hidden');
            $('compress-summary').innerHTML =
                `<strong>${this.file.name}</strong> - ${formatSize(this.file.size)} · ${info.pages} page(s)`;
        });
    },

    async run() {
        if (this.files && this.files.length > 1) {
            return UI.run('Compressing files…', () => runBatch({
                files: this.files, docId: 'compress', suffix: 'compressed',
                apply: () => engine.call('compress', 'compress', $('compress-level').value),
                report: (html) => batchReport('compress', html),
            }));
        }
        await UI.run('Compressing…', async () => {
            const bytes = await engine.call('compress', 'compress', $('compress-level').value);
            const saved = this.file.size - bytes.length;
            const pct = Math.round((saved / this.file.size) * 100);
            download(bytes, 'compressed.pdf');
            const box = $('compress-result');
            box.classList.remove('hidden');
            box.innerHTML = pct > 0
                ? `${formatSize(this.file.size)} → <strong>${formatSize(bytes.length)}</strong> (${pct}% smaller). Text stays searchable.`
                : `This file is already well optimised (${formatSize(bytes.length)}). Try a higher level for more.`;
        });
    },
};

/* ------------------------------------------------------------------ */
/* 7. Protect                                                          */
/* ------------------------------------------------------------------ */

const ProtectTool = {
    init() {
        $('protect-file').addEventListener('change', (e) => this.open(Array.from(e.target.files)));
        $('protect-run').addEventListener('click', () => this.encrypt());
        $('protect-remove').addEventListener('click', () => this.decrypt());
    },

    async open(files) {
        const list = Array.isArray(files) ? files : [files];
        if (!list.length || !list[0]) return;
        this.files = list;
        this.file = list[0];
        if (list.length > 1) return batchChosen('protect', list);
        await UI.run('Opening document…', async () => {
            const file = this.file;
            const bytes = await fileToBytes(file);
            const info = await engine.openDoc('protect', bytes, file.name);
            $('protect-workspace').classList.remove('hidden');
            $('protect-summary').innerHTML =
                `<strong>${file.name}</strong> - ${info.pages} page(s) · ${formatSize(file.size)}`;
        });
    },

    async encrypt() {
        const pw = $('protect-user-pw').value;
        if (!pw) return UI.toast('Enter an open password', 'error');
        const settings = () => engine.call('encrypt', 'protect', pw, $('protect-owner-pw').value,
                                           $('perm-print').checked, $('perm-copy').checked,
                                           $('perm-modify').checked, $('perm-annot').checked);
        if (this.files && this.files.length > 1) {
            return UI.run('Encrypting files…', () => runBatch({
                files: this.files, docId: 'protect', suffix: 'protected',
                apply: settings, report: (html) => batchReport('protect', html),
            }));
        }
        await UI.run('Encrypting with AES-256…', async () => {
            const bytes = await engine.call('encrypt', 'protect', pw, $('protect-owner-pw').value,
                                            $('perm-print').checked, $('perm-copy').checked,
                                            $('perm-modify').checked, $('perm-annot').checked);
            download(bytes, 'protected.pdf');
            UI.toast('Encrypted - text stays searchable for anyone with the password', 'success');
        });
    },

    async decrypt() {
        if (this.files && this.files.length > 1) {
            // A folder of statements from the same bank shares one password,
            // and unlocking them one at a time is the chore this removes.
            // Each is asked for separately only if that one does not open.
            return UI.run('Removing protection…', () => runBatch({
                files: this.files, docId: 'protect', suffix: 'unlocked',
                apply: () => engine.call('decrypt', 'protect'),
                report: (html) => batchReport('protect', html),
                sharedPassword: true,
            }));
        }
        await UI.run('Removing protection…', async () => {
            const bytes = await engine.call('decrypt', 'protect');
            download(bytes, 'unlocked.pdf');
            UI.toast('Password removed', 'success');
        });
    },
};

/* ------------------------------------------------------------------ */
/* 8. Redact                                                           */
/* ------------------------------------------------------------------ */

const RedactTool = {
    marks: [],
    applied: 0,

    init() {
        this.view = new DocView('redact', { onRender: () => this.drawMarks() });
        // Named apart from the "Undo mark" button, which takes back the last
        // box you drew rather than a change already written into the file.
        this.history = makeHistory('redact', this.view,
                                   { undo: 'redact-doc-undo', redo: 'redact-doc-redo' });
        $('redact-file').addEventListener('change', (e) => this.open(e.target.files[0]));
        $('redact-undo').addEventListener('click', () => {
            for (let i = this.marks.length - 1; i >= 0; i--) {
                if (this.marks[i].page === this.view.page) { this.marks.splice(i, 1); break; }
            }
            this.drawMarks();
        });
        $('redact-clear').addEventListener('click', () => {
            this.marks = this.marks.filter((m) => m.page !== this.view.page);
            this.drawMarks();
        });
        $('redact-search').addEventListener('click', () => this.findAndRedact());
        $('redact-apply').addEventListener('click', () => this.apply());
        // Repaint existing marks so the preview matches the chosen colour.
        $('redact-color').addEventListener('input', () => this.drawMarks());
        this.setupDraw();
    },

    async open(file) {
        if (!file) return;
        this.marks = [];
        this.applied = 0;
        await UI.run('Opening document…', () => this.view.load(file));
    },

    setupDraw() {
        const overlay = this.view.overlay;
        let start = null;
        let ghost = null;
        overlay.addEventListener('mousedown', (e) => {
            start = this.view.fracFromEvent(e);
            ghost = document.createElement('div');
            ghost.className = 'draw-ghost draw-ghost--redact';
            overlay.appendChild(ghost);
        });
        overlay.addEventListener('mousemove', (e) => {
            if (!start || !ghost) return;
            const now = this.view.fracFromEvent(e);
            Object.assign(ghost.style, {
                left: `${Math.min(start.x, now.x) * 100}%`,
                top: `${Math.min(start.y, now.y) * 100}%`,
                width: `${Math.abs(now.x - start.x) * 100}%`,
                height: `${Math.abs(now.y - start.y) * 100}%`,
            });
        });
        window.addEventListener('mouseup', (e) => {
            if (!start) return;
            const end = this.view.fracFromEvent(e);
            const from = start;
            start = null;
            if (ghost) { ghost.remove(); ghost = null; }
            const w = Math.abs(end.x - from.x);
            const h = Math.abs(end.y - from.y);
            if (w < 0.01 || h < 0.005) return;
            this.marks.push({ page: this.view.page, xFrac: Math.min(from.x, end.x),
                              yFrac: Math.min(from.y, end.y), wFrac: w, hFrac: h });
            this.drawMarks();
        });
    },

    drawMarks() {
        const overlay = this.view.overlay;
        const colour = $('redact-color').value;
        overlay.innerHTML = '';
        this.marks.filter((m) => m.page === this.view.page).forEach((m) => {
            const el = document.createElement('div');
            el.className = 'redact-box';
            el.style.cssText = `left:${m.xFrac * 100}%;top:${m.yFrac * 100}%;` +
                               `width:${m.wFrac * 100}%;height:${m.hFrac * 100}%;` +
                               `background:${colour}`;
            overlay.appendChild(el);
        });
        $('redact-count').textContent = this.marks.length
            ? `${this.marks.length} area(s) marked across the document.` : '';
    },

    async findAndRedact() {
        const term = $('redact-term').value.trim();
        if (!term) return UI.toast('Enter text to redact', 'error');
        await UI.run('Finding and redacting…', async () => {
            await this.history.mark();
            const n = await engine.call('redact_search', 'redact', term, '', $('redact-color').value);
            if (n) this.applied += n;
            await this.view.render();
            UI.toast(n ? `Redacted ${n} occurrence(s) - content deleted from the file` : 'No matches found',
                     n ? 'success' : 'error');
        });
    },

    async apply() {
        // Find-and-redact applies immediately, so allow saving those results
        // even when no hand-drawn marks are outstanding.
        if (!this.marks.length && !this.applied) {
            return UI.toast('Mark an area, or use find & redact, first', 'error');
        }
        await UI.run('Applying redactions…', async () => {
            if (this.marks.length) {
                await this.history.mark();
                await engine.call('redact', 'redact', JSON.stringify(this.marks),
                              $('redact-images').checked, $('redact-color').value);
                this.marks = [];
            }
            this.applied = 0;
            await this.view.render();
            await this.view.save('redacted.pdf');
            UI.toast('Redactions applied - the covered content is gone from the file', 'success');
        });
    },
};

/* ------------------------------------------------------------------ */
/* 9. Export                                                           */
/* ------------------------------------------------------------------ */

const ExportTool = {
    init() {
        $('export-file').addEventListener('change', (e) => this.open(Array.from(e.target.files)));
        $('export-doc').addEventListener('click', () => this.doc());
        $('export-html').addEventListener('click', () => this.html());
        $('export-text').addEventListener('click', () => this.text());
        $('export-tables-xlsx').addEventListener('click', () => this.tablesXlsx());
        $('export-tables').addEventListener('click', () => this.tables());
        $('export-images').addEventListener('click', () => this.images());
        $('export-pages').addEventListener('click', () => this.pageImages());
        $('export-fmt').addEventListener('change', (e) => {
            const isJpeg = e.target.value === 'jpeg';
            $('export-quality-row').classList.toggle('hidden', !isJpeg);
            $('export-pages-label').textContent = `Pages → ${isJpeg ? 'JPEG' : 'PNG'}`;
        });
        $('export-quality').addEventListener('input', (e) => {
            $('export-quality-value').textContent = e.target.value;
        });
        $('export-scan-ocr').addEventListener('click', () => this.readScan());
    },

    /** Which pages have no text at all. Every export here reads text, so a
     *  scan yields an empty Word file or "no tables detected" - which reads
     *  as a broken export rather than as a document with nothing to read. */
    async checkScanned(pages) {
        const scanned = [];
        for (let i = 0; i < pages; i++) {
            if (await engine.call('needs_ocr', 'export', i)) scanned.push(i);
        }
        this.scanned = scanned;
        const notice = $('export-scan-notice');
        notice.classList.toggle('hidden', !scanned.length);
        if (scanned.length) {
            $('export-scan-title').textContent = scanned.length === pages
                ? 'These pages are scans.'
                : `${scanned.length} of ${pages} pages are scans.`;
            $('export-scan-status').classList.add('hidden');
        }
    },

    /** Read the scanned pages in place, so the exports below then work. */
    async readScan() {
        if (!this.scanned || !this.scanned.length) return;
        const bar = $('export-scan-bar');
        const status = $('export-scan-status');
        bar.classList.remove('hidden');
        status.classList.remove('hidden');
        $('export-scan-ocr').disabled = true;
        try {
            const res = await recognisePages('export', this.scanned,
                $('export-scan-lang').value,
                (pct, label) => {
                    $('export-scan-fill').style.width = `${pct}%`;
                    $('export-scan-pct').textContent = `${pct}%`;
                    if (label) status.textContent = label;
                },
                $('export-scan-upright').checked);
            const doubt = res.uncertain ? ` ${res.uncertain} word(s) were hard to read.` : '';
            status.textContent =
                `${res.pages} page(s) read - the exports below will now find text.${doubt}`;
            await this.checkScanned(this.pages);
            if (this.scanned.length) $('export-scan-notice').classList.remove('hidden');
            UI.toast('Pages read', 'success');
        } catch (err) {
            status.textContent = `Could not read the pages: ${UI.explain(err)}`;
        } finally {
            $('export-scan-ocr').disabled = false;
        }
    },

    async open(files) {
        const list = Array.isArray(files) ? files : [files].filter(Boolean);
        if (!list.length) return;
        this.files = list;

        if (list.length > 1) {
            // Several statements are one body of work. Only the table export
            // can honestly combine them - a Word file or a page render per
            // document is a pile of downloads, not one result - so the rest
            // are put away rather than left to do something surprising.
            this.name = `${list.length}-files`;
            $('export-workspace').classList.remove('hidden');
            $('export-scan-notice').classList.add('hidden');
            $('export-summary').innerHTML =
                `<strong>${list.length} files</strong> - ${formatSize(list.reduce((n, f) => n + f.size, 0))} in total.<br>` +
                '<span class="muted">Tables from all of them go into one workbook, a sheet per file. ' +
                'The other exports work on one document at a time.</span>';
            $$('.export-card').forEach((c) => {
                c.disabled = c.id !== 'export-tables-xlsx';
                c.classList.toggle('export-card--off', c.disabled);
            });
            this.result('');
            return;
        }

        $$('.export-card').forEach((c) => {
            c.disabled = false;
            c.classList.remove('export-card--off');
        });
        const file = list[0];
        this.name = file.name.replace(/\.pdf$/i, '');
        await UI.run('Opening document…', async () => {
            const info = await engine.openDoc('export', await fileToBytes(file), file.name);
            this.pages = info.pages;
            $('export-workspace').classList.remove('hidden');
            $('export-summary').innerHTML = `<strong>${file.name}</strong> - ${info.pages} page(s)`;
            await this.checkScanned(info.pages);
        });
    },

    /** One workbook from several documents, a sheet per file. */
    async tablesXlsxBatch() {
        const one = $('export-tables-one-sheet').checked;
        await UI.run('Reading tables…', async () => {
            await engine.call('start_table_batch', 'export');
            const skipped = [];
            for (const file of this.files) {
                this.result(`Reading <strong>${file.name}</strong>…`);
                try {
                    await engine.openDoc('export', await fileToBytes(file), file.name);
                    const label = file.name.replace(/\.pdf$/i, '');
                    await engine.callJSON('add_to_table_batch', 'export', 'export', label, one);
                } catch (err) {
                    const why = String(err.message || err).includes('CANCELLED')
                        ? 'skipped - no password given' : UI.explain(err);
                    skipped.push(`${file.name} - ${why}`);
                }
            }
            const res = await engine.callJSON('finish_table_batch', 'export');
            if (!res.ok) return this.result(res.reason);
            const mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
            download(b64ToBytes(res.b64), `tables-${this.files.length}-files.xlsx`, mime);
            const note = skipped.length
                ? `<br><span class="warn">${skipped.length} could not be read:</span><br>${skipped.join('<br>')}`
                : '';
            this.result(`One workbook with <strong>${res.sheets.length}</strong> sheet(s): ` +
                        res.sheets.map((s) => `${s.name} (${s.rows}×${s.cols})`).join(', ') + note);
        });
    },

    result(html) {
        const box = $('export-result');
        box.classList.remove('hidden');
        box.innerHTML = html;
    },

    async doc() {
        await UI.run('Building document…', async () => {
            const b64 = await engine.call('export_docx', 'export');
            const mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
            download(b64ToBytes(b64), `${this.name}.docx`, mime);
            this.result('Word document exported. Headings, bold and italic are preserved, ' +
                        'and the text is fully editable.');
        });
    },

    async html() {
        await UI.run('Building page…', async () => {
            const html = await engine.call('export_html', 'export');
            download(new Blob([html], { type: 'text/html' }), `${this.name}.html`);
            this.result('Web page exported, keeping the original page layout.');
        });
    },

    async text() {
        await UI.run('Extracting text…', async () => {
            const text = await engine.call('export_text', 'export');
            download(new Blob([text], { type: 'text/plain' }), `${this.name}.txt`);
            this.result(`Extracted ${text.length.toLocaleString()} characters.`);
        });
    },

    async tablesXlsx() {
        if (this.files && this.files.length > 1) return this.tablesXlsxBatch();
        await UI.run('Detecting tables…', async () => {
            const one = $('export-tables-one-sheet').checked;
            const result = await engine.callJSON('export_tables_xlsx', 'export', one);
            if (!result.ok) return this.result(result.reason);
            const mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
            download(b64ToBytes(result.b64), `${this.name}-tables.xlsx`, mime);
            this.result(one
                ? `Built one sheet holding <strong>${result.sheets[0].rows}</strong> rows ` +
                  `× ${result.sheets[0].cols} columns, with a column naming the page each row came from.`
                : `Built one Excel workbook with <strong>${result.sheets.length}</strong> sheet(s): ` +
                  result.sheets.map((s) => `${s.name} (${s.rows}×${s.cols})`).join(', '));
        });
    },

    async tables() {
        await UI.run('Detecting tables…', async () => {
            const tables = await engine.callJSON('export_tables', 'export');
            if (!tables.length) return this.result('No tables were detected in this PDF.');
            const zip = new JSZip();
            tables.forEach((t) => zip.file(t.name, t.csv));
            download(await zip.generateAsync({ type: 'blob' }), `${this.name}-tables.zip`, 'application/zip');
            this.result(`Found <strong>${tables.length}</strong> table(s): ` +
                        tables.map((t) => `${t.name} (${t.rows}×${t.cols})`).join(', '));
        });
    },

    async images() {
        await UI.run('Extracting images…', async () => {
            const images = await engine.callJSON('export_images', 'export');
            if (!images.length) return this.result('No embedded images found.');
            const zip = new JSZip();
            images.forEach((i) => zip.file(i.name, b64ToBytes(i.b64)));
            download(await zip.generateAsync({ type: 'blob' }), `${this.name}-images.zip`, 'application/zip');
            this.result(`Extracted <strong>${images.length}</strong> image(s) at original quality.`);
        });
    },

    async pageImages() {
        await UI.run('Rendering pages…', async () => {
            const fmt = $('export-fmt').value;
            const pages = await engine.callJSON('export_page_images', 'export',
                                                Number($('export-dpi').value || 150), fmt,
                                                $('export-pages-spec').value,
                                                Number($('export-quality').value || 90));
            const zip = new JSZip();
            pages.forEach((p) => zip.file(p.name, b64ToBytes(p.b64)));
            download(await zip.generateAsync({ type: 'blob' }), `${this.name}-pages.zip`, 'application/zip');
            this.result(`Rendered <strong>${pages.length}</strong> ${fmt === 'jpeg' ? 'JPEG' : 'PNG'} page image(s).`);
        });
    },
};

/* ------------------------------------------------------------------ */
/* 10. Compare                                                         */
/* ------------------------------------------------------------------ */

const CompareTool = {
    loaded: { a: false, b: false },

    init() {
        ['A', 'B'].forEach((slot) => {
            $(`compare${slot}-file`).addEventListener('change', async (e) => {
                const file = e.target.files[0];
                if (!file) return;
                await UI.run('Loading…', async () => {
                    await engine.openDoc(`cmp${slot}`, await fileToBytes(file), file.name);
                    $(`compare${slot}-name`).textContent = file.name;
                    this.loaded[slot.toLowerCase()] = true;
                    $('compare-run').disabled = !(this.loaded.a && this.loaded.b);
                });
            });
        });
        $('compare-run').addEventListener('click', () => this.run());
        $('compare-download').addEventListener('click', () => this.download());
    },

    async run() {
        await UI.run('Comparing documents…', async () => {
            const diff = await engine.callJSON('compare', 'cmpA', 'cmpB');
            const box = $('compare-result');
            if (!diff.totalChanges) {
                box.innerHTML = '<div class="summary-box">The two documents are textually identical.</div>';
                return;
            }
            // Text that only moved is said to have moved, rather than being
            // shown as a deletion here and an unrelated rewrite there.
            const moved = (c) => {
                if (c.type === 'move') {
                    return `<div class="diff-row diff-row--move">
                                <div class="diff-move">→ Moved to page ${c.movedTo + 1}, unchanged</div>
                                <div class="diff-same">${c.old}</div>
                            </div>`;
                }
                return `<div class="diff-row diff-row--move">
                            <div class="diff-move">← Moved here from page ${c.movedFrom + 1}, unchanged</div>
                            <div class="diff-same">${c.new}</div>
                        </div>`;
            };
            const counts = diff.moves
                ? `<strong>${diff.edits}</strong> edit(s) and <strong>${diff.moves}</strong> moved passage(s).`
                : `<strong>${diff.totalChanges}</strong> change(s) found.`;
            box.innerHTML = `<div class="summary-box">${counts}</div>` +
                diff.pages.filter((p) => p.changes.length || p.onlyIn).map((p) => `
                    <div class="diff-page">
                        <h4>Page ${p.page + 1} <span class="muted">(${p.similarity}% similar)</span></h4>
                        ${p.onlyIn ? `<p class="muted">Only present in the ${p.onlyIn === 'a' ? 'original' : 'revised'} document.</p>` : ''}
                        ${p.changes.map((c) => (c.type === 'move' || c.type === 'moved-here') ? moved(c) : `
                            <div class="diff-row">
                                ${c.old ? `<div class="diff-old">− ${c.old}</div>` : ''}
                                ${c.new ? `<div class="diff-new">+ ${c.new}</div>` : ''}
                            </div>`).join('')}
                    </div>`).join('');
            $('compare-download').disabled = false;
        });
    },

    async download() {
        await UI.run('Highlighting differences…', async () => {
            const n = await engine.call('highlight_differences', 'cmpB', 'cmpA');
            const bytes = await engine.call('save', 'cmpB');
            download(bytes, 'compared-highlighted.pdf');
            UI.toast(`${n} difference(s) highlighted in the revised copy`, 'success');
        });
    },
};

/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* 11. Find sensitive data                                             */
/* ------------------------------------------------------------------ */

/** Undo and redo for a tab that changes its document in place.
 *
 * Watermarking, stamping, signing and redacting are all as destructive as
 * editing text - a stamp on the wrong page or a Bates run started at the
 * wrong number used to mean reloading the file and beginning again. The
 * engine already keeps the snapshots; this is the shared wiring so each tab
 * does not carry its own copy of it.
 *
 * @param key   the tab's key, used for its document and its button ids
 * @param view  the DocView to redraw after stepping
 */
function makeHistory(key, view, ids = {}) {
    const undoBtn = $(ids.undo || `${key}-undo`);
    const redoBtn = $(ids.redo || `${key}-redo`);

    const history = {
        /** Record the state before a change, so it can be stepped back to. */
        async mark() {
            Dirty.touch(view.docId);
            try {
                await engine.call('snapshot', view.docId);
                await history.refresh();
            } catch (err) {
                // History is a convenience; never let it block the change.
                console.warn('Could not record history', err);
            }
        },

        /** Never allowed to throw: this runs straight after a successful
         *  change, and failing here would report that change as an error. */
        async refresh() {
            try {
                const state = await engine.callJSON('history_state', view.docId);
                if (undoBtn) undoBtn.disabled = !state.undo;
                if (redoBtn) redoBtn.disabled = !state.redo;
                return state;
            } catch (err) {
                console.warn('Could not read history state', err);
                return { undo: 0, redo: 0 };
            }
        },

        async step(direction) {
            await UI.run(direction === 'undo' ? 'Undoing…' : 'Redoing…', async () => {
                const res = await engine.callJSON(direction, view.docId);
                if (!res.ok) {
                    return UI.toast(direction === 'undo' ? 'Nothing left to undo'
                                                         : 'Nothing to redo', 'error');
                }
                await view.render();
                const state = await history.refresh();
                if (state.undo) Dirty.touch(view.docId); else Dirty.clear(view.docId);
                UI.toast(direction === 'undo' ? 'Change undone' : 'Change redone', 'success');
            });
        },
    };

    // A tab with something of its own to do first - the Edit tab has an open
    // inline editor to close - passes onStep; otherwise the buttons step
    // straight through.
    const step = ids.onStep || history.step;
    if (undoBtn) undoBtn.addEventListener('click', () => step('undo'));
    if (redoBtn) redoBtn.addEventListener('click', () => step('redo'));
    // Ctrl+Z / Ctrl+Shift+Z, but only while this tab is the one on screen.
    // A tab that binds its own shortcut opts out rather than firing twice.
    if (ids.bindKeys !== false) document.addEventListener('keydown', (e) => {
        if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 'z') return;
        const panel = $(`${key}-tab`);
        if (!panel || !panel.classList.contains('active')) return;
        if (/^(INPUT|TEXTAREA)$/.test(e.target.tagName) || e.target.isContentEditable) return;
        e.preventDefault();
        step(e.shiftKey ? 'redo' : 'undo');
    });
    return history;
}

const ScanTool = {
    hits: [],
    chosen: new Set(),

    init() {
        this.view = new DocView('scan', { onRender: () => this.drawMarks() });
        $('scan-file').addEventListener('change', (e) => this.open(Array.from(e.target.files)));
        $('scan-run').addEventListener('click', () => this.run());
        $('scan-redact').addEventListener('click', () => this.redact());
        $('scan-select-all').addEventListener('click', () => this.selectAll());
        $('scan-mode').addEventListener('change', () => this.modeChanged());
    },

    async open(files) {
        const list = Array.isArray(files) ? files : [files].filter(Boolean);
        if (!list.length) return;
        this.files = list;
        this.hits = [];
        this.chosen = new Set();
        $('scan-results').innerHTML = '';

        if (list.length > 1) {
            // Nobody ticks boxes across twenty files, so the choice of what to
            // look for is made once and every match in every file is removed.
            $('scan-viewer').classList.add('hidden');
            $('scan-hint').textContent =
                'Everything found in every file will be removed - there is no page to tick items off on. ' +
                'Choose what to look for, then run it.';
            $('scan-run').textContent = '🛡️ Scan and redact all files';
            // How to remove it still has to be chosen - it is the run button
            // that does the removing here, so only the two controls that need
            // a list of hits are put away.
            $('scan-actions').classList.remove('hidden');
            $('scan-select-all').classList.add('hidden');
            $('scan-redact').classList.add('hidden');
            return batchChosen('scan', list);
        }

        $('scan-viewer').classList.remove('hidden');
        $('scan-hint').textContent =
            'Scan the document, then see exactly what will be removed marked on the page. ' +
            'Click a mark, or a row in the list, to select or deselect it.';
        $('scan-run').textContent = '🛡️ Scan document';
        $('scan-select-all').classList.remove('hidden');
        $('scan-redact').classList.remove('hidden');
        const file = list[0];
        await UI.run('Opening document…', async () => {
            const info = await this.view.load(file);
            $('scan-actions').classList.add('hidden');
            $('scan-summary').classList.remove('hidden');
            $('scan-summary').innerHTML = `<strong>${file.name}</strong> - ${info.pages} page(s)`;
        });
    },

    kinds() {
        return $$('#scan-kinds input:checked').map((b) => b.value);
    },

    async run() {
        const kinds = this.kinds();
        if (!kinds.length) return UI.toast('Choose at least one thing to look for', 'error');

        if (this.files && this.files.length > 1) return this.runBatch(kinds);

        await UI.run('Scanning for sensitive data…', async () => {
            const res = await engine.callJSON('scan_sensitive', 'scan', JSON.stringify(kinds));
            this.hits = res.hits;
            // Everything found starts selected: the common case is removing
            // all of it, and unticking the few exceptions is less work than
            // ticking forty rows.
            this.chosen = new Set(this.hits.map((_, i) => i));
            this.renderList(res);
            this.drawMarks();
            if (res.total) UI.toast(`${res.total} item(s) found`, 'success');
        });
    },

    /** Scan and redact every chosen file, into one ZIP. */
    runBatch(kinds) {
        const mode = $('scan-mode').value;
        const fill = $('scan-color').value;
        let removed = 0;
        const perKind = {};
        return UI.run('Scanning and redacting files…', async () => {
            const result = await runBatch({
                files: this.files, docId: 'scan', suffix: 'redacted',
                apply: async () => {
                    const res = await engine.callJSON('scan_and_redact', 'scan',
                                                      JSON.stringify(kinds), fill, mode);
                    removed += res.total;
                    Object.entries(res.summary).forEach(([k, n]) => {
                        perKind[k] = (perKind[k] || 0) + n;
                    });
                    return engine.call('save', 'scan');
                },
                report: (html) => batchReport('scan', html),
            });
            if (!result.done) return;
            // Say what was actually removed rather than only that it ran: a
            // run that found nothing looks identical otherwise.
            const chips = Object.entries(perKind)
                .map(([k, n]) => `<span class="chip">${k} · ${n}</span>`).join('');
            // Into the list area, not the progress box: overwriting that
            // would throw away the names of any files that could not be done.
            $('scan-results').innerHTML = removed
                ? `<strong>${removed} item(s)</strong> removed across ${result.done} file(s).` +
                  `<div class="chip-row">${chips}</div>`
                : `<strong>Nothing matching was found</strong> in any of the ${result.done} file(s), ` +
                  'so they were saved unchanged.';
        });
    },

    renderList(res) {
        const box = $('scan-results');
        if (!res.total) {
            box.innerHTML = '<div class="result-box">Nothing matching was found in this document.</div>';
            $('scan-actions').classList.add('hidden');
            return;
        }

        // Grouped by kind: a flat list of four hundred rows cannot be read,
        // and what you usually want is "all the card numbers".
        const byKind = {};
        this.hits.forEach((h, i) => { (byKind[h.kind] = byKind[h.kind] || []).push(i); });

        box.innerHTML = Object.entries(byKind).map(([kind, indexes]) => `
            <section class="scan-group">
                <header class="scan-group__head">
                    <label class="checkbox-row">
                        <input type="checkbox" data-kind="${kind}" checked>
                        <strong>${kind}</strong> <span class="muted">${indexes.length}</span>
                    </label>
                    <span class="muted scan-label">${this.hits[indexes[0]].label}</span>
                </header>
                ${indexes.map((i) => `
                    <label class="scan-row" data-row="${i}">
                        <input type="checkbox" data-hit="${i}" checked>
                        <code class="scan-text">${this.hits[i].text.replace(/[<>&]/g, '')}</code>
                        <span class="muted">page ${this.hits[i].page + 1}</span>
                    </label>`).join('')}
            </section>`).join('');

        $$('#scan-results input[data-hit]').forEach((b) => {
            b.addEventListener('change', () => this.setChosen(Number(b.dataset.hit), b.checked));
        });
        $$('#scan-results input[data-kind]').forEach((b) => {
            b.addEventListener('change', () => {
                byKind[b.dataset.kind].forEach((i) => this.setChosen(i, b.checked));
                this.syncBoxes();
            });
        });
        // Clicking the row itself walks to that page, so a value in the list
        // can be checked against where it actually sits before it is deleted.
        $$('#scan-results .scan-row').forEach((row) => {
            row.addEventListener('click', (e) => {
                if (e.target.tagName === 'INPUT') return;
                e.preventDefault();
                this.goToHit(Number(row.dataset.row));
            });
        });

        $('scan-actions').classList.remove('hidden');
    },

    setChosen(index, on) {
        if (on) this.chosen.add(index); else this.chosen.delete(index);
        this.markStyle(index);
    },

    /** Put every checkbox back in step with this.chosen. */
    syncBoxes() {
        $$('#scan-results input[data-hit]').forEach((b) => {
            b.checked = this.chosen.has(Number(b.dataset.hit));
        });
    },

    selectAll() {
        const turnOn = this.chosen.size < this.hits.length;
        this.chosen = new Set(turnOn ? this.hits.map((_, i) => i) : []);
        this.syncBoxes();
        $$('#scan-results input[data-kind]').forEach((b) => { b.checked = turnOn; });
        this.drawMarks();
    },

    async goToHit(index) {
        const hit = this.hits[index];
        if (!hit) return;
        if (hit.page !== this.view.page) await this.view.go(hit.page);
        const mark = $$(`#scan-overlay [data-mark="${index}"]`)[0];
        if (mark) {
            mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
            mark.classList.add('scan-mark--flash');
            setTimeout(() => mark.classList.remove('scan-mark--flash'), 1200);
        }
    },

    /** Draw a box over every hit on the page being viewed, so what is about
     *  to be permanently deleted can be seen before it is. */
    drawMarks() {
        const overlay = this.view.overlay;
        if (!overlay) return;
        overlay.innerHTML = '';
        this.hits.forEach((hit, index) => {
            if (hit.page !== this.view.page) return;
            hit.rects.forEach((r) => {
                const el = document.createElement('div');
                el.className = 'scan-mark';
                el.dataset.mark = String(index);
                el.title = `${hit.kind}: ${hit.text}`;
                el.style.cssText = `left:${r.xFrac * 100}%;top:${r.yFrac * 100}%;` +
                                   `width:${r.wFrac * 100}%;height:${r.hFrac * 100}%`;
                el.addEventListener('click', () => {
                    this.setChosen(index, !this.chosen.has(index));
                    this.syncBoxes();
                });
                overlay.appendChild(el);
            });
            this.markStyle(index);
        });
    },

    markStyle(index) {
        const on = this.chosen.has(index);
        $$(`#scan-overlay [data-mark="${index}"]`).forEach((el) => {
            el.classList.toggle('scan-mark--off', !on);
        });
    },

    /** A colour picker means nothing when the replacement is masked text. */
    modeChanged() {
        const masking = $('scan-mode').value === 'mask';
        $('scan-color-row').classList.toggle('hidden', masking);
        $('scan-redact').textContent = masking
            ? '🔒 Mask selected & save' : '⬛ Redact selected & save';
    },

    async redact() {
        const chosen = [...this.chosen].sort((a, b) => a - b).map((i) => this.hits[i]);
        if (!chosen.length) return UI.toast('Select at least one item', 'error');
        const mode = $('scan-mode').value;

        await UI.run(`Removing ${chosen.length} item(s)…`, async () => {
            await engine.call('redact_hits', 'scan', JSON.stringify(chosen),
                              $('scan-color').value, mode);
            const bytes = await engine.call('save', 'scan');
            download(bytes, mode === 'mask' ? 'masked.pdf' : 'redacted.pdf');
            // The document in the viewer is the redacted one now, so the marks
            // and the list would be pointing at text that no longer exists.
            this.hits = [];
            this.chosen = new Set();
            $('scan-results').innerHTML =
                `<div class="result-box"><strong>${chosen.length} item(s)</strong> permanently removed. ` +
                'Scan again to check nothing was missed.</div>';
            $('scan-actions').classList.add('hidden');
            await this.view.render();
            UI.toast(`${chosen.length} item(s) permanently removed`, 'success');
        });
    },
};

/* ------------------------------------------------------------------ */
/* 12. Inspect & sanitize                                              */
/* ------------------------------------------------------------------ */

const InspectTool = {
    init() {
        $('inspect-file').addEventListener('change', (e) => this.open(Array.from(e.target.files)));
        $('inspect-clean').addEventListener('click', () => this.clean());
    },

    async open(files) {
        const list = Array.isArray(files) ? files : [files].filter(Boolean);
        if (!list.length) return;
        this.files = list;
        if (list.length > 1) {
            // A findings list belongs to one document; in a batch the chosen
            // parts are simply stripped from each.
            $('inspect-findings').innerHTML =
                '<p class="muted">Findings are listed for a single document. ' +
                'With several chosen, the parts ticked below are stripped from every one.</p>';
            $('inspect-counts').innerHTML = '';
            return batchChosen('inspect', list);
        }
        const file = list[0];
        $('inspect-summary').classList.add('hidden');
        await UI.run('Inspecting document…', async () => {
            await engine.openDoc('inspect', await fileToBytes(file), file.name);
            const res = await engine.callJSON('inspect', 'inspect');
            $('inspect-workspace').classList.remove('hidden');
            $('inspect-result').classList.add('hidden');

            const c = res.counts;
            $('inspect-counts').innerHTML =
                `<strong>${file.name}</strong> - ${res.pages} page(s) · ` +
                `<span class="lvl lvl--risk">${c.risk || 0} risk</span> ` +
                `<span class="lvl lvl--warn">${c.warn || 0} to review</span> ` +
                `<span class="lvl lvl--info">${c.info || 0} informational</span>`;

            $('inspect-findings').innerHTML = res.findings.length
                ? res.findings.map((f) => `
                    <div class="finding finding--${f.level}">
                        <span class="finding__area">${f.area}</span>
                        <span class="finding__detail">${String(f.detail).replace(/[<>]/g, '')}</span>
                    </div>`).join('')
                : '<div class="result-box">Nothing hidden was found - this document is clean.</div>';
        });
    },

    async clean() {
        const strip = () => engine.callJSON('sanitize', 'inspect',
            $('san-metadata').checked, $('san-attach').checked, $('san-js').checked,
            $('san-annots').checked, $('san-links').checked, $('san-forms').checked,
            $('san-hidden').checked);
        if (this.files && this.files.length > 1) {
            return UI.run('Sanitizing files…', () => runBatch({
                files: this.files, docId: 'inspect', suffix: 'sanitized',
                apply: async () => { await strip(); return engine.call('save', 'inspect'); },
                report: (html) => batchReport('inspect', html),
            }));
        }
        await UI.run('Sanitizing…', async () => {
            const res = await engine.callJSON('sanitize', 'inspect',
                $('san-metadata').checked, $('san-attach').checked, $('san-js').checked,
                $('san-annots').checked, $('san-links').checked, $('san-forms').checked,
                $('san-hidden').checked);
            const bytes = await engine.call('save', 'inspect');
            download(bytes, 'sanitized.pdf');
            const box = $('inspect-result');
            box.classList.remove('hidden');
            box.innerHTML = res.removed.length
                ? `Removed:<ul>${res.removed.map((r) => `<li>${r}</li>`).join('')}</ul>`
                : 'Nothing needed removing.';
            UI.toast('Sanitized copy saved', 'success');
        });
    },
};

/* ------------------------------------------------------------------ */
/* 13. Find & replace                                                  */
/* ------------------------------------------------------------------ */

const ReplaceTool = {
    init() {
        $('replace-file').addEventListener('change', (e) => this.open(Array.from(e.target.files)));
        $('replace-preview').addEventListener('click', () => this.preview());
        $('replace-run').addEventListener('click', () => this.run());
    },

    async open(files) {
        const list = Array.isArray(files) ? files : [files].filter(Boolean);
        if (!list.length) return;
        this.files = list;
        if (list.length > 1) {
            // Previewing hits means looking at one document; across a batch
            // the replacement is applied straight out.
            $('replace-preview').disabled = true;
            return batchChosen('replace', list);
        }
        $('replace-preview').disabled = false;
        const file = list[0];
        await UI.run('Opening document…', async () => {
            const info = await engine.openDoc('replace', await fileToBytes(file), file.name);
            $('replace-workspace').classList.remove('hidden');
            $('replace-result').classList.add('hidden');
            $('replace-summary').innerHTML = `<strong>${file.name}</strong> - ${info.pages} page(s)`;
        });
    },

    async preview() {
        const find = $('replace-find').value;
        if (!find) return UI.toast('Enter text to find', 'error');
        await UI.run('Searching…', async () => {
            const res = await engine.callJSON('find_occurrences', 'replace', find,
                                              $('replace-pages').value);
            const box = $('replace-result');
            box.classList.remove('hidden');
            box.innerHTML = res.count
                ? `<strong>${res.count}</strong> match(es): ` +
                  res.hits.slice(0, 12).map((h) => `page ${h.page + 1}${h.font ? ` (${h.font} ${h.size}pt)` : ''}`).join(', ') +
                  (res.count > 12 ? ' …' : '')
                : 'No matches found.';
        });
    },

    async run() {
        const find = $('replace-find').value;
        if (!find) return UI.toast('Enter text to find', 'error');
        if (this.files && this.files.length > 1) {
            // A file with no match still belongs in the ZIP, unchanged - the
            // alternative is silently dropping it from the results.
            return UI.run('Replacing across files…', () => runBatch({
                files: this.files, docId: 'replace', suffix: 'replaced',
                apply: async () => {
                    await engine.call('find_replace', 'replace', find,
                                      $('replace-with').value, $('replace-pages').value);
                    return engine.call('save', 'replace');
                },
                report: (html) => batchReport('replace', html),
            }));
        }
        await UI.run('Replacing…', async () => {
            const n = await engine.call('find_replace', 'replace', find,
                                        $('replace-with').value, $('replace-pages').value);
            if (!n) return UI.toast('No matches found', 'error');
            const bytes = await engine.call('save', 'replace');
            download(bytes, 'replaced.pdf');
            const box = $('replace-result');
            box.classList.remove('hidden');
            box.innerHTML = `Replaced <strong>${n}</strong> occurrence(s). The original text was removed from the file, not covered over.`;
            UI.toast(`${n} replaced`, 'success');
        });
    },
};

/* ------------------------------------------------------------------ */
/* 14. Auto-split by content                                           */
/* ------------------------------------------------------------------ */

const AutoSplitTool = {
    init() {
        $('autosplit-file').addEventListener('change', (e) => this.open(e.target.files[0]));
        $('autosplit-preview').addEventListener('click', () => this.preview());
        $('autosplit-run').addEventListener('click', () => this.run());
    },

    async open(file) {
        if (!file) return;
        this.name = file.name.replace(/\.pdf$/i, '');
        await UI.run('Opening document…', async () => {
            const info = await engine.openDoc('autosplit', await fileToBytes(file), file.name);
            $('autosplit-workspace').classList.remove('hidden');
            $('autosplit-result').innerHTML = '';
            $('autosplit-run').disabled = true;
            $('autosplit-summary').innerHTML = `<strong>${file.name}</strong> - ${info.pages} page(s)`;
        });
    },

    args() {
        return [$('autosplit-pattern').value, $('autosplit-regex').checked,
                $('autosplit-name').checked];
    },

    async preview() {
        const [pattern] = this.args();
        if (!pattern) return UI.toast('Enter a marker to split on', 'error');
        await UI.run('Finding split points…', async () => {
            const res = await engine.callJSON('split_by_pattern', 'autosplit', ...this.args(), true);
            const box = $('autosplit-result');
            if (!res.parts.length) {
                box.innerHTML = `<div class="result-box">${res.message || 'No page matched.'}</div>`;
                $('autosplit-run').disabled = true;
                return;
            }
            box.innerHTML = `<div class="result-box">Will produce <strong>${res.count}</strong> file(s):</div>` +
                res.parts.map((p) => `
                    <div class="split-row">
                        <code>${p.name.replace(/[<>&]/g, '')}</code>
                        <span class="muted">pages ${p.from}–${p.to} (${p.pages})</span>
                    </div>`).join('');
            $('autosplit-run').disabled = false;
        });
    },

    async run() {
        await UI.run('Splitting…', async () => {
            const res = await engine.callJSON('split_by_pattern', 'autosplit', ...this.args(), false);
            if (!res.parts.length) return UI.toast('No page matched', 'error');
            const zip = new JSZip();
            const used = new Map();
            res.parts.forEach((p) => {
                // Two invoices can carry the same marker; keep both.
                const n = (used.get(p.name) || 0) + 1;
                used.set(p.name, n);
                zip.file(n > 1 ? p.name.replace(/\.pdf$/, `-${n}.pdf`) : p.name, b64ToBytes(p.b64));
            });
            download(await zip.generateAsync({ type: 'blob' }), `${this.name}-split.zip`, 'application/zip');
            UI.toast(`Split into ${res.count} files`, 'success');
        });
    },
};

/* ------------------------------------------------------------------ */
/* dashboard                                                           */
/* ------------------------------------------------------------------ */

// One manifest drives the home cards, so adding a tool means editing one place.
const TOOL_CARDS = [
    { group: 'Edit', tab: 'edit', icon: '📝', name: 'Edit Text',
      blurb: 'Rewrite text for real - line by line or a whole paragraph with reflow.' },
    { group: 'Edit', tab: 'sign', icon: '🖊️', name: 'Fill & Sign',
      blurb: 'Draw or type a signature, add dates and checks, fill form fields.' },
    { group: 'Edit', tab: 'stamp', icon: '💧', name: 'Watermark & Stamps',
      blurb: 'Watermarks, approval stamps, page numbers and Bates numbering.' },
    { group: 'Edit', tab: 'replace', icon: '🔁', name: 'Find & Replace',
      blurb: 'Search and replace inside a PDF, matching the original styling.', badge: 'New' },

    { group: 'Organise', tab: 'pages', icon: '📚', name: 'Pages',
      blurb: 'Merge, split, reorder by drag, rotate and delete.' },
    { group: 'Organise', tab: 'autosplit', icon: '✂️', name: 'Auto-Split',
      blurb: 'Break a bundle into one file per invoice, named from the text.', badge: 'New' },
    { group: 'Organise', tab: 'compare', icon: '⚖️', name: 'Compare',
      blurb: 'Word-level diff between two versions, with changes highlighted.' },
    { group: 'Organise', tab: 'compress', icon: '🗜️', name: 'Compress',
      blurb: 'Shrink the file while text stays real, searchable text.' },

    { group: 'Protect', tab: 'scan', icon: '🛡️', name: 'Find Sensitive Data',
      blurb: 'Detect PAN, GSTIN, Aadhaar, cards and more - then redact them.', badge: 'New' },
    { group: 'Protect', tab: 'inspect', icon: '🕵️', name: 'Inspect & Sanitize',
      blurb: 'See what hides in a PDF - metadata, scripts, attachments - and strip it.', badge: 'New' },
    { group: 'Protect', tab: 'redact', icon: '⬛', name: 'Redact',
      blurb: 'Black out content so it is deleted from the file, not just covered.' },
    { group: 'Protect', tab: 'protect', icon: '🔒', name: 'Password Protect',
      blurb: 'AES-256 encryption with per-permission control.' },

    { group: 'Convert', tab: 'ocr', icon: '🔍', name: 'OCR',
      blurb: 'Turn a scan into a genuinely searchable, selectable document.' },
    { group: 'Convert', tab: 'export', icon: '📤', name: 'Export',
      blurb: 'Word, plain text, tables to CSV, images and page renders.' },
];

const Dashboard = {
    init() {
        const root = $('dashboard');
        if (!root) return;
        const groups = [];
        TOOL_CARDS.forEach((card) => {
            // Skip anything whose tab is missing, so the two can't fall out of sync.
            if (!$(`${card.tab}-tab`)) return;
            let group = groups.find((g) => g.name === card.group);
            if (!group) { group = { name: card.group, cards: [] }; groups.push(group); }
            group.cards.push(card);
        });

        root.innerHTML = groups.map((g) => `
            <section class="dash-group">
                <h3 class="dash-group__title">${g.name}</h3>
                <div class="dash-grid">
                    ${g.cards.map((c) => `
                        <button class="dash-card" data-goto="${c.tab}">
                            <span class="dash-card__icon">${c.icon}</span>
                            <span class="dash-card__name">${c.name}${
                                c.badge ? `<span class="dash-card__badge">${c.badge}</span>` : ''}</span>
                            <span class="dash-card__blurb">${c.blurb}</span>
                        </button>`).join('')}
                </div>
            </section>`).join('');

        $$('[data-goto]', root).forEach((btn) => {
            btn.addEventListener('click', () => UI.showTab(btn.dataset.goto));
        });
    },
};

const Tools = [EditTool, PagesTool, SignTool, StampTool, OcrTool,
               CompressTool, ProtectTool, RedactTool, ExportTool, CompareTool,
               ScanTool, InspectTool, ReplaceTool, AutoSplitTool, Dashboard];

// Start fetching the engine immediately - this file is loaded at the end of
// <body>, so the download overlaps with DOM construction instead of queueing
// behind it. Errors are handled when the UI attaches its progress display.
engine.boot().catch(() => {});

document.addEventListener('DOMContentLoaded', () => UI.init());

// Keep the engine in Cache Storage rather than leaving it to the HTTP cache,
// which evicts it often enough that returning visitors paid the full download
// again. Registered after load so it never competes with the first paint.
// Harmless where service workers are unavailable (private windows, file://).
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register(`sw.js?v=${APP_VERSION}`).catch(() => {});
    });
}

window.PDFSuite = { engine, UI, Tools, TOOL_CARDS, Dirty };
