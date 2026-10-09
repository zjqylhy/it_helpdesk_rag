FROM golang:1.27-alpine AS build
WORKDIR /app
COPY backend/go.mod backend/go.sum ./
RUN go mod download
COPY backend/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /server .
FROM alpine:3.23
RUN apk add --no-cache ca-certificates && adduser -D -u 10001 app
WORKDIR /app
COPY --from=build /server /app/server
COPY db/schema.sql /app/schema.sql
ENV SCHEMA_PATH=/app/schema.sql HTTP_ADDR=0.0.0.0:8080 UPLOAD_DIR=/data/uploads
RUN mkdir -p /data/uploads && chown -R app:app /data
USER app
CMD ["/app/server"]
