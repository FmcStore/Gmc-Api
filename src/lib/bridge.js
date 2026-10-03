// bridge.js — menjalankan scraper Python sebagai child process dari API Node
// sehingga GMC UI berdiri sendiri (satu repo, satu `npm start`).
//
// Sokuja        : src/lib/sokuja/sokuja_api.py
// KawanFilm21   : src/lib/kawanfilm21/app.py  (+ data/kawanfilm21.db)
//
// Semua logala scraper hidup di folder GMC UI; folder asal (/root/sokuja-api,
// /root/kawanfilm21-api) tidak pernah disentuh.
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');

let CREATOR = 'GMC';
try {
    CREATOR = JSON.parse(
        require('fs').readFileSync(path.join(ROOT, 'src', 'settings.json'), 'utf-8')
    ).apiSettings?.creator || 'GMC';
} catch (_) { /* pakai default */ }

function freePort() {
    return new Promise((resolve, reject) => {
        const srv = net.createServer();
        srv.unref();
        srv.on('error', reject);
        srv.listen(0, '127.0.0.1', () => {
            const p = srv.address().port;
            srv.close(() => resolve(p));
        });
    });
}

async function waitUp(url, tries = 60, delay = 500) {
    for (let i = 0; i < tries; i++) {
        try {
            const r = await fetch(url);
            if (r.ok || r.status === 404) return true;
        } catch (_) { /* belum hidup */ }
        await new Promise((r) => setTimeout(r, delay));
    }
    return false;
}

const services = new Map(); // name -> { base, proc, starting }

async function ensure(name) {
    if (services.has(name)) return services.get(name);

    const pending = (async () => {
        const port = await freePort();
        let cmd, args, cwd, base, health;

        if (name === 'sokuja') {
            cwd = path.join(ROOT, 'src', 'lib', 'sokuja');
            args = ['sokuja_api.py', String(port)];
            base = `http://127.0.0.1:${port}`;
            health = `${base}/api/latest?page=1`;
        } else if (name === 'kawanfilm21') {
            cwd = path.join(ROOT, 'src', 'lib', 'kawanfilm21');
            args = ['app.py', '--port', String(port)];
            base = `http://127.0.0.1:${port}`;
            health = `${base}/health`;
        } else {
            throw new Error('unknown service: ' + name);
        }

        const proc = spawn('python3', args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
        proc.stdout.on('data', (b) => process.stdout.write(`[${name}] ${b}`));
        proc.stderr.on('data', (b) => process.stderr.write(`[${name}] ${b}`));
        proc.on('exit', (code) => {
            services.delete(name);
            console.error(`[${name}] exited code=${code}`);
        });

        const ok = await waitUp(health);
        if (!ok) throw new Error(`${name} gagal start (port ${port})`);

        const svc = { base, proc };
        services.set(name, svc);
        return svc;
    })();

    const holder = { starting: pending };
    services.set(name, holder);
    return pending;
}

/** Proxy request apa adanya ke scraper Python. Status upstream diteruskan. */
async function proxy(name, urlPath, query, res) {
    let svc;
    try {
        svc = await ensure(name);
    } catch (e) {
        return res.status(502).json({ status: false, error: `${name} tidak jalan: ${e.message}` });
    }
    const qs = query && Object.keys(query).length
        ? '?' + new URLSearchParams(query).toString()
        : '';
    try {
        const r = await fetch(svc.base + urlPath + qs, { headers: { Accept: 'application/json' } });
        let text = await r.text();
        try {
            const body = JSON.parse(text);
            if (body && typeof body === 'object' && !Array.isArray(body)) {
                // samakan identitas dengan setting repo: ganti Rynn lama, atau tambahkan
                body.creator = CREATOR;
                text = JSON.stringify(body, null, 2);
            }
        } catch (_) { /* bukan JSON, kirim apa adanya */ }
        res.status(r.status);
        res.set('Content-Type', r.headers.get('content-type') || 'application/json');
        return res.send(text);
    } catch (e) {
        return res.status(502).json({ status: false, error: e.message });
    }
}

function shutdown() {
    for (const [, s] of services) {
        const svc = s && s.base ? s : null;
        if (svc && svc.proc) { try { svc.proc.kill(); } catch (_) {} }
    }
}
process.on('exit', shutdown);
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

module.exports = { ensure, proxy };