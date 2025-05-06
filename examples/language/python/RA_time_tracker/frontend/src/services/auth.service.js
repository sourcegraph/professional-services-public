import api from './api.js';

const AuthService = {
  /**
   * Log in a user
   * @param {Object} credentials Login credentials
   * @returns {Promise} Promise with user data and token
   */
  login: async (credentials) => {
    const response = await api.post('/auth/login', credentials);
    if (response.data.token) {
      localStorage.setItem('token', response.data.token);
      localStorage.setItem('user', JSON.stringify(response.data));
    }
    return response.data;
  },

  /**
   * Register a new user
   * @param {Object} userData User registration data
   * @returns {Promise} Promise with user data
   */
  register: async (userData) => {
    const response = await api.post('/auth/register', userData);
    return response.data;
  },

  /**
   * Log out the current user
   */
  logout: () => {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
  },

  /**
   * Get the current logged in user
   * @returns {Object|null} User object or null if not logged in
   */
  getCurrentUser: () => {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      return JSON.parse(userStr);
    }
    return null;
  },

  /**
   * Refresh the auth token
   * @returns {Promise} Promise with new token
   */
  refreshToken: async () => {
    const response = await api.post('/auth/refresh');
    if (response.data.token) {
      localStorage.setItem('token', response.data.token);
      // Update the stored user with the new token
      const user = AuthService.getCurrentUser();
      if (user) {
        user.token = response.data.token;
        localStorage.setItem('user', JSON.stringify(user));
      }
    }
    return response.data;
  },
};

export default AuthService;