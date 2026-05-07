import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { bridge } from './ws/bridge';
import './styles.css';

// Start the single persistent WS connection for the app's lifetime.
bridge.start();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter basename="/dashboard">
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
