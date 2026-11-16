import React from 'react';
import ReactDOM from 'react-dom/client';
import { CssBaseline, ThemeProvider, createTheme } from '@mui/material';
import App from './App';
import './styles.css';

const theme = createTheme({
  palette: { primary: { main: '#20554a' }, background: { default: '#f6f7f3', paper: '#ffffff' }, text: { primary: '#192f2a', secondary: '#64736e' } },
  typography: { fontFamily: '"Avenir Next", "Segoe UI", sans-serif', button: { textTransform: 'none', fontWeight: 600 }, h4: { fontWeight: 650, letterSpacing: '-1px' } },
  shape: { borderRadius: 10 },
  components: {
    MuiButton: { defaultProps: { disableElevation: true } },
    MuiOutlinedInput: { styleOverrides: { root: { backgroundColor: '#fff' } } },
  },
});

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ThemeProvider theme={theme}><CssBaseline /><App /></ThemeProvider></React.StrictMode>,
);
