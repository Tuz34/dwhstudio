/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Consolas', 'Monaco', 'monospace'],
      },
      colors: {
        oracle: {
          DEFAULT: '#c8102e',
          dark: '#a00d24',
          light: '#e8304e',
          glow: 'rgba(200,16,46,0.25)',
        },
      },
      boxShadow: {
        glass: '0 8px 32px 0 rgba(0,0,0,0.05)',
        'glass-lg': '0 16px 48px 0 rgba(0,0,0,0.08)',
        'oracle-glow': '0 0 12px rgba(200,16,46,0.4)',
      },
      animation: {
        'fade-in': 'fadeIn 0.3s ease forwards',
        'slide-down': 'slideDown 0.3s ease forwards',
        float: 'float 4s ease-in-out infinite',
        'typing-bounce': 'typingBounce 1.2s ease-in-out infinite',
        'status-pulse': 'statusPulse 1.5s ease-in-out infinite alternate',
        'error-pulse': 'errorPulse 0.8s ease-in-out infinite alternate',
      },
      keyframes: {
        fadeIn: {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        slideDown: {
          from: { opacity: '0', transform: 'translateY(-8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        float: {
          '0%, 100%': { transform: 'translateY(0px)' },
          '50%': { transform: 'translateY(-6px)' },
        },
        typingBounce: {
          '0%, 80%, 100%': { transform: 'scale(0.8)', opacity: '0.5' },
          '40%': { transform: 'scale(1)', opacity: '1' },
        },
        statusPulse: {
          from: { boxShadow: '0 0 0 0 rgba(16, 185, 129, 0.3)' },
          to: { boxShadow: '0 0 0 8px rgba(16, 185, 129, 0)' },
        },
        errorPulse: {
          from: { boxShadow: '0 0 0 0 rgba(239, 68, 68, 0.4)' },
          to: { boxShadow: '0 0 0 8px rgba(239, 68, 68, 0)' },
        },
      },
    },
  },
  plugins: [],
}
