const express = require('express');
const timeEntryController = require('../controllers/timeEntry.controller');
const authMiddleware = require('../middleware/auth.middleware');

const router = express.Router();

// Apply authentication middleware to all routes
router.use(authMiddleware.verifyToken);

// GET /api/timeentries/month/:monthId - Get time entries for a month
router.get('/month/:monthId', timeEntryController.getTimeEntriesForMonth);

// GET /api/timeentries/:id - Get a time entry by ID
router.get('/:id', timeEntryController.getTimeEntryById);

// POST /api/timeentries - Save a time entry (create or update)
router.post('/', timeEntryController.saveTimeEntry);

// DELETE /api/timeentries/:id - Delete a time entry
router.delete('/:id', authMiddleware.isManagerOrAdmin, timeEntryController.deleteTimeEntry);

module.exports = router;