FROM golang:1.27-alpine AS build

WORKDIR /src
COPY apps/realtime/go.mod apps/realtime/go.sum ./
RUN go mod download

COPY apps/realtime/ ./
RUN CGO_ENABLED=0 go build -o /gateway .

FROM alpine:3.20
COPY --from=build /gateway /gateway
RUN adduser -S -H -u 10001 app
USER app
EXPOSE 9000
ENTRYPOINT ["/gateway"]
