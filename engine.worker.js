/* The PDF engine, running off the main thread.
 *
 * Everything here used to run in the page. That meant the browser could not
 * repaint while a document was being worked on: the interface froze, no
 * progress could be shown however hard it tried, and on a long job Chrome
 * offered to kill the tab - "Page unresponsive - Exit / Wait" - while the
 * work was in fact going perfectly well.
 *
 * So the whole of Pyodide and PyMuPDF lives in this worker, and the page
 * talks to it by message. The page then has nothing to do but draw, which is
 * what makes a progress bar possible at all.
 */

let pyodide = null;
let engineModule = null;
let booting = null;

const post = (message) => self.postMessage(message);

/** Turn a Python return value into something structured-clone can carry. */
function unwrap(value) {
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

async function boot({ indexURL, wheel, engineSource }) {
    post({ type: 'progress', message: 'Setting up your workspace…', pct: 5 });
    // A module worker resolves a bare path like "pyodide/" as a package name,
    // so make it a real URL against the worker's own location first.
    const entry = new URL(`${indexURL}pyodide.mjs`, self.location.href).href;
    const { loadPyodide } = await import(entry);
    pyodide = await loadPyodide({ indexURL });

    post({ type: 'progress', message: 'Downloading PDF tools (17.5 MB, one time)…', pct: -1 });
    await pyodide.loadPackage(wheel);

    post({ type: 'progress', message: 'Almost ready…', pct: 90 });
    pyodide.FS.writeFile('/pdf_engine.py', engineSource);
    pyodide.runPython('import sys\nif "/" not in sys.path: sys.path.insert(0, "/")');
    engineModule = pyodide.pyimport('pdf_engine');

    // A way for the long loops inside the engine to say where they have got
    // to. Without it the only honest thing to show is a spinner.
    try {
        pyodide.globals.set('miyee_progress', (done, total, label) => {
            post({ type: 'step', done, total, label: label || '' });
        });
        engineModule.set_progress_hook(pyodide.globals.get('miyee_progress'));
    } catch (err) {
        // An engine without the hook still works; it just cannot report.
    }

    post({ type: 'progress', message: 'Ready', pct: 100 });
    post({ type: 'ready' });
}

self.onmessage = async (event) => {
    const { id, type } = event.data || {};

    if (type === 'boot') {
        if (!booting) {
            booting = boot(event.data).catch((err) => {
                post({ type: 'bootfailed', error: String((err && err.message) || err) });
                booting = null;
                throw err;
            });
        }
        try { await booting; } catch (err) { /* already reported */ }
        return;
    }

    if (type === 'call') {
        const { fn, args } = event.data;
        try {
            if (!engineModule) throw new Error('The engine is not ready yet.');
            const target = engineModule[fn];
            if (!target) throw new Error(`pdf_engine has no function '${fn}'`);
            post({ id, ok: true, result: unwrap(target(...args)) });
        } catch (err) {
            // Python tracebacks arrive as one long string; the page turns the
            // last line into something a person can read.
            post({ id, ok: false, error: String((err && err.message) || err) });
        }
    }
};
