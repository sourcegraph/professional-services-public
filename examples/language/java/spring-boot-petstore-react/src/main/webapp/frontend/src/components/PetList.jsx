import React, { useState, useEffect } from 'react';
import axios from 'axios';

function PetList() {
  const [pets, setPets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    axios.get('/api/v3/pet/findByStatus?status=available')
      .then(response => {
        setPets(response.data);
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  if (loading) return <div>Loading pets...</div>;
  if (error) return <div>Error loading pets: {error}</div>;

  return (
    <div className="pet-list">
      <h2>Available Pets</h2>
      <div className="pet-grid">
        {pets.map(pet => (
          <div key={pet.id} className="pet-card">
            <h3>{pet.name}</h3>
            <p>Status: {pet.status}</p>
            <p>Category: {pet.category?.name || 'Not categorized'}</p>
            {pet.photoUrls && pet.photoUrls.length > 0 && (
              <img src={pet.photoUrls[0]} alt={pet.name} />
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

export default PetList;
