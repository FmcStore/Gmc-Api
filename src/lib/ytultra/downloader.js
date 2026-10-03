// Downloader + merger
const fs = require('fs');
const path = require('path');
const https = require('https');
const { spawnSync } = require('child_process');
const { fetchInfo, pick, UA } = require('./ytultra');

function safeName(s, fallback) {
  const base = String(s || fallback || 'video')
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 120);
  return base || fallback;
}

/** Streaming download dengan progress, panjang = byte total */
function download(url, dest, { timeoutMs = 60000 } = {}) {
  return new Promise((resolve, reject) => {
    const tmp = dest + '.part';
    const out = fs.createWriteStream(tmp);
    let written = 0, total = 0, redirects = 0;

    const start = (u, redirectsLeft) => {
      const req = https.get(u, {
        headers: {
          'User-Agent': UA,
          Accept: '*/*',
          'Accept-Language': 'id-ID,id;q=0.9,en;q=0.8',
          Origin: 'https://www.ytultra.com',
          Referer: 'https://www.ytultra.com/',
        },
      }, (res) => {
        if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location && redirectsLeft > 0) {
          res.resume();
          return start(new URL(res.headers.location, u).toString(), redirectsLeft - 1);
        }
        if (res.statusCode !== 200) {
          res.resume();
          return reject(new Error('Download HTTP ' + res.statusCode));
        }
        total = parseInt(res.headers['content-length'] || '0', 10);
        res.on('data', (c) => {
          written += c.length;
          if (process.env.QUIET !== '1') {
            const pct = total ? Math.floor((written / total) * 100) : 0;
            const mb = (n) => (n / 1048576).toFixed(1);
            process.stderr.write(`\r  ${path.basename(dest)}  ${mb(written)}/${mb(total)} MB (${pct}%)   `);
          }
        });
        res.pipe(out);
        out.on('finish', () => {
          // verifikasi ukuran vs content-length (jangan lapor ok kalau terpotong)
          if (total && written < total) {
            out.close();
            return reject(new Error(`File terpotong: ${written}/${total} byte`));
          }
          fs.renameSync(tmp, dest);
          if (process.env.QUIET !== '1') process.stderr.write('\n');
          resolve({ bytes: written, path: dest });
        });
        out.on('error', reject);
      });
      req.setTimeout(timeoutMs, () => req.destroy(new Error('Download timeout')));
      req.on('error', (e) => { out.close(); fs.existsSync(tmp) && fs.unlinkSync(tmp); reject(e); });
    };
    start(url, 5);
  });
}

function mergeTracks(videoPath, audioPath, outPath) {
  const r = spawnSync('ffmpeg', ['-y', '-i', videoPath, '-i', audioPath,
    '-c', 'copy', '-map', '0:v:0', '-map', '1:a:0', outPath],
    { encoding: 'utf8' });
  if (r.status !== 0) throw new Error('ffmpeg merge gagal: ' + (r.stderr || '').slice(-500));
  return outPath;
}

/**
 * downloadVideo(url, opts)
 *  - quality: 'best' | 'worst' | '<tinggi>p' | itag
 *  - merge: true -> jika video != mp4 muxed (hanya video-only), ambil audio terbaik lalu merge ffmpeg
 *  - dir, qualitySelectorHook: (info) => media
 */
async function downloadVideo(url, opts = {}) {
  const { quality = 'best', dir = '.', merge = true, onProgress = null, mediaSelector = null, fileName = null } = opts;
  const info = await fetchInfo(url);
  if (onProgress) onProgress({ stage: 'info', info });

  fs.mkdirSync(dir, { recursive: true });
  const title = safeName(fileName || info.title, info.videoId);

  const muxedItags = ['18', '22'];   // itag yang sudah carry video+audio
  const chosen = mediaSelector ? mediaSelector(info) : pick(merge ? info.videoOnly : info.medias, quality);

  if (!chosen) throw new Error('Kualitas video tidak tersedia');
  if (chosen.locked) throw new Error('Media terkunci (butuh login/premium): itag ' + chosen.itag);

  // merge HANYA kalau media terpilih memang tidak membawa audio
  const useMerge = merge && !muxedItags.includes(chosen.itag);

  const vPath = path.join(dir, `${title} [${chosen.itag}].${chosen.ext}`);
  const res = await download(chosen.url, vPath);

  if (!useMerge) {
    if (onProgress) onProgress({ stage: 'done', file: res.path, bytes: res.bytes, merged: false, info, media: chosen });
    return { ...res, merged: false, info, media: chosen };
  }

  const audio = info.audioOnly[0];
  if (!audio) {
    if (onProgress) onProgress({ stage: 'done', file: res.path, bytes: res.bytes, merged: false, info, media: chosen });
    return { ...res, merged: false, info, media: chosen, note: 'tidak ada track audio terpisah' };
  }

  const aPath = path.join(dir, `${title} [${audio.itag}].${audio.ext}`);
  await download(audio.url, aPath);
  const outPath = path.join(dir, `${title}.mp4`);
  mergeTracks(vPath, aPath, outPath);
  if (process.env.KEEP_PARTS !== '1') { fs.unlinkSync(vPath); fs.unlinkSync(aPath); }

  const stat = fs.statSync(outPath);
  if (onProgress) onProgress({ stage: 'done', file: outPath, bytes: stat.size, merged: true, info, media: chosen, audio });
  return { path: outPath, bytes: stat.size, merged: true, info, media: chosen, audio };
}

module.exports = { download, downloadVideo, mergeTracks, safeName };