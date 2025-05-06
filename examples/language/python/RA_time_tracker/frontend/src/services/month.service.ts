import api from './api';

export interface Month {
  id: number;
  year: number;
  month: number;
  created_at: string;
  updated_at: string;
  weeks?: Week[];
}

export interface Week {
  id: number;
  month_id: number;
  start_date: string;
  end_date: string;
  week_number: number;
  created_at: string;
  updated_at: string;
}

const MonthService = {
  /**
   * Get all months
   * @returns Promise with list of months
   */
  getAllMonths: async (): Promise<Month[]> => {
    const response = await api.get('/months');
    return response.data;
  },

  /**
   * Get month by ID
   * @param id Month ID
   * @returns Promise with month details including weeks
   */
  getMonthById: async (id: number): Promise<Month> => {
    const response = await api.get(`/months/${id}`);
    return response.data;
  },

  /**
   * Create a new month
   * @param year Year number
   * @param month Month number (1-12)
   * @returns Promise with created month
   */
  createMonth: async (year: number, month: number): Promise<Month> => {
    const response = await api.post('/months/create', { year, month });
    return response.data;
  },
};

export default MonthService;