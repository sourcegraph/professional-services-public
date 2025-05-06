const express = require('express');
const monthController = require('../controllers/month.controller');
const authMiddleware = require('../middleware/auth.middleware');

const router = express.Router();

// Apply authentication middleware to all routes
router.use(authMiddleware.verifyToken);

// GET /api/months - Get all months
router.get('/', monthController.getAllMonths);

// POST /api/months/create - Create a new month
router.post('/create', monthController.createMonth);

// GET /api/months/:id - Get a month by ID
router.get('/:id', monthController.getMonthById);

module.exports = router;