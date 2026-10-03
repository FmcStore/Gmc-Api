// Low-level client untuk API internal ytultra.com
const https = require('https');
const { URL } = require('url');

const API_HOST = 'api.ytultra.com';
const API_PATH = '/ikool/youtube/download';
const WEB_ORIGIN = 'https://www.ytultra.com';

const UA = 'Mozilla/5.0 (Linux; Android 16; 24117RN76O Build/BP2A.250605.031.A3) ' +
  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.8010.36 Mobile Safari/537.36';

function headers() {
  return {
    'User-Agent': UA,
    'Content-Type': 'application/json',
    Accept: 'application/json, text/plain, */*',
    'Accept-Language': 'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7',
    Origin: WEB_ORIGIN,
    Referer: WEB_ORIGIN + '/',
    'x-requested-with': 'mark.via.gp',
    'sec-ch-ua': '"Android WebView";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
    'sec-ch-ua-mobile': '?1',
    'sec-ch-ua-platform': '"Android"',
    'sec-fetch-site': 'same-site',
    'sec-fetch-mode': 'cors',
    'sec-fetch-dest': 'empty',
    priority: 'u=1, i',
  };
}

/** POST JSON ke API, return {status, json} */
function apiPost(path, body, timeoutMs = 30000) {
  const payload = Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const req = https.request(
      { host: API_HOST, path, method: 'POST', headers: { ...headers(), 'Content-Length': payload.length } },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          let json = null;
          try { json = JSON.parse(text); } catch (_) {}
          resolve({ status: res.statusCode, json, text });
        });
      }
    );
    req.setTimeout(timeoutMs, () => req.destroy(new Error('API timeout ' + timeoutMs + 'ms')));
    req.on('error', reject);
    req.end(payload);
  });
}

/** Ambil id video dari berbagai bentuk input (id, watch?v=, youtu.be/, shorts/) */
function extractVideoId(input) {
  const s = String(input || '').trim();
  if (!s) throw new Error('URL kosong');
  if (/^[\w-]{11}$/.test(s)) return s;
  let u;
  try { u = new URL(s); } catch (_) { throw new Error('URL tidak valid: ' + s); }
  const host = u.hostname.replace(/^www\./, '');
  if (host === 'youtu.be') return u.pathname.slice(1).split('/')[0];
  if (!/(^|\.)youtube(-nocookie)?\.com$/.test(host)) {
    throw new Error('Hanya YouTube yang didukung, dapat: ' + host);
  }
  const v = u.searchParams.get('v');
  if (v && /^[\w-]{11}$/.test(v)) return v;
  if (v) throw new Error('Video id tidak valid: ' + v);
  const m = u.pathname.match(/\/(shorts|embed|live|v)\/([\w-]{11})/);
  if (m) return m[2];
  throw new Error('Video id tidak ditemukan di URL: ' + s);
}

/**
 * Hitung API, kembalikan struktur rapi:
 * { videoId, input, imageUrl, medias: [{itag, kind, quality, height, ext, fileSize, url}] }
 */
async function fetchInfo(input) {
  const videoId = extractVideoId(input);
  const payloadUrl = 'https://www.youtube.com/watch?v=' + videoId;
  const res = await apiPost(API_PATH, { url: payloadUrl });

  if (res.status !== 200) throw new Error('HTTP ' + res.status + ' dari API');
  const j = res.json;
  if (!j || j.code !== '0000') throw new Error('API error: ' + (j ? j.code + ' ' + j.msg : res.text.slice(0, 200)));

  const data = j.data || {};
  const seen = new Set();
  const medias = [];
  for (const m of data.medias || []) {
    if (!m || !m.url) continue;
    const itag = (m.url.match(/[?&]itag=(\d+)/) || [])[1] || null;
    const key = (itag || '') + '|' + (m.format || '');
    if (seen.has(key)) continue;            //(itag 140 audio dobel per kualitas)
    seen.add(key);
    const fmt = m.format || '';
    const ext = (fmt.match(/\[\.(\w+)\]/) || [])[1] ||
      ((m.url.match(/mime=video%2F(\w+)/) || [])[1] || (m.url.match(/mime=audio%2F([\w.+-]+)/) || [])[1] || 'mp4');
    const isAudio = /^audio\//.test(decodeURIComponent((m.url.match(/mime=([^&]+)/) || [])[1] || ''));
    const height = parseInt((fmt.match(/(\d{3,4})p/) || [])[1] || '0', 10);
    medias.push({
      itag,
      kind: isAudio ? 'audio' : 'video',
      quality: fmt.replace(/\s*\(.*$/, '') || null,   // "1080p (16.91 MB) [.mp4]" -> "1080p"
      height,
      ext: ext.replace('mp4a', 'm4a'),
      fileSize: m.fileSize || null,
      sizeStr: m.sizeStr || null,
      locked: m.locked === true,
      url: m.url,
    });
  }

  medias.sort((a, b) => (b.height - a.height) || ((b.fileSize || 0) - (a.fileSize || 0)));

  // Buang entri video tanpa tinggi (format "None") supaya tidak jadi pilihan best/worst
  const videoOnly = medias.filter((m) => m.kind === 'video' && m.height > 0);

  return {
    videoId,
    input,
    title: data.title || null,
    imageUrl: data.imageUrl || null,
    duration: data.duration || null,
    medias,
    videoOnly,
    audioOnly: medias.filter((m) => m.kind === 'audio'),
  };
}

/** Pilih media: quality mis. "1080p", 'best' | 'worst' | '<tinggi>p' | itag angka */
function pick(medias, quality) {
  if (!medias.length) throw new Error('Tidak ada media tersedia (mungkin video privat/terblokir)');
  const q = String(quality || 'best').toLowerCase();
  if (q === 'best') return medias[0];
  if (q === 'worst') return medias[medias.length - 1];
  if (/^\d+$/.test(q) && !q.endsWith('p')) {
    const byItag = medias.find((m) => m.itag === q);
    if (byItag) return byItag;
  }
  const h = parseInt(q.replace('p', ''), 10);
  if (!Number.isNaN(h)) {
    const exact = medias.filter((m) => m.height === h);
    if (exact.length) return exact[0];
    //below the requested height -> the HIGHEST one under it (list is sorted desc)
    const lower = medias.filter((m) => m.height > 0 && m.height < h);
    if (lower.length) return lower[0];
    //asking ABOVE what exists -> take the best available, don't silently jump to the worst
    const above = medias.filter((m) => m.height > 0);
    if (above.length) return above[0];
  }
  throw new Error('Kualitas tidak tersedia: ' + quality);
}

module.exports = { apiPost, extractVideoId, fetchInfo, pick, headers, API_HOST, API_PATH, WEB_ORIGIN, UA };