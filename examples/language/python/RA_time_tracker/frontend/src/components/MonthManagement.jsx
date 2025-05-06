import React, { useState, useEffect } from 'react';
import MonthService from '../services/month.service.js';
import TimeEntrySheet from './TimeEntrySheet';
import './MonthManagement.css';

const MonthManagement = () => {
  const [months, setMonths] = useState([]);
  const [selectedMonth, setSelectedMonth] = useState(null);
  const [showTimeEntrySheet, setShowTimeEntrySheet] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [newMonth, setNewMonth] = useState({
    year: new Date().getFullYear(),
    month: new Date().getMonth() + 1,
  });

  // Fetch all months on component mount
  useEffect(() => {
    fetchMonths();
  }, []);

  // Fetch all months from the API
  const fetchMonths = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await MonthService.getAllMonths();
      setMonths(data);
    } catch (err) {
      setError('Failed to fetch months');
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  // Create a new month
  const handleCreateMonth = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      await MonthService.createMonth(newMonth.year, newMonth.month);
      // Refresh the months list
      await fetchMonths();
      // Reset the form
      setNewMonth({
        year: new Date().getFullYear(),
        month: new Date().getMonth() + 1,
      });
    } catch (err) {
      setError(err.response?.data?.message || 'Failed to create month');
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  // Handle input changes for new month form
  const handleInputChange = (e) => {
    const { name, value } = e.target;
    setNewMonth({
      ...newMonth,
      [name]: parseInt(value, 10),
    });
  };

  // View details of a specific month
  const handleViewMonth = async (id) => {
    setLoading(true);
    setError(null);

    try {
      const data = await MonthService.getMonthById(id);
      setSelectedMonth(data);
    } catch (err) {
      setError('Failed to fetch month details');
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  // Format month name
  const formatMonthName = (monthNum) => {
    const monthNames = [
      'January', 'February', 'March', 'April', 'May', 'June',
      'July', 'August', 'September', 'October', 'November', 'December'
    ];
    return monthNames[monthNum - 1];
  };

  return (
    <div className="month-management">
      <h2>Month Management</h2>

      {/* Create New Month Form */}
      <div className="create-month-form">
        <h3>Create New Month</h3>
        <form onSubmit={handleCreateMonth}>
          <div className="form-group">
            <label htmlFor="year">Year:</label>
            <input
              type="number"
              id="year"
              name="year"
              value={newMonth.year}
              onChange={handleInputChange}
              min="2000"
              max="2100"
              required
            />
          </div>

          <div className="form-group">
            <label htmlFor="month">Month:</label>
            <select
              id="month"
              name="month"
              value={newMonth.month}
              onChange={handleInputChange}
              required
            >
              {Array.from({ length: 12 }, (_, i) => i + 1).map(monthNum => (
                <option key={monthNum} value={monthNum}>
                  {formatMonthName(monthNum)}
                </option>
              ))}
            </select>
          </div>

          <button type="submit" disabled={loading}>
            {loading ? 'Creating...' : 'Create Month'}
          </button>
        </form>

        {error && <div className="error-message">{error}</div>}
      </div>

      {/* Months List */}
      <div className="months-list">
        <h3>Available Months</h3>
        {loading && !months.length ? (
          <p>Loading months...</p>
        ) : (
          <ul>
            {months.map((month) => (
              <li key={month.id}>
                <span>
                  {formatMonthName(month.month)} {month.year}
                </span>
                <div className="button-group">
                  <button onClick={() => handleViewMonth(month.id)}>
                    View Details
                  </button>
                  <button 
                    className="time-entry-button"
                    onClick={() => {
                      handleViewMonth(month.id);
                      setShowTimeEntrySheet(true);
                    }}
                  >
                    Time Entries
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        {!loading && !months.length && <p>No months available. Create one!</p>}
      </div>

      {/* Selected Month Details */}
      {!selectedMonth ? null : (
        <div className="month-details">
          <h3>
            {formatMonthName(selectedMonth.month)} {selectedMonth.year} Details
          </h3>

          <h4>Weeks:</h4>
          <table>
            <thead>
              <tr>
                <th>Week #</th>
                <th>Start Date</th>
                <th>End Date</th>
              </tr>
            </thead>
            <tbody>
              {selectedMonth.weeks?.map((week) => (
                <tr key={week.id}>
                  <td>{week.week_number}</td>
                  <td>{week.start_date}</td>
                  <td>{week.end_date}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="actions">
            <button onClick={() => setSelectedMonth(null)}>Close Details</button>
            <button 
              className="time-entry-button"
              onClick={() => setShowTimeEntrySheet(true)}
            >
              Manage Time Entries
            </button>
          </div>
        </div>
      )}
      
      {/* Time Entry Sheet */}
      {showTimeEntrySheet && selectedMonth && (
        <TimeEntrySheet 
          month={selectedMonth} 
          onClose={() => setShowTimeEntrySheet(false)} 
        />
      )}
    </div>
  );
};

export default MonthManagement;