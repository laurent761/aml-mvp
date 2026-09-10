FROM node:22.19.0-bookworm-slim AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY . .

ARG BACKEND_API_URL=http://api:8000
ENV BACKEND_API_URL=${BACKEND_API_URL}
RUN npm run build

FROM node:22.19.0-bookworm-slim AS runtime

ENV NODE_ENV=production \
    PORT=3000 \
    BACKEND_API_URL=http://api:8000
WORKDIR /app
COPY --from=build /app ./

USER node
EXPOSE 3000
CMD ["npm", "run", "start"]
