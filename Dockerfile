FROM debian:bookworm-slim

WORKDIR /opt/nexoradar
COPY nexoradar ./nexoradar
RUN chmod 0755 ./nexoradar

EXPOSE 9000
CMD ["./nexoradar"]
