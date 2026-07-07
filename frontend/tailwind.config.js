/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js}'],
  theme: {
    extend: {
      colors: {
        ink:      '#0a0a0a', // near-black — text, fills, borders at full opacity
        paper:    '#f6f4ee', // warm off-white base — deliberately not pure #fff
        paperdim: '#ece9e0', // recessed surfaces: track backgrounds, disabled fills
        hairline: 'rgba(10, 10, 10, 0.14)', // the only "gray" — all borders/dividers
        fog:      '#7a776e', // secondary/mono-label text, still monochrome-adjacent
      },
      fontFamily: {
        sans:      ['"IBM Plex Sans"', '-apple-system', 'BlinkMacSystemFont', 'sans-serif'],
        condensed: ['"IBM Plex Sans Condensed"', '-apple-system', 'sans-serif'],
        mono:      ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      borderRadius: {
        DEFAULT: '2px',
        sm: '1px',
        md: '2px',
        lg: '2px', // intentionally flat — no rounded-2xl chat-bubble shapes
      },
      boxShadow: {
        none: 'none', // elevation comes from hairline + texture, never shadow
      },
      backgroundImage: {
        // dot-texture overlay for "filled" surfaces (progress bars, chart fills)
        // usage: bg-stipple bg-stipple-size
        'stipple': 'radial-gradient(circle, currentColor 1px, transparent 1.2px)',
      },
      backgroundSize: {
        'stipple-sm': '4px 4px',
        'stipple-md': '6px 6px',
      },
    },
  },
  plugins: [],
}