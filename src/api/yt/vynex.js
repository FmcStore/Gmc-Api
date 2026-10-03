const V = require('../../lib/vynex-yt');
module.exports = function(app) {
    app.get('/yt/vynex/info', async (req, res) => {
        const url = req.query.url;
        if (!url) return res.status(400).json({ status: false, error: 'url required' });
        try {
            const data = await V.info(url);
            const { formats, audio, ...meta } = data;
            res.json({ status: true, source: 'vynex', ...meta, video_formats: formats, audio_tracks: audio || [] });
        } catch (e) { res.status(502).json({ status: false, error: e.message }); }
    });
    app.get('/yt/vynex/link', async (req, res) => {
        const { url, height } = req.query;
        if (!url) return res.status(400).json({ status: false, error: 'url required' });
        try {
            const data = await V.info(url);
            const h = parseInt(height || '720', 10);
            const fmt = (data.formats || []).find(f => f.height === h) || (data.formats || [])[0];
            if (!fmt) return res.status(404).json({ status: false, error: 'no formats available' });
            const link = fmt.url || V.dlUrl('video', data.id, { height: fmt.height });
            res.json({ status: true, source: 'vynex', id: data.id, title: data.title, height: fmt.height, label: fmt.label, ext: fmt.ext, sizeMB: fmt.size ? +(fmt.size/1048576).toFixed(2) : null, url: link });
        } catch (e) { res.status(502).json({ status: false, error: e.message }); }
    });
};
