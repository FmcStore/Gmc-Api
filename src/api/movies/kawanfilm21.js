const { proxy } = require('../../lib/bridge');
module.exports = function(app) {
    app.get('/movies/kawanfilm21', (req, res) => proxy('kawanfilm21', '/api/movies', req.query, res));
    app.get('/movies/kawanfilm21/latest', (req, res) => proxy('kawanfilm21', '/api/movies/latest', req.query, res));
    app.get('/movies/kawanfilm21/tv', (req, res) => proxy('kawanfilm21', '/api/tv', req.query, res));
    app.get('/movies/kawanfilm21/episodes', (req, res) => proxy('kawanfilm21', '/api/episodes', req.query, res));
    app.get('/movies/kawanfilm21/stats', (req, res) => proxy('kawanfilm21', '/api/stats', req.query, res));
    app.get('/movies/kawanfilm21/genres', (req, res) => proxy('kawanfilm21', '/api/genres', req.query, res));
    app.get('/movies/kawanfilm21/search', (req, res) => proxy('kawanfilm21', '/api/search', req.query, res));
    app.get('/movies/kawanfilm21/:kind/:id', (req, res) => proxy('kawanfilm21', `/api/${req.params.kind}/${req.params.id}`, req.query, res));
    app.get('/movies/kawanfilm21/:kind/:id/downloads', (req, res) => proxy('kawanfilm21', `/api/${req.params.kind}/${req.params.id}/downloads`, req.query, res));
    app.get('/movies/kawanfilm21/genres/:id', (req, res) => proxy('kawanfilm21', `/api/genres/${req.params.id}`, req.query, res));
    app.get('/movies/kawanfilm21/genres/:id/items', (req, res) => proxy('kawanfilm21', `/api/genres/${req.params.id}/items`, req.query, res));
};
