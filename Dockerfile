FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV TICKFORGE_HOST=0.0.0.0
ENV TICKFORGE_PORT=5003
EXPOSE 5003
CMD ["tickforge", "serve"]
