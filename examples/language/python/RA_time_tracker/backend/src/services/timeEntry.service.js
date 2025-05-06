const db = require('../models');
const { TimeEntry, Resource, Category, Week, Month } = db;
const { Op } = db.Sequelize;

/**
 * TimeEntry Service - Handles time entry management
 */
class TimeEntryService {
  /**
  * Get time entries for a month
  * @param {number} monthId - Month ID
  * @returns {Array} List of time entries
  */
async getTimeEntriesForMonth(monthId) {
  try {
    // Get all weeks for the month
  const weeks = await Week.findAll({
      where: { month_id: monthId }
    });
    
    if (!weeks || weeks.length === 0) {
      return [];
    }
  
  const weekIds = weeks.map(week => week.id);
  
  // Get all time entries for these weeks
  return TimeEntry.findAll({
  where: {
    week_id: { [Op.in]: weekIds }
    }
    });
  } catch (error) {
    console.error('Error getting time entries:', error);
    return [];
  }
}

  /**
   * Get a time entry by ID
   * @param {number} id - TimeEntry ID
   * @returns {Object} TimeEntry
   */
  async getTimeEntryById(id) {
    return TimeEntry.findByPk(id, {
      include: [
        { model: Resource, as: 'resource' },
        { model: Category, as: 'category' },
        { model: Week, as: 'week' }
      ]
    });
  }

  /**
   * Create or update a time entry
   * @param {Object} data - Time entry data
   * @param {number} userId - User ID for created_by
   * @returns {Object} Created or updated time entry
   */
  async saveTimeEntry(data, userId) {
    const { resource_id, category_id, week_id, hours } = data;
    
    // Check if entry already exists
    const existingEntry = await TimeEntry.findOne({
      where: {
        resource_id,
        category_id,
        week_id
      }
    });
    
    if (existingEntry) {
      // Update existing entry
      return existingEntry.update({ hours });
    } else {
      // Create new entry
      return TimeEntry.create({
        resource_id,
        category_id,
        week_id,
        hours,
        created_by: userId
      });
    }
  }

  /**
   * Delete a time entry
   * @param {number} id - TimeEntry ID
   * @returns {boolean} Success status
   */
  async deleteTimeEntry(id) {
    const timeEntry = await TimeEntry.findByPk(id);
    
    if (!timeEntry) {
      throw new Error(`TimeEntry with id ${id} not found`);
    }
    
    await timeEntry.destroy();
    return true;
  }
}

module.exports = new TimeEntryService();