const db = require('../models');
const { Month, Week, Resource, Category } = db;
const { Sequelize, sequelize } = db;

/**
 * Month Service - Handles month creation and management
 */
class MonthService {
  /**
   * Create a new month with auto-generated weeks
   * @param {number} year - The year
   * @param {number} month - The month (1-12)
   * @returns {Object} Created month with weeks
   */
  async createMonth(year, month) {
    const transaction = await sequelize.transaction();

    try {
      // Check if month already exists
      const existingMonth = await Month.findOne({
        where: { year, month },
        transaction
      });

      if (existingMonth) {
        throw new Error(`Month ${month}/${year} already exists`);
      }

      // Create the month
      const newMonth = await Month.create(
        { year, month },
        { transaction }
      );

      // Calculate and create weeks for the month
      const weeks = this.calculateWeeksForMonth(year, month);
      const createdWeeks = [];

      for (const weekData of weeks) {
        const week = await Week.create(
          {
            ...weekData,
            month_id: newMonth.id
          },
          { transaction }
        );
        createdWeeks.push(week);
      }

      await transaction.commit();

      return {
        ...newMonth.toJSON(),
        weeks: createdWeeks
      };
    } catch (error) {
      await transaction.rollback();
      throw error;
    }
  }

  /**
   * Calculate week ranges for a given month and year
   * @param {number} year - The year
   * @param {number} month - The month (1-12)
   * @returns {Array} Array of week objects with start and end dates
   */
  calculateWeeksForMonth(year, month) {
    const weeks = [];
    
    // Create a date for the first day of the month
    const firstDay = new Date(year, month - 1, 1);
    
    // Create a date for the last day of the month
    const lastDay = new Date(year, month, 0);
    
    // Start with the first day of the month
    let currentDate = new Date(firstDay);
    let weekNumber = 1;
    
    // While we're still in the month
    while (currentDate <= lastDay) {
      // Determine the end of the week (Saturday) or the end of the month
      let endOfWeek = new Date(currentDate);
      
      // If we're not at the start of a week (Monday), then find the next Saturday
      if (currentDate.getDay() !== 1) { // 1 is Monday
        // If we're already past Monday, move to the next Monday first
        if (currentDate.getDay() > 1) {
          const daysToAdd = 8 - currentDate.getDay(); // Days until next Monday
          endOfWeek.setDate(currentDate.getDate() + daysToAdd - 1); // -1: end on Sunday
        } else {
          // If we're on Sunday, end on Saturday (6 days ahead)
          endOfWeek.setDate(currentDate.getDate() + 6);
        }
      } else {
        // If we're starting on Monday, end on Sunday (6 days later)
        endOfWeek.setDate(currentDate.getDate() + 6);
      }
      
      // If end of week is beyond the month, cap it at the last day
      if (endOfWeek > lastDay) {
        endOfWeek = lastDay;
      }
      
      // Format dates as YYYY-MM-DD strings for database
      const startDateStr = this.formatDate(currentDate);
      const endDateStr = this.formatDate(endOfWeek);
      
      // Add the week to our array
      weeks.push({
        start_date: startDateStr,
        end_date: endDateStr,
        week_number: weekNumber++
      });
      
      // Move to the next day after the end of this week
      currentDate = new Date(endOfWeek);
      currentDate.setDate(currentDate.getDate() + 1);
    }
    
    return weeks;
  }

  /**
   * Format a date as YYYY-MM-DD
   * @param {Date} date - Date object
   * @returns {string} Formatted date string
   */
  formatDate(date) {
    return date.toISOString().split('T')[0];
  }

  /**
   * Get all months
   * @returns {Array} List of months
   */
  async getAllMonths() {
    return Month.findAll({
      order: [['year', 'DESC'], ['month', 'DESC']]
    });
  }

  /**
   * Get a month by ID with its weeks
   * @param {number} id - Month ID
   * @returns {Object} Month with weeks
   */
  async getMonthById(id) {
    return Month.findByPk(id, {
      include: [{
        model: Week,
        as: 'weeks',
        order: [['week_number', 'ASC']]
      }]
    });
  }

  /**
   * Get a month by year and month number with its weeks
   * @param {number} year - Year
   * @param {number} month - Month number (1-12)
   * @returns {Object} Month with weeks
   */
  async getMonthByYearAndMonth(year, month) {
    return Month.findOne({
      where: { year, month },
      include: [{
        model: Week,
        as: 'weeks',
        order: [['week_number', 'ASC']]
      }]
    });
  }
}

module.exports = new MonthService();