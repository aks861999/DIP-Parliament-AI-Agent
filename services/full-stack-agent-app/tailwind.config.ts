// services/full-stack-agent-app/tailwind.config.ts
import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: { accent: "#FFCC00" },
    },
  },
  plugins: [require("@tailwindcss/typography")],
};

export default config;