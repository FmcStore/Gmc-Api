#!/usr/bin/env node
// vynex-yt-scraper.js — YouTube video/audio downloader via vynex.ai internal API
// Usage:
//   node vynex-yt-scraper.js info --url "<youtube_url>"
//   node vynex-yt-scraper.js video --url "<youtube_url>" --height 720
//   node vynex-yt-scraper.js audio --url "<youtube_url>"
//   node vynex-yt-scraper.js all   --url "<youtube_url>" --height 720

const https = require('https');
const fs = require('fs');
const path = require('path');

const API = 'https://vynex.ai/api/tools/youtube-video';
const UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';

function get(urlStr) {
  return new Promise((resolve, reject) => {
    const req = https.get(urlStr, { headers: { 'User-Agent': UA, 'Referer': 'https://vynex.ai/tools/youtube-video-downloader', 'Accept': 'application/json' } }, (res) => {
      let data = '';
      res.on('data', (c) => data += c);
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(data), headers: res.headers }); }
        catch (e) { reject(new Error('Bad JSON from ' + urlStr + ': ' + data.slice(0, 200))); }
      });
    });
    req.on('error', reject);
  });
}

function extractId(url) {
  const m = url.match(/(?:youtube\.com\/(?:watch\?v=|shorts\/|embed\/)|youtu\.be\/)([A-Za-z0-9_-]{11})/);
  return m ? m[1] : (/^[A-Za-z0-9_-]{11}$/.test(url) ? url : null);
}

async function info(url) {
  const id = extractId(url);
  if (!id) throw new Error('URL tidak valid. Butuh link YouTube / youtu.be / id 11 char.');
  const r = await get(API + '?url=' + encodeURIComponent(url));
  if (r.status !== 200 || r.body.error) throw new Error(r.body.error || ('HTTP ' + r.status));
  return r.body;
}

function download(url, outPath) {
  return new Promise((resolve, reject) => {
    const file = fs.createWriteStream(outPath + '.part');
    const req = https.get(url, { headers: { 'User-Agent': UA, 'Referer': 'https://vynex.ai/' } }, (res) => {
      // follow redirects
      if ([301,302,303,307,308].includes(res.statusCode) && res.headers.location) {
        file.close(); fs.unlinkSync(outPath + '.part');
        return resolve(download(res.headers.location, outPath));
      }
      if (res.statusCode !== 200) { file.close(); fs.unlinkSync(outPath + '.part'); return reject(new Error('HTTP ' + res.statusCode)); }
      const total = parseInt(res.headers['content-length'] || '0', 10);
      let got = 0;
      res.on('data', (c) => { got += c.length; process.stderr.write(`\r${(got/1048576).toFixed(1)} MB downloaded`); });
      res.pipe(file);
      file.on('finish', () => file.close(() => {
        process.stderr.write('\n');
        const written = fs.statSync(outPath + '.part').size;
        if (total && written !== total) return reject(new Error(`Truncated: ${written}/${total}`));
        fs.renameSync(outPath + '.part', outPath);
        resolve(outPath);
      }));
    });
    req.on('error', (e) => { try { fs.unlinkSync(outPath + '.part'); } catch {} reject(e); });
  });
}

function dlUrl(kind, id, opts) {
  const token = Date.now().toString(36) + Math.random().toString(36).slice(2);
  if (kind === 'video') return `${API}/download?id=${encodeURIComponent(id)}&height=${opts.height}&token=${token}`;
  return `${API}/audio?id=${encodeURIComponent(id)}&format_id=${encodeURIComponent(opts.format_id)}&token=${token}`;
}


module.exports = { info, get, extractId, dlUrl, API };
