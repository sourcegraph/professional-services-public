import api from './api.js';

const MonthService = {
  /**
   * Get all months
   * @returns Promise with list of months
   */
  getAllMonths: async () => {
    const response = await api.get('/months');
    return response.data;
  },

  /**
   * Get month by ID
   * @param {number} id Month ID
   * @returns Promise with month details including weeks
   */
  getMonthById: async (id) => {
    const response = await api.get(`/months/${id}`);
    return response.data;
  },

  /**
   * Create a new month
   * @param {number} year Year number
   * @param {number} month Month number (1-12)
   * @returns Promise with created month
   */
  createMonth: async (year, month) => {
    const response = await api.post('/months/create', { year, month });
    return response.data;
  },

  /**
   * Get all resources
   * @returns Promise with list of resources
   */
  getResources: async () => {
    const response = await api.get('/resources');
    return response.data;
  },

  /**
   * Get all categories
   * @returns Promise with list of categories
   */
  getCategories: async () => {
    const response = await api.get('/categories');
    return response.data;
  },

  /**
   * Get time entries for a month
   * @param {number} monthId Month ID
   * @returns Promise with list of time entries
   */
  getTimeEntriesForMonth: async (monthId) => {
    const response = await api.get(`/timeentries/month/${monthId}`);
    return response.data;
  },

  /**
   * Save a time entry (create or update)
   * @param {Object} timeEntryData Time entry data
   * @returns Promise with saved time entry
   */
  saveTimeEntry: async (timeEntryData) => {
    const response = await api.post('/timeentries', timeEntryData);
    return response.data;
  },
};

export default MonthService;