import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { applyTheme, initialTheme } from './theme';
import './styles.css';
applyTheme(initialTheme());
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
