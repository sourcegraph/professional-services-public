import React, { useState, useEffect } from 'react';
import MonthManagement from './components/MonthManagement';
import AuthService from './services/auth.service.js';
import './App.css';

const App = () => {
  const [currentUser, setCurrentUser] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loginForm, setLoginForm] = useState({ username: '', password: '' });

  useEffect(() => {
    // Check if user is already logged in
    const user = AuthService.getCurrentUser();
    if (user) {
      setCurrentUser(user);
    }
    setIsLoading(false);
  }, []);

  const handleLogin = async (e) => {
    e.preventDefault();
    setIsLoading(true);
    
    try {
      const user = await AuthService.login({
        username: loginForm.username,
        password: loginForm.password
      });
      setCurrentUser(user);
      setLoginForm({ username: '', password: '' });
    } catch (error) {
      console.error('Login failed:', error);
      alert('Login failed. Please check your credentials.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleLogout = () => {
    AuthService.logout();
    setCurrentUser(null);
  };

  const handleInputChange = (e) => {
    const { name, value } = e.target;
    setLoginForm({
      ...loginForm,
      [name]: value
    });
  };

  if (isLoading) {
    return <div className="loading">Loading...</div>;
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Time Tracker</h1>
        {currentUser ? (
          <div className="user-info">
            <span>Welcome, {currentUser.username}</span>
            <button onClick={handleLogout}>Logout</button>
          </div>
        ) : null}
      </header>

      <main className="app-main">
        {currentUser ? (
          <MonthManagement />
        ) : (
          <div className="login-form">
            <h2>Login</h2>
            <form onSubmit={handleLogin}>
              <div className="form-group">
                <label htmlFor="username">Username:</label>
                <input
                  type="text"
                  id="username"
                  name="username"
                  value={loginForm.username}
                  onChange={handleInputChange}
                  required
                />
              </div>
              
              <div className="form-group">
                <label htmlFor="password">Password:</label>
                <input
                  type="password"
                  id="password"
                  name="password"
                  value={loginForm.password}
                  onChange={handleInputChange}
                  required
                />
              </div>
              
              <button type="submit" disabled={isLoading}>
                {isLoading ? 'Logging in...' : 'Login'}
              </button>
            </form>
          </div>
        )}
      </main>

      <footer className="app-footer">
        <p>&copy; {new Date().getFullYear()} Time Tracker Application</p>
      </footer>
    </div>
  );
};

export default App;