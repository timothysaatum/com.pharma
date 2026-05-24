/** @type {import('tailwindcss').Config} */
export default {
    content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
    theme: {
        extend: {
            colors: {
                brand: {
                    50: "#f0fdf4",
                    100: "#dcfce7",
                    200: "#bbf7d0",
                    300: "#86efac",
                    400: "#4ade80",
                    500: "#22c55e",
                    600: "#16a34a",
                    700: "#15803d",
                    800: "#166534",
                    900: "#14532d",
                    950: "#052e16",
                },
                surface: {
                    DEFAULT: "#ffffff",
                    secondary: "#f8fafc",
                    tertiary: "#f1f5f9",
                },
                ink: {
                    DEFAULT: "#0f172a",
                    secondary: "#475569",
                    muted: "#94a3b8",
                },
            },
            fontFamily: {
                sans: ["'DM Sans'", "system-ui", "sans-serif"],
                display: ["'Syne'", "system-ui", "sans-serif"],
                mono: ["'JetBrains Mono'", "monospace"],
            },
            borderRadius: {
                "2xl": "1rem",
                "3xl": "1.5rem",
            },
            boxShadow: {
                soft: "0 2px 8px 0 rgba(0,0,0,0.06), 0 1px 2px 0 rgba(0,0,0,0.04)",
                card: "0 4px 24px 0 rgba(0,0,0,0.08)",
                "card-hover": "0 8px 32px 0 rgba(0,0,0,0.12)",
            },
            animation: {
                "slide-in": "slideIn 0.3s ease-out",
                "fade-up": "fadeUp 0.4s ease-out",
            },
            keyframes: {
                slideIn: {
                    "0%": { transform: "translateX(-10px)", opacity: "0" },
                    "100%": { transform: "translateX(0)", opacity: "1" },
                },
                fadeUp: {
                    "0%": { transform: "translateY(12px)", opacity: "0" },
                    "100%": { transform: "translateY(0)", opacity: "1" },
                },
            },
        },
    },
    plugins: [],
};