import React, { useState, useEffect } from 'react';
import MonthService from '../services/month.service.js';
import './MonthDetail.css';

const MonthDetail = ({ monthId, onClose }) => {
  const [monthData, setMonthData] = useState(null);
  const [resources, setResources] = useState([]);
  const [categories, setCategories] = useState([]);
  const [timeEntries, setTimeEntries] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    async function fetchData() {
      setLoading(true);
      try {
        // Fetch month data with weeks
        const data = await MonthService.getMonthById(monthId);
        setMonthData(data);
        
        // Fetch resources
        const resourcesData = await MonthService.getResources();
        setResources(resourcesData);
        
        // Fetch categories
        const categoriesData = await MonthService.getCategories();
        setCategories(categoriesData);
        
        // Fetch time entries for this month
        const entriesData = await MonthService.getTimeEntriesForMonth(monthId);
        
        // Organize entries by resource, category and week
        const organizedEntries = {};
        entriesData.forEach(entry => {
          const key = `${entry.resource_id}-${entry.category_id}-${entry.week_id}`;
          organizedEntries[key] = entry;
        });
        
        setTimeEntries(organizedEntries);
      } catch (err) {
        console.error('Error fetching month details:', err);
        setError('Failed to load month details');
      } finally {
        setLoading(false);
      }
    }
    
    fetchData();
  }, [monthId]);

  const formatMonthName = (monthNum) => {
    const monthNames = [
      'January', 'February', 'March', 'April', 'May', 'June',
      'July', 'August', 'September', 'October', 'November', 'December'
    ];
    return monthNames[monthNum - 1];
  };

  const formatDateRange = (startDate, endDate) => {
    const start = new Date(startDate);
    const end = new Date(endDate);
    return `${start.getDate()} - ${end.getDate()}`;
  };

  const getTimeEntryValue = (resourceId, categoryId, weekId) => {
    const key = `${resourceId}-${categoryId}-${weekId}`;
    return timeEntries[key]?.hours || 0;
  };

  const handleTimeEntryChange = async (resourceId, categoryId, weekId, value) => {
    const hours = parseFloat(value) || 0;
    
    try {
      // Save the time entry to the database
      await MonthService.saveTimeEntry({
        resource_id: resourceId,
        category_id: categoryId,
        week_id: weekId,
        hours
      });
      
      // Update local state
      const key = `${resourceId}-${categoryId}-${weekId}`;
      setTimeEntries(prev => ({
        ...prev,
        [key]: { ...timeEntries[key], hours }
      }));
    } catch (err) {
      console.error('Error saving time entry:', err);
      setError('Failed to save time entry');
    }
  };

  const calculateRowTotal = (resourceId, categoryId) => {
    if (!monthData?.weeks) return 0;
    
    return monthData.weeks.reduce((total, week) => {
      return total + parseFloat(getTimeEntryValue(resourceId, categoryId, week.id) || 0);
    }, 0).toFixed(2);
  };

  const calculateWeekTotal = (weekId) => {
    if (!resources || !categories) return 0;
    
    let total = 0;
    resources.forEach(resource => {
      categories.forEach(category => {
        total += parseFloat(getTimeEntryValue(resource.id, category.id, weekId) || 0);
      });
    });
    
    return total.toFixed(2);
  };

  const calculateGrandTotal = () => {
    if (!monthData?.weeks || !resources || !categories) return 0;
    
    let total = 0;
    resources.forEach(resource => {
      categories.forEach(category => {
        monthData.weeks.forEach(week => {
          total += parseFloat(getTimeEntryValue(resource.id, category.id, week.id) || 0);
        });
      });
    });
    
    return total.toFixed(2);
  };

  if (loading) {
    return <div className="loading">Loading month details...</div>;
  }

  if (error) {
    return <div className="error-message">{error}</div>;
  }

  if (!monthData) {
    return <div className="error-message">Month data not found</div>;
  }

  return (
    <div className="month-detail">
      <div className="month-detail-header">
        <h2>Booking Resident Architect Program Time Tracking</h2>
        <h3>
          {formatMonthName(monthData.month)} {monthData.year}
        </h3>
        <button className="close-button" onClick={onClose}>Close</button>
      </div>

      <div className="time-entry-grid">
        <table>
          <thead>
            <tr>
              <th className="resource-col">Resource / Team</th>
              <th className="category-col">Work Category</th>
              {monthData.weeks.map(week => (
                <th key={week.id} className="week-col">
                  {formatMonthName(monthData.month)} {formatDateRange(week.start_date, week.end_date)}
                </th>
              ))}
              <th className="total-col">Totals</th>
            </tr>
          </thead>
          <tbody>
            {resources.map(resource => (
              categories.map(category => (
                <tr key={`${resource.id}-${category.id}`}>
                  <td className="resource-col">{resource.name}</td>
                  <td className="category-col">{category.name}</td>
                  {monthData.weeks.map(week => (
                    <td key={week.id} className="week-col">
                      <input 
                        type="number" 
                        step="0.01" 
                        min="0" 
                        value={getTimeEntryValue(resource.id, category.id, week.id)}
                        onChange={(e) => handleTimeEntryChange(resource.id, category.id, week.id, e.target.value)}
                      />
                    </td>
                  ))}
                  <td className="total-col">{calculateRowTotal(resource.id, category.id)}</td>
                </tr>
              ))
            ))}
            <tr className="totals-row">
              <td colSpan="2">Total hrs</td>
              {monthData.weeks.map(week => (
                <td key={week.id}>{calculateWeekTotal(week.id)}</td>
              ))}
              <td>{calculateGrandTotal()}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
};

export default MonthDetail;