import api from './api';

export interface User {
  id: number;
  username: string;
  email: string;
  role: 'admin' | 'manager' | 'user';
  token?: string;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface RegisterRequest {
  username: string;
  email: string;
  password: string;
  role?: string;
}

const AuthService = {
  /**
   * Log in a user
   * @param credentials Login credentials
   * @returns Promise with user data and token
   */
  login: async (credentials: LoginRequest): Promise<User> => {
    const response = await api.post('/auth/login', credentials);
    if (response.data.token) {
      localStorage.setItem('token', response.data.token);
      localStorage.setItem('user', JSON.stringify(response.data));
    }
    return response.data;
  },

  /**
   * Register a new user
   * @param userData User registration data
   * @returns Promise with user data
   */
  register: async (userData: RegisterRequest): Promise<User> => {
    const response = await api.post('/auth/register', userData);
    return response.data;
  },

  /**
   * Log out the current user
   */
  logout: (): void => {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
  },

  /**
   * Get the current logged in user
   * @returns User object or null if not logged in
   */
  getCurrentUser: (): User | null => {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      return JSON.parse(userStr);
    }
    return null;
  },

  /**
   * Refresh the auth token
   * @returns Promise with new token
   */
  refreshToken: async (): Promise<{ token: string }> => {
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