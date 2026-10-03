const { proxy } = require('../../lib/bridge');
module.exports = function(app) {
    app.get('/anime/sokuja/latest', (req, res) => proxy('sokuja', '/api/latest', req.query, res));
    app.get('/anime/sokuja/search', (req, res) => proxy('sokuja', '/api/search', req.query, res));
    app.get('/anime/sokuja/anime/:slug', (req, res) => proxy('sokuja', `/api/anime/${req.params.slug}`, req.query, res));
    app.get('/anime/sokuja/episode/:slug', (req, res) => proxy('sokuja', `/api/episode/${req.params.slug}`, req.query, res));
    app.get('/anime/sokuja/genres', (req, res) => proxy('sokuja', '/api/genres', req.query, res));
    app.get('/anime/sokuja/genre/:slug', (req, res) => proxy('sokuja', `/api/genre/${req.params.slug}`, req.query, res));
    app.get('/anime/sokuja/browse', (req, res) => proxy('sokuja', '/api/browse', req.query, res));
    app.get('/anime/sokuja/schedule', (req, res) => proxy('sokuja', '/api/schedule', req.query, res));
};
