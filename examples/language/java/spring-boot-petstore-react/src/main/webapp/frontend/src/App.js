import React from 'react';
import './App.css';
import PetList from './components/PetList';

function App() {
  return (
    <div className="App">
      <header className="App-header">
        <div className="header-content">
          <h1>Pet Store Application</h1>
          <p className="header-subtitle">Manage your pet inventory with ease</p>
        </div>
      </header>
      <main className="App-main">
        <div className="content-container">
          <PetList />
        </div>
      </main>
      <footer className="App-footer">
        <p>Powered by Sourcegraph Technology</p>
      </footer>
    </div>
  );
}

export default App;
