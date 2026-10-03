const YT = require('../../lib/ytultra/ytultra');
module.exports = function(app) {
    app.get('/yt/ytultra/info', async (req, res) => {
        const url = req.query.url;
        if (!url) return res.status(400).json({ status: false, error: 'url required' });
        try {
            const info = await YT.fetchInfo(url);
            res.json({ status: true, source: 'ytultra', videoId: info.videoId, title: info.title, image: info.imageUrl, duration: info.duration, videos: info.videoOnly, audios: info.audioOnly });
        } catch (e) { res.status(502).json({ status: false, error: e.message }); }
    });
    app.get('/yt/ytultra/pick', async (req, res) => {
        const { url, quality } = req.query;
        if (!url || !quality) return res.status(400).json({ status: false, error: 'url and quality required' });
        try {
            const info = await YT.fetchInfo(url);
            const m = YT.pick(info, quality);
            res.json({ status: true, source: 'ytultra', itag: m.itag, kind: m.kind, quality: m.quality, ext: m.ext, sizeMB: m.fileSize ? +(m.fileSize/1048576).toFixed(2) : null, url: m.url });
        } catch (e) { res.status(502).json({ status: false, error: e.message }); }
    });
};
