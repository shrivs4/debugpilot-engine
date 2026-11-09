FROM node:22-bookworm-slim

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

EXPOSE 5173
# `vite preview` serves the production build and proxies /api to the API container.
CMD ["npm", "run", "preview"]
