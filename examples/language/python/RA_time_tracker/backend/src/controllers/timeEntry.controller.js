const timeEntryService = require('../services/timeEntry.service');

/**
 * TimeEntry Controller - Handles HTTP requests for time entry management
 */
class TimeEntryController {
  /**
   * Get time entries for a month
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async getTimeEntriesForMonth(req, res) {
    try {
      const monthId = req.params.monthId;
      
      if (!monthId) {
        return res.status(400).json({ message: 'Month ID is required' });
      }
      
      const timeEntries = await timeEntryService.getTimeEntriesForMonth(monthId);
      res.json(timeEntries);
    } catch (error) {
      console.error('Error getting time entries:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while retrieving time entries.'
      });
    }
  }

  /**
   * Get a time entry by ID
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async getTimeEntryById(req, res) {
    try {
      const id = req.params.id;
      const timeEntry = await timeEntryService.getTimeEntryById(id);
      
      if (!timeEntry) {
        return res.status(404).json({ message: `Time entry with id ${id} not found` });
      }
      
      res.json(timeEntry);
    } catch (error) {
      console.error('Error getting time entry:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while retrieving the time entry.'
      });
    }
  }

  /**
   * Save a time entry (create or update)
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async saveTimeEntry(req, res) {
    try {
      const { resource_id, category_id, week_id, hours } = req.body;
      
      if (!resource_id || !category_id || !week_id || hours === undefined) {
        return res.status(400).json({ message: 'Resource ID, Category ID, Week ID, and hours are required' });
      }
      
      // Get user ID from authenticated user
      const userId = req.user ? req.user.id : null;
      
      const timeEntry = await timeEntryService.saveTimeEntry(req.body, userId);
      res.status(201).json(timeEntry);
    } catch (error) {
      console.error('Error saving time entry:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while saving the time entry.'
      });
    }
  }

  /**
   * Delete a time entry
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async deleteTimeEntry(req, res) {
    try {
      const id = req.params.id;
      
      await timeEntryService.deleteTimeEntry(id);
      res.json({ message: 'Time entry was deleted successfully!' });
    } catch (error) {
      console.error('Error deleting time entry:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while deleting the time entry.'
      });
    }
  }
}

module.exports = new TimeEntryController();