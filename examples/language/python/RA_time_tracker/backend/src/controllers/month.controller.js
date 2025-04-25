const monthService = require('../services/month.service');

/**
 * Month Controller - Handles HTTP requests for month management
 */
class MonthController {
  /**
   * Create a new month
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async createMonth(req, res) {
    try {
      const { year, month } = req.body;
      
      // Validate input
      if (!year || !month) {
        return res.status(400).json({ message: 'Year and month are required' });
      }
      
      if (isNaN(year) || isNaN(month) || month < 1 || month > 12) {
        return res.status(400).json({ message: 'Invalid year or month' });
      }

      const result = await monthService.createMonth(parseInt(year), parseInt(month));
      res.status(201).json(result);
    } catch (error) {
      console.error('Error creating month:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while creating the month.'
      });
    }
  }

  /**
   * Get all months
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async getAllMonths(req, res) {
    try {
      const months = await monthService.getAllMonths();
      res.json(months);
    } catch (error) {
      console.error('Error getting months:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while retrieving months.'
      });
    }
  }

  /**
   * Get a month by ID
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async getMonthById(req, res) {
    try {
      const id = req.params.id;
      const month = await monthService.getMonthById(id);
      
      if (!month) {
        return res.status(404).json({ message: `Month with id ${id} not found` });
      }
      
      res.json(month);
    } catch (error) {
      console.error('Error getting month:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while retrieving the month.'
      });
    }
  }
}

module.exports = new MonthController();