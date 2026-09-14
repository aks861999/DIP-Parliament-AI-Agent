# services/full-stack-agent-app/Dockerfile
FROM node:20-slim AS builder
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
ENV HOSTNAME="0.0.0.0"
ENV PORT=3001
EXPOSE 3001
HEALTHCHECK --interval=30s --timeout=30s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:3001/api/health || exit 1
CMD ["node", "server.js"]