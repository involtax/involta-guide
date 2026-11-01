FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG NEXT_PUBLIC_BASE_PATH=/guide
ENV NEXT_PUBLIC_BASE_PATH=$NEXT_PUBLIC_BASE_PATH
RUN npm run build

FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production
ARG NEXT_PUBLIC_BASE_PATH=/guide
ENV NEXT_PUBLIC_BASE_PATH=$NEXT_PUBLIC_BASE_PATH
ENV SITE_URL=https://involta.ru/guide
COPY --from=build --chown=node:node /app ./
RUN mkdir -p /app/var/audio && chown -R node:node /app/var
USER node
EXPOSE 3000
CMD ["npm", "start"]
